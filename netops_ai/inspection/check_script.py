"""巡检助手写的「判定脚本」：模板和固定规则表达不了的需求（例如整张路由表和上次逐条比），由助手按真实输出样本写一个
小函数，在沙箱里跑，用户审核后才启用。

脚本长这样::

    def evaluate(ctx):
        # ctx["output"]         本次采集到的命令输出（字符串）
        # ctx["previous"]       上一次成功采集的输出（没有就是 None）
        # ctx["previous_run"]   上一次运行的 id（没有就是 None）
        # ctx["device"]         设备名
        # ctx["params"]         计划里给它的参数（字典）
        return {"status": "pass" | "fail" | "baseline", "summary": "一句话结论",
                "metrics": {"name": 数字}, "findings": ["具体发现，最多 20 条"]}

脚本**只做文本处理**：不能执行设备命令（命令由系统过只读白名单执行，脚本只拿到输出）、不能联网、不能读写文件。
三道防线：①源码先过 AST 白名单（只准 re/json/ipaddress/collections/datetime/statistics/math/itertools；
不准 open/eval/exec/__import__/双下划线属性）；②单独的 `python -I` 子进程，限时、限输出、环境变量清空；
③创建时用户逐行审核，保存的是源码 + sha256，源码被改动就拒绝运行。

注意：这是「防止助手写出的脚本出事」的纵深防御，不是对抗恶意代码的安全边界——能审核源码的人是维护者。
"""
from __future__ import annotations

import ast
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ALLOWED_MODULES = ("re", "json", "csv", "ipaddress", "collections", "datetime", "statistics", "math", "itertools")
FORBIDDEN_NAMES = {"open", "eval", "exec", "compile", "__import__", "input", "breakpoint", "globals", "locals", "vars",
                   "getattr", "setattr", "delattr", "memoryview", "exit", "quit", "help", "dir"}
#: `"{0.__class__.__mro__}".format(x)` 这类格式串属性遍历能绕过「禁止双下划线属性」的静态检查（信息泄露）：整个禁掉，
#: 要拼字符串用 f-string（f-string 里的属性访问是 AST 节点，照样被检查）
FORBIDDEN_ATTRS = {"format", "format_map"}
#: 子进程内存上限（字节）。`'a' * 10**10` 这类内存炸弹在上限处被系统掐掉，不会拖垮主机
MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
MAX_SOURCE_CHARS = 12_000
MAX_INPUT_CHARS = 400_000
MAX_RESULT_CHARS = 20_000
MAX_FINDINGS = 20
TIMEOUT_SECONDS = 5.0
STATUSES = ("pass", "fail", "baseline")


class ScriptError(Exception):
    pass


def sha256(source: str) -> str:
    return hashlib.sha256(source.replace("\r\n", "\n").strip().encode("utf-8")).hexdigest()


def validate_source(source: str) -> list[str]:
    """静态检查。返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    if not isinstance(source, str) or not source.strip():
        return ["script: empty"]
    if len(source) > MAX_SOURCE_CHARS:
        return [f"script: longer than {MAX_SOURCE_CHARS} characters"]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"script: syntax error line {exc.lineno}: {exc.msg}"]
    has_eval = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "evaluate" and len(node.args.args) == 1:
            has_eval = True
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_MODULES:
                    problems.append(f"script: import of {a.name!r} is not allowed (allowed: {', '.join(ALLOWED_MODULES)})")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in ALLOWED_MODULES or node.level:
                problems.append(f"script: import from {node.module!r} is not allowed")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"script: use of {node.id!r} is not allowed")
        elif isinstance(node, ast.Attribute) and (node.attr.startswith("__") or node.attr in FORBIDDEN_ATTRS):
            problems.append(f"script: attribute {node.attr!r} is not allowed" +
                            (" (use an f-string instead)" if node.attr in FORBIDDEN_ATTRS else ""))
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append("script: global / nonlocal is not allowed")
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.Yield, ast.YieldFrom)):
            problems.append("script: async / generators are not allowed")
    if not has_eval:
        problems.append("script: define `def evaluate(ctx):` (one argument)")
    return list(dict.fromkeys(problems))


_RUNNER = r"""
import builtins, json, sys
req = json.loads(sys.stdin.read())
ALLOWED = set(req["allowed_modules"])
real_import = builtins.__import__
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or name.split(".")[0] not in ALLOWED:
        raise ImportError("import of %r is not allowed" % name)
    return real_import(name, globals, locals, fromlist, level)
safe = {k: getattr(builtins, k) for k in (
    "abs all any bool dict enumerate filter float frozenset int isinstance iter len list map max min next "
    "range repr reversed round set sorted str sum tuple zip sorted divmod ord chr hash bytes bytearray "
    "ValueError TypeError KeyError IndexError Exception StopIteration ZeroDivisionError AttributeError "
    "True False None print slice property staticmethod classmethod object type issubclass").split() if hasattr(builtins, k)}
safe["__import__"] = guarded_import
safe["__build_class__"] = builtins.__build_class__
safe["__name__"] = "script"
g = {"__builtins__": safe}
try:
    exec(compile(req["source"], "<check-script>", "exec"), g)
    out = g["evaluate"](req["ctx"])
    sys.stdout.write(json.dumps({"ok": True, "result": out}, ensure_ascii=False, default=str))
except BaseException as exc:  # noqa: BLE001
    sys.stdout.write(json.dumps({"ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:300])}))
"""


def _run_limited(argv: list[str], stdin: bytes, env: dict[str, str], cwd: str, timeout: float) -> tuple[bytes, bytes]:
    """起子进程并限制资源：内存上限 + 超时杀进程。
    Windows：Job Object（进程内存上限、关闭作业即杀进程）；POSIX：`RLIMIT_AS` / `RLIMIT_CPU`。
    限制设置失败不阻止运行（有静态检查 + 超时兜底），但会在 stderr 里留下一句，方便排查。"""
    kwargs: dict[str, Any] = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "env": env, "cwd": cwd}
    if os.name != "nt":
        import resource

        def _limits() -> None:
            resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES * 2, MEMORY_LIMIT_BYTES * 2))
            resource.setrlimit(resource.RLIMIT_CPU, (int(timeout) + 2, int(timeout) + 2))
            os.setsid()

        kwargs["preexec_fn"] = _limits
    proc = subprocess.Popen(argv, **kwargs)
    job = _windows_job(proc) if os.name == "nt" else None
    try:
        return proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    finally:
        if proc.poll() is None:
            proc.kill()
        if job is not None:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(job)  # KILL_ON_JOB_CLOSE：连同它可能起的子进程一起收掉


def _windows_job(proc: subprocess.Popen) -> Any:
    """把子进程放进一个带内存上限的 Job Object。返回作业句柄（失败返回 None）。"""
    import ctypes
    from ctypes import wintypes

    class _Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

    class _Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", _IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9
    k32 = ctypes.windll.kernel32
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    try:
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _Extended()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        info.ProcessMemoryLimit = MEMORY_LIMIT_BYTES
        ok = k32.SetInformationJobObject(wintypes.HANDLE(job), JobObjectExtendedLimitInformation, ctypes.byref(info),
                                         ctypes.sizeof(info))
        if not ok or not k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle))):  # noqa: SLF001
            k32.CloseHandle(wintypes.HANDLE(job))
            return None
        return job
    except Exception:  # noqa: BLE001 - 拿不到 Job Object 不阻止运行，静态检查 + 超时仍在
        return None


# --------------------------------------------------------------------------- 签名（批准过的脚本不能在应用之外被改）


def _key_path() -> Path:
    env = os.environ.get("NETOPS_SCRIPT_KEY_FILE")
    return Path(env) if env else Path.home() / ".netops-ai" / "script-signing-key"  # 不放进 records/：那里会被备份、同步


def _key() -> bytes:
    """本机签名密钥：第一次用到时随机生成，只存在这台机器。拿到 YAML 文件的人没有它，改了源码就签不出有效签名。"""
    path = _key_path()
    try:
        return bytes.fromhex(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        pass
    key = secrets.token_bytes(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key.hex(), encoding="ascii")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return bytes.fromhex(path.read_text(encoding="ascii").strip())
    except OSError:
        return key  # 写不了就退到进程内密钥：本进程内有效，重启后需要重新批准（更安全的失败方向）


def sign(source: str) -> str:
    """批准时给源码签名（HMAC-SHA256）。sha256 只能发现「改了」，任何人都能重算；签名只有持有本机密钥的应用能给。"""
    return hmac.new(_key(), sha256(source).encode("ascii"), hashlib.sha256).hexdigest()


def verify(source: str, signature: Any) -> bool:
    return isinstance(signature, str) and hmac.compare_digest(sign(source), signature)


def describe(source: str) -> dict[str, Any]:
    """给审核界面看的「这个脚本能干什么」：用了哪些模块、读不读上一次输出、有没有写 / 删之类的调用。静态分析，只是辅助。"""
    imports: list[str] = []
    uses_previous = False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"imports": [], "uses_previous": False, "loops": 0, "functions": 0}
    loops = functions = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module.split(".")[0])
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and node.slice.value == "previous":
            uses_previous = True
        elif isinstance(node, (ast.For, ast.While, ast.comprehension)):
            loops += 1
        elif isinstance(node, ast.FunctionDef):
            functions += 1
    return {"imports": sorted(set(imports)), "uses_previous": uses_previous, "loops": loops, "functions": functions,
            "lines": len(source.strip().splitlines())}


def _normalize(out: Any) -> dict[str, Any]:
    if not isinstance(out, dict):
        raise ScriptError("evaluate() must return a dict")
    status = out.get("status")
    if status not in STATUSES:
        raise ScriptError(f"status must be one of {', '.join(STATUSES)} (got {status!r})")
    metrics: dict[str, float | int] = {}
    for k, v in (out.get("metrics") or {}).items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ScriptError(f"metric {k!r} must be a number")
        metrics[str(k)[:64]] = v
    findings = [str(x)[:300] for x in (out.get("findings") or [])][:MAX_FINDINGS]
    res = {"status": status, "summary": str(out.get("summary") or "")[:300], "metrics": metrics, "findings": findings}
    if len(json.dumps(res, ensure_ascii=False)) > MAX_RESULT_CHARS:
        raise ScriptError("result is too large")
    return res


def run_script(source: str, ctx: dict[str, Any], *, timeout: float = TIMEOUT_SECONDS) -> dict[str, Any]:
    """在沙箱子进程里跑一次 evaluate。成功返回规整后的结果；任何问题抛 ScriptError（带人话原因）。"""
    problems = validate_source(source)
    if problems:
        raise ScriptError("; ".join(problems))
    ctx = dict(ctx)
    for k in ("output", "previous"):
        if isinstance(ctx.get(k), str) and len(ctx[k]) > MAX_INPUT_CHARS:
            ctx[k] = ctx[k][:MAX_INPUT_CHARS]
    payload = json.dumps({"source": source, "ctx": ctx, "allowed_modules": list(ALLOWED_MODULES)}, ensure_ascii=False)
    env = {"PYTHONIOENCODING": "utf-8", "PYTHONHASHSEED": "0"}
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    try:
        with tempfile.TemporaryDirectory() as cwd:  # 空目录当工作目录，脚本旁边没有任何文件
            stdout, stderr = _run_limited([sys.executable, "-I", "-c", _RUNNER], payload.encode("utf-8"), env, cwd, timeout)
    except subprocess.TimeoutExpired as exc:
        raise ScriptError(f"script timed out after {timeout:g} s") from exc
    raw = stdout.decode("utf-8", "replace")[:MAX_RESULT_CHARS * 2]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        hint = stderr.decode("utf-8", "replace")[-200:] or raw[:200]
        raise ScriptError("script produced no valid result (it may have exceeded the memory limit): " + hint) from exc
    if not data.get("ok"):
        raise ScriptError(str(data.get("error") or "script failed"))
    return _normalize(data["result"])


# --------------------------------------------------------------------------- 自测


def mutate_cases(sample: str) -> list[dict[str, Any]]:
    """用真实样本造几组确定性用例：首次、持平、少一行、多一行、空输出、乱码。
    返回 [{name, ctx, expect}]，expect 是「合理的脚本应当给出的状态集合」，用来自动判断自测过没过。"""
    lines = sample.splitlines()
    body = [i for i, ln in enumerate(lines) if ln.strip()]
    mid = body[len(body) // 2] if body else 0
    one_less = "\n".join(ln for i, ln in enumerate(lines) if i != mid)
    one_more = sample + "\n" + (lines[mid] if lines else "x") + " (extra)"
    base = {"device": "DEV", "params": {}, "previous_run": "r0"}
    return [
        {"name": "first run (no previous)", "ctx": {**base, "output": sample, "previous": None, "previous_run": None},
         "expect": {"baseline", "pass"}},
        {"name": "unchanged", "ctx": {**base, "output": sample, "previous": sample}, "expect": {"pass"}},
        # 增 / 删一行：不预设期望（用户的需求可能明确不关心增删），只看不崩、确定性；是否报警由模型按用户目标给的用例判定
        {"name": "one line disappeared", "ctx": {**base, "output": one_less, "previous": sample}, "expect": {"fail", "pass", "baseline"}},
        {"name": "one line appeared", "ctx": {**base, "output": one_more, "previous": sample}, "expect": {"fail", "pass", "baseline"}},
        {"name": "garbage output", "ctx": {**base, "output": "% Invalid input detected at '^' marker.", "previous": sample},
         "expect": {"fail", "baseline", "pass"}},
    ]


def goal_cases(sample: str, specs: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """把模型按用户目标给的用例（在真实样本上做替换：{name, edits: [{find, replace}], expect: pass|fail}）变成可跑的用例。
    previous = 样本原文，output = 样本替换后。find 在样本里找不到、expect 不合法的用例作废并报告（让模型改）。"""
    cases: list[dict[str, Any]] = []
    problems: list[str] = []
    for i, spec in enumerate(specs if isinstance(specs, list) else []):
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or f"case {i + 1}")[:80]
        expect = spec.get("expect")
        if expect not in ("pass", "fail"):
            problems.append(f"test '{name}': expect must be 'pass' or 'fail'")
            continue
        out = sample
        ok = True
        for ed in spec.get("edits") or []:
            find, rep = str((ed or {}).get("find") or ""), str((ed or {}).get("replace") or "")
            if not find or find not in out:
                problems.append(f"test '{name}': the text to replace was not found in the sample: {find[:60]!r}")
                ok = False
                break
            out = out.replace(find, rep, 1)
        if ok:
            cases.append({"name": name, "ctx": {"device": "DEV", "params": {}, "output": out, "previous": sample, "previous_run": "r0"},
                          "expect": {expect}, "from_goal": True})
    return cases, problems


def self_test(source: str, sample: str, goal_specs: Any = None) -> dict[str, Any]:
    """静态检查 + 在样本派生的通用用例和「按用户目标给的用例」上各跑一次（并重复一次确认确定性）。
    `ok` = 没崩、返回合规、确定性；`plausible` = 通用用例合理，且目标用例全部给出了期望的结果，并且至少有一个
    「期望 fail」的目标用例（没有就无法证明脚本真的会报警——总是返回 pass 的脚本过不了这关）。"""
    out: dict[str, Any] = {"ok": False, "source_ok": False, "sha256": sha256(source), "cases": [], "problems": []}
    probs = validate_source(source)
    if probs:
        out["problems"] = probs
        return out
    out["source_ok"] = True
    extra, spec_problems = goal_cases(sample, goal_specs)
    out["goal_problems"] = spec_problems
    for case in [*mutate_cases(sample), *extra]:
        item: dict[str, Any] = {"name": case["name"], "from_goal": bool(case.get("from_goal")), "expect": set(case["expect"])}
        try:
            r1 = run_script(source, case["ctx"])
            r2 = run_script(source, case["ctx"])
            item.update(status=r1["status"], summary=r1["summary"], findings=r1["findings"][:5], metrics=r1["metrics"],
                        deterministic=(r1 == r2), plausible=(r1["status"] in case["expect"]),
                        _sig=json.dumps([r1["status"], r1["summary"], r1["findings"], r1["metrics"]], sort_keys=True, ensure_ascii=False))
        except ScriptError as exc:
            item.update(status="error", error=str(exc), deterministic=False, plausible=False)
        out["cases"].append(item)
    for c in out["cases"]:
        c.pop("_sig", None)
    out["problems"] = [f"{c['name']}: {c.get('error') or 'not deterministic'}" for c in out["cases"]
                       if c.get("status") == "error" or not c.get("deterministic")]
    out["ok"] = not out["problems"]
    goal_ok = [c for c in out["cases"] if c.get("from_goal")]
    expects_fail = any("fail" in c.get("expect", ()) for c in extra)
    out["has_goal_tests"] = bool(goal_ok) and expects_fail and not spec_problems
    out["plausible"] = all(c.get("plausible") for c in out["cases"]) and out["has_goal_tests"]
    for c in out["cases"]:
        c.pop("expect", None)
    return out

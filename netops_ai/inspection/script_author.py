"""巡检助手「自己开发判定脚本」：用户的需求模板和固定规则表达不了时，按这条命令在真实设备上采到的输出样本，让模型写一个
`evaluate(ctx)` 函数，在沙箱里自测，不过就带着问题让模型改（最多几轮），最后连源码、自测结果一起交给用户审核。

纯函数 + 注入的 LLM 客户端，不 import 框架；任何模型 / 沙箱失败都变成 `ok=False` 带人话原因，不抛给调用方。
"""
from __future__ import annotations

import json
import re
from typing import Any

from netops_ai.inspection import check_script as CS

MAX_ROUNDS = 3
SAMPLE_CHARS = 6000

SYSTEM_PROMPT = """You write ONE small Python function for a read-only network inspection check.

The check runs a single read-only `show` command on a device on a schedule. Your function judges the output of that run,
usually by comparing it with the output of the previous run.

```
def evaluate(ctx):
    # ctx["output"]        str   output of this run
    # ctx["previous"]      str|None   output of the previous successful run (None on the first run)
    # ctx["previous_run"]  str|None   id of that run
    # ctx["device"]        str   device name
    # ctx["params"]        dict  parameters (may be empty)
    return {"status": "pass" | "fail" | "baseline", "summary": "<one sentence>",
            "metrics": {"<name>": <number>}, "findings": ["<specific finding>", ...]}   # findings: at most 20
```

Rules:
- Text processing only. Allowed imports: re, json, ipaddress, collections, datetime, statistics, math, itertools. No file, network,
  process or reflection access; no open/eval/exec/__import__; no attribute names starting with "__".
- First run (`previous is None`): return status "baseline" and put what you counted in `metrics` — never "fail" on a first run.
- Deterministic: same input, same result. No randomness, no wall clock.
- Look at the REAL sample output below and parse it as it really looks. Ignore parts that change on their own every run
  (ages, uptimes, timers, counters the user did not ask about) — strip them before comparing, otherwise every run will "change".
- Be specific: findings must name the thing that changed (the route / neighbor / line), not just "changed".
- "fail" only for what the user asked to alarm on; other differences go into `findings` with status "pass" only if the user
  said they are fine, otherwise treat the user's wording literally.
- Keep it short (under 60 lines) and readable; comments in the user's language are welcome.

Also give 2-4 test cases that prove the script does what THE USER asked, built from the real sample by text replacement
(the previous run is the sample itself; the current run is the sample with your edits applied):
  {"name": "<short name in {language}>", "edits": [{"find": "<exact text that occurs in the sample>", "replace": "<new text>"}],
   "expect": "pass" | "fail"}
At least one case must change something the user wants to be alarmed about (expect "fail"), and at least one must change
something the user said to ignore or accept (expect "pass"). Every `find` must occur verbatim in the sample.

Reply with ONE JSON object: {"source": "<the full python source>", "explanation": "<2-3 sentences in {language}: what it compares,
what it ignores, when it fails>", "tests": [<the test cases>]}."""


def _messages(goal: str, command: str, sample: str, lang: str, feedback: str = "", previous_source: str = "") -> list[dict[str, str]]:
    system = SYSTEM_PROMPT.replace("{language}", "English" if lang == "en" else "Simplified Chinese")
    sample_head = sample[:SAMPLE_CHARS] + ("\n... (sample truncated)" if len(sample) > SAMPLE_CHARS else "")
    user = (f"What the user wants to detect:\n{goal}\n\nCommand: {command}\n\nReal output sample from a device "
            f"({len(sample.splitlines())} lines):\n```\n{sample_head}\n```")
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if previous_source:
        msgs += [{"role": "assistant", "content": json.dumps({"source": previous_source, "explanation": ""}, ensure_ascii=False)},
                 {"role": "user", "content": "That version has problems. Fix them and return the full corrected JSON:\n" + feedback}]
    return msgs


def _parse(resp: Any) -> dict[str, Any] | None:
    if isinstance(getattr(resp, "parsed", None), dict):
        return resp.parsed
    text = getattr(resp, "content", "") or ""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _feedback(test: dict[str, Any]) -> str:
    if not test.get("source_ok"):
        return "\n".join(test.get("problems") or ["the source does not pass the static checks"])
    parts = list(test.get("problems") or []) + list(test.get("goal_problems") or [])
    if not test.get("has_goal_tests") and not test.get("goal_problems"):
        parts.append("`tests` must contain at least one case that expects 'fail' (something the user wants alarmed) and at least one "
                     "that expects 'pass' (something the user said to ignore), built from the sample with `edits`")
    for c in test.get("cases") or []:
        if c.get("status") != "error" and not c.get("plausible"):
            parts.append(f"case '{c['name']}': " + (c.get("note") or f"returned status {c.get('status')!r}, which is not what a correct "
                         "check would return (first run → baseline; unchanged → pass; a line disappeared / appeared → it must notice)"))
    return "\n".join(parts)


def author_script(goal: str, command: str, sample: str, llm: Any, *, lang: str = "zh", max_rounds: int = MAX_ROUNDS) -> dict[str, Any]:
    """返回 {ok, source, sha256, explanation, tests, rounds, error}。ok = 通过静态检查、没崩、确定性、各用例合理。
    没写成时 ok=False，`source` 保留最后一版（可能为空）和 `tests`，给用户看卡在哪。"""
    out: dict[str, Any] = {"ok": False, "source": "", "sha256": "", "explanation": "", "tests": None, "rounds": 0, "error": ""}
    if llm is None:
        out["error"] = "no LLM client configured"
        return out
    if not sample.strip():
        out["error"] = "the command returned no output on the sample device; cannot write a script from nothing"
        return out
    feedback, prev = "", ""
    for rnd in range(1, max_rounds + 1):
        out["rounds"] = rnd
        try:
            resp = llm.complete(_messages(goal, command, sample, lang, feedback, prev), response_format={"type": "json_object"},
                                temperature=0.2, max_completion_tokens=3000)
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"model call failed: {str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__}"
            return out
        data = _parse(resp)
        src = str((data or {}).get("source") or "").strip("\n")
        if not data or not src:
            feedback, prev = "the reply was not a JSON object with a non-empty `source`", prev
            out["error"] = "the model did not return a script"
            continue
        out.update(source=src, sha256=CS.sha256(src), explanation=str(data.get("explanation") or "")[:600], error="")
        test = CS.self_test(src, sample, data.get("tests"))
        out["tests"] = test
        if test["ok"] and test.get("plausible"):
            out["ok"] = True
            return out
        feedback, prev = _feedback(test), src
    # 用尽：保留最后一版和自测结果；能跑但用例不合理的也交给用户看（用户可以拒绝）
    out["error"] = out["error"] or ("the script runs but did not behave as expected on the test cases: " + feedback[:300])
    return out


def pending_check(check_id: str, title: str, command: str, authored: dict[str, Any], *, devices: list[str] | None = None,
                  severity: str = "critical", params: dict[str, Any] | None = None) -> dict[str, Any]:
    """把写好的脚本变成草案里的一条检查项。`approved: False`——用户在审核气泡里点了批准才会变成 True，
    之前校验不过（`not been reviewed`），所以草案不会进入确认环节。"""
    item: dict[str, Any] = {
        "id": check_id, "title": title, "command": command,
        "expect": [{"type": "script", "name": check_id, "source": authored["source"], "sha256": authored["sha256"],
                    "approved": False, "severity": severity, "params": params or {}}],
        "extract": [],
    }
    if devices:
        item["devices"] = list(devices)
    return item

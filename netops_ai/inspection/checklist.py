"""Inspection checklist prototype: a declarative list of read-only commands per device, with expectations.

A checklist (YAML or JSON) names the devices and, for each check, one read-only command, what the output
must (or must not) contain, and optional numbers to extract. A run executes every command through the
read-only command whitelist (`DeviceAdapter.run`), evaluates the expectations with fixed rules (no model
involved), and stores one structured JSON result per run. A separate step turns the stored runs into a
trend-analysis prompt for an LLM.

Nothing here changes a device: commands that the whitelist refuses are recorded as `denied` and never sent.
`validate_checklist` runs the same whitelist before anything is executed, so a bad command is found at
authoring time, not at 3 a.m.

Checklist format::

    name: core-health            # used in result file names
    vendor: cisco                # whitelist rule set
    devices:
      - {name: D1, host: 192.0.2.10}
    checks:
      - id: interfaces
        command: show ip interface brief
        expect:
          - {type: not_contains, value: administratively down, severity: warning}
          - {type: regex, value: 'GigabitEthernet0/0\\s+\\S+\\s+YES\\s+\\S+\\s+up\\s+up', severity: critical}
        extract:
          - {name: input_errors, regex: '(\\d+) input errors', cast: int}
      - id: ospf
        command: show ip ospf neighbor
        devices: [D1]            # optional: run this check only on these devices (default: all)
        extract:
          - {name: full_neighbors, regex: '(FULL)', agg: count}   # agg: first (default) | sum | max | count

Stateful metric rules (expect entries that look at an extracted number instead of the text)::

      - id: routes
        command: show ip route | count ^[A-Z][A-Z0-9* ]+[0-9]+[.]
        extract:
          - {name: route_count, regex: '= (\\d+)', cast: int}
        expect:
          - {type: metric_vs_previous, metric: route_count, direction: decrease, tolerance_pct: 0, severity: critical}
          - {type: metric_threshold, metric: route_count, op: '>=', value: 1, severity: warning}
          - {type: metric_vs_baseline, metric: route_count, direction: change, tolerance_pct: 20}   # baseline: optional fixed value

The previous value / baseline comes from the stored runs (same checklist, device, check and metric; the most recent
run that actually collected the metric — unreachable devices and refused commands never become a baseline).
First run → `baseline` (recorded, not judged); earlier runs without the metric → `no_baseline` (not judged);
`reset_ok: true` treats a value lower than before as a counter reset, not a failure.

Output detail comparison (line sets, e.g. the routing table entry by entry)::

      - id: route_table
        command: show ip route | begin Gateway
        expect:
          - type: output_diff_vs_previous
            ignore_regex: ['is (variably )?subnetted']        # lines to drop before comparing
            strip_regex: ['\\s\\d+:\\d{2}:\\d{2},']            # volatile parts to cut out of every line (route age, uptime)
            continuation_regex: '^\\s+\\['                     # optional: lines that continue the previous entry (ECMP paths)
            key_regex: '^.*?\\d+\\.\\d+\\.\\d+\\.\\d+/\\d+'    # optional: the part of the entry line a continuation inherits
            max_added: 0                                      # allowed new lines (default 0)
            max_removed: 0                                    # allowed missing lines (default 0)
            severity: critical

Compared with the most recent run that actually collected this check's output (pass or fail; unreachable devices,
refused commands and errors are never a reference). The output is stored once per check (up to
`DIFF_MAX_CHARS`, beyond that it is cut at a line boundary and marked truncated); the evaluation itself carries only
the counts and the first `sample` (default 10) added / removed lines. If either side is truncated, only what both
sides can see is judged: lines missing from a truncated current output are not counted as removed, lines new
against a truncated reference are not counted as added.

A plan (`netops_ai/inspection/plans.py`) is the same mapping plus `schedule` and `enabled`;
the extra keys are ignored here.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from netops_ai.devices.whitelist import SUPPORTED_VENDORS, check as whitelist_check

TEXT_EXPECT_TYPES = ("contains", "not_contains", "regex", "not_regex")
#: 有状态的指标规则：看提取出来的数字，跟上一次 / 基线 / 固定阈值比
METRIC_EXPECT_TYPES = ("metric_vs_previous", "metric_threshold", "metric_vs_baseline")
#: 有状态的明细对比：整份输出按行集合跟上一次成功采到的输出比（新增 / 消失了哪些行）
DIFF_EXPECT_TYPES = ("output_diff_vs_previous",)
#: 助手按用户需求写、用户审核过的判定脚本（check_script.py）：模板和固定规则表达不了的需求
SCRIPT_EXPECT_TYPES = ("script",)
STATEFUL_EXPECT_TYPES = METRIC_EXPECT_TYPES + DIFF_EXPECT_TYPES + SCRIPT_EXPECT_TYPES
EXPECT_TYPES = TEXT_EXPECT_TYPES + STATEFUL_EXPECT_TYPES
DIRECTIONS = ("decrease", "increase", "change")
THRESHOLD_OPS = (">", "<", ">=", "<=")
#: 一条输出里提取多处数字时怎么合成一个值：first 第一处（默认）/ sum 求和 / max 最大 / count 命中次数
EXTRACT_AGGS = ("first", "sum", "max", "count")
SEVERITIES = ("info", "warning", "critical")
MAX_OUTPUT_CHARS = 6000
#: 明细对比的检查项单独留一份完整输出用来比（只存一份，不进每条判定），超过就按行截断并标记
DIFF_MAX_CHARS = 200_000
DIFF_SAMPLE = 10

AdapterFactory = Callable[[Any], Any]


# --------------------------------------------------------------------------- load / validate


def load_checklist(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    data = json.loads(text) if p.suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{p}: the checklist must be a mapping at the top level")
    return data


def validate_checklist(cl: dict[str, Any]) -> list[str]:
    """Return a list of problems; an empty list means the checklist is valid and every command passes the whitelist."""
    problems: list[str] = []
    name = cl.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name or ""):
        problems.append("name: required, 1-64 characters of letters, digits, '_', '.', '-'")
    vendor = cl.get("vendor", "cisco")
    if vendor not in SUPPORTED_VENDORS:
        problems.append(f"vendor: {vendor!r} is not supported (supported: {', '.join(SUPPORTED_VENDORS)})")
    devices = cl.get("devices")
    if not isinstance(devices, list) or not devices:
        problems.append("devices: a non-empty list of {name, host} is required")
    else:
        seen_dev: set[str] = set()
        for i, d in enumerate(devices):
            if not isinstance(d, dict) or not d.get("name") or not d.get("host"):
                problems.append(f"devices[{i}]: needs both name and host")
                continue
            if d["name"] in seen_dev:
                problems.append(f"devices[{i}]: duplicate name {d['name']!r}")
            seen_dev.add(d["name"])
    checks = cl.get("checks")
    if not isinstance(checks, list) or not checks:
        problems.append("checks: a non-empty list is required")
        return problems
    seen: set[str] = set()
    device_names = {d.get("name") for d in devices if isinstance(d, dict)} if isinstance(devices, list) else set()
    for i, c in enumerate(checks):
        where = f"checks[{i}]"
        if not isinstance(c, dict):
            problems.append(f"{where}: must be a mapping")
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", cid or ""):
            problems.append(f"{where}.id: required, letters, digits, '_', '.', '-'")
        elif cid in seen:
            problems.append(f"{where}.id: duplicate id {cid!r}")
        else:
            seen.add(cid)
        cmd = c.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            problems.append(f"{where}.command: required")
        elif vendor in SUPPORTED_VENDORS:
            verdict = whitelist_check(vendor, cmd)
            if not verdict.allowed:
                problems.append(f"{where}.command {cmd!r}: refused by the read-only whitelist ({verdict.reason})")
        scope = c.get("devices")
        if scope is not None:
            if not isinstance(scope, list) or not all(isinstance(x, str) for x in scope):
                problems.append(f"{where}.devices: a list of device names (omit it to run on every device)")
            else:
                unknown = [x for x in scope if x not in device_names]
                if unknown:
                    problems.append(f"{where}.devices: {', '.join(unknown)} not in the checklist's devices")
        metric_names = {x.get("name") for x in c.get("extract") or [] if isinstance(x, dict)}
        for j, e in enumerate(c.get("expect") or []):
            problems.extend(_validate_expect(f"{where}.expect[{j}]", e, metric_names))
        for j, x in enumerate(c.get("extract") or []):
            problems.extend(_validate_extract(f"{where}.extract[{j}]", x))
    return problems


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_metric_expect(where: str, e: dict[str, Any], metric_names: set[str]) -> list[str]:
    out: list[str] = []
    if e.get("metric") not in metric_names:
        out.append(f"{where}.metric: must name one of this check's extract metrics ({', '.join(sorted(n for n in metric_names if n)) or 'none defined'})")
    if e["type"] == "metric_threshold":
        if e.get("op") not in THRESHOLD_OPS:
            out.append(f"{where}.op: one of {', '.join(THRESHOLD_OPS)}")
        if not _is_num(e.get("value")):
            out.append(f"{where}.value: a number")
    else:
        if e.get("direction") not in DIRECTIONS:
            out.append(f"{where}.direction: one of {', '.join(DIRECTIONS)}")
        for k in ("tolerance_pct", "tolerance_abs"):
            if k in e and (not _is_num(e[k]) or e[k] < 0):
                out.append(f"{where}.{k}: a number >= 0")
        if "reset_ok" in e and not isinstance(e["reset_ok"], bool):
            out.append(f"{where}.reset_ok: true or false")
        if e["type"] == "metric_vs_baseline" and "baseline" in e and not _is_num(e["baseline"]):
            out.append(f"{where}.baseline: a number (omit it to use the first collected value)")
    if e.get("severity", "warning") not in SEVERITIES:
        out.append(f"{where}.severity: one of {', '.join(SEVERITIES)}")
    return out


def _regex_list(v: Any) -> list[str]:
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)


def _validate_diff_expect(where: str, e: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for k in ("ignore_regex", "strip_regex"):
        v = e.get(k)
        if v is None:
            continue
        if not (isinstance(v, str) or (isinstance(v, list) and all(isinstance(x, str) for x in v))):
            out.append(f"{where}.{k}: a regex or a list of regexes")
            continue
        for rx in _regex_list(v):
            try:
                re.compile(rx)
            except re.error as exc:
                out.append(f"{where}.{k}: invalid regex {rx!r} ({exc})")
    for k in ("continuation_regex", "key_regex"):
        if k in e:
            try:
                re.compile(e[k] if isinstance(e[k], str) else "(")
            except re.error as exc:
                out.append(f"{where}.{k}: invalid regex ({exc})")
    for k in ("max_added", "max_removed", "sample"):
        if k in e and (not isinstance(e[k], int) or isinstance(e[k], bool) or e[k] < 0):
            out.append(f"{where}.{k}: an integer >= 0")
    if e.get("severity", "warning") not in SEVERITIES:
        out.append(f"{where}.severity: one of {', '.join(SEVERITIES)}")
    return out


def _validate_script_expect(where: str, e: dict[str, Any]) -> list[str]:
    """判定脚本：静态检查源码；sha256 必须对得上；**没有用户审核通过（approved: true）的脚本不能保存**。"""
    from netops_ai.inspection import check_script as CS

    out = [f"{where}: {p}" for p in CS.validate_source(e.get("source"))]
    if e.get("sha256") != CS.sha256(str(e.get("source") or "")):
        out.append(f"{where}.sha256: does not match the source (the script was changed after review)")
    if e.get("approved") is not True:
        out.append(f"{where}.approved: the script has not been reviewed and approved by the user yet")
    if e.get("severity", "warning") not in SEVERITIES:
        out.append(f"{where}.severity: one of {', '.join(SEVERITIES)}")
    if not isinstance(e.get("params", {}), dict):
        out.append(f"{where}.params: must be a mapping")
    return out


def _validate_expect(where: str, e: Any, metric_names: set[str] | None = None) -> list[str]:
    if not isinstance(e, dict):
        return [f"{where}: must be a mapping"]
    if e.get("type") in DIFF_EXPECT_TYPES:
        return _validate_diff_expect(where, e)
    if e.get("type") in SCRIPT_EXPECT_TYPES:
        return _validate_script_expect(where, e)
    if e.get("type") in METRIC_EXPECT_TYPES:
        return _validate_metric_expect(where, e, metric_names or set())
    out: list[str] = []
    if e.get("type") not in EXPECT_TYPES:
        out.append(f"{where}.type: one of {', '.join(EXPECT_TYPES)}")
    if not isinstance(e.get("value"), str) or not e.get("value"):
        out.append(f"{where}.value: required string")
    elif e.get("type") in ("regex", "not_regex"):
        try:
            re.compile(e["value"])
        except re.error as exc:
            out.append(f"{where}.value: invalid regex ({exc})")
    if e.get("severity", "warning") not in SEVERITIES:
        out.append(f"{where}.severity: one of {', '.join(SEVERITIES)}")
    return out


def _validate_extract(where: str, x: Any) -> list[str]:
    if not isinstance(x, dict):
        return [f"{where}: must be a mapping"]
    out: list[str] = []
    if not isinstance(x.get("name"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", x.get("name") or ""):
        out.append(f"{where}.name: required, letters, digits, '_', '.', '-'")
    try:
        rx = re.compile(x.get("regex") or "")
        if rx.groups < 1:
            out.append(f"{where}.regex: needs one capture group")
    except re.error as exc:
        out.append(f"{where}.regex: invalid regex ({exc})")
    if x.get("cast", "float") not in ("int", "float"):
        out.append(f"{where}.cast: int or float")
    if x.get("agg", "first") not in EXTRACT_AGGS:
        out.append(f"{where}.agg: one of {', '.join(EXTRACT_AGGS)}")
    return out


# --------------------------------------------------------------------------- run


def _evaluate(expect: dict[str, Any], output: str) -> dict[str, Any]:
    kind, value = expect["type"], expect["value"]
    if kind == "contains":
        ok = value in output
    elif kind == "not_contains":
        ok = value not in output
    elif kind == "regex":
        ok = re.search(value, output, re.MULTILINE) is not None
    else:  # not_regex
        ok = re.search(value, output, re.MULTILINE) is None
    return {"type": kind, "value": value, "severity": expect.get("severity", "warning"), "passed": ok}


def _extract(spec: dict[str, Any], output: str) -> float | int | None:
    agg = spec.get("agg", "first")
    cast = int if spec.get("cast", "float") == "int" else float
    if agg == "count":
        return len(re.findall(spec["regex"], output, re.MULTILINE))  # 没命中就是 0，不是"取不到"
    if agg == "first":
        m = re.search(spec["regex"], output, re.MULTILINE)
        if not m:
            return None
        try:
            return cast(m.group(1))
        except ValueError:
            return None
    values = []
    for m in re.finditer(spec["regex"], output, re.MULTILINE):
        try:
            values.append(cast(m.group(1)))
        except ValueError:
            continue
    if not values:
        return None
    return sum(values) if agg == "sum" else max(values)


class MetricHistory:
    """历史运行里每个（设备, 检查项, 指标）成功采到的值，按时间从旧到新。只从存下来的运行结果建，不靠内存。
    设备不可达、命令被拒 / 出错、或者那次没提取到这个指标的运行，都不会成为「上一次」或「基线」。"""

    def __init__(self, runs: list[dict[str, Any]] | None = None) -> None:
        self.values: dict[tuple[str, str, str], list[tuple[str, str, float]]] = {}
        self.attempted: set[tuple[str, str]] = set()
        #: 明细对比用：每个（设备, 检查项）最近一次成功采到的完整输出 (时间, run id, 输出, 是否截断)
        self.snapshots: dict[tuple[str, str], tuple[str, str, str, bool]] = {}
        for r in sorted(runs or [], key=lambda r: r.get("started_at", "")):
            for d in r.get("devices") or []:
                for c in d.get("checks") or []:
                    self.attempted.add((d.get("name"), c.get("id")))
                    if c.get("status") not in ("pass", "fail"):
                        continue
                    if isinstance(c.get("diff_output"), str):
                        self.snapshots[(d.get("name"), c.get("id"))] = (r.get("started_at", ""), r.get("run_id", ""),
                                                                        c["diff_output"], bool(c.get("diff_truncated")))
                    for m, v in (c.get("metrics") or {}).items():
                        if _is_num(v):
                            self.values.setdefault((d.get("name"), c.get("id"), m), []).append((r.get("started_at", ""), r.get("run_id", ""), v))

    def previous(self, dev: str, cid: str, metric: str) -> tuple[str, str, float] | None:
        xs = self.values.get((dev, cid, metric))
        return xs[-1] if xs else None

    def first(self, dev: str, cid: str, metric: str) -> tuple[str, str, float] | None:
        xs = self.values.get((dev, cid, metric))
        return xs[0] if xs else None

    def ever_attempted(self, dev: str, cid: str) -> bool:
        return (dev, cid) in self.attempted

    def snapshot(self, dev: str, cid: str) -> tuple[str, str, str, bool] | None:
        return self.snapshots.get((dev, cid))


def cap_diff_output(output: str) -> tuple[str, bool]:
    """明细对比要存的那份输出：不超过 DIFF_MAX_CHARS；超了就在行边界截断（不留半行），返回 (文本, 是否截断)。"""
    if len(output) <= DIFF_MAX_CHARS:
        return output, False
    cut = output[:DIFF_MAX_CHARS]
    nl = cut.rfind("\n")
    return (cut[:nl] if nl > 0 else ""), True


def normalize_lines(output: str, e: dict[str, Any]) -> set[str]:
    """把输出变成用来比较的行集合：去掉忽略行、剪掉易变字段（路由老化时间等）、折叠空白；
    续行（如等价多路径的第二条下一跳）带上它所属条目的前缀，各自成一行，丢了一条路径也能看出来。"""
    ignore = [re.compile(x) for x in _regex_list(e.get("ignore_regex"))]
    strip = [re.compile(x) for x in _regex_list(e.get("strip_regex"))]
    cont = re.compile(e["continuation_regex"]) if e.get("continuation_regex") else None
    key = re.compile(e["key_regex"]) if e.get("key_regex") else None
    lines: set[str] = set()
    parent = ""
    for raw in output.splitlines():
        if not raw.strip() or any(rx.search(raw) for rx in ignore):
            continue
        is_cont = bool(cont and cont.search(raw))
        text = raw
        for rx in strip:
            text = rx.sub("", text)
        text = " ".join(text.split())
        if not text:
            continue
        if is_cont and parent:
            text = f"{parent} {text}"
        else:
            m = key.search(text) if key else None
            parent = m.group(0) if m else text
        lines.add(text)
    return lines


def _describe_diff_rule(e: dict[str, Any]) -> str:
    return f"output lines vs previous run (allowed +{e.get('max_added', 0)} / -{e.get('max_removed', 0)})"


def evaluate_diff_rule(e: dict[str, Any], output: str, truncated: bool, dev: str, cid: str, hist: MetricHistory) -> dict[str, Any]:
    """明细对比一条规则的结果。只带计数和前 `sample` 条样例，不带整表。`passed` 为 None 表示这次不判。"""
    sample = int(e.get("sample", DIFF_SAMPLE))
    cur = normalize_lines(output, e)
    out: dict[str, Any] = {"type": e["type"], "severity": e.get("severity", "warning"), "value": _describe_diff_rule(e),
                           "current": len(cur), "reference": None, "reference_run_id": None, "reference_at": None,
                           "reference_source": None, "added": None, "removed": None, "added_sample": [], "removed_sample": [],
                           "truncated": {"current": truncated, "reference": False}}
    ref = hist.snapshot(dev, cid)
    if ref is None:
        if hist.ever_attempted(dev, cid):
            return {**out, "passed": None, "state": "no_baseline",
                    "detail": "earlier runs did not collect this output (unreachable, refused or error); recorded now, not judged"}
        return {**out, "passed": None, "state": "baseline", "detail": f"first run: {len(cur)} lines recorded as the baseline, not judged"}
    at, run_id, ref_text, ref_trunc = ref
    old = normalize_lines(ref_text, e)
    added = sorted(cur - old)
    removed = sorted(old - cur)
    out.update(reference=len(old), reference_run_id=run_id, reference_at=at, reference_source="run",
               truncated={"current": truncated, "reference": ref_trunc})
    # 截断的一边看不全：当前输出截断 → 「消失」可能只是没采到，不算；参考输出截断 → 「新增」可能只是上次没采到，不算
    judge_removed, judge_added = not truncated, not ref_trunc
    out.update(added=len(added) if judge_added else None, removed=len(removed) if judge_removed else None,
               added_sample=added[:sample] if judge_added else [], removed_sample=removed[:sample] if judge_removed else [])
    if not judge_added and not judge_removed:
        return {**out, "passed": None, "state": "truncated", "detail": "both outputs were truncated; not judged"}
    bad = []
    if judge_added and len(added) > int(e.get("max_added", 0)):
        bad.append(f"+{len(added)} > {e.get('max_added', 0)}")
    if judge_removed and len(removed) > int(e.get("max_removed", 0)):
        bad.append(f"-{len(removed)} > {e.get('max_removed', 0)}")
    parts = [f"+{len(added)}" if judge_added else "+? (reference truncated)", f"-{len(removed)}" if judge_removed else "-? (current truncated)"]
    detail = f"{' / '.join(parts)} vs run {run_id}" + (f": over the limit ({', '.join(bad)})" if bad else "")
    state = "fail" if bad else ("pass" if (judge_added and judge_removed) else "partial")
    return {**out, "passed": not bad, "state": state, "detail": detail}


def evaluate_expectations(check: dict[str, Any], output: str, metrics: dict[str, Any], dev: str,
                          hist: MetricHistory) -> list[dict[str, Any]]:
    """一条检查项的全部判定（正式运行和试运行共用）。明细对比只看存得下的那部分（cap_diff_output）。"""
    evals = []
    diff_text, diff_trunc = cap_diff_output(output)
    for e in check.get("expect") or []:
        if e.get("type") in DIFF_EXPECT_TYPES:
            evals.append(evaluate_diff_rule(e, diff_text, diff_trunc, dev, check["id"], hist))
        elif e.get("type") in SCRIPT_EXPECT_TYPES:
            evals.append(evaluate_script_rule(e, diff_text, diff_trunc, dev, check["id"], hist))
        elif e.get("type") in METRIC_EXPECT_TYPES:
            evals.append(evaluate_metric_rule(e, metrics.get(e["metric"]), dev, check["id"], hist))
        else:
            evals.append(_evaluate(e, output))
    return evals


def has_diff_rule(check: dict[str, Any]) -> bool:
    """需要把整份输出存下来当下次参考的检查项（明细对比 / 判定脚本）。"""
    return any(isinstance(e, dict) and e.get("type") in DIFF_EXPECT_TYPES + SCRIPT_EXPECT_TYPES for e in check.get("expect") or [])


def evaluate_script_rule(e: dict[str, Any], output: str, truncated: bool, dev: str, cid: str, hist: MetricHistory) -> dict[str, Any]:
    """跑一条判定脚本（沙箱）。`passed` 为 None = 这次不判（首次建基线）；脚本出错 / 未审核 / 源码被改 = 判失败并写明原因，
    **不会静默通过**。比较用的「上一次」取自存档里最近一次成功采集到的输出。"""
    from netops_ai.inspection import check_script as CS

    out: dict[str, Any] = {"type": "script", "severity": e.get("severity", "warning"), "name": e.get("name") or "script",
                           "value": f"script {e.get('name') or ''} {str(e.get('sha256') or '')[:8]}".strip(), "findings": [],
                           "reference_run_id": None, "script_metrics": {}, "truncated": truncated}
    if e.get("approved") is not True or e.get("sha256") != CS.sha256(str(e.get("source") or "")):
        return {**out, "passed": False, "state": "error", "detail": "script is not approved or was changed after review; not executed"}
    ref = hist.snapshot(dev, cid)
    prev_text = ref[2] if ref else None
    out["reference_run_id"] = ref[1] if ref else None
    try:
        r = CS.run_script(e["source"], {"output": output, "previous": prev_text, "previous_run": out["reference_run_id"],
                                        "device": dev, "params": e.get("params") or {}})
    except CS.ScriptError as exc:
        return {**out, "passed": False, "state": "error", "detail": f"script error: {exc}"}
    note = " (output truncated: judged on what was collected)" if truncated else ""
    out.update(findings=r["findings"], script_metrics=r["metrics"], detail=(r["summary"] or r["status"]) + note)
    if r["status"] == "baseline":
        return {**out, "passed": None, "state": "baseline"}
    return {**out, "passed": r["status"] == "pass", "state": r["status"]}


def _describe_metric_rule(e: dict[str, Any]) -> str:
    if e["type"] == "metric_threshold":
        return f"{e['metric']} {e['op']} {e['value']}"
    tol = []
    if e.get("tolerance_pct"):
        tol.append(f"{e['tolerance_pct']}%")
    if e.get("tolerance_abs"):
        tol.append(str(e["tolerance_abs"]))
    ref = "previous run" if e["type"] == "metric_vs_previous" else ("baseline " + str(e["baseline"]) if "baseline" in e else "first collected value")
    return f"{e['metric']} {e['direction']} vs {ref}" + (f" (tolerance {' / '.join(tol)})" if tol else "") + (" [reset ok]" if e.get("reset_ok") else "")


def evaluate_metric_rule(e: dict[str, Any], current: Any, dev: str, cid: str, hist: MetricHistory) -> dict[str, Any]:
    """一条指标规则的结果。`passed` 为 None 表示这次不判（首次建基线 / 没有基线 / 这次没采到）。
    写明比较的双方：current、reference 和 reference 来自哪次运行（run id + 时间）。"""
    out: dict[str, Any] = {"type": e["type"], "metric": e["metric"], "severity": e.get("severity", "warning"),
                           "value": _describe_metric_rule(e), "current": current, "reference": None,
                           "reference_run_id": None, "reference_at": None, "reference_source": None}
    if not _is_num(current):
        return {**out, "passed": None, "state": "no_value", "detail": f"{e['metric']} was not collected this time; not judged"}
    if e["type"] == "metric_threshold":
        ok = {">": current > e["value"], "<": current < e["value"], ">=": current >= e["value"], "<=": current <= e["value"]}[e["op"]]
        return {**out, "reference": e["value"], "reference_source": "threshold", "passed": ok, "state": "pass" if ok else "fail",
                "detail": f"{current} {e['op']} {e['value']}: {'yes' if ok else 'no'}"}
    if e["type"] == "metric_vs_baseline" and "baseline" in e:
        ref = (None, None, e["baseline"])  # 用户固定的基线：没有来源运行
    else:
        ref = hist.previous(dev, cid, e["metric"]) if e["type"] == "metric_vs_previous" else hist.first(dev, cid, e["metric"])
    if ref is None:
        if hist.ever_attempted(dev, cid):
            return {**out, "passed": None, "state": "no_baseline",
                    "detail": "earlier runs did not collect this metric (unreachable, refused or not extracted); recorded now, not judged"}
        return {**out, "passed": None, "state": "baseline", "detail": "first run: value recorded as the baseline, not judged"}
    at, run_id, refv = ref
    out.update(reference=refv, reference_run_id=run_id, reference_at=at, reference_source="run" if run_id else "fixed")
    tol = max(float(e.get("tolerance_abs") or 0), abs(refv) * float(e.get("tolerance_pct") or 0) / 100)
    if e.get("reset_ok") and current < refv:
        return {**out, "passed": True, "state": "reset", "detail": f"{current} < {refv}: treated as a counter reset"}
    if e["direction"] == "decrease":
        ok = current >= refv - tol
    elif e["direction"] == "increase":
        ok = current <= refv + tol
    else:
        ok = abs(current - refv) <= tol
    delta = current - refv
    return {**out, "passed": ok, "state": "pass" if ok else "fail",
            "detail": f"{current} vs {refv} ({'run ' + run_id if run_id else 'fixed baseline'}): {'+' if delta >= 0 else ''}{delta:g}, allowed {tol:g}"}


def _run_check(adapter: Any, check: dict[str, Any], dev_name: str = "", hist: MetricHistory | None = None) -> dict[str, Any]:
    res = adapter.run(check["command"])
    item: dict[str, Any] = {"id": check["id"], "command": check["command"], "evaluations": [], "metrics": {}}
    if not res.allowed:
        item.update(status="denied", detail=res.denial_reason, output="")
        return item
    if res.error:
        item.update(status="error", detail=res.error, output="")
        return item
    output = res.output or ""
    item["output"] = output[:MAX_OUTPUT_CHARS]
    if len(output) > MAX_OUTPUT_CHARS:
        item["output_truncated"] = True
    for x in check.get("extract") or []:
        item["metrics"][x["name"]] = _extract(x, output)
    if has_diff_rule(check):
        # 下次对比的参考：只存这一份（不进每条判定）；不可达 / 被拒 / 出错的运行走不到这里，不会覆盖基线
        item["diff_output"], item["diff_truncated"] = cap_diff_output(output)
    item["evaluations"] = evaluate_expectations(check, output, item["metrics"], dev_name, hist or MetricHistory())
    for ev in item["evaluations"]:
        if ev.get("script_metrics"):  # 脚本自己算出来的数（如路由条数）和提取出来的一样进趋势表
            item["metrics"].update(ev["script_metrics"])
    failed = [e for e in item["evaluations"] if e["passed"] is False]  # None = 这次不判（首次基线等）
    item["status"] = "fail" if failed else "pass"
    if failed:
        order = {s: i for i, s in enumerate(SEVERITIES)}
        item["severity"] = max((e["severity"] for e in failed), key=order.__getitem__)
    return item


def run_checklist(cl: dict[str, Any], adapter_factory: AdapterFactory, *, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Execute a validated checklist once. `adapter_factory(device)` returns an object with `.run(command)`.
    `history`: earlier stored runs of this checklist (any order) — needed by the stateful metric rules."""
    problems = validate_checklist(cl)
    if problems:
        raise ValueError("invalid checklist: " + "; ".join(problems))
    started = datetime.now(timezone.utc)
    hist = MetricHistory(history)
    devices_out: list[dict[str, Any]] = []
    for d in cl["devices"]:
        dev: dict[str, Any] = {"name": d["name"], "host": d["host"], "checks": []}
        try:
            adapter = adapter_factory(type("Dev", (), {"name": d["name"], "host": d["host"]})())
        except Exception as exc:  # noqa: BLE001 - one unreachable device must not stop the run
            dev["error"] = f"{type(exc).__name__}: {exc}"
            devices_out.append(dev)
            continue
        try:
            for c in cl["checks"]:
                if c.get("devices") is not None and d["name"] not in c["devices"]:
                    continue  # 这条检查只针对别的设备
                dev["checks"].append(_run_check(adapter, c, d["name"], hist))
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()
        devices_out.append(dev)
    all_checks = [c for d in devices_out for c in d["checks"]]
    summary = {s: sum(1 for c in all_checks if c["status"] == s) for s in ("pass", "fail", "denied", "error")}
    summary["unreachable_devices"] = sum(1 for d in devices_out if d.get("error"))
    return {
        "schema": 1,
        "run_id": uuid.uuid4().hex[:8],
        "checklist": cl["name"],
        "vendor": cl.get("vendor", "cisco"),
        "started_at": started.isoformat(timespec="microseconds"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "devices": devices_out,
        "summary": summary,
    }


def save_result(result: dict[str, Any], out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = result["started_at"].replace(":", "").replace("-", "").replace("+0000", "Z").replace("+00:00", "Z")
    path = out / f"{stamp}-{result['checklist']}-{result['run_id']}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_history(out_dir: str | Path, name: str, last: int = 12) -> list[dict[str, Any]]:
    """The most recent `last` stored runs of one checklist, oldest first."""
    runs = []
    for f in Path(out_dir).glob(f"*-{name}-*.json"):
        try:
            run = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # 文件名通配会串名：`core` 的通配也命中 `core-health` 的结果文件，所以按记录里的名字再筛一遍
        if isinstance(run, dict) and run.get("checklist") == name:
            runs.append(run)
    runs.sort(key=lambda r: r.get("started_at", ""))  # order by the recorded start time, not by file name
    return runs[-last:]


# --------------------------------------------------------------------------- trend analysis


TREND_SYSTEM_PROMPT = """You are a network operations analyst reviewing the stored results of a scheduled, read-only inspection checklist.

You are given, for each device and check, the pass/fail status and the extracted numeric metrics of several consecutive runs, oldest first.
Rules:
- Use only the data given. Do not invent runs, values or causes. Cite run ids (the short ids in the table) for every claim.
- Classify each metric or failing check as one of: worsening, improving, flapping (alternating), stable, insufficient data (fewer than 3 runs or missing values).
- A counter that only grows is a trend only if it grows faster than before or the growth continues across at least 3 runs; a single jump is a spike, not a trend. A counter that drops to a lower value means the device or interface was reset, not that it improved.
- Say which findings need action soon, which can wait, and which you cannot judge. For anything you cannot judge, name the data or the read-only command that would settle it.
- Lines marked "rule" are stateful metric rules (compared with the previous run, a baseline or a threshold): "baseline" means the first run that recorded the value, "no_baseline" that earlier runs did not collect it, "reset" a counter that dropped and was treated as reset — none of these is a failure.
- Lines "rule output diff" compare the whole output line by line with the previous run: "+N/-M" lines appeared / disappeared, with up to three sample lines (for a routing table each line is a route or a path). "truncated" or "?" means part of the output was not collected, so that side was not judged. A change that appears and is reversed in the next run is a flap, not a trend.
- Never suggest a configuration change as something you have done. Suggestions are for a human to decide.
Answer concisely in the language the operator used for the checklist notes (default: the language of this prompt), as a short list: device, check, classification, evidence (run ids and values), recommendation."""


def trend_table(history: list[dict[str, Any]]) -> str:
    """Render the stored runs as a compact text table the model can read."""
    if not history:
        return "(no runs stored)"
    lines = ["runs (oldest first): " + ", ".join(f"{r['run_id']}@{r['started_at']}" for r in history)]
    keys: dict[tuple[str, str], list[str]] = {}
    for r in history:
        for d in r["devices"]:
            for c in d["checks"]:
                keys.setdefault((d["name"], c["id"]), [])
    for (dev, cid) in keys:
        cells: list[str] = []
        metric_names: list[str] = []
        for r in history:
            cell = "-"
            for d in r["devices"]:
                if d["name"] != dev:
                    continue
                if d.get("error"):
                    cell = "unreachable"
                for c in d["checks"]:
                    if c["id"] != cid:
                        continue
                    cell = c["status"]
                    for m, v in c.get("metrics", {}).items():
                        if m not in metric_names:
                            metric_names.append(m)
            cells.append(cell)
        lines.append(f"{dev}/{cid} status: " + " | ".join(cells))
        for m in metric_names:
            vals = []
            for r in history:
                v = None
                for d in r["devices"]:
                    if d["name"] == dev:
                        for c in d["checks"]:
                            if c["id"] == cid:
                                v = c.get("metrics", {}).get(m)
                vals.append("-" if v is None else str(v))
            lines.append(f"{dev}/{cid} {m}: " + " | ".join(vals))
        rules: dict[str, list[str]] = {}
        for r in history:
            got: dict[str, str] = {}
            for d in r["devices"]:
                if d["name"] != dev:
                    continue
                for c in d["checks"]:
                    if c["id"] != cid:
                        continue
                    for e in c.get("evaluations") or []:
                        if e.get("type") in DIFF_EXPECT_TYPES:
                            if e.get("state") in ("pass", "fail", "partial"):
                                a = "?" if e.get("added") is None else e["added"]
                                rm = "?" if e.get("removed") is None else e["removed"]
                                samples = "; ".join([f"+{s}" for s in (e.get("added_sample") or [])[:3]] + [f"-{s}" for s in (e.get("removed_sample") or [])[:3]])
                                got["output diff"] = f"{e['state']}(+{a}/-{rm}" + (f": {samples}" if samples else "") + ")"
                            else:
                                got["output diff"] = str(e.get("state"))
                        elif e.get("type") in METRIC_EXPECT_TYPES:
                            ref = "" if e.get("reference") is None else f"({e['reference']}→{e.get('current')})"
                            got[e.get("value", e["type"])] = f"{e.get('state')}{ref}"
            for k in got:
                rules.setdefault(k, [])
            for k in rules:
                rules[k].append(got.get(k, "-"))
        for k, cells in rules.items():
            lines.append(f"{dev}/{cid} rule {k}: " + " | ".join(cells))
    return "\n".join(lines)


def build_trend_messages(history: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": TREND_SYSTEM_PROMPT},
        {"role": "user", "content": "Stored inspection runs:\n\n" + trend_table(history)},
    ]

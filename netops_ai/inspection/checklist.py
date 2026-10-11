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
EXPECT_TYPES = TEXT_EXPECT_TYPES + METRIC_EXPECT_TYPES
DIRECTIONS = ("decrease", "increase", "change")
THRESHOLD_OPS = (">", "<", ">=", "<=")
#: 一条输出里提取多处数字时怎么合成一个值：first 第一处（默认）/ sum 求和 / max 最大 / count 命中次数
EXTRACT_AGGS = ("first", "sum", "max", "count")
SEVERITIES = ("info", "warning", "critical")
MAX_OUTPUT_CHARS = 6000

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


def _validate_expect(where: str, e: Any, metric_names: set[str] | None = None) -> list[str]:
    if not isinstance(e, dict):
        return [f"{where}: must be a mapping"]
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
        for r in sorted(runs or [], key=lambda r: r.get("started_at", "")):
            for d in r.get("devices") or []:
                for c in d.get("checks") or []:
                    self.attempted.add((d.get("name"), c.get("id")))
                    if c.get("status") not in ("pass", "fail"):
                        continue
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
    for e in check.get("expect") or []:
        if e.get("type") in METRIC_EXPECT_TYPES:
            item["evaluations"].append(evaluate_metric_rule(e, item["metrics"].get(e["metric"]), dev_name, check["id"], hist or MetricHistory()))
        else:
            item["evaluations"].append(_evaluate(e, output))
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
                        if e.get("type") in METRIC_EXPECT_TYPES:
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

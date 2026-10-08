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

EXPECT_TYPES = ("contains", "not_contains", "regex", "not_regex")
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
        for j, e in enumerate(c.get("expect") or []):
            problems.extend(_validate_expect(f"{where}.expect[{j}]", e))
        for j, x in enumerate(c.get("extract") or []):
            problems.extend(_validate_extract(f"{where}.extract[{j}]", x))
    return problems


def _validate_expect(where: str, e: Any) -> list[str]:
    if not isinstance(e, dict):
        return [f"{where}: must be a mapping"]
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
    m = re.search(spec["regex"], output, re.MULTILINE)
    if not m:
        return None
    try:
        return (int if spec.get("cast", "float") == "int" else float)(m.group(1))
    except ValueError:
        return None


def _run_check(adapter: Any, check: dict[str, Any]) -> dict[str, Any]:
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
    for e in check.get("expect") or []:
        item["evaluations"].append(_evaluate(e, output))
    for x in check.get("extract") or []:
        item["metrics"][x["name"]] = _extract(x, output)
    failed = [e for e in item["evaluations"] if not e["passed"]]
    item["status"] = "fail" if failed else "pass"
    if failed:
        order = {s: i for i, s in enumerate(SEVERITIES)}
        item["severity"] = max((e["severity"] for e in failed), key=order.__getitem__)
    return item


def run_checklist(cl: dict[str, Any], adapter_factory: AdapterFactory) -> dict[str, Any]:
    """Execute a validated checklist once. `adapter_factory(device)` returns an object with `.run(command)`."""
    problems = validate_checklist(cl)
    if problems:
        raise ValueError("invalid checklist: " + "; ".join(problems))
    started = datetime.now(timezone.utc)
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
                dev["checks"].append(_run_check(adapter, c))
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
            runs.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
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
    return "\n".join(lines)


def build_trend_messages(history: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": TREND_SYSTEM_PROMPT},
        {"role": "user", "content": "Stored inspection runs:\n\n" + trend_table(history)},
    ]

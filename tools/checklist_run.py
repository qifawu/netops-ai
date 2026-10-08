"""Inspection checklist prototype CLI: validate a checklist, run it (once or on a timer), analyse the trend.

    python tools/checklist_run.py validate examples/checklists/core-health.yaml
    python tools/checklist_run.py run      examples/checklists/core-health.yaml            # one run, stored as JSON
    python tools/checklist_run.py run      examples/checklists/core-health.yaml --every 60 --count 24
    python tools/checklist_run.py trend    core-health --last 12                           # print the LLM prompt
    python tools/checklist_run.py trend    core-health --last 12 --ask-llm                 # and ask the model

Device credentials come from `.env` (`DEVICE_USERNAME`, `DEVICE_PASSWORD`, `DEVICE_VENDOR`, `DEVICE_TRANSPORT`):
use a read-only account. Every command goes through the read-only command whitelist; refused commands are
recorded as `denied` and never sent. For a real schedule use cron / Task Scheduler with `run ... ` (one run each time).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netops_ai.inspection.checklist import (  # noqa: E402
    build_trend_messages,
    load_checklist,
    load_history,
    run_checklist,
    save_result,
    validate_checklist,
)

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "records" / "checklist-runs"


def _print_summary(result: dict, path: Path) -> None:
    s = result["summary"]
    print(f"run {result['run_id']}  {result['checklist']}  pass={s['pass']} fail={s['fail']} denied={s['denied']} "
          f"error={s['error']} unreachable_devices={s['unreachable_devices']}")
    for d in result["devices"]:
        if d.get("error"):
            print(f"  {d['name']}: UNREACHABLE ({d['error']})")
            continue
        for c in d["checks"]:
            extra = f" metrics={c['metrics']}" if c.get("metrics") else ""
            note = f" ({c['detail']})" if c.get("detail") else ""
            print(f"  {d['name']}/{c['id']}: {c['status']}{note}{extra}")
    print(f"stored: {path}")


def cmd_validate(args: argparse.Namespace) -> int:
    problems = validate_checklist(load_checklist(args.checklist))
    if problems:
        print("INVALID")
        for p in problems:
            print(" -", p)
        return 1
    print("OK: format valid, every command passes the read-only whitelist")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from netops_ai.topology import _default_adapter_factory

    cl = load_checklist(args.checklist)
    problems = validate_checklist(cl)
    if problems:
        print("INVALID")
        for p in problems:
            print(" -", p)
        return 1
    n = 0
    worst = 0
    while True:
        result = run_checklist(cl, _default_adapter_factory)
        path = save_result(result, args.out)
        _print_summary(result, path)
        s = result["summary"]
        worst = max(worst, 2 if (s["fail"] or s["error"] or s["unreachable_devices"]) else 0)
        n += 1
        if not args.every or (args.count and n >= args.count):
            return worst
        time.sleep(args.every * 60)


def cmd_trend(args: argparse.Namespace) -> int:
    history = load_history(args.out, args.name, args.last)
    if not history:
        print(f"no stored runs of {args.name!r} in {args.out}")
        return 1
    messages = build_trend_messages(history)
    if not args.ask_llm:
        print(messages[0]["content"])
        print("\n--- user message ---\n")
        print(messages[1]["content"])
        return 0
    from netops_ai.llm.client import LLMClient

    resp = LLMClient().complete(messages, temperature=0.2)
    print(resp.content)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("checklist")
    v.set_defaults(fn=cmd_validate)
    r = sub.add_parser("run")
    r.add_argument("checklist")
    r.add_argument("--out", default=str(DEFAULT_OUT))
    r.add_argument("--every", type=float, default=0, help="repeat every N minutes (default: run once)")
    r.add_argument("--count", type=int, default=0, help="with --every: stop after N runs (default: forever)")
    r.set_defaults(fn=cmd_run)
    t = sub.add_parser("trend")
    t.add_argument("name", help="checklist name")
    t.add_argument("--out", default=str(DEFAULT_OUT))
    t.add_argument("--last", type=int, default=12)
    t.add_argument("--ask-llm", action="store_true")
    t.set_defaults(fn=cmd_trend)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

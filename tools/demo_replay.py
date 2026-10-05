#!/usr/bin/env python
"""Offline replay: render saved alert records the way the live pipeline would present them.
No Zabbix, no devices, no model, no credentials.

    python tools/demo_replay.py                      # replay the synthetic examples in examples/alerts/
    python tools/demo_replay.py path/to/alert.json   # replay your own record (same shape as records/alert-*.json)
    python tools/demo_replay.py --card               # also print the Feishu card JSON

离线回放：拿落盘的告警记录，渲染成线上同样的展示（结论摘要 + 飞书卡片）。不连 Zabbix、不连设备、不调模型。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from netops_ai.feishu.card import build_card  # noqa: E402

DEFAULT_DIR = REPO_ROOT / "examples" / "alerts"


def replay(record: dict) -> dict:
    """Replay one record and return the result (no printing)."""
    analysis = record.get("analysis_parsed") or {}
    payload = record.get("webhook_payload") or {}
    eventid = str(record.get("eventid") or payload.get("eventid") or "")
    card = build_card({
        "eventids": [eventid],
        "analysis": analysis,
        "alert_names": {eventid: payload.get("name", "")},
        "host": payload.get("host", ""),
    })
    return {"eventid": eventid, "card": card, "analysis": analysis, "payload": payload}


def _print(result: dict, *, show_card: bool) -> None:
    payload, analysis = result["payload"], result["analysis"]
    print(f"\n=== 告警 {result['eventid']} · {payload.get('host', '')} · {payload.get('name', '')}")
    print(f"根因：{analysis.get('root_cause', '（无）')}")
    print(f"置信度：{analysis.get('confidence', '?')}")
    evidence = analysis.get("evidence") or []
    print(f"证据（{len(evidence)} 条）：")
    for item in evidence:
        print(f"  ({item.get('source_from', '')}) {item.get('claim', '')}")
    header = result["card"].get("header") or {}
    title = (header.get("title") or {}).get("content", "")
    print(f"飞书卡片：标题「{title}」，配色 {header.get('template', '')}")
    if show_card:
        print(json.dumps(result["card"], ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Offline replay of saved alert records.")
    ap.add_argument("records", nargs="*", help="alert record json files; default: everything in examples/alerts/")
    ap.add_argument("--card", action="store_true", help="also print the Feishu card JSON")
    args = ap.parse_args(argv)
    paths = [Path(p) for p in args.records] or sorted(DEFAULT_DIR.glob("*.json"))
    if not paths:
        print(f"没有可回放的记录（默认目录 {DEFAULT_DIR} 为空）")
        return 2
    for path in paths:
        _print(replay(json.loads(path.read_text(encoding="utf-8"))), show_card=args.card)
    print(f"\n汇总：{len(paths)} 条记录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

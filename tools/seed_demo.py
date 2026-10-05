#!/usr/bin/env python
"""Fill `records/` with the synthetic example alerts so the dashboard is not empty on first run.

    python tools/seed_demo.py              # copy examples/alerts/*.json into records/alert-<eventid>.json
    python tools/seed_demo.py --records-dir /tmp/demo-records

Timestamps are shifted to "a few hours ago" so the time-window widgets (last 24 h, trends) show them.
Existing files are never overwritten. The records are synthetic: no real network, host or address.

把合成示例告警灌进 `records/`，让看板第一次打开就有数据。时间戳会平移到「几小时前」，
这样「最近 24 小时」这类按时间窗口的卡片能看到它们。已有文件不会被覆盖。记录是合成的。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SRC = REPO_ROOT / "examples" / "alerts"
DEFAULT_DST = REPO_ROOT / "records"


def shift(record: dict, minutes_ago: int, now: float) -> dict:
    """Return a copy of the record whose timestamps are `minutes_ago` minutes before `now`."""
    rec = json.loads(json.dumps(record))
    clock = int(now - minutes_ago * 60)
    old_clock = int(rec.get("alert_clock") or clock)
    delta = clock - old_clock
    rec["alert_clock"] = clock
    rec.setdefault("webhook_payload", {})["clock"] = str(clock)
    for key in ("started_at", "finished_at"):
        value = rec.get(key)
        if value:
            moved = datetime.fromisoformat(value) + timedelta(seconds=delta)
            rec[key] = moved.astimezone(timezone.utc).isoformat()
    if rec.get("incident_id"):
        rec["incident_id"] = f"demo{rec.get('eventid')}-{clock}"
    return rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Seed records/ with the synthetic example alerts.")
    ap.add_argument("--examples-dir", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--records-dir", type=Path, default=DEFAULT_DST)
    args = ap.parse_args(argv)
    sources = sorted(args.examples_dir.glob("*.json"))
    if not sources:
        print(f"没有找到示例：{args.examples_dir}")
        return 2
    args.records_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    written = skipped = 0
    for index, path in enumerate(sources):
        record = json.loads(path.read_text(encoding="utf-8"))
        target = args.records_dir / f"alert-{record['eventid']}.json"
        if target.exists():
            skipped += 1
            continue
        # 最早的一条 6 小时前，之后每条晚一小时，最近的一条在 1 小时前
        minutes_ago = max(60, 360 - index * 60)
        shifted = shift(record, minutes_ago, now)
        target.write_text(json.dumps(shifted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written += 1
    print(f"写入 {written} 条，跳过已存在 {skipped} 条 → {args.records_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

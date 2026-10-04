"""巡检历史：每次巡检存一份**压缩快照**，页面能列历史、跟上一次对比「新增/消失了哪些」。

以前只有 `inspection-latest.json`，每次覆盖，没有历史、没有对比。

存的是压缩版：趋势发现只留（kind, 主机, 监控项, key）这几个标识，不带曲线点（一份完整 payload 上 MB，
每小时一份撑不住）；状态快照本来就小，整份存。文件名 `<kind>-<UTC 时间>.json`，kind 是 `trend` / `status`。
**不自动清理**——压缩后一份几十 KB，一个月几十 MB，要清理由人决定。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KINDS = ("trend", "status")


def _stamp(at: datetime | None = None) -> str:
    return (at or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")


def compact_trend(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **{k: payload.get(k) for k in ("scanned_at", "scanned_hosts", "scanned_items", "items_with_data")},
        "findings": [
            {k: f.get(k, "") for k in ("kind", "host_name", "item_name", "item_key")}
            for f in payload.get("findings") or []
        ],
    }


def save(kind: str, payload: dict[str, Any], directory: Path, *, at: datetime | None = None) -> Path:
    if kind not in KINDS:
        raise ValueError(f"未知的巡检历史类型 {kind!r}")
    body = compact_trend(payload) if kind == "trend" else payload
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{kind}-{_stamp(at)}.json"
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _files(kind: str, directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(directory.glob(f"{kind}-*.json"), reverse=True)  # 时间戳定宽，字典序 = 时间序


def load_recent(kind: str, directory: Path, limit: int = 2) -> list[dict[str, Any]]:
    """最近 `limit` 份，新的在前。坏文件跳过，不抛。"""
    out: list[dict[str, Any]] = []
    for path in _files(kind, directory):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if len(out) >= limit:
            break
    return out


def list_runs(directory: Path, limit: int = 20) -> list[dict[str, Any]]:
    """页面「历史」列表：两类各取最近 `limit` 份，按时间新的在前，只带一行摘要。"""
    runs: list[dict[str, Any]] = []
    for kind in KINDS:
        for data in load_recent(kind, directory, limit):
            if kind == "trend":
                runs.append({"kind": kind, "at": data.get("scanned_at") or "", "count": len(data.get("findings") or []),
                             "note": f"{len(data.get('findings') or [])} 条趋势发现"})
            else:
                s = data.get("summary") or {}
                runs.append({"kind": kind, "at": data.get("checked_at") or "", "count": s.get("bad", 0),
                             "note": f"{s.get('bad', 0)} 项异常 / {s.get('warn', 0)} 项关注 / {s.get('unreachable', 0)} 台连不上"})
    runs.sort(key=lambda r: r["at"], reverse=True)
    return runs[:limit]


def _trend_key(f: dict[str, Any]) -> tuple[str, str, str]:
    return (f.get("kind", ""), f.get("host_name", ""), f.get("item_key", ""))


def diff_trend(prev: dict[str, Any] | None, cur: dict[str, Any]) -> dict[str, Any] | None:
    """趋势发现的新增/消失。没有上一份返回 None（页面不画对比，而不是画「全是新增」）。"""
    if prev is None:
        return None
    before = {_trend_key(f): f for f in prev.get("findings") or []}
    after = {_trend_key(f): f for f in cur.get("findings") or []}

    def _row(f: dict[str, Any]) -> dict[str, str]:
        return {"kind": f.get("kind", ""), "host": f.get("host_name", ""), "item": f.get("item_name", ""), "key": f.get("item_key", "")}

    new = [_row(f) for k, f in after.items() if k not in before]
    gone = [_row(f) for k, f in before.items() if k not in after]
    return {"prev_at": prev.get("scanned_at") or "", "new_count": len(new), "gone_count": len(gone), "new": new[:20], "gone": gone[:20]}


_RANK = {"ok": 0, "skip": 0, "warn": 1, "bad": 2}


def diff_status(prev: dict[str, Any] | None, cur: dict[str, Any]) -> dict[str, Any] | None:
    """状态巡检：哪些（设备, 检查项）变差了、哪些恢复了。只比状态等级，不比说明文字。"""
    if prev is None:
        return None

    def _index(snap: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        return {(d["name"], c["check"]): c for d in snap.get("devices") or [] for c in d.get("checks") or []}

    before, after = _index(prev), _index(cur)
    worse, better = [], []
    for key, c in after.items():
        old = before.get(key)
        if old is None:
            continue
        delta = _RANK.get(c["status"], 0) - _RANK.get(old["status"], 0)
        row = {"device": key[0], "check": key[1], "from": old["status"], "to": c["status"], "summary": c["summary"]}
        if delta > 0:
            worse.append(row)
        elif delta < 0:
            better.append(row)
    return {"prev_at": prev.get("checked_at") or "", "worse": worse, "better": better}

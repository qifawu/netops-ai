"""巡检分析两层的分流：通用类结果沉淀成固定脚本（可复用、可定时跑），特殊类结果走 AI 实时分析（不落脚本）。

规则（阈值可调，默认 `MIN_REPEAT = 3`）：
1. 现成的模板 / 通用规则（含表对表）已经能表达 → 直接用，不写脚本也不调 AI（最便宜、最可审计）；
2. 同一类结果（同一条命令 + 同一类判断）在窗口期内重复出现 ≥ N 次，或者需求本身能写成确定性规则 → 沉淀成脚本，
   进待审，批准后定时复用；
3. 其余（一次性、每次都不一样、需要推理上下文）→ AI 智能分析，实时推理，结论只进本次报告，不生成可复用脚本。

出现次数存在脚本库目录的 `occurrences.json` 里（见 `script_registry.lib_dir`），按「类别指纹」计数。纯函数 + 小文件，
不 import 框架。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from netops_ai.inspection import script_registry as SR

MIN_REPEAT = 3
WINDOW_DAYS = 30
_MAX_STAMPS = 200


def fingerprint(command: str, category: str = "") -> str:
    """类别指纹：命令（小写、空白折叠）+ 判断类别（如 route-diff / arp-change）。同一类结果落同一个指纹。"""
    norm = " ".join(str(command).lower().split()) + "|" + " ".join(str(category).lower().split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _file(root: Path | None) -> Path:
    return (root or SR.lib_dir()) / "occurrences.json"


def _load(root: Path | None) -> dict[str, list[str]]:
    try:
        data = json.loads(_file(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record(key: str, *, now: datetime | None = None, root: Path | None = None) -> int:
    """这一类结果又出现了一次，返回窗口期内累计次数。"""
    now = now or datetime.now(timezone.utc)
    data = _load(root)
    stamps = [s for s in data.get(key, []) if _fresh(s, now)] + [now.isoformat(timespec="seconds")]
    data[key] = stamps[-_MAX_STAMPS:]
    path = _file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return len(data[key])


def _fresh(stamp: str, now: datetime, window_days: int = WINDOW_DAYS) -> bool:
    try:
        return datetime.fromisoformat(stamp) >= now - timedelta(days=window_days)
    except ValueError:
        return False


def count(key: str, *, now: datetime | None = None, window_days: int = WINDOW_DAYS, root: Path | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    return sum(1 for s in _load(root).get(key, []) if _fresh(s, now, window_days))


def decide(key: str, *, template_covers: bool = False, rule_expressible: bool = False, min_repeat: int = MIN_REPEAT,
           now: datetime | None = None, root: Path | None = None) -> dict[str, Any]:
    """返回 {layer: "script"|"ai", via: "template"|"script"|"ai", repeats, reason}。
    `template_covers`：现成模板 / 通用规则能直接表达；`rule_expressible`：需求能写成确定性规则（如逐条对表、阈值）。"""
    repeats = count(key, now=now, root=root)
    approved = SR.get_approved(key, root=root) if SR._ID_RE.match(key) else None
    if approved:
        return {"layer": "script", "via": "script", "repeats": repeats, "script_version": approved["version"],
                "reason": "an approved script already exists for this kind of result; reuse it"}
    if template_covers:
        return {"layer": "script", "via": "template", "repeats": repeats,
                "reason": "an existing template or generic rule expresses it; no new script and no AI call needed"}
    if rule_expressible or repeats >= min_repeat:
        why = "the judgement can be written as a deterministic rule" if rule_expressible else f"seen {repeats} times (threshold {min_repeat})"
        return {"layer": "script", "via": "script", "repeats": repeats,
                "reason": f"{why}; distil it into a script (goes to review before it can be scheduled)"}
    return {"layer": "ai", "via": "ai", "repeats": repeats,
            "reason": f"seen {repeats} of {min_repeat} times and not rule-shaped; analyse with the AI in real time, no script kept"}

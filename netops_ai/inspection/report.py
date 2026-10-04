"""把巡检结果渲染成 markdown 报告。纯字符串拼接，不依赖模板引擎。

**为什么单独一个文件**：`scan.py` 只管发现问题，渲染是另一件事；
而且这份 markdown 有两个出口——对话里 agent 原样贴出来，前端巡检页也用它。
"""

from __future__ import annotations

from netops_ai.labels import humanize_text, label_for

#: **trend 不能一律叫「走坏」。** 检测器给的是方向（rising/falling）加一个
#: `health_impact`（improving/worsening/unknown）。真实数据里 ICMP ping 从 0.06
#: 涨到 1.0 是设备恢复了，标成「持续走坏」是把好消息报成坏消息。
TREND_CN = {code: label_for(code) for code in ("worsening", "improving", "unknown")}

KIND_CN = {
    code: label_for(code) for code in ("trend", "periodic_spike", "self_healing_flap")
}

#: 说明按**标签**给，不按 kind 给。trend 一个 kind 对三种标签，
#: 好转的那批不能跟走坏的共用「再走下去就会越过告警线」这句。
LABEL_WHY = {
    "持续走坏": "一直往坏的方向走，还没越过告警线，但再走下去就会。",
    "持续好转": "一直往好的方向走，通常是刚恢复或者刚整改过，确认一下是不是预期内的。",
    "持续单向变化": "一直往一个方向走，但现有数据分不出这个方向是好是坏，需要人看一眼。",
}

KIND_WHY = {
    "trend": "一直在往一个方向走，还没越过告警线，但再走下去就会。",
    "periodic_spike": "每天同一时段冲高，没触发告警，多半是定时任务或业务潮汐。",
    "self_healing_flap": "起落起落每次都自己恢复，所以从来没报过持续性告警。",
}


def _why(label: str, kind: str) -> str:
    return LABEL_WHY.get(label) or KIND_WHY.get(kind, "")


def _kind_label(row: dict) -> str:
    f = row.get("finding") or {}
    if row.get("kind") == "trend":
        return TREND_CN.get(str(f.get("health_impact", "")), TREND_CN["unknown"])
    return KIND_CN.get(row.get("kind", ""), label_for(row.get("kind", "")))


def _detail(row: dict) -> str:
    f = row.get("finding") or {}
    return {
        "trend": lambda: f"{f.get('direction','')} · 前段均值 {f.get('first_segment_mean')} → 后段 {f.get('last_segment_mean')}"
                         f" · 变化 {f.get('relative_change')} · 单调占比 {f.get('monotonic_fraction')}",
        "periodic_spike": lambda: f"每天 {f.get('hour_of_day_utc')} 点（UTC）· 命中 {f.get('distinct_days')} 天"
                                  f" · 该时段均值 {f.get('hour_mean')} vs 全段 {f.get('overall_mean')} · 倍数 {f.get('ratio')}",
        "self_healing_flap": lambda: f"抖动 {f.get('flap_count')} 次 · 平均恢复 {f.get('avg_recovery_seconds')} 秒"
                                     f" · 最长 {f.get('max_recovery_seconds')} 秒",
    }.get(row.get("kind", ""), lambda: "")()


def _one(row: dict) -> str:
    f = row.get("finding") or {}
    head = f"- **{row.get('host_name','')} · {row.get('item_name','')}**（`{row.get('item_key','')}`）"
    detail = _detail(row)
    lines = [head]
    if detail:
        lines.append("  " + detail)
    if f.get("reason"):
        lines.append(f"  判定依据：{humanize_text(f['reason'])}")
    return "\n".join(lines)


def group_findings(payload: dict) -> list[dict]:
    """按「人能看懂的标签」分组。**markdown 和前端共用这一份**，
    标签逻辑只写一次，不在 TS 里再写一遍。
    """
    by_kind: dict[str, list[dict]] = {}
    for row in payload.get("findings") or []:
        by_kind.setdefault(_kind_label(row), []).append(row)
    return [
        {
            "label": label,
            "why": _why(label, (rows[0] or {}).get("kind", "")),
            "rows": [
                {
                    "host": r.get("host_name", ""),
                    "item": r.get("item_name", ""),
                    "key": r.get("item_key", ""),
                    "detail": _detail(r),
                    "reason": humanize_text((r.get("finding") or {}).get("reason", "")),
                    # 新增字段：老记录里没有就是 None，前端按"没有就不画"处理
                    "host_raw": r.get("host_name", ""),
                    "key_raw": r.get("item_key", ""),
                    "rule": r.get("rule"),
                    "series": r.get("series"),
                }
                for r in rows
            ],
        }
        for label, rows in by_kind.items()
    ]


def render_inspection_markdown(payload: dict) -> str:
    findings = payload.get("findings") or []
    by_kind: dict[str, list[dict]] = {}
    for row in findings:
        by_kind.setdefault(_kind_label(row), []).append(row)

    out = ["## 巡检报告", ""]
    out.append(
        f"扫了 {payload.get('scanned_hosts', 0)} 台主机、{payload.get('scanned_items', 0)} 个数值监控项，"
        f"其中 {payload.get('items_with_data', 0)} 个有数据。"
    )
    out.append("")

    if not findings:
        out.append("**没有发现值得关注的趋势。** 这不等于网络没问题，只说明这三类判定都没命中：")
        out += [f"- {KIND_CN[k]}：{KIND_WHY[k]}" for k in KIND_WHY]
    else:
        out.append(f"**发现 {len(findings)} 条**，按类型分：")
        out += [f"- {k} {len(v)} 条" for k, v in by_kind.items()]
        for label, rows in by_kind.items():
            why = _why(label, (rows[0] or {}).get("kind", ""))
            out += ["", f"### {label}（{len(rows)} 条）", "", why, ""]
            out += [_one(r) for r in rows]

    errors = payload.get("errors") or []
    if errors:
        out += ["", f"### 取数出错 {len(errors)} 条", ""]
        out += [f"- {e}" for e in errors[:20]]
        if len(errors) > 20:
            out.append(f"- …另外还有 {len(errors) - 20} 条")

    out += ["", "巡检只读，不动设备，也不改配置。"]
    return "\n".join(out)

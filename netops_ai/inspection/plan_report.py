"""巡检计划报告导出（markdown / 自包含 html）。

跟 `export.py`（内置巡检报告）同一套做法：先拼成中性的「块」，再用 `export.blocks_to_markdown` /
`export.blocks_to_html` 渲染，两种格式不会各写一份。内容：计划定义、选定区间内的运行汇总、最近一次运行
每台设备每项检查的结果和设备输出原文、指标趋势表、失败项清单、（做过的话）趋势分析结论。

设备输出原文照抄（代码块），不改写、不翻译。报告里只有计划和运行结果，没有任何凭据。
跟内置巡检导出一致：导出的是真实内容，网页演示模式的遮罩只作用在页面上。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from netops_ai.inspection.checklist import METRIC_EXPECT_TYPES, _describe_metric_rule
from netops_ai.inspection.export import Block, blocks_to_html, blocks_to_markdown

STATUS_CN = {"pass": "通过", "fail": "未通过", "error": "出错", "denied": "被拒绝"}
#: 指标规则这一次的状态（不判的几种要写清楚原因）
RULE_STATE_CN = {"pass": "通过", "fail": "未通过", "baseline": "首次运行，已记录基线（不判）",
                 "no_baseline": "之前没有采到这个指标，本次不判", "no_value": "本次没有采到这个指标，不判", "reset": "比上次小，按计数复位处理"}
EVIDENCE_CHARS = 2000


def _t(v: Any) -> str:
    """ISO 时间去掉微秒（报告里给人看，精确到秒够了）。"""
    text = str(v or "")
    if "." in text and "T" in text:
        head, _, tail = text.partition(".")
        zone = next((tail[i:] for i in range(len(tail)) if tail[i] in "+-Z"), "")
        return head + zone
    return text


def _sched(s: dict[str, Any]) -> str:
    if isinstance(s.get("every_minutes"), int):
        n = s["every_minutes"]
        return "每小时" if n == 60 else (f"每 {n // 60} 小时" if n % 60 == 0 else f"每 {n} 分钟")
    return f"每天 {s['daily_at']}" if s.get("daily_at") else "未定"


def _fmt_rule(e: dict[str, Any]) -> str:
    if e.get("type") in METRIC_EXPECT_TYPES:
        # 运行结果里的规则已经带着描述（value）；计划定义里的规则没有，现拼一句
        desc = e.get("value") if "state" in e else _describe_metric_rule(e)
        return f"{desc}（{e.get('severity', 'warning')}）"
    return f"{e.get('type')} {e.get('value')}（{e.get('severity', 'warning')}）"


def _rule_outcome(e: dict[str, Any]) -> str:
    """一条指标规则这次的结果，写出比较的双方和来源运行。"""
    state = RULE_STATE_CN.get(e.get("state"), e.get("state") or "")
    if e.get("reference") is not None:
        src = f"，对照运行 {e['reference_run_id']}" if e.get("reference_run_id") else "（固定值）"
        return f"{e.get('value')}：{state}，本次 {e.get('current')} / 对照 {e.get('reference')}{src}"
    return f"{e.get('value')}：{state}" + (f"，本次 {e.get('current')}" if e.get("current") is not None else "")


def build_blocks(plan: dict[str, Any], runs: list[dict[str, Any]], trend: dict[str, Any] | None = None,
                 *, generated_at: str | None = None) -> list[Block]:
    """`runs` 按时间从旧到新。"""
    name = plan.get("name", "")
    blocks: list[Block] = [("h1", f"巡检计划报告：{name}")]
    meta = [f"生成时间：{generated_at or datetime.now().astimezone().isoformat(timespec='seconds')}",
            f"状态：{'启用' if plan.get('enabled') else '已暂停'}；周期：{_sched(plan.get('schedule') or {})}"]
    if plan.get("description"):
        meta.insert(0, f"说明：{plan['description']}")
    if runs:
        meta.append(f"运行区间：{_t(runs[0].get('started_at'))} — {_t(runs[-1].get('started_at'))}，共 {len(runs)} 次")
    else:
        meta.append("运行区间：还没有运行记录")
    blocks.append(("ul", meta))

    # ---- 1. 计划定义
    blocks.append(("h2", "1. 计划定义"))
    blocks.append(("table", ["设备", "地址", "角色"],
                   [[d.get("name", ""), d.get("host", ""), d.get("role", "")] for d in plan.get("devices") or []]))
    rows = []
    for c in plan.get("checks") or []:
        rules = "；".join(_fmt_rule(e) for e in c.get("expect") or []) or "—"
        metrics = "、".join(x.get("name", "") for x in c.get("extract") or []) or "—"
        rows.append([c.get("id", ""), c.get("command", ""), ", ".join(c["devices"]) if c.get("devices") else "全部", rules, metrics])
    blocks.append(("table", ["检查项", "命令（只读）", "设备", "判定规则", "指标"], rows))

    if not runs:
        blocks.append(("h2", "2. 运行汇总"))
        blocks.append(("p", "这个计划还没有运行记录。"))
        return blocks

    # ---- 2. 运行汇总
    blocks.append(("h2", "2. 运行汇总"))
    srows = []
    tot = {"pass": 0, "fail": 0, "error": 0, "denied": 0, "unreachable_devices": 0}
    for r in runs:
        s = r.get("summary") or {}
        for k in tot:
            tot[k] += int(s.get(k, 0) or 0)
        srows.append([_t(r.get("started_at")), r.get("run_id", ""), s.get("pass", 0), s.get("fail", 0), s.get("error", 0),
                      s.get("denied", 0), s.get("unreachable_devices", 0)])
    blocks.append(("ul", [f"合计：通过 {tot['pass']}、未通过 {tot['fail']}、出错 {tot['error']}、被拒绝 {tot['denied']}、"
                          f"设备不可达 {tot['unreachable_devices']} 台次"]))
    blocks.append(("table", ["开始时间", "运行", "通过", "未通过", "出错", "被拒绝", "不可达"], list(reversed(srows))))

    # ---- 3. 最近一次结果 + 证据原文
    last = runs[-1]
    blocks.append(("h2", f"3. 最近一次结果（{_t(last.get('started_at'))}，运行 {last.get('run_id')}）"))
    rrows, evidence = [], []
    for d in last.get("devices") or []:
        if d.get("error"):
            rrows.append([d.get("name"), "（整台）", "不可达", d["error"]])
            continue
        for c in d.get("checks") or []:
            metrics = "，".join(f"{k}={v}" for k, v in (c.get("metrics") or {}).items()) or (c.get("detail") or "—")
            rules = [_rule_outcome(e) for e in c.get("evaluations") or [] if e.get("type") in METRIC_EXPECT_TYPES]
            if rules:
                metrics += "；" + "；".join(rules)
            rrows.append([d.get("name"), c.get("id"), STATUS_CN.get(c.get("status"), c.get("status")), metrics])
            evidence.append((d.get("name"), c))
    blocks.append(("table", ["设备", "检查项", "结果", "指标 / 说明"], rrows))
    blocks.append(("h3", "设备输出原文（证据）"))
    for dev, c in evidence:
        out = (c.get("output") or "").strip("\r\n")  # 只去掉首尾的空行（设备回显前常有几行空白），正文一字不改
        head = f"{dev} · {c.get('id')} · `{c.get('command')}` · {STATUS_CN.get(c.get('status'), c.get('status'))}"
        blocks.append(("p", head))
        if out:
            blocks.append(("code", out[:EVIDENCE_CHARS] + ("\n……（已截断）" if len(out) > EVIDENCE_CHARS or c.get("output_truncated") else "")))
        else:
            blocks.append(("p", f"（没有输出：{c.get('detail') or '空'}）"))

    # ---- 4. 指标趋势
    blocks.append(("h2", "4. 指标趋势"))
    series: dict[tuple[str, str, str], list[Any]] = {}
    for i, r in enumerate(runs):
        for d in r.get("devices") or []:
            for c in d.get("checks") or []:
                for m, v in (c.get("metrics") or {}).items():
                    series.setdefault((d.get("name"), c.get("id"), m), [None] * len(runs))[i] = v
    if not series:
        blocks.append(("p", "这个计划没有提取数值指标。"))
    else:
        mrows = []
        for (dev, cid, m), vals in series.items():
            nums = [v for v in vals if isinstance(v, (int, float))]
            change = (nums[-1] - nums[0]) if len(nums) >= 2 else "—"
            mrows.append([dev, cid, m, " → ".join("-" if v is None else str(v) for v in vals), nums[-1] if nums else "-", change])
        blocks.append(("table", ["设备", "检查项", "指标", f"数值（最近 {len(runs)} 次，旧→新）", "最新", "变化"], mrows))

    # 指标规则：每一次的结果（首次运行建基线、没基线、复位这些「不判」的情况也写清楚）
    rule_rows: dict[tuple[str, str, str], list[str]] = {}
    for i, r in enumerate(runs):
        for d in r.get("devices") or []:
            for c in d.get("checks") or []:
                for e in c.get("evaluations") or []:
                    if e.get("type") not in METRIC_EXPECT_TYPES:
                        continue
                    key = (d.get("name"), c.get("id"), e.get("value") or e.get("type"))
                    cell = RULE_STATE_CN.get(e.get("state"), e.get("state") or "")
                    if e.get("reference") is not None:
                        cell += f"（{e.get('reference')}→{e.get('current')}）"
                    rule_rows.setdefault(key, ["-"] * len(runs))[i] = cell
    if rule_rows:
        blocks.append(("h3", "指标规则每次的结果"))
        blocks.append(("table", ["设备", "检查项", "规则", f"结果（最近 {len(runs)} 次，旧→新）"],
                       [[dev, cid, rule, " → ".join(cells)] for (dev, cid, rule), cells in rule_rows.items()]))

    # ---- 5. 失败项清单
    blocks.append(("h2", "5. 失败项清单"))
    frows = []
    for r in reversed(runs):
        for d in r.get("devices") or []:
            if d.get("error"):
                frows.append([_t(r.get("started_at")), d.get("name"), "（整台）", "不可达", d["error"]])
            for c in d.get("checks") or []:
                if c.get("status") == "pass":
                    continue
                why = "；".join(_rule_outcome(e) if e.get("type") in METRIC_EXPECT_TYPES else _fmt_rule(e)
                               for e in c.get("evaluations") or [] if e.get("passed") is False) or (c.get("detail") or "")
                frows.append([_t(r.get("started_at")), d.get("name"), c.get("id"), STATUS_CN.get(c.get("status"), c.get("status")), why])
    if frows:
        blocks.append(("table", ["时间", "设备", "检查项", "结果", "没通过的规则 / 原因"], frows))
    else:
        blocks.append(("p", f"选定的 {len(runs)} 次运行里没有失败项。"))

    # ---- 6. 趋势分析
    blocks.append(("h2", "6. 趋势分析结论"))
    if trend and trend.get("analysis"):
        blocks.append(("ul", [f"分析时间：{trend.get('at', '')}；基于 {trend.get('runs', '?')} 次运行"]))
        blocks.append(("text", trend["analysis"]))
    else:
        blocks.append(("p", "还没有做过趋势分析（在网页上点「趋势分析」，或 `python tools/checklist_run.py trend <计划名> --ask-llm`）。"))
    return blocks


def _classify(row: list[Any]) -> tuple[int, str] | None:
    for i, cell in enumerate(row):
        if cell in ("未通过", "不可达"):
            return i, "bad"
        if cell in ("出错", "被拒绝"):
            return i, "warn"
        if cell == "通过" and i == 2:
            return i, "ok"
    return None


def render_markdown(plan: dict[str, Any], runs: list[dict[str, Any]], trend: dict[str, Any] | None = None, **kw: Any) -> str:
    return blocks_to_markdown(build_blocks(plan, runs, trend, **kw))


def render_html(plan: dict[str, Any], runs: list[dict[str, Any]], trend: dict[str, Any] | None = None, **kw: Any) -> str:
    return blocks_to_html(build_blocks(plan, runs, trend, **kw), f"巡检计划报告：{plan.get('name', '')}", classify=_classify)

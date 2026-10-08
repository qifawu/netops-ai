"""巡检报告导出（markdown / 自包含 html）。

运维报告最后总要发给别人，所以页面上有「导出」。内容就是页面上那份 `build_inspection_view` 的数据，
**不另算一遍**——两边口径一致。先拼成一串中性的「块」，再分别渲染成 md 和 html，
这样两种格式不会各写一份、慢慢长得不一样。

证据（设备输出原话、AI 建议里的引文）用代码块原样放，不改写。纯字符串拼接，不依赖模板引擎。
"""
from __future__ import annotations

from html import escape
from typing import Any

Block = tuple  # ("h1"|"h2"|"h3"|"p", text) / ("table", headers, rows) / ("code", text) / ("ul", [text])

_STATUS_CN = {"ok": "正常", "warn": "关注", "bad": "异常", "skip": "不适用"}
_CHECK_CN = {"interfaces": "接口状态", "ospf": "OSPF 邻居", "bgp": "BGP 邻居", "errors": "错误计数", "reachable": "可登录"}
_KIND_CN = {"trend": "趋势", "periodic_spike": "周期冲高", "self_healing_flap": "自愈抖动"}


def build_blocks(view: dict[str, Any]) -> list[Block]:
    blocks: list[Block] = [("h1", "网络巡检报告")]
    meta = []
    if view.get("scanned_at"):
        meta.append(f"趋势巡检时间：{view['scanned_at']}")
    status = view.get("status") or {}
    if status.get("checked_at"):
        meta.append(f"状态巡检时间：{status['checked_at']}")
    blocks.append(("ul", meta or ["（还没有巡检记录）"]))

    blocks.append(("h2", "1. 概览"))
    s = status.get("summary") or {}
    overview = [
        f"趋势巡检：扫描 {view.get('scanned_hosts', 0)} 台主机、{view.get('scanned_items', 0)} 个监控项，发现 {view.get('finding_count', 0)} 条值得看的趋势。",
    ]
    if s:
        overview.append(f"状态巡检：{s.get('devices', 0)} 台设备，{s.get('bad', 0)} 项异常、{s.get('warn', 0)} 项关注、{s.get('unreachable', 0)} 台连不上。")
    blocks.append(("ul", overview))

    diff = view.get("diff") or {}
    if diff.get("status") or diff.get("trend"):
        blocks.append(("h2", "2. 与上一次对比"))
        items: list[str] = []
        ds, dt = diff.get("status"), diff.get("trend")
        if ds:
            items.append(f"状态巡检（对比 {ds['prev_at']}）：变差 {len(ds['worse'])} 项，恢复 {len(ds['better'])} 项。")
            items += [f"变差：{r['device']} {_CHECK_CN.get(r['check'], r['check'])} {_STATUS_CN.get(r['from'], r['from'])}→{_STATUS_CN.get(r['to'], r['to'])}：{r['summary']}" for r in ds["worse"]]
            items += [f"恢复：{r['device']} {_CHECK_CN.get(r['check'], r['check'])} {_STATUS_CN.get(r['from'], r['from'])}→{_STATUS_CN.get(r['to'], r['to'])}" for r in ds["better"]]
        if dt:
            items.append(f"趋势巡检（对比 {dt['prev_at']}）：新增 {dt['new_count']} 条，消失 {dt['gone_count']} 条。")
            items += [f"新增：{r['host']} · {r['item']}（{_KIND_CN.get(r['kind'], r['kind'])}）" for r in dt["new"][:10]]
        blocks.append(("ul", items))

    blocks.append(("h2", "3. 状态巡检（登设备只读检查）"))
    devices = status.get("devices") or []
    if not devices:
        blocks.append(("p", "还没有状态巡检记录。"))
    else:
        rows = []
        for dev in devices:
            for c in dev.get("checks") or []:
                rows.append([dev["name"], dev.get("role") or "-", _CHECK_CN.get(c["check"], c["check"]), _STATUS_CN.get(c["status"], c["status"]), c["summary"]])
        blocks.append(("table", ["设备", "角色", "检查项", "结果", "说明"], rows))
        for dev in devices:
            for c in dev.get("checks") or []:
                if c["status"] in ("bad", "warn") and c.get("evidence"):
                    blocks.append(("h3", f"{dev['name']} · {_CHECK_CN.get(c['check'], c['check'])}（设备输出原话，命令 `{c.get('command', '')}`）"))
                    blocks.append(("code", "\n".join(c["evidence"])))

    blocks.append(("h2", "4. AI 处置建议"))
    advice = view.get("advice")
    if not advice:
        blocks.append(("p", "还没有生成处置建议。"))
    else:
        blocks.append(("p", advice.get("summary", "")))
        for a in advice.get("needs_attention") or []:
            blocks.append(("h3", f"{a.get('host', '')} · {a.get('item_key', '')}（{a.get('severity', '')}）"))
            blocks.append(("p", a.get("why", "")))
            blocks.append(("p", "建议：" + a.get("suggestion", "")))
            blocks.append(("code", a.get("evidence", "")))
        blocks.append(("p", f"AI 认为可以不管 {len(advice.get('ignorable') or [])} 条；需要人工看 {len(advice.get('cannot_tell') or [])} 条。"))

    blocks.append(("h2", "5. 趋势发现（按类型）"))
    groups = view.get("groups") or []
    if not groups:
        blocks.append(("p", "没有趋势发现。"))
    for g in groups:
        blocks.append(("h3", f"{g['label']} · {len(g['rows'])} 条"))
        blocks.append(("p", g.get("why", "")))
        blocks.append(("table", ["主机", "监控项", "key", "变化"], [[r["host"], r["item"], r["key"], r["detail"]] for r in g["rows"][:30]]))
        if len(g["rows"]) > 30:
            blocks.append(("p", f"……另有 {len(g['rows']) - 30} 条未列出。"))
    return blocks


def _md_cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def blocks_to_markdown(blocks: list[Block]) -> str:
    """中性的块 → Markdown。巡检报告和巡检计划报告共用。"""
    out: list[str] = []
    for block in blocks:
        kind = block[0]
        if kind in ("h1", "h2", "h3"):
            out += [f"{'#' * int(kind[1])} {block[1]}", ""]
        elif kind == "p":
            out += [block[1], ""] if block[1] else []
        elif kind == "text":  # 模型写的 Markdown 段落（趋势分析结论），原样放
            out += [block[1].strip(), ""] if block[1] else []
        elif kind == "ul":
            out += [*(f"- {item}" for item in block[1]), ""]
        elif kind == "code":
            out += ["```", block[1], "```", ""]
        elif kind == "table":
            headers, rows = block[1], block[2]
            out += ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
            out += ["| " + " | ".join(_md_cell(c) for c in row) + " |" for row in rows]
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_markdown(view: dict[str, Any]) -> str:
    return blocks_to_markdown(build_blocks(view))


_CSS = (
    "body{font:14px/1.7 -apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;max-width:980px;margin:32px auto;padding:0 20px;color:#1e293b}"
    "h1{font-size:24px}h2{font-size:18px;margin-top:32px;border-bottom:1px solid #e2e8f0;padding-bottom:6px}h3{font-size:15px;margin-top:20px}"
    "table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid #e2e8f0;padding:6px 10px;text-align:left;vertical-align:top}"
    "th{background:#f8fafc}pre{background:#0f172a;color:#e2e8f0;padding:10px 14px;border-radius:8px;overflow-x:auto;font-size:12px}"
    ".bad{color:#be123c;font-weight:600}.warn{color:#b45309;font-weight:600}.ok{color:#047857}"
    ".text{white-space:pre-wrap;border-left:3px solid #e2e8f0;padding-left:12px}"
)


def _status_cell(row: list[Any]) -> tuple[int, str] | None:
    """巡检报告的状态表（5 列，第 4 列是状态）给状态那一格上色。"""
    if len(row) != 5:
        return None
    cls = {v: k for k, v in _STATUS_CN.items()}.get(str(row[3]), "")
    return (3, cls) if cls else None


def blocks_to_html(blocks: list[Block], title: str, classify: Any = _status_cell) -> str:
    """中性的块 → 自包含 HTML。`classify(row)` 返回（要上色的列, css 类）或 None。"""
    body: list[str] = []
    for block in blocks:
        kind = block[0]
        if kind in ("h1", "h2", "h3"):
            body.append(f"<{kind}>{escape(block[1])}</{kind}>")
        elif kind == "p":
            if block[1]:
                body.append(f"<p>{escape(block[1])}</p>")
        elif kind == "text":
            if block[1]:
                body.append(f'<div class="text">{escape(block[1].strip())}</div>')
        elif kind == "ul":
            body.append("<ul>" + "".join(f"<li>{escape(i)}</li>" for i in block[1]) + "</ul>")
        elif kind == "code":
            body.append(f"<pre>{escape(block[1])}</pre>")
        elif kind == "table":
            head = "".join(f"<th>{escape(h)}</th>" for h in block[1])
            rows = []
            for row in block[2]:
                hit = classify(row) if classify else None
                cells = "".join(
                    f'<td class="{hit[1]}">{escape(str(c))}</td>' if hit and i == hit[0] else f"<td>{escape(str(c))}</td>"
                    for i, c in enumerate(row)
                )
                rows.append(f"<tr>{cells}</tr>")
            body.append(f"<table><tr>{head}</tr>{''.join(rows)}</table>")
    return f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{escape(title)}</title><style>{_CSS}</style></head><body>{"".join(body)}</body></html>'


def render_html(view: dict[str, Any]) -> str:
    return blocks_to_html(build_blocks(view), "网络巡检报告")

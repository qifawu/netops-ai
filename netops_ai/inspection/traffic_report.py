"""链路流量日报 / 周报 / 月报：从 Zabbix 趋势数据汇总，出 PDF（含图表和对比上一周期的变化）。

数据：Zabbix 的 `net.if.in / net.if.out`（每个接口 "Bits received / Bits sent"，单位 bps）和接口速率（`net.if.speed`）的**小时趋势**
（`trend.get`：每小时 min / avg / max），所以日报 / 周报 / 月报用同一套口径，不依赖只保留几天的原始历史。
只读：本模块只调 Zabbix 的读接口（经 `ZabbixClient` 的白名单）。

流程：`period_range` 定出周期（日 = 昨天整天；周 = 上周一到周日；月 = 上个整月）→ `collect` 取数据 → `summarize` 汇总并和上一
周期比 → `render_pdf` 出文件。`summarize` 和 `render_pdf` 是纯函数，`collect` 的 Zabbix 客户端可注入，方便测试。
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

KINDS = ("daily", "weekly", "monthly")
HIGH_UTIL_PCT = 80.0
TOP_N = 10
_NAME_RE = re.compile(r"^Interface (?P<iface>.+?)\(.*?\): (?P<what>Bits received|Bits sent|Speed)$")


# ---------------------------------------------------------------- 周期

def period_range(kind: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """报告覆盖的 [起, 止)：日 = 昨天整天；周 = 上一个自然周（周一 00:00 起）；月 = 上一个自然月。按本地时区。"""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    now = (now or datetime.now().astimezone())
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "daily":
        return today - timedelta(days=1), today
    if kind == "weekly":
        this_monday = today - timedelta(days=today.weekday())
        return this_monday - timedelta(days=7), this_monday
    first = today.replace(day=1)
    prev_last = first - timedelta(days=1)
    return prev_last.replace(day=1), first


def previous_range(kind: str, start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """上一个同类周期，用来做环比。"""
    if kind == "monthly":
        prev_last = start - timedelta(days=1)
        return prev_last.replace(day=1), start
    return start - (end - start), start


# ---------------------------------------------------------------- 取数

def _pair_items(items: list[dict]) -> dict[str, dict[str, Any]]:
    """把一台主机的监控项按接口配对：{接口: {in: itemid, out: itemid, speed: itemid}}。"""
    out: dict[str, dict[str, Any]] = {}
    for it in items:
        m = _NAME_RE.match(str(it.get("name") or ""))
        if not m or str(it.get("status", "0")) != "0":
            continue
        slot = {"Bits received": "in", "Bits sent": "out", "Speed": "speed"}[m["what"]]
        # 速率监控项默认不存趋势，只有最新值（lastvalue）：协商速率不怎么变，取最新值就够
        out.setdefault(m["iface"].strip(), {})[slot] = it.get("lastvalue") if slot == "speed" else it["itemid"]
    return {k: v for k, v in out.items() if "in" in v or "out" in v}


def _series(zbx: Any, itemid: str, start: int, end: int) -> list[tuple[int, float, float, float]]:
    rows = zbx.get_trends(itemid, time_from=start, time_till=end, limit=5000)
    pts = [(int(r["clock"]), float(r["value_min"]), float(r["value_avg"]), float(r["value_max"])) for r in rows]
    return sorted(pts)


def collect(zbx: Any, start: datetime, end: datetime, *, hosts: list[str] | None = None,
            interfaces: dict[str, list[str]] | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    """{主机: {接口: {"in": [(clock, min, avg, max)...], "out": [...], "speed": bps|None}}}。
    `hosts`：只要这些主机（不给 = 全部）；`interfaces`：{主机: [接口名...]} 只要这些接口（不给 = 全部）。"""
    t0, t1 = int(start.timestamp()), int(end.timestamp())
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for h in zbx.list_hosts():
        name = h.get("host") or h.get("name") or h["hostid"]
        if hosts and name not in hosts:
            continue
        want = (interfaces or {}).get(name)
        for iface, ids in _pair_items(zbx.list_items(h["hostid"])).items():
            if want and iface not in want:
                continue
            try:
                speed = int(float(ids["speed"])) if ids.get("speed") and float(ids["speed"]) > 0 else None
            except (TypeError, ValueError):
                speed = None
            result.setdefault(name, {})[iface] = {"in": _series(zbx, ids["in"], t0, t1) if ids.get("in") else [],
                                                   "out": _series(zbx, ids["out"], t0, t1) if ids.get("out") else [],
                                                   "speed": speed}
    return result


# ---------------------------------------------------------------- 汇总

def fmt_bps(v: float | None) -> str:
    if v is None:
        return "-"
    for unit, div in (("Gbps", 1e9), ("Mbps", 1e6), ("Kbps", 1e3)):
        if abs(v) >= div:
            return f"{v / div:.2f} {unit}"
    return f"{v:.0f} bps"


def _stats(pts: list[tuple[int, float, float, float]]) -> dict[str, Any] | None:
    if not pts:
        return None
    avgs = [p[2] for p in pts]
    peak = max(pts, key=lambda p: p[3])
    ordered = sorted(avgs)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    return {"avg": statistics.fmean(avgs), "peak": peak[3], "peak_at": peak[0], "p95": p95, "hours": len(pts)}


def _pct(part: float | None, whole: float | None) -> float | None:
    return (round(part / whole * 100, 2) if part / whole < 0.01 else round(part / whole * 100, 1)) if part is not None and whole else None


def summarize(cur: dict, prev: dict | None = None, *, high_util_pct: float = HIGH_UTIL_PCT, top_n: int = TOP_N) -> dict[str, Any]:
    """汇总：每个接口的平均 / 峰值 / P95 / 利用率，和上一周期比（环比），挑出 TopN、高利用率、没有数据的接口。"""
    rows = []
    for host, ifs in sorted(cur.items()):
        for iface, d in sorted(ifs.items()):
            sin, sout = _stats(d["in"]), _stats(d["out"])
            if sin is None and sout is None:
                rows.append({"host": host, "iface": iface, "no_data": True})
                continue
            speed = d.get("speed")
            peak = max((s["peak"] for s in (sin, sout) if s), default=0.0)
            old = ((prev or {}).get(host) or {}).get(iface) or {}
            oin, oout = _stats(old.get("in") or []), _stats(old.get("out") or [])

            def delta(now: dict | None, then: dict | None) -> float | None:
                return round((now["avg"] - then["avg"]) / then["avg"] * 100, 1) if now and then and then["avg"] > 0 else None
            rows.append({"host": host, "iface": iface, "speed": speed, "in": sin, "out": sout, "peak_bps": peak,
                         "peak_util_pct": _pct(peak, speed), "avg_in_util_pct": _pct(sin["avg"] if sin else None, speed),
                         "avg_out_util_pct": _pct(sout["avg"] if sout else None, speed),
                         "in_change_pct": delta(sin, oin), "out_change_pct": delta(sout, oout), "has_previous": bool(oin or oout)})
    live = [r for r in rows if not r.get("no_data")]
    top = sorted(live, key=lambda r: r["peak_bps"], reverse=True)[:top_n]
    high = sorted((r for r in live if (r["peak_util_pct"] or 0) >= high_util_pct), key=lambda r: r["peak_util_pct"], reverse=True)
    movers = sorted((r for r in live if r["has_previous"] and max(abs(r["in_change_pct"] or 0), abs(r["out_change_pct"] or 0)) >= 50),
                    key=lambda r: max(abs(r["in_change_pct"] or 0), abs(r["out_change_pct"] or 0)), reverse=True)[:top_n]
    return {"interfaces": rows, "top": top, "high_util": high, "movers": movers, "no_data": [r for r in rows if r.get("no_data")],
            "high_util_pct": high_util_pct, "count": len(live)}


def facts_text(summary: dict, title: str) -> str:
    """把汇总压成给 LLM 写总结 / 给人读的事实清单（不含任何推断）。"""
    lines = [f"{title}：共 {summary['count']} 个有数据的接口，{len(summary['no_data'])} 个没有数据。"]
    for r in summary["top"][:5]:
        lines.append(f"- {r['host']} {r['iface']}：峰值 {fmt_bps(r['peak_bps'])}"
                     + (f"（利用率 {r['peak_util_pct']}%）" if r["peak_util_pct"] is not None else ""))
    for r in summary["high_util"]:
        lines.append(f"- 高利用率：{r['host']} {r['iface']} 峰值利用率 {r['peak_util_pct']}%")
    for r in summary["movers"][:5]:
        lines.append(f"- 环比变化大：{r['host']} {r['iface']} 入 {r['in_change_pct']}% / 出 {r['out_change_pct']}%")
    return "\n".join(lines)


# ---------------------------------------------------------------- 图表

def render_chart(cur: dict, summary: dict, path: Path, *, top_n: int = 5) -> Path | None:
    """TopN 接口的小时平均入 / 出流量折线图（PNG）。没有数据返回 None。matplotlib 懒加载。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    picks = summary["top"][:top_n]
    if not picks:
        return None
    peak = max((r["peak_bps"] for r in picks), default=0.0)
    unit, div = ("Mbps", 1e6) if peak >= 1e6 else ("Kbps", 1e3) if peak >= 1e3 else ("bps", 1.0)
    fig, axes = plt.subplots(len(picks), 1, figsize=(8.2, 1.9 * len(picks)), sharex=True, squeeze=False)
    for ax, r in zip(axes[:, 0], picks):
        d = cur[r["host"]][r["iface"]]
        for key, color in (("in", "#2563eb"), ("out", "#ea580c")):
            pts = d[key]
            if pts:
                ax.plot([datetime.fromtimestamp(p[0]) for p in pts], [p[2] / div for p in pts], color=color, linewidth=1.2, label=key)
        ax.set_ylabel(unit, fontsize=8)
        ax.set_title(f"{r['host']} {r['iface']}", fontsize=9, loc="left")
        ax.tick_params(labelsize=7)
        ax.grid(alpha=0.25)
        ax.legend(fontsize=7, loc="upper right")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


# ---------------------------------------------------------------- PDF

_CJK_FONT = "STSong-Light"
KIND_ZH = {"daily": "日报", "weekly": "周报", "monthly": "月报"}


def render_pdf(path: Path, *, title: str, start: datetime, end: datetime, summary: dict, chart: Path | None = None,
               narrative: str = "", generated_at: datetime | None = None) -> Path:
    """出 PDF。中文用 Adobe 内置 CJK 字体（STSong-Light），不依赖机器上装了哪些字体文件。"""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont(_CJK_FONT))
    base = ParagraphStyle("base", fontName=_CJK_FONT, fontSize=9.5, leading=14)
    h1 = ParagraphStyle("h1", parent=base, fontSize=18, leading=24, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=base, fontSize=12.5, leading=18, spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#1e3a8a"))
    small = ParagraphStyle("small", parent=base, fontSize=8, leading=11, textColor=colors.HexColor("#555555"))
    cell = ParagraphStyle("cell", parent=base, fontSize=8, leading=10)

    def esc(s: Any) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def table(head: list[str], body: list[list[Any]], widths: list[float]) -> Table:
        data = [[Paragraph(esc(h), cell) for h in head]] + [[Paragraph(esc(c), cell) for c in row] for row in body]
        t = Table(data, colWidths=[w * mm for w in widths], repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e0e7ff")), ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c7c7c7")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        return t

    def pct(v: float | None) -> str:
        return "-" if v is None else f"{v:+.1f}%"

    gen = (generated_at or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M")
    story: list[Any] = [Paragraph(esc(title), h1),
                        Paragraph(f"统计周期：{start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}　生成时间：{gen}　数据来源：Zabbix 小时趋势", small)]
    if narrative.strip():
        story += [Paragraph("摘要", h2)] + [Paragraph(esc(p), base) for p in narrative.strip().split("\n") if p.strip()]
    story += [Paragraph("概览", h2), Paragraph(
        f"有数据的接口 {summary['count']} 个；峰值利用率 ≥ {summary['high_util_pct']:.0f}% 的接口 {len(summary['high_util'])} 个；"
        f"没有数据的接口 {len(summary['no_data'])} 个。利用率 = 峰值 ÷ 接口协商速率，接口速率未知时不算。", base)]

    def row_of(r: dict) -> list[Any]:
        i, o = r.get("in") or {}, r.get("out") or {}
        return [f"{r['host']} {r['iface']}", fmt_bps(r.get("speed")), fmt_bps(i.get("avg")), fmt_bps(o.get("avg")), fmt_bps(r["peak_bps"]),
                "-" if r["peak_util_pct"] is None else f"{r['peak_util_pct']}%", pct(r.get("in_change_pct")), pct(r.get("out_change_pct"))]

    head = ["接口", "速率", "入向平均", "出向平均", "峰值", "峰值利用率", "入向环比", "出向环比"]
    widths = [40, 17, 20, 20, 20, 18, 17, 17]
    story += [Paragraph(f"流量最高的接口（Top {len(summary['top'])}，按峰值）", h2)]
    story += [table(head, [row_of(r) for r in summary["top"]], widths)] if summary["top"] else [Paragraph("本周期没有任何接口有数据。", base)]
    if summary["high_util"]:
        story += [Paragraph("高利用率接口", h2), table(head, [row_of(r) for r in summary["high_util"]], widths)]
    if summary["movers"]:
        story += [Paragraph("环比变化超过 50% 的接口", h2), table(head, [row_of(r) for r in summary["movers"]], widths)]
    if summary["no_data"]:
        story += [Paragraph("没有数据的接口", h2), Paragraph("、".join(esc(f"{r['host']} {r['iface']}") for r in summary["no_data"][:40]), base)]
    if chart and chart.exists():
        story += [KeepTogether([Paragraph("流量趋势（小时平均，Top 接口）", h2), Image(str(chart), width=170 * mm, height=_img_h(chart, 170))])]
    story += [Spacer(1, 6), Paragraph("环比 = 本周期平均流量相对上一个同类周期的变化；P95 与峰值取自 Zabbix 小时趋势的最大值，不是秒级峰值。", small)]
    path.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                      title=title, author="netops-ai").build(story)
    return path


def _img_h(path: Path, width_mm: float) -> float:
    from PIL import Image as PILImage

    with PILImage.open(path) as im:
        w, h = im.size
    return width_mm * h / w * 2.834645669  # mm → pt


# ---------------------------------------------------------------- 总入口

def generate(kind: str, zbx: Any, out_dir: Path, *, now: datetime | None = None, hosts: list[str] | None = None,
             interfaces: dict[str, list[str]] | None = None, narrate: Callable[[str], str] | None = None) -> dict[str, Any]:
    """出一份报告。`narrate(事实清单) -> 摘要文字`：可选，给管理层看的总结由调用方接 LLM；不给就没有摘要段。
    返回 {path, summary, facts, start, end}。"""
    start, end = period_range(kind, now)
    pstart, pend = previous_range(kind, start, end)
    cur = collect(zbx, start, end, hosts=hosts, interfaces=interfaces)
    prev = collect(zbx, pstart, pend, hosts=hosts, interfaces=interfaces)
    summary = summarize(cur, prev)
    title = f"链路流量{KIND_ZH[kind]}（{start:%Y-%m-%d}" + ("）" if kind == "daily" else f" ~ {(end - timedelta(seconds=1)):%Y-%m-%d}）")
    facts = facts_text(summary, title)
    narrative = ""
    if narrate is not None:
        try:
            narrative = narrate(facts)
        except Exception:  # noqa: BLE001 - 总结生成失败不挡住报告本身
            narrative = ""
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"traffic-{kind}-{start:%Y%m%d}"
    chart = render_chart(cur, summary, out_dir / f"{name}.png") if summary["top"] else None
    path = render_pdf(out_dir / f"{name}.pdf", title=title, start=start, end=end, summary=summary, chart=chart, narrative=narrative)
    return {"path": path, "summary": summary, "facts": facts, "start": start, "end": end, "narrative": narrative}


def main() -> None:
    """`python -m netops_ai.inspection.traffic_report daily|weekly|monthly [--out 目录] [--host 主机 ...]`"""
    import argparse

    from netops_ai.llm.factory import env
    from netops_ai.zabbix.client import ZabbixClient

    ap = argparse.ArgumentParser(description="链路流量日报 / 周报 / 月报 PDF")
    ap.add_argument("kind", choices=KINDS)
    ap.add_argument("--out", default="reports", help="输出目录（默认 ./reports）")
    ap.add_argument("--host", action="append", help="只统计这些 Zabbix 主机（可重复）")
    a = ap.parse_args()
    e = env()
    with ZabbixClient(url=e.get("ZABBIX_URL", ""), user=e.get("ZABBIX_USER", ""), password=e.get("ZABBIX_PASSWORD", "")) as zbx:
        res = generate(a.kind, zbx, Path(a.out), hosts=a.host)
    print(res["path"])


if __name__ == "__main__":
    main()

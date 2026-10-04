"""把 zbx_chart 取到的时间序列画成 PNG，对话里直接显示（不另接 Grafana，一个系统里处理完）。

用 matplotlib 的无头 `Agg` 后端：空数据、单点、全零这些边界别人都处理过，不手写渲染器。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from netops_ai.graph.agent_loop import REPO_ROOT

CHART_DIR = REPO_ROOT / "records" / "charts"


#: 图上要写中文，matplotlib 自带的 DejaVu Sans 没有汉字，
#: 不处理的话标题全是豆腐块——**中文教程里这是不能接受的**。
#:
#: 三个平台各挑几个几乎一定存在的字体。找不到就退回纯 ASCII 标题
#: （见 `_safe_title`），宁可标题朴素，不要满图方块。
_CJK_FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",                    # macOS
    "/System/Library/Fonts/Hiragino Sans GB.ttc",            # macOS
    "C:/Windows/Fonts/msyh.ttc",                             # Windows 微软雅黑
    "C:/Windows/Fonts/simhei.ttf",                           # Windows 黑体
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",          # Linux 文泉驿
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
)


def _cjk_font():
    """找一个能写汉字的字体。找不到返回 None。"""
    from matplotlib import font_manager

    for candidate in _CJK_FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                return font_manager.FontProperties(fname=candidate)
            except Exception:  # noqa: BLE001
                continue
    return None


def _safe_title(title: str, font) -> str:
    """没有中文字体时，把汉字从标题里拿掉，别画一排方块。"""
    if font is not None:
        return title
    return "".join(ch for ch in title if ord(ch) < 128).strip(" ()（）") or "chart"


def _points(payload: dict[str, Any]) -> list[tuple[datetime, float, float, float]]:
    """把 `zbx_chart` 的两种返回（history / trends）统一成一串点。

    history 每点只有一个 `value`；trends 每点有 `min/avg/max`。
    统一成 (时间, 下界, 主值, 上界)，history 的上下界就等于主值。
    """
    out: list[tuple[datetime, float, float, float]] = []
    for point in payload.get("series") or []:
        try:
            when = datetime.fromtimestamp(int(point["t"]), tz=timezone.utc).astimezone()
        except (KeyError, TypeError, ValueError, OSError):
            continue
        if "value" in point:
            value = point.get("value")
            if value is None:
                continue
            out.append((when, float(value), float(value), float(value)))
        else:
            avg = point.get("avg")
            if avg is None:
                continue
            low = point.get("min", avg)
            high = point.get("max", avg)
            out.append((when, float(low if low is not None else avg), float(avg), float(high if high is not None else avg)))
    return out


def _human(value: float) -> str:
    """Zabbix 里流量类监控项是 bps，直接标 12000000000 没人看得懂。"""
    for unit, scale in (("G", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(value) >= scale:
            return f"{value / scale:.2f}{unit}"
    return f"{value:.0f}"


def render_chart_png(payload: dict[str, Any], *, title: str = "", out_dir: Path = CHART_DIR) -> Path | None:
    """画一张图存成 PNG，返回文件路径。**画不出来就返回 None，绝不抛异常**
    ——出不了图是小事，让一次排障回答整个失败是大事。
    """
    try:
        import matplotlib

        matplotlib.use("Agg")  # 无头后端，服务器上没有图形界面
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt

        points = _points(payload)
        if len(points) < 2:
            # 一个点画不出趋势，画出来反而误导。
            return None

        out_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        path = out_dir / f"{key}.png"
        if path.exists():
            return path

        times = [p[0] for p in points]
        lows = [p[1] for p in points]
        values = [p[2] for p in points]
        highs = [p[3] for p in points]

        font = _cjk_font()
        fig, ax = plt.subplots(figsize=(9, 3.2), dpi=140)
        ax.plot(times, values, linewidth=1.6, color="#2f6fb3")
        if any(h != l for h, l in zip(highs, lows)):
            # trends 才有上下界；history 的上下界等于主值，画出来是条实线，没意义。
            ax.fill_between(times, lows, highs, alpha=0.18, color="#2f6fb3", linewidth=0)
        raw_title = title or f"itemid {payload.get('item_id', '')} ({payload.get('source', '')})"
        ax.set_title(_safe_title(raw_title, font), fontsize=10, fontproperties=font)
        ax.grid(True, alpha=0.25, linewidth=0.6)
        ax.set_ylim(bottom=0 if min(lows) >= 0 else None)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: _human(v)))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
        fig.autofmt_xdate(rotation=0, ha="center")
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        fig.tight_layout()
        fig.savefig(path)
        plt.close(fig)
        return path
    except Exception:  # noqa: BLE001 - 出不了图不该让整次回答失败
        return None


def chart_markdown(payload: dict[str, Any], base_url: str, *, title: str = "") -> str:
    """画图并返回一段 markdown。画不出来就返回空串。

    URL 必须是绝对的——客户端在另一台机器/另一个容器里，相对路径取不到。
    """
    path = render_chart_png(payload, title=title)
    if path is None:
        return ""
    points = len(payload.get("series") or [])
    source = payload.get("source", "")
    return (
        f"\n\n![{title or 'chart'}]({base_url.rstrip('/')}/charts/{path.name})\n\n"
        f"*itemid `{payload.get('item_id', '')}`，{points} 个点，数据来自 Zabbix {source}*"
    )

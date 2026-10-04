"""给每条发现补两样东西：**命中了哪条规则（阈值 vs 实际值）**和**降采样后的原始序列**。

检测器本身一个字不改；这里只是拿检测器的结果加上输入序列，把"为什么算发现"摊开讲。
所有数字都是检测器已经算出来的，或者直接从序列数出来的，不在这里重新发明判定。
"""

from __future__ import annotations

from typing import Any

from netops_ai.inspection.detectors import Series

MAX_SERIES_POINTS = 60


def downsample(series: Series, max_points: int = MAX_SERIES_POINTS) -> list[list[float]]:
    """≤ max_points 点。超出时按时间切成 max_points/2 桶，每桶保留最小和最大点（按时间序），
    这样 up/down 抖动、尖峰这种极值不会在降采样里被抹平。"""
    if len(series) <= max_points:
        return [[c, v] for c, v in series]
    buckets = max(1, max_points // 2)
    n = len(series)
    out: list[tuple[int, float]] = []
    for b in range(buckets):
        chunk = series[b * n // buckets:(b + 1) * n // buckets]
        if not chunk:
            continue
        lo = min(chunk, key=lambda p: p[1])
        hi = max(chunk, key=lambda p: p[1])
        out.extend(sorted({lo, hi}, key=lambda p: p[0]))
    return [[c, v] for c, v in out[:max_points]]


_OP_CN = {">=": "≥", "<=": "≤"}


def _check(label: str, observed: float, op: str, threshold: float, *, fmt: str = "{:.4g}", unit: str = "") -> dict[str, Any]:
    passed = observed >= threshold if op == ">=" else observed <= threshold
    return {
        "label": label,
        "observed": observed,
        "op": op,
        "threshold": threshold,
        "passed": bool(passed),
        "text": f"{label} {fmt.format(observed)}{unit} {_OP_CN[op]} {fmt.format(threshold)}{unit}",
    }


def _pct(label: str, observed: float, op: str, threshold: float, signed: bool = False) -> dict[str, Any]:
    c = _check(label, observed, op, threshold, fmt="{:.0%}")
    if signed:
        c["text"] = f"{label} {observed:+.0%} {_OP_CN[op]} {threshold:.0%}"
    return c


def explain(finding: Any, series: Series, params: dict[str, Any]) -> dict[str, Any]:
    """`params` 是跑这个检测器时实际用的那组阈值。"""
    kind = finding.kind
    n = len(series)
    span = series[-1][0] - series[0][0] if n else 0
    checks: list[dict[str, Any]] = []
    if kind == "trend":
        name = "趋势（前后段均值 + 同向比例）"
        direction = finding.direction
        # 方向向下时，"变化幅度"要比的是下降的幅度
        change = finding.relative_change if direction == "rising" else -finding.relative_change
        checks = [
            _pct("前后段均值变化", change, ">=", params["min_relative_change"], signed=False),
            _pct("相邻点同向", finding.monotonic_fraction, ">=", params["monotonic_fraction_threshold"]),
            _check("数据点数", n, ">=", params["min_points"], fmt="{:.0f}"),
            _check("时间跨度", span / 3600, ">=", params["min_time_span_seconds"] / 3600, fmt="{:.1f}", unit=" 小时"),
        ]
        thr = params["min_relative_change"]
        checks[0]["text"] = (
            f"前后段均值变化 {finding.relative_change:+.0%} ≥ {thr:.0%}" if direction == "rising"
            else f"前后段均值变化 {finding.relative_change:+.0%}（下降幅度 {change:.0%}）≥ {thr:.0%}"
        )
    elif kind == "periodic_spike":
        name = "周期冲高（按 UTC 小时分桶）"
        checks = [
            _check("该小时均值 / 整体均值", finding.ratio, ">=", params["spike_ratio_threshold"], fmt="{:.2f}", unit=" 倍"),
            _check("出现的不同日期数", finding.distinct_days, ">=", params["min_distinct_days"], fmt="{:.0f}", unit=" 天"),
            _check("数据点数", n, ">=", params["min_points"], fmt="{:.0f}"),
            _check("时间跨度", span / 3600, ">=", params["min_time_span_seconds"] / 3600, fmt="{:.1f}", unit=" 小时"),
        ]
    elif kind == "self_healing_flap":
        name = "自愈抖动（down 后很快恢复）"
        checks = [
            _check("自愈抖动次数", finding.flap_count, ">=", params["min_flaps"], fmt="{:.0f}", unit=" 次"),
            _check("最长恢复时间", finding.max_recovery_seconds, "<=", params["max_recovery_seconds"], fmt="{:.0f}", unit=" 秒"),
        ]
    else:
        name = kind
    return {
        "name": name,
        "kind": kind,
        "params": dict(params),
        "checks": checks,
        "summary": "；".join(c["text"] for c in checks[:2]) if kind != "self_healing_flap" else "；".join(c["text"] for c in checks),
        "points": n,
        "span_seconds": span,
    }

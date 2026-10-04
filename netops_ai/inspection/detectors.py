"""三类"正在变坏但没触发阈值告警"的判定，**全部是纯函数**：输入一段
`(clock, value)` 时间序列（`clock` 是 Unix epoch 秒，按时间升序排好），
输出判定结果或 `None`。不碰网络、不碰模型，方便单测、方便复现。

这三类只是"有没有异常"这一层，"这个异常说明什么、要不要紧"是分析层
（模型）的事，这里不做。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import mean

Series = list[tuple[int, float]]


@dataclass(frozen=True)
class TrendFinding:
    """单调上涨/下跌或持续偏移——不是"这次坏了"，是"一直在往一个方向走"。"""

    kind: str
    direction: str  # "rising" | "falling"
    health_impact: str  # "improving" | "worsening" | "unknown"
    first_segment_mean: float
    last_segment_mean: float
    relative_change: float
    monotonic_fraction: float
    reason: str


@dataclass(frozen=True)
class PeriodicSpikeFinding:
    """每天同一时段冲高，但没越过告警阈值。"""

    kind: str
    hour_of_day_utc: int
    distinct_days: int
    hour_mean: float
    overall_mean: float
    ratio: float
    reason: str


@dataclass(frozen=True)
class SelfHealingFlapFinding:
    """起落起落，每次都自己恢复，从来没触发过持续性告警。"""

    kind: str
    flap_count: int
    avg_recovery_seconds: float
    max_recovery_seconds: float
    reason: str


def detect_trend(
    series: Series,
    *,
    min_points: int = 6,
    min_time_span_seconds: int = 12 * 3600,
    monotonic_fraction_threshold: float = 0.7,
    min_relative_change: float = 0.2,
    higher_is_worse: bool | None = None,
) -> TrendFinding | None:
    """把序列切成前 1/3 和后 1/3 比均值，同时要求"连续同方向变化"的比例
    达到阈值——只看首尾均值差会被一次性的跳变误判成趋势，只看单调比例
    又会被围绕同一个值上下抖动的噪音误判，两个条件都满足才算真趋势。
    """
    if len(series) < min_points:
        return None
    if series[-1][0] - series[0][0] < min_time_span_seconds:
        return None

    values = [v for _, v in series]
    diffs = [values[i + 1] - values[i] for i in range(len(values) - 1)]
    if not diffs:
        return None

    non_decreasing = sum(1 for d in diffs if d >= 0) / len(diffs)
    non_increasing = sum(1 for d in diffs if d <= 0) / len(diffs)

    segment_len = max(1, len(values) // 3)
    first_mean = mean(values[:segment_len])
    last_mean = mean(values[-segment_len:])

    if first_mean == 0 and last_mean == 0:
        relative_change = 0.0
    else:
        # first_mean 为 0 时用 last_mean 当分母（结果 1.0），否则除出 inf，JSON 序列化会 500。
        denom = abs(first_mean) if first_mean != 0 else abs(last_mean)
        relative_change = (last_mean - first_mean) / denom

    def impact_for(direction: str) -> str:
        if higher_is_worse is None:
            return "unknown"
        is_rising = direction == "rising"
        return "worsening" if is_rising == higher_is_worse else "improving"

    if non_decreasing >= monotonic_fraction_threshold and relative_change >= min_relative_change:
        health_impact = impact_for("rising")
        return TrendFinding(
            kind="trend",
            direction="rising",
            health_impact=health_impact,
            first_segment_mean=first_mean,
            last_segment_mean=last_mean,
            relative_change=relative_change,
            monotonic_fraction=non_decreasing,
            reason=(
                f"前段均值 {first_mean:.4g} -> 后段均值 {last_mean:.4g}"
                f"（变化 {relative_change:+.1%}），{non_decreasing:.0%} 的相邻点非降"
            ),
        )
    if non_increasing >= monotonic_fraction_threshold and -relative_change >= min_relative_change:
        health_impact = impact_for("falling")
        return TrendFinding(
            kind="trend",
            direction="falling",
            health_impact=health_impact,
            first_segment_mean=first_mean,
            last_segment_mean=last_mean,
            relative_change=relative_change,
            monotonic_fraction=non_increasing,
            reason=(
                f"前段均值 {first_mean:.4g} -> 后段均值 {last_mean:.4g}"
                f"（变化 {relative_change:+.1%}），{non_increasing:.0%} 的相邻点非升"
            ),
        )
    return None


def detect_periodic_spike(
    series: Series,
    *,
    min_points: int = 6,
    min_time_span_seconds: int = 2 * 86400,
    min_distinct_days: int = 2,
    spike_ratio_threshold: float = 1.5,
) -> PeriodicSpikeFinding | None:
    """按 UTC 小时分桶，找哪个小时的均值明显高于整体均值，且这个模式
    在至少 `min_distinct_days` 个不同日期都出现过（只出现一次不算"周期性"，
    只是一次偶发尖峰）。
    """
    if len(series) < min_points:
        return None
    if series[-1][0] - series[0][0] < min_time_span_seconds:
        return None

    overall_mean = mean(v for _, v in series)
    if overall_mean == 0:
        return None  # 基准是 0，比值没有意义，宁可不报也不报一个无意义的 inf

    hour_values: dict[int, list[float]] = defaultdict(list)
    hour_days: dict[int, set] = defaultdict(set)
    for clock, value in series:
        dt = datetime.fromtimestamp(clock, tz=timezone.utc)
        hour_values[dt.hour].append(value)
        hour_days[dt.hour].add(dt.date())

    best: tuple[int, float, float, int] | None = None
    for hour, values in hour_values.items():
        distinct_days = len(hour_days[hour])
        if distinct_days < min_distinct_days:
            continue
        hour_mean = mean(values)
        ratio = hour_mean / overall_mean
        if ratio >= spike_ratio_threshold and (best is None or ratio > best[1]):
            best = (hour, ratio, hour_mean, distinct_days)

    if best is None:
        return None
    hour, ratio, hour_mean, distinct_days = best
    return PeriodicSpikeFinding(
        kind="periodic_spike",
        hour_of_day_utc=hour,
        distinct_days=distinct_days,
        hour_mean=hour_mean,
        overall_mean=overall_mean,
        ratio=ratio,
        reason=(
            f"UTC {hour:02d}:00 时段均值 {hour_mean:.4g}，是整体均值 {overall_mean:.4g} 的 "
            f"{ratio:.1f} 倍，这个模式在 {distinct_days} 个不同日期都出现过"
        ),
    )


def detect_self_healing_flap(
    series: Series,
    *,
    down_value: float = 2,
    up_value: float = 1,
    min_flaps: int = 2,
    max_recovery_seconds: float = 3600,
) -> SelfHealingFlapFinding | None:
    """给一个状态类监控项（比如 `ifOperStatus`，1=up/2=down）的序列，
    数"down 之后很快自己恢复成 up"这种循环出现了几次。**只看数据本身**，
    不区分"是不是有人手动恢复的"——这条信息数据里没有，硬猜就是编造。
    """
    if len(series) < 2:
        return None

    recoveries: list[float] = []
    down_since: int | None = None
    for clock, value in series:
        if value == down_value and down_since is None:
            down_since = clock
        elif value == up_value and down_since is not None:
            recovery_seconds = clock - down_since
            if recovery_seconds <= max_recovery_seconds:
                recoveries.append(recovery_seconds)
            down_since = None

    if len(recoveries) < min_flaps:
        return None
    return SelfHealingFlapFinding(
        kind="self_healing_flap",
        flap_count=len(recoveries),
        avg_recovery_seconds=mean(recoveries),
        max_recovery_seconds=max(recoveries),
        reason=(
            f"{len(recoveries)} 次 down 之后在 {max(recoveries):.0f} 秒内自己恢复成 up"
            f"（平均 {mean(recoveries):.0f} 秒），从数据本身看不出是否有人工介入"
        ),
    )

"""巡检：没告警的时候发现「哪里正在变坏」（错包在涨、周期性冲高、反复自愈）。

有没有异常用统计规则判（`detectors.py`，可测、可复现），不交给模型；模型只在处置建议那一步出现。
"""

from .detectors import (
    PeriodicSpikeFinding,
    SelfHealingFlapFinding,
    TrendFinding,
    detect_periodic_spike,
    detect_self_healing_flap,
    detect_trend,
)

__all__ = [
    "TrendFinding",
    "PeriodicSpikeFinding",
    "SelfHealingFlapFinding",
    "detect_trend",
    "detect_periodic_spike",
    "detect_self_healing_flap",
]

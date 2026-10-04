"""M5 三类判定的纯函数单测——全部用合成数据，不碰网络。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from netops_ai.inspection.detectors import (
    detect_periodic_spike,
    detect_self_healing_flap,
    detect_trend,
)


def _ts(base: int, offset_seconds: int) -> int:
    return base + offset_seconds


class TestDetectTrend(unittest.TestCase):
    def test_持续上涨被识别(self):
        series = [(i * 7200, 10.0 + i * 2) for i in range(12)]  # 10, 12, 14 ... 32
        finding = detect_trend(series)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.direction, "rising")
        self.assertEqual(finding.health_impact, "unknown")

    def test_持续下跌被识别(self):
        series = [(i * 7200, 100.0 - i * 5) for i in range(12)]
        finding = detect_trend(series)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.direction, "falling")

    def test_围绕同一个值抖动不算趋势(self):
        values = [10, 12, 9, 11, 10, 13, 9, 12, 10, 11, 9, 10]
        series = [(i * 7200, v) for i, v in enumerate(values)]
        self.assertIsNone(detect_trend(series))

    def test_点数不够不判断(self):
        series = [(i * 60, 10.0 + i * 2) for i in range(3)]
        self.assertIsNone(detect_trend(series, min_points=6))

    def test_时间跨度不够不判断(self):
        series = [(i * 60, 10.0 + i * 2) for i in range(12)]
        self.assertIsNone(detect_trend(series))

    def test_小幅变化不到阈值不算趋势(self):
        # 单调但变化幅度很小(5%)，不该被判成"正在变坏"的趋势
        series = [(i * 7200, 100.0 + i * 0.3) for i in range(12)]
        self.assertIsNone(detect_trend(series, min_relative_change=0.2))

    def test_从零基线上涨不产生inf(self):
        # 回归测试：真实巡检接口跑起来后撞过 500——从 0 涨到非零时
        # relative_change 算出 inf，FastAPI 的 JSONResponse(allow_nan=False)
        # 直接拒绝序列化。这条固定住"不管数据长什么样，这个字段必须是有限值"
        import math

        series = [(i * 7200, 0.0 if i < 4 else 100.0) for i in range(12)]
        finding = detect_trend(series)
        self.assertIsNotNone(finding)
        self.assertTrue(math.isfinite(finding.relative_change))

    def test_语义方向区分变好和变坏(self):
        series = [(i * 7200, 0.0 if i < 4 else 1.0) for i in range(12)]
        improving = detect_trend(series, higher_is_worse=False)
        worsening = detect_trend(series, higher_is_worse=True)
        self.assertIsNotNone(improving)
        self.assertIsNotNone(worsening)
        self.assertEqual(improving.health_impact, "improving")
        self.assertEqual(worsening.health_impact, "worsening")


class TestDetectPeriodicSpike(unittest.TestCase):
    def _daily_spike_series(self, days: int, spike_hour: int = 14) -> list[tuple[int, float]]:
        base = int(datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp())
        series = []
        for day in range(days):
            for hour in range(24):
                clock = base + day * 86400 + hour * 3600
                value = 80.0 if hour == spike_hour else 10.0
                series.append((clock, value))
        return series

    def test_多天同一时段冲高被识别(self):
        series = self._daily_spike_series(days=4)
        finding = detect_periodic_spike(series)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.hour_of_day_utc, 14)
        self.assertGreaterEqual(finding.distinct_days, 2)

    def test_时间跨度不够不判周期尖峰(self):
        base = int(datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp())
        series = [(base + h * 3600, 80.0 if h in (14, 38) else 10.0) for h in range(40)]
        self.assertIsNone(detect_periodic_spike(series, min_distinct_days=2))

    def test_只出现一天不算周期性(self):
        base = int(datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp())
        series = [(base + h * 3600, 80.0 if h == 14 else 10.0) for h in range(24)]
        self.assertIsNone(detect_periodic_spike(series, min_distinct_days=2))

    def test_没有明显尖峰不报(self):
        base = int(datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp())
        series = [(base + d * 86400 + h * 3600, 10.0) for d in range(3) for h in range(24)]
        self.assertIsNone(detect_periodic_spike(series))

    def test_基准均值为0时不报避免除零(self):
        series = [(i * 3600, 0.0) for i in range(48)]
        self.assertIsNone(detect_periodic_spike(series))


class TestDetectSelfHealingFlap(unittest.TestCase):
    def test_反复自愈被识别(self):
        # down -> up 循环 3 次，每次都在 300 秒内恢复
        series = []
        t = 0
        for _ in range(3):
            series.append((t, 2))
            t += 300
            series.append((t, 1))
            t += 600
        finding = detect_self_healing_flap(series)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.flap_count, 3)

    def test_只flap一次不算反复(self):
        series = [(0, 2), (300, 1)]
        self.assertIsNone(detect_self_healing_flap(series, min_flaps=2))

    def test_一直down不恢复不算自愈(self):
        series = [(0, 1), (100, 2), (200, 2), (300, 2)]
        self.assertIsNone(detect_self_healing_flap(series))

    def test_恢复时间超过阈值不算自愈(self):
        series = [(0, 2), (10000, 1), (20000, 2), (30000, 1)]
        self.assertIsNone(detect_self_healing_flap(series, max_recovery_seconds=3600))

    def test_一直保持up不算flap(self):
        series = [(i * 60, 1) for i in range(10)]
        self.assertIsNone(detect_self_healing_flap(series))


if __name__ == "__main__":
    unittest.main()

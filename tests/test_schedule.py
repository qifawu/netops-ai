"""定时任务：跑一轮 = 巡检 + 每台设备存一份配置；状态落盘，头区状态点读它。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from netops_ai.api import dashboard, schedule


class TestSchedule(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        p = mock.patch.object(schedule, "STATE_PATH", Path(self.td.name) / "s.json")
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.td.cleanup)

    def test_没配间隔就不开_状态点如实说(self):
        self.assertEqual(schedule.interval_minutes({}), 0)
        self.assertEqual(schedule.interval_minutes({"SCHEDULE_INTERVAL_MINUTES": "abc"}), 0)
        st = dashboard._schedule_status({})
        self.assertFalse(st["ok"])
        self.assertIn("未开", st["label"])


    @mock.patch.object(dashboard, "run_inspection_and_persist", side_effect=RuntimeError("Zabbix 连不上"))
    @mock.patch.object(dashboard, "_env", return_value={})
    def test_巡检失败不抛_记下来(self, _env, _run):
        state = schedule.run_once()
        self.assertFalse(state["jobs"]["inspection"]["ok"])
        self.assertIn("Zabbix 连不上", state["jobs"]["inspection"]["note"])

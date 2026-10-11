"""基础巡检：说明接口来自代码（status.COMMANDS、inspection.yaml 阈值）而不是写死；周期设置写回
SCHEDULE_INTERVAL_MINUTES（合法 / 非法 / 关闭），定时线程现读周期、改了下一轮生效；「以基础巡检为起点」预置草案。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from netops_ai.api import dashboard, schedule, settings
from netops_ai.api.app import app
from netops_ai.inspection import basic_spec, status
from netops_ai.inspection import config as insp_config


class TestSpec(unittest.TestCase):
    def test_每个状态检查都有说明(self):
        self.assertEqual(set(status.COMMANDS), set(basic_spec.STATUS_CHECKS))  # COMMANDS 多了一项没写说明 → 红

    def test_命令现读status_COMMANDS(self):
        with mock.patch.dict(status.COMMANDS, {"interfaces": "show ip interface brief | exclude unassigned"}):
            spec = basic_spec.build_spec("zh")
        row = next(c for c in spec["status_checks"] if c["key"] == "interfaces")
        self.assertEqual(row["command"], "show ip interface brief | exclude unassigned")
        self.assertEqual(row["title"], "接口 up/up")

    def test_阈值现读配置(self):
        cfg = insp_config.InspectionConfig()
        cfg.detectors["trend"]["min_points"] = 42
        with mock.patch.object(insp_config, "load_config", return_value=cfg):
            spec = basic_spec.build_spec("en")
        trend = next(d for d in spec["detectors"] if d["name"] == "trend")
        self.assertEqual(next(p["value"] for p in trend["params"] if p["key"] == "min_points"), 42)
        self.assertEqual(trend["label"], "Sustained one-way trend")
        self.assertEqual([d["name"] for d in spec["detectors"]], ["trend", "periodic_spike", "self_healing_flap"])
        self.assertIn("no model", spec["note"])
        keys = {p["key"] for d in spec["detectors"] for p in d["params"]}
        self.assertEqual(keys - set(basic_spec.PARAM_EN), set())  # 每个阈值参数都有英文名

    def test_接口(self):
        r = TestClient(app).get("/api/inspection/basic/spec").json()
        self.assertEqual([c["key"] for c in r["status_checks"]], list(status.COMMANDS))
        self.assertEqual(r["schedule"]["options"], [0, 15, 30, 60, 360, 1440])


class TestSchedule(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = Path(self.td.name) / ".env"
        self.env.write_text("FOO=bar\nSCHEDULE_INTERVAL_MINUTES=60\n", encoding="utf-8")
        for target, attr, value in ((settings, "ENV_PATH", self.env),
                                    (dashboard, "_env", lambda: settings.load_dotenv_dict(self.env)),
                                    (schedule, "STATE_PATH", Path(self.td.name) / "state.json")):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.td.cleanup)
        self.c = TestClient(app)

    def test_改周期写回env_只改这一个键(self):
        r = self.c.put("/api/inspection/basic/schedule", json={"minutes": 15})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["interval_minutes"], 15)
        self.assertEqual(self.env.read_text(encoding="utf-8"), "FOO=bar\nSCHEDULE_INTERVAL_MINUTES=15\n")

    def test_关闭和非法值(self):
        r = self.c.put("/api/inspection/basic/schedule", json={"minutes": 0}).json()
        self.assertEqual((r["interval_minutes"], r["next_run_at"]), (0, None))
        self.assertIn("SCHEDULE_INTERVAL_MINUTES=0", self.env.read_text(encoding="utf-8"))
        for bad in (7, -1, 99999):
            self.assertEqual(self.c.put("/api/inspection/basic/schedule", json={"minutes": bad}).status_code, 400, bad)
        self.assertEqual(self.c.put("/api/inspection/basic/schedule", json={"minutes": "abc"}).status_code, 422)
        self.assertIn("SCHEDULE_INTERVAL_MINUTES=0", self.env.read_text(encoding="utf-8"))  # 非法值没写进去

    def test_定时线程现读周期_改了下一轮生效(self):
        t0 = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
        runs = []
        with mock.patch.object(schedule, "_basic_started_at", t0), mock.patch.object(schedule, "_last_basic_attempt", None):
            self.assertFalse(schedule.basic_tick(t0 + timedelta(minutes=30), runner=lambda: runs.append(1)))
            self.c.put("/api/inspection/basic/schedule", json={"minutes": 15})  # 改成 15 分钟：不用重启
            self.assertEqual(schedule.next_basic_run(), t0 + timedelta(minutes=15))
            self.assertTrue(schedule.basic_tick(t0 + timedelta(minutes=30), runner=lambda: runs.append(1)))
            self.assertFalse(schedule.basic_tick(t0 + timedelta(minutes=40), runner=lambda: runs.append(1)))
            self.c.put("/api/inspection/basic/schedule", json={"minutes": 0})  # 关闭
            self.assertFalse(schedule.basic_tick(t0 + timedelta(days=3), runner=lambda: runs.append(1)))
        self.assertEqual(len(runs), 1)

    def test_刚启动不因为旧的运行记录立刻补跑(self):
        now = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
        with mock.patch.object(schedule, "_basic_started_at", now), mock.patch.object(schedule, "_last_basic_attempt", None), \
                mock.patch.object(schedule, "load_state", return_value={"at": "2025-12-01T00:00:00+00:00", "jobs": {"inspection": {"ok": True}}}):
            self.assertEqual(schedule.next_basic_run(), now + timedelta(minutes=60))


class TestSeed(unittest.TestCase):
    def test_以基础巡检为起点_预置检查项_设备让用户选(self):
        from netops_ai.api import plans as api_plans
        from tests.test_inspection_plans import _topo

        with mock.patch.object(api_plans, "_topology", _topo), mock.patch.object(api_plans, "_CONVERSATION", None), \
                mock.patch("netops_ai.llm.client.LLMClient.complete", side_effect=AssertionError("no model")):
            c = TestClient(app)
            r = c.post("/api/inspection/plans/chat", json={"session": "seed1", "messages": [], "lang": "zh", "seed": "basic"}).json()
            self.assertEqual([x["template"] for x in r["draft"]["checks"]],
                             [basic_spec.BASIC_TEMPLATES[k] for k in status.COMMANDS])
            self.assertEqual(r["draft"]["devices"], [])
            self.assertEqual(r["widget"]["type"], "device_picker")
            self.assertIn("基础巡检", r["reply"])
            w = r["widget"]
            r = c.post("/api/inspection/plans/chat", json={"session": "seed1", "messages": [], "draft": r["draft"], "lang": "zh",
                                                         "selection": {"widget_id": w["id"], "type": "device_picker", "value": {"devices": ["V1"]}}}).json()
            self.assertEqual(r["widget"]["type"], "schedule_picker")  # 检查项已有，选完设备直接问周期
            w = r["widget"]
            r = c.post("/api/inspection/plans/chat", json={"session": "seed1", "messages": [], "draft": r["draft"], "lang": "zh",
                                                         "selection": {"widget_id": w["id"], "type": "schedule_picker", "value": {"every_minutes": 60}}}).json()
            self.assertTrue(r["awaiting_confirm"])


if __name__ == "__main__":
    unittest.main()

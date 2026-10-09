"""对话里的选择气泡和提示泡：缺口 → 对应气泡、都不缺 → 确认卡；选择回传确定性地改草案、不调模型；
不在拓扑里的设备 / 不认识的模板被拒；旧气泡失效；提示泡每个对话只出现一次。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from netops_ai.graph.plan_graph import PlanConversation
from netops_ai.inspection import plan_widgets as W
from netops_ai.inspection import plans as P
from tests.test_inspection_plans import FakeLLM, _out, _topo


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        p = mock.patch.object(P, "PLANS_DIR", Path(self.td.name) / "plans")
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.td.cleanup)
        self.topo = _topo()

    def conv(self, outputs=None):
        self.llm = FakeLLM(outputs or [AssertionError("the model must not be called")])
        return PlanConversation(llm_factory=lambda: self.llm, topology_loader=lambda: self.topo)

    def pick(self, conv, session, r, kind, value):
        self.assertEqual(r["widget"]["type"], kind)
        return conv.chat(session, [], r["draft"], "zh", selection={"widget_id": r["widget"]["id"], "type": kind, "value": value})


class TestWidgetRules(Base):
    def test_缺口到气泡(self):
        d = P.empty_draft()
        self.assertEqual(W.widget_for_gap(d, self.topo)["type"], "device_picker")
        d = P.apply_patch(d, {"add_devices": ["V1"]}, self.topo)
        self.assertEqual(W.widget_for_gap(d, self.topo)["type"], "schedule_picker")
        d["schedule"] = {"every_minutes": 60}
        self.assertEqual(W.widget_for_gap(d, self.topo)["type"], "check_picker")
        d = P.apply_patch(d, {"add_checks": [{"template": "cpu", "devices": None}]}, self.topo)
        self.assertIsNone(W.widget_for_gap(d, self.topo))

    def test_设备气泡按层分组_预勾选草案里的设备(self):
        d = P.apply_patch(P.empty_draft(), {"add_devices": ["D1"]}, self.topo)
        w = W.build_widget("device_picker", d, self.topo)
        self.assertEqual([g["role"] for g in w["groups"]], ["core", "aggregation", "access"])
        self.assertEqual(w["groups"][0]["devices"][0], {"name": "V1", "role": "core", "host": "192.0.2.50", "neighbors": 2})
        self.assertEqual(w["selected"], ["D1"])

    def test_检查项气泡按角色预勾推荐(self):
        d = P.apply_patch(P.empty_draft(), {"add_devices": ["A1"]}, self.topo)
        w = W.build_widget("check_picker", d, self.topo)
        self.assertEqual(w["selected"], ["interfaces", "uplink_errors", "cpu"])
        self.assertTrue(all(i["command"].startswith("show ") for i in w["items"]))

    def test_周期气泡带理由提示(self):
        d = P.apply_patch(P.empty_draft(), {"add_devices": ["V1"], "add_checks": [{"template": "interface_errors", "devices": None}]}, self.topo)
        self.assertIn("5 分钟", W.build_widget("schedule_picker", d, self.topo)["hint"])

    def test_apply_selection_合法与非法(self):
        d, problems, summary = W.apply_selection(None, {"type": "device_picker", "value": {"devices": ["V1", "D1"]}}, self.topo)
        self.assertEqual(problems, [])
        self.assertEqual([x["name"] for x in d["devices"]], ["V1", "D1"])
        self.assertEqual(summary, "已选择：核心 1、汇聚 1")
        d2, problems, _ = W.apply_selection(d, {"type": "device_picker", "value": {"devices": ["V1", "X9"]}}, self.topo)
        self.assertTrue(problems and "X9" in problems[0])
        self.assertEqual([x["name"] for x in d2["devices"]], ["V1", "D1"])  # 被拒时草案不变
        _, problems, _ = W.apply_selection(d, {"type": "check_picker", "value": {"templates": ["cpu", "reload_all"]}}, self.topo)
        self.assertTrue(problems)
        _, problems, _ = W.apply_selection(d, {"type": "schedule_picker", "value": {"daily_at": "25:00"}}, self.topo)
        self.assertTrue(problems)
        d3, problems, _ = W.apply_selection(d, {"type": "check_picker", "value": {"templates": ["ospf_neighbors", "bgp_sessions"]}}, self.topo)
        self.assertEqual(problems, [])
        bgp = next(c for c in d3["checks"] if c["id"] == "bgp_sessions")
        self.assertEqual(bgp["devices"], ["V1"])  # 只给核心
        self.assertNotIn("devices", next(c for c in d3["checks"] if c["id"] == "ospf_neighbors"))  # 覆盖全部设备

    def test_换设备时自定义命令保留_只针对被去掉设备的检查一起去掉(self):
        d = P.apply_patch(P.empty_draft(), {"add_devices": ["V1", "A1"], "add_checks": [{"template": "uplink_errors", "devices": ["A1"]}],
                                            "add_custom_checks": [{"command": "show clock"}]}, self.topo)
        d2, problems, _ = W.apply_selection(d, {"type": "device_picker", "value": {"devices": ["V1"]}}, self.topo)
        self.assertEqual(problems, [])
        self.assertEqual([c["command"] for c in d2["checks"]], ["show clock"])

    def test_核心已经看了OSPF和BGP就不再提示(self):
        d = P.apply_patch(P.empty_draft(), {"add_devices": ["V1"]}, self.topo)
        self.assertIn("core-ospf-bgp", [h["id"] for h in W.hints_for(d)])
        d = P.apply_patch(d, {"add_checks": [{"template": "ospf_neighbors", "devices": None}, {"template": "bgp_sessions", "devices": ["V1"]}]}, self.topo)
        self.assertNotIn("core-ospf-bgp", [h["id"] for h in W.hints_for(d, opening=False)])

    def test_提示泡去重(self):
        d = P.apply_patch(P.empty_draft(), {"add_devices": ["V1"], "add_checks": [{"template": "interface_errors", "devices": None}]}, self.topo)
        d["schedule"] = {"every_minutes": 60}
        first = W.hints_for(d, shown=[], opening=True)
        self.assertEqual(len(first), 2)
        second = W.hints_for(d, shown=[h["id"] for h in first])
        self.assertTrue(second)
        self.assertFalse({h["id"] for h in first} & {h["id"] for h in second})
        third = W.hints_for(d, shown=[h["id"] for h in first + second])
        self.assertEqual(third, [])


class TestRescope(Base):
    def test_设备按层排序_新加接入设备不套OSPF(self):
        d = P.apply_patch(P.empty_draft(), {"add_devices": ["D1", "V1"]}, self.topo)
        d, _, _ = W.apply_selection(d, {"type": "check_picker", "value": {"templates": ["interfaces", "ospf_neighbors", "bgp_sessions"]}}, self.topo)
        self.assertNotIn("devices", next(c for c in d["checks"] if c["id"] == "ospf_neighbors"))
        d2, problems, _ = W.apply_selection(d, {"type": "device_picker", "value": {"devices": ["A1", "D1", "V1"]}}, self.topo)
        self.assertEqual(problems, [])
        self.assertEqual([x["name"] for x in d2["devices"]], ["V1", "D1", "A1"])
        checks = {c["id"]: c.get("devices") for c in d2["checks"]}
        self.assertEqual(checks["ospf_neighbors"], ["V1", "D1"])  # A1 不跑 OSPF
        self.assertIsNone(checks["interfaces"])  # 接口状态对所有层都适用
        self.assertEqual(checks["bgp_sessions"], ["V1"])
        d3, _, _ = W.apply_selection(d2, {"type": "device_picker", "value": {"devices": ["A1"]}}, self.topo)
        self.assertEqual(sorted(c["id"] for c in d3["checks"]), ["interfaces"])  # 只剩接入设备：OSPF / BGP 没有适用的设备，拿掉

    def test_有气泡时不出建议卡(self):
        c = self.conv()
        r = c.chat("s", [], None, "zh")
        self.assertEqual(r["widget"]["type"], "device_picker")
        self.assertEqual(r["suggestions"], [])


class TestWidgetFlow(Base):
    def test_开场给设备气泡_一路选完进确认卡_不调模型(self):
        c = self.conv()
        r = c.chat("s", [], None, "zh")
        self.assertEqual(r["widget"]["type"], "device_picker")
        self.assertEqual([h["id"] for h in r["hints"]], ["readonly"])
        r = self.pick(c, "s", r, "device_picker", {"devices": ["V1", "V2", "D1"]})
        self.assertIn("已选择：核心 2、汇聚 1", r["reply"])
        self.assertEqual(r["source"], "selection")
        r = self.pick(c, "s", r, "schedule_picker", {"every_minutes": 60})
        r = self.pick(c, "s", r, "check_picker", {"templates": r["widget"]["selected"]})
        self.assertIsNone(r["widget"])
        self.assertTrue(r["awaiting_confirm"])
        self.assertTrue(r["confirm_card"])
        self.assertEqual(self.llm.calls, [])

    def test_提示泡在整个对话里不重复(self):
        c = self.conv()
        shown: list[str] = []
        r = c.chat("s", [], None, "zh")
        for kind, value in (("device_picker", {"devices": ["V1", "V2"]}), ("schedule_picker", {"every_minutes": 15}), ("check_picker", None)):
            shown += [h["id"] for h in r["hints"]]
            r = self.pick(c, "s", r, kind, value or {"templates": r["widget"]["selected"]})
        shown += [h["id"] for h in r["hints"]]
        self.assertEqual(len(shown), len(set(shown)))
        self.assertIn("core-ospf-bgp", shown)

    def test_旧气泡失效_非法设备被拒且不调模型(self):
        c = self.conv()
        r = c.chat("s", [], None, "zh")
        old = r["widget"]
        r = c.chat("s", [], r["draft"], "zh", selection={"widget_id": old["id"], "type": "device_picker", "value": {"devices": ["X9"]}})
        self.assertIn("没有生效", r["reply"])
        self.assertEqual(r["draft"]["devices"], [])
        self.assertEqual(r["widget"]["type"], "device_picker")
        self.assertNotEqual(r["widget"]["id"], old["id"])  # 重新给了一个
        r2 = c.chat("s", [], r["draft"], "zh", selection={"widget_id": old["id"], "type": "device_picker", "value": {"devices": ["V1"]}})
        self.assertIn("过期", r2["reply"])
        self.assertEqual(r2["draft"]["devices"], [])
        r3 = self.pick(c, "s", r2, "device_picker", {"devices": ["V1"]})
        self.assertEqual([d["name"] for d in r3["draft"]["devices"]], ["V1"])
        r4 = c.chat("s", [], r3["draft"], "zh", selection={"widget_id": r2["widget"]["id"], "type": "device_picker", "value": {"devices": ["V2"]}})
        self.assertIn("过期", r4["reply"])  # 用过一次的气泡也作废
        self.assertEqual(self.llm.calls, [])

    def test_确认前重新打开设备气泡改范围(self):
        c = self.conv()
        r = c.chat("s", [], None, "zh")
        r = self.pick(c, "s", r, "device_picker", {"devices": ["V1", "V2"]})
        r = self.pick(c, "s", r, "schedule_picker", {"every_minutes": 60})
        r = self.pick(c, "s", r, "check_picker", {"templates": ["ospf_neighbors"]})
        self.assertTrue(r["awaiting_confirm"])
        w = c.open_widget("s", "device_picker", r["draft"], "zh")
        self.assertEqual(w["selected"], ["V1", "V2"])
        r = c.chat("s", [], r["draft"], "zh", selection={"widget_id": w["id"], "type": "device_picker", "value": {"devices": ["V1", "D1"]}})
        self.assertEqual([d["name"] for d in r["draft"]["devices"]], ["V1", "D1"])
        self.assertTrue(r["awaiting_confirm"])  # 还是可确认的，确认卡里是新范围
        self.assertEqual([x["device"] for x in r["confirm_card"]["commands"]], ["V1", "D1"])
        self.assertEqual(self.llm.calls, [])

    def test_模型给的草案缺周期_回复里带周期气泡_不进确认卡(self):
        c = self.conv([_out(schedule={"every_minutes": 0, "daily_at": ""}, ready=True)])
        r = c.chat("s", [{"role": "user", "content": "核心设备的 OSPF"}], None, "zh")
        self.assertEqual(r["widget"]["type"], "schedule_picker")
        self.assertFalse(r["awaiting_confirm"])
        r = self.pick(c, "s", r, "schedule_picker", {"daily_at": "08:30"})
        self.assertEqual(r["draft"]["schedule"], {"daily_at": "08:30"})
        self.assertTrue(r["awaiting_confirm"])
        self.assertEqual(len(self.llm.calls), 1)  # 选择那一步没调模型

    def test_调整现有计划时重新选设备(self):
        d = P.recommended_draft(self.topo, devices=["V1", "V2"], templates=["ospf_neighbors"], schedule={"every_minutes": 60})
        d["name"] = "keep"
        P.save_plan(d)
        c = self.conv()
        r = c.chat("e", [], None, "zh", mode="edit", plan="keep")
        self.assertIsNone(r["widget"])  # 计划是完整的，不主动弹
        w = c.open_widget("e", "device_picker", r["draft"], "zh")
        r = c.chat("e", [], r["draft"], "zh", mode="edit", plan="keep",
                   selection={"widget_id": w["id"], "type": "device_picker", "value": {"devices": ["V1", "D1", "A1"]}})
        self.assertTrue(r["awaiting_confirm"])
        done = c.confirm("e", r["draft"], {"commands": True, "devices": True, "schedule": True})
        self.assertEqual(done["saved"]["name"], "keep")
        self.assertEqual([x["name"] for x in P.load_plan("keep")["devices"]], ["V1", "D1", "A1"])


class TestWidgetApi(Base):
    def test_接口_选择回传和重新打开气泡(self):
        from fastapi.testclient import TestClient

        from netops_ai.api import plans as api_plans
        from netops_ai.api.app import app

        for attr, value in (("_topology", lambda: self.topo), ("_CONVERSATION", None)):
            p = mock.patch.object(api_plans, attr, value)
            p.start()
            self.addCleanup(p.stop)
        with mock.patch("netops_ai.llm.client.LLMClient.complete", side_effect=AssertionError("no model")):
            c = TestClient(app)
            r = c.post("/api/inspection/plans/chat", json={"session": "w1", "messages": [], "lang": "en"}).json()
            self.assertEqual(r["widget"]["type"], "device_picker")
            self.assertEqual(r["widget"]["groups"][0]["label"], "Core")
            r = c.post("/api/inspection/plans/chat", json={"session": "w1", "messages": [], "draft": r["draft"], "lang": "en",
                                                         "selection": {"widget_id": r["widget"]["id"], "type": "device_picker",
                                                                       "value": {"devices": ["V1"]}}}).json()
            self.assertEqual(r["widget"]["type"], "schedule_picker")
            w = c.post("/api/inspection/plans/widget", json={"session": "w1", "type": "device_picker", "draft": r["draft"], "lang": "en"}).json()
            self.assertEqual(w["selected"], ["V1"])
            self.assertEqual(c.post("/api/inspection/plans/widget", json={"session": "nope", "type": "device_picker"}).status_code, 409)


if __name__ == "__main__":
    unittest.main()

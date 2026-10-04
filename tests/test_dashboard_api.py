"""只读查询层 + 三个前端接口。

**为什么有这个文件**：前端四页原先只能吃 fixture，因为这三个路由不存在
（EVE-NG 侧 验收时如实记下了这个缺口）。这里锁住两件事：
接口返回的字段名跟 fixture 一致，以及挂在 `/` 上的静态前端不会吃掉 `/api`。
"""

import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from netops_ai.api import dashboard
from netops_ai.api.app import app


#: **测试读 fixture，不读真实 records/**（真实目录一直在长，断言会随数据漂）。
#: 说明见 tests/fixtures/records/README.md。
_FIXTURE_RECORDS = Path(__file__).parent / "fixtures" / "records"
_records_patch = mock.patch.object(dashboard, "RECORDS_DIR", _FIXTURE_RECORDS)


def setUpModule():
    _records_patch.start()


def tearDownModule():
    _records_patch.stop()


client = TestClient(app)


class QueryLayerTests(unittest.TestCase):

    def test_连带告警必须写明被谁引起(self):
        for inc in dashboard.build_incidents(dashboard.load_alerts()):
            for a in inc["alerts"]:
                if a["role"] == "consequence":
                    self.assertTrue(a["caused_by"], "连带告警必须写明被谁引起")


    def test_总览卡片都有值(self):
        alerts = dashboard.load_alerts()
        ov = dashboard.build_overview(alerts, dashboard.build_incidents(alerts))
        # 卡片数字不写死成某个数——**每加一张卡就来改一次断言，这条测试就退化成
        # "跟着代码走"的回声**，拦不住任何东西。真正该锁的是"每张卡都有值"。
        self.assertGreaterEqual(len(ov["cards"]), 6)
        for c in ov["cards"]:
            self.assertTrue(str(c["value"]), f"{c['label']} 没有值")


class RouteTests(unittest.TestCase):

    def test_静态前端不许吃掉接口(self):
        """`StaticFiles(html=True)` 挂在 `/` 上会吃掉它下面所有路径，
        挂早了 `/api/*` 全部 404。这条就是拦这个。"""
        self.assertEqual(client.get("/healthz").status_code, 200)
        self.assertEqual(client.get("/api").json()["name"], "netops-ai")
        self.assertEqual(client.get("/api/overview").status_code, 200)



if __name__ == "__main__":
    unittest.main()


class TestNextStepWording(unittest.TestCase):
    """「需人工介入」不能只是换个好听的标签。

 维护者让把「判不出」的措辞改柔和一点、给点建议。改措辞是对的
 （同一件事，「需人工介入」是流程状态，「判不出」听起来像无能），
 **但改完必须还能看出它是"判不出时如实转人工"，而且要说清下一步干什么**——
 只换标签不给下一步，就是把诚实换成了体面。
    """


    def test_下一步来自结论那次的原话不是另外生成的(self):
        # 再问模型一次是多花一次钱换一句可能跟结论对不上的话。
        for inc in dashboard.build_incidents(dashboard.load_alerts()):
            if not inc["next_step"]:
                continue
            pool = "；".join(x.get("what_data_would_help", "") for x in inc["undistinguishable"])
            for part in inc["next_step"].split("；"):
                self.assertIn(part, pool)

    def test_卡片措辞柔和了但没把事实说含糊(self):
        alerts = dashboard.load_alerts()
        card = next(c for c in dashboard.build_overview(alerts, dashboard.build_incidents(alerts))["cards"]
                    if c["key"] == "undetermined")
        self.assertEqual(card["label"], "需人工介入")
        self.assertIn("判不出", card["sub"])
        self.assertIn("不硬给结论", card["sub"])


class TestAuditView(unittest.TestCase):


    def test_对话轨迹的设备命令进入时间线(self):
        import json as _json
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            ledger = os.path.join(td, "l.jsonl")
            trace_dir = Path(td) / "chat-traces"
            trace_dir.mkdir()
            (trace_dir / "chat-1.json").write_text(_json.dumps({
                "trace_id": "chat-1",
                "started_at": 1780000000,
                "tool_calls": [{
                    "tool": "device_show",
                    "args": {"device": "R3", "command": "show interfaces"},
                    "ok": True,
                }],
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"TOKEN_LEDGER_PATH": ledger}):
                with mock.patch.object(dashboard, "CHAT_TRACE_DIR", trace_dir):
                    v = dashboard.build_audit_view([])

        self.assertEqual(v["summary"]["chat_traces"], 1)
        self.assertEqual(v["summary"]["command_total"], 1)
        self.assertEqual(v["timeline"][0]["source_type"], "chat")
        self.assertIn("对话", v["timeline"][0]["source"])

    def test_审计设备走拓扑别名归一化并保留未指明和下线标签(self):
        import json as _json
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            ledger = os.path.join(td, "l.jsonl")
            trace_dir = Path(td) / "chat-traces"
            trace_dir.mkdir()
            (trace_dir / "chat-1.json").write_text(_json.dumps({
                "trace_id": "chat-1",
                "host": "V1-vios",
                "started_at": 1780000000,
                "tool_calls": [
                    {"tool": "device_show", "args": {"command": "show version"}, "ok": True},
                    {"tool": "device_show", "args": {"device": "A4", "command": "show version"}, "ok": True},
                ],
            }), encoding="utf-8")
            alerts = [{
                "eventid": "9",
                "alert_clock": 5,
                "webhook_payload": {"host": "D1-vios"},
                "device_commands_run": [{"command": "show version", "allowed": True, "ok": True}],
            }]
            with mock.patch.dict(os.environ, {"TOKEN_LEDGER_PATH": ledger}):
                with mock.patch.object(dashboard, "CHAT_TRACE_DIR", trace_dir):
                    v = dashboard.build_audit_view(alerts)

        self.assertEqual({row["device"] for row in v["devices"]}, {"V1", "A4", "D1"})
        self.assertEqual({row["device"] for row in v["timeline"]}, {"V1", "A4", "D1"})

    def test_审计空设备显示未指明并按命令理由聚合拒绝(self):
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            ledger = os.path.join(td, "l.jsonl")
            alerts = [
                {
                    "eventid": eventid,
                    "alert_clock": index,
                    "webhook_payload": {"host": host},
                    "device_commands_run": [{
                        "command": "show version | include uptime|reload|System image|Last reload|Cisco IOS Software",
                        "allowed": False,
                        "denial_reason": "只允许一个管道",
                    }],
                }
                for index, (eventid, host) in enumerate(
                    [("101559", "V1-vios"), ("101560", "V2-vios"), ("101559", "D1-vios")],
                    start=1,
                )
            ]
            with mock.patch.dict(os.environ, {"TOKEN_LEDGER_PATH": ledger}):
                v = dashboard.build_audit_view(alerts)

        self.assertEqual(v["summary"]["denied_commands"], 3)
        self.assertEqual(v["summary"]["denied_groups"], 1)
        self.assertEqual(len(v["denials"]), 1)
        denial = v["denials"][0]
        self.assertEqual(denial["count"], 3)
        self.assertEqual(denial["device_count"], 3)
        self.assertEqual(denial["alert_count"], 2)
        self.assertIn("第二个管道符", denial["reason"])
        self.assertEqual(denial["source"], "告警 101559、101560 的取证")

    def test_合并incident的token汇总全部告警(self):
        import json as _json
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            ledger = os.path.join(td, "l.jsonl")
            with open(ledger, "w", encoding="utf-8") as f:
                for eventid, tokens in (("100", 30), ("101", 70)):
                    f.write(_json.dumps({"eventid": eventid, "tag": "langchain", "calls": 1,
                                         "total_tokens": tokens, "cost_cny": None}) + "\n")
            alerts = [
                {"eventid": "100", "incident_id": "same", "incident_role": "root",
                 "alert_clock": 1, "webhook_payload": {"host": "R1"},
                 "analysis_parsed": {}, "diagnostic_trace": []},
                {"eventid": "101", "incident_id": "same", "incident_role": "consequence",
                 "alert_clock": 2, "webhook_payload": {"host": "R1"},
                 "analysis_parsed": {}, "diagnostic_trace": []},
            ]
            with mock.patch.dict(os.environ, {"TOKEN_LEDGER_PATH": ledger}):
                row = dashboard.build_incidents(alerts)[0]

        self.assertEqual(row["token_cost"]["forensics"], 100)
        self.assertEqual(row["token_cost"]["total"], 100)



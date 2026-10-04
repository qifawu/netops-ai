"""FastAPI webhook 入口。用 `TestClient`（不起真实网络端口），验证两件事：
请求立刻返回、真正干活的部分被丢进了后台任务，不是同步跑在请求里。
"""

from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from netops_ai.api.app import app


class TestWebhookEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_healthz(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)

    @mock.patch("netops_ai.api.app.process_alert")
    def test_webhook立刻返回accepted(self, mock_process):
        resp = self.client.post(
            "/webhooks/zabbix",
            json={"eventid": "123", "name": "Interface down", "severity": "3", "clock": "1789700000"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "accepted", "eventid": "123"})

    @mock.patch("netops_ai.api.app.process_alert")
    def test_真正干活的函数被调用而不是被忽略(self, mock_process):
        self.client.post(
            "/webhooks/zabbix",
            json={"eventid": "456", "name": "CPU high"},
        )
        mock_process.assert_called_once()
        (called_payload,), _ = mock_process.call_args
        self.assertEqual(called_payload["eventid"], "456")

    def test_缺eventid字段被拒(self):
        resp = self.client.post("/webhooks/zabbix", json={"name": "x"})
        self.assertEqual(resp.status_code, 422)


class TestDashboardRoutes(unittest.TestCase):
    """M4 看板路由：只测"路由接对了函数、状态码对不对"，业务逻辑本身的
    单测在 `test_api_dashboard.py`/`test_api_chat.py`，这里不重复。
    """

    def setUp(self):
        self.client = TestClient(app)

    def test_导航挪到api首页让位给前端(self):
        """**这条 改过，改的是规矩本身，不是为了让测试变绿。**

 原来断言的是「`/` 只回一张导航，后端不出界面」。那条规矩立在砍掉
 Vue 看板和 Grafana 那次，针对的是"三个界面登三次"。现在自建前端
 （`web/`）回来了，但吸取的正是那次教训：**产物交给这个进程托管，
 仍然只有一个端口、一个界面**，不是又起一个服务。

 所以 `/` 让位给前端，导航挪到 `/api`。后端依然不"写"界面，
 只是托管构建产物。
        """
        body = self.client.get("/api").json()
        self.assertEqual(body["health"], "/healthz")

    @mock.patch("netops_ai.api.app.dashboard.list_records", return_value=[{"eventid": "1"}])
    def test_records列表(self, mock_list):
        resp = self.client.get("/api/records")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [{"eventid": "1"}])

    @mock.patch("netops_ai.api.app.dashboard.load_record", return_value=None)
    def test_record不存在返回404(self, mock_load):
        resp = self.client.get("/api/records/999")
        self.assertEqual(resp.status_code, 404)

    @mock.patch("netops_ai.api.app.dashboard.load_record", return_value={"eventid": "1", "analysis_parsed": {"root_cause": "x"}})
    def test_record存在返回全文(self, mock_load):
        resp = self.client.get("/api/records/1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["analysis_parsed"]["root_cause"], "x")

    @mock.patch("netops_ai.api.app.dashboard.load_record", return_value={"eventid": "1"})
    @mock.patch("netops_ai.api.app.dashboard.render_markdown_report", return_value="# 报告")
    def test_导出md报告(self, mock_render, mock_load):
        resp = self.client.get("/api/records/1/report.md")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("报告", resp.text)

    @mock.patch("netops_ai.api.app.dashboard.load_latest_inspection", return_value=None)
    def test_没跑过巡检返回404(self, mock_load):
        resp = self.client.get("/api/inspection")
        self.assertEqual(resp.status_code, 404)

    @mock.patch("netops_ai.api.app.dashboard.run_full_inspection")
    def test_触发巡检丢后台立刻返回(self, mock_run):
        resp = self.client.post("/api/inspection/run")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "started"})



if __name__ == "__main__":
    unittest.main()

"""`report_single_alert()`：`pipeline.py` 用的入口，串起 build_card +
send_card，负责判断"webhook 到底配没配真实地址"。"""

from __future__ import annotations

import unittest
from unittest import mock

from netops_ai.feishu.report import is_configured_webhook, report_single_alert


class TestIsConfiguredWebhook(unittest.TestCase):
    def test_空字符串不算配置好(self):
        self.assertFalse(is_configured_webhook(""))

    def test_占位值不算配置好(self):
        self.assertFalse(is_configured_webhook("https://your-webhook-url-here"))

    def test_真实前缀才算配置好(self):
        self.assertTrue(is_configured_webhook("https://open.feishu.cn/open-apis/bot/v2/hook/abc123"))


class TestReportSingleAlert(unittest.TestCase):
    def test_没有分析结果直接跳过不发送(self):
        with mock.patch("netops_ai.feishu.report.send_card") as m:
            result = report_single_alert("1", None, "https://open.feishu.cn/open-apis/bot/v2/hook/x")
        self.assertFalse(result["attempted"])
        self.assertFalse(result["sent"])
        m.assert_not_called()

    def test_webhook没配置直接跳过不发送(self):
        with mock.patch("netops_ai.feishu.report.send_card") as m:
            result = report_single_alert("1", {"root_cause": "x", "confidence": "high"}, "")
        self.assertFalse(result["attempted"])
        self.assertFalse(result["sent"])
        m.assert_not_called()

    def test_webhook是占位值也跳过(self):
        with mock.patch("netops_ai.feishu.report.send_card") as m:
            result = report_single_alert("1", {"root_cause": "x", "confidence": "high"}, "https://example.com/placeholder")
        self.assertFalse(result["attempted"])
        m.assert_not_called()

    def test_配置好且分析有结果就真的调用send_card(self):
        with mock.patch("netops_ai.feishu.report.send_card", return_value=(True, "")) as m:
            result = report_single_alert(
                "1", {"root_cause": "x", "confidence": "high", "evidence": [], "undistinguishable_candidates": []},
                "https://open.feishu.cn/open-apis/bot/v2/hook/x",
            )
        self.assertTrue(result["attempted"])
        self.assertTrue(result["sent"])
        m.assert_called_once()

    def test_send_card失败时原样记录不抛异常(self):
        with mock.patch("netops_ai.feishu.report.send_card", return_value=(False, "网络错误")):
            result = report_single_alert(
                "1", {"root_cause": "x", "confidence": "high", "evidence": [], "undistinguishable_candidates": []},
                "https://open.feishu.cn/open-apis/bot/v2/hook/x",
            )
        self.assertTrue(result["attempted"])
        self.assertFalse(result["sent"])
        self.assertEqual(result["error"], "网络错误")


if __name__ == "__main__":
    unittest.main()


class 两条通道怎么挑(unittest.TestCase):
    """M3 补：webhook 之外多了一条应用身份通道，`report_single_alert()`
    要按 `.env` 里配了什么自动挑，并且把挑了哪条记进 `transport`。
    """

    _APP = {"app_id": "cli_x", "app_secret": "sec", "chat_id": "oc_y"}
    _ANALYSIS = {"root_cause": "x", "confidence": "high"}
    _WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/abc"

    def test_is_configured_app要三个都齐(self):
        from netops_ai.feishu.report import is_configured_app

        self.assertTrue(is_configured_app("cli_x", "sec", "oc_y"))
        self.assertFalse(is_configured_app("cli_x", "", "oc_y"), "只配一半算没配")
        self.assertFalse(is_configured_app("", "sec", "oc_y"))
        self.assertFalse(is_configured_app("cli_x", "sec", ""))
        self.assertFalse(is_configured_app("your-app-id", "sec", "oc_y"), "占位值要挡住")
        self.assertFalse(is_configured_app("cli_x", "sec", "your-chat-id"))

    def test_两个都配了走webhook(self):
        with mock.patch("netops_ai.feishu.report.send_card", return_value=(True, "")) as w, \
             mock.patch("netops_ai.feishu.report.send_card_as_app") as a:
            result = report_single_alert("1", self._ANALYSIS, self._WEBHOOK, **self._APP)
        self.assertEqual(result["transport"], "webhook")
        w.assert_called_once()
        a.assert_not_called()

    def test_只配了应用身份就走应用(self):
        with mock.patch("netops_ai.feishu.report.send_card") as w, \
             mock.patch("netops_ai.feishu.report.send_card_as_app", return_value=(True, "")) as a:
            result = report_single_alert("1", self._ANALYSIS, "", **self._APP)
        self.assertEqual(result["transport"], "app")
        self.assertTrue(result["sent"])
        w.assert_not_called()
        a.assert_called_once()
        self.assertEqual(a.call_args[0][:3], ("cli_x", "sec", "oc_y"))

    def test_两条都没配就跳过且错误信息把两条都点名(self):
        with mock.patch("netops_ai.feishu.report.send_card") as w, \
             mock.patch("netops_ai.feishu.report.send_card_as_app") as a:
            result = report_single_alert("1", self._ANALYSIS, "")
        self.assertFalse(result["attempted"])
        self.assertEqual(result["transport"], "")
        self.assertIn("FEISHU_WEBHOOK_URL", result["error"])
        self.assertIn("FEISHU_APP_ID", result["error"])
        w.assert_not_called()
        a.assert_not_called()

    def test_错误信息里不许出现app_secret原文(self):
        result = report_single_alert("1", self._ANALYSIS, "", app_id="cli_x", app_secret="SUPER-SECRET", chat_id="")
        self.assertNotIn("SUPER-SECRET", result["error"])

    def test_应用通道失败时原样记录不抛异常(self):
        with mock.patch("netops_ai.feishu.report.send_card_as_app", return_value=(False, "bot 不在群里")):
            result = report_single_alert("1", self._ANALYSIS, "", **self._APP)
        self.assertTrue(result["attempted"])
        self.assertFalse(result["sent"])
        self.assertEqual(result["error"], "bot 不在群里")
        self.assertEqual(result["transport"], "app")

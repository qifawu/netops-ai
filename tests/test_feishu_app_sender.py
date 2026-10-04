"""`app_sender.py`：走应用身份（tenant_access_token + im/v1/messages）发卡片。

全部用假响应，**一次都不打真实飞书接口**——真实调用是"接通了没有"这种
一次性验证该干的事（已经在 Mac 侧用 lark-cli 验过），不该混进日常单测，
否则单测会因为别人的网络/配额而红。
"""

import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netops_ai.feishu import app_sender  # noqa: E402


def _fake_post(responses):
    """按调用顺序依次返回 `responses` 里的 `(parsed, err)`，并把每次收到的
    (url, payload, headers) 记下来供断言。
    """
    calls = []

    def _post(url, payload, headers, timeout):
        calls.append({"url": url, "payload": payload, "headers": headers})
        return responses[len(calls) - 1]

    return _post, calls


class TenantAccessToken(unittest.TestCase):
    def setUp(self):
        app_sender._TOKEN_CACHE.clear()

    def test_凭据没配全直接拒绝不发请求(self):
        with mock.patch.object(app_sender, "_post_json") as m:
            token, err = app_sender.get_tenant_access_token("", "secret")
            self.assertEqual(token, "")
            self.assertIn("没配全", err)
            m.assert_not_called()

    def test_换到token并写进缓存(self):
        post, calls = _fake_post([({"code": 0, "tenant_access_token": "t-abc", "expire": 7200}, "")])
        with mock.patch.object(app_sender, "_post_json", post):
            token, err = app_sender.get_tenant_access_token("cli_x", "sec")
        self.assertEqual((token, err), ("t-abc", ""))
        self.assertEqual(calls[0]["payload"], {"app_id": "cli_x", "app_secret": "sec"})
        self.assertIn("cli_x", app_sender._TOKEN_CACHE)

    def test_第二次命中缓存不再发请求(self):
        post, calls = _fake_post([({"code": 0, "tenant_access_token": "t-abc", "expire": 7200}, "")])
        with mock.patch.object(app_sender, "_post_json", post):
            app_sender.get_tenant_access_token("cli_x", "sec")
            token, _ = app_sender.get_tenant_access_token("cli_x", "sec")
        self.assertEqual(token, "t-abc")
        self.assertEqual(len(calls), 1, "命中缓存时不该再打一次接口")

    def test_force_refresh绕过缓存(self):
        post, calls = _fake_post(
            [
                ({"code": 0, "tenant_access_token": "t-1", "expire": 7200}, ""),
                ({"code": 0, "tenant_access_token": "t-2", "expire": 7200}, ""),
            ]
        )
        with mock.patch.object(app_sender, "_post_json", post):
            app_sender.get_tenant_access_token("cli_x", "sec")
            token, _ = app_sender.get_tenant_access_token("cli_x", "sec", force_refresh=True)
        self.assertEqual(token, "t-2")
        self.assertEqual(len(calls), 2)

    def test_飞书拒绝时错误信息里不能出现app_secret(self):
        post, _ = _fake_post([({"code": 10003, "msg": "invalid app_secret"}, "")])
        with mock.patch.object(app_sender, "_post_json", post):
            token, err = app_sender.get_tenant_access_token("cli_x", "SUPER-SECRET-VALUE")
        self.assertEqual(token, "")
        self.assertNotIn("SUPER-SECRET-VALUE", err, "错误信息会落进 records/，不许带凭据")
        self.assertIn("10003", err)


class SendCardAsApp(unittest.TestCase):
    def setUp(self):
        app_sender._TOKEN_CACHE.clear()

    def test_没配chat_id直接拒绝(self):
        ok, err = app_sender.send_card_as_app("cli_x", "sec", "", {"elements": []})
        self.assertFalse(ok)
        self.assertIn("FEISHU_CHAT_ID", err)

    def test_发送成功并且content是JSON字符串(self):
        card = {"header": {"template": "green"}, "elements": [{"tag": "div"}]}
        post, calls = _fake_post(
            [
                ({"code": 0, "tenant_access_token": "t-abc", "expire": 7200}, ""),
                ({"code": 0, "data": {"message_id": "om_x"}}, ""),
            ]
        )
        with mock.patch.object(app_sender, "_post_json", post):
            ok, err = app_sender.send_card_as_app("cli_x", "sec", "oc_y", card)

        self.assertEqual((ok, err), (True, ""))
        send = calls[1]
        self.assertEqual(send["headers"]["Authorization"], "Bearer t-abc")
        self.assertEqual(send["payload"]["receive_id"], "oc_y")
        self.assertEqual(send["payload"]["msg_type"], "interactive")
        self.assertIsInstance(
            send["payload"]["content"], str, "飞书要求 content 是 JSON 字符串，不是嵌套对象"
        )
        self.assertEqual(json.loads(send["payload"]["content"]), card)

    def test_token过期时强刷一次并重试成功(self):
        post, calls = _fake_post(
            [
                ({"code": 0, "tenant_access_token": "t-old", "expire": 7200}, ""),
                ({"code": 99991663, "msg": "token expired"}, ""),
                ({"code": 0, "tenant_access_token": "t-new", "expire": 7200}, ""),
                ({"code": 0, "data": {"message_id": "om_x"}}, ""),
            ]
        )
        with mock.patch.object(app_sender, "_post_json", post):
            ok, err = app_sender.send_card_as_app("cli_x", "sec", "oc_y", {"elements": []})

        self.assertEqual((ok, err), (True, ""))
        self.assertEqual(calls[3]["headers"]["Authorization"], "Bearer t-new")

    def test_非token类错误不重试直接原样返回(self):
        post, calls = _fake_post(
            [
                ({"code": 0, "tenant_access_token": "t-abc", "expire": 7200}, ""),
                ({"code": 230002, "msg": "bot is not in the chat"}, ""),
            ]
        )
        with mock.patch.object(app_sender, "_post_json", post):
            ok, err = app_sender.send_card_as_app("cli_x", "sec", "oc_y", {"elements": []})

        self.assertFalse(ok)
        self.assertIn("230002", err)
        self.assertIn("bot is not in the chat", err)
        self.assertEqual(len(calls), 2, "不是 token 问题就不该重试")

    def test_网络异常不抛出去(self):
        def boom(url, data=None, headers=None, method=None):
            raise urllib.error.URLError("connection refused")

        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            ok, err = app_sender.send_card_as_app("cli_x", "sec", "oc_y", {"elements": []})
        self.assertFalse(ok)
        self.assertIn("URLError", err)


if __name__ == "__main__":
    unittest.main()

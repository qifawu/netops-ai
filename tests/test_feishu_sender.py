"""飞书 webhook 发送（M3 加的）：单测全部用假 `urllib`，不真发。"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from netops_ai.feishu.sender import send_card


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestSendCard(unittest.TestCase):
    def test_空url直接拒绝不发送(self):
        with mock.patch("urllib.request.urlopen") as m:
            ok, err = send_card("", {"header": {}})
        self.assertFalse(ok)
        self.assertIn("看起来不是一个真实地址", err)
        m.assert_not_called()

    def test_非http开头拒绝发送(self):
        with mock.patch("urllib.request.urlopen") as m:
            ok, err = send_card("your-webhook-placeholder", {"header": {}})
        self.assertFalse(ok)
        m.assert_not_called()

    def test_飞书返回code0算成功(self):
        with mock.patch(
            "urllib.request.urlopen",
            return_value=_FakeResponse(json.dumps({"code": 0, "msg": "success"}).encode()),
        ):
            ok, err = send_card("https://open.feishu.cn/open-apis/bot/v2/hook/xxx", {"header": {}})
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_飞书返回非0算失败(self):
        with mock.patch(
            "urllib.request.urlopen",
            return_value=_FakeResponse(json.dumps({"code": 19021, "msg": "invalid sign"}).encode()),
        ):
            ok, err = send_card("https://open.feishu.cn/open-apis/bot/v2/hook/xxx", {"header": {}})
        self.assertFalse(ok)
        self.assertIn("invalid sign", err)

    def test_网络错误不抛异常(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("连不上")):
            ok, err = send_card("https://open.feishu.cn/open-apis/bot/v2/hook/xxx", {"header": {}})
        self.assertFalse(ok)
        self.assertIn("连不上", err)

    def test_响应不是合法json不崩(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(b"not json")):
            ok, err = send_card("https://open.feishu.cn/open-apis/bot/v2/hook/xxx", {"header": {}})
        self.assertFalse(ok)
        self.assertIn("不是合法 JSON", err)


if __name__ == "__main__":
    unittest.main()

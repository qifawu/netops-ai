"""LLM 客户端的测试。全部用假响应，不打真实 API——真实调用是这次回归实验
本身要干的事，不该混进日常单测里（会花钱、会慢、会因为限流变得不稳定）。
"""

from __future__ import annotations

import json
import socket
import unittest
from unittest import mock

from netops_ai.llm.client import LLMClient, LLMError


def _fake_response(payload: dict):
    body = json.dumps(payload).encode("utf-8")
    resp = mock.MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class TestConfig(unittest.TestCase):
    def test_没配全直接报错不发请求(self):
        client = LLMClient(base_url="", api_key="x", model="m")
        with self.assertRaises(LLMError):
            client.complete([{"role": "user", "content": "hi"}])


class TestComplete(unittest.TestCase):
    @mock.patch("netops_ai.llm.client.urllib.request.urlopen")
    def test_普通文本回复(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
                ],
                "usage": {"total_tokens": 10},
            }
        )
        client = LLMClient(base_url="http://fake/v1", api_key="k", model="m")
        resp = client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(resp.content, "hello")
        self.assertIsNone(resp.parsed)
        self.assertEqual(resp.finish_reason, "stop")

    @mock.patch("netops_ai.llm.client.urllib.request.urlopen")
    def test_structured_output解析成dict(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"root_cause": "x", "confidence": "high"}',
                            "reasoning": "先看了这个再看了那个",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"completion_tokens": 20},
            }
        )
        client = LLMClient(base_url="http://fake/v1", api_key="k", model="m")
        resp = client.complete(
            [{"role": "user", "content": "hi"}],
            response_format={"type": "json_schema", "json_schema": {"name": "x", "schema": {}}},
        )
        self.assertEqual(resp.parsed, {"root_cause": "x", "confidence": "high"})
        self.assertEqual(resp.reasoning, "先看了这个再看了那个")

    @mock.patch("netops_ai.llm.client.urllib.request.urlopen")
    def test_模型没吐合法json时parsed是None不炸穿(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response(
            {
                "choices": [{"message": {"content": "不是json"}, "finish_reason": "stop"}],
                "usage": {},
            }
        )
        client = LLMClient(base_url="http://fake/v1", api_key="k", model="m")
        resp = client.complete(
            [{"role": "user", "content": "hi"}],
            response_format={"type": "json_schema", "json_schema": {"name": "x", "schema": {}}},
        )
        self.assertIsNone(resp.parsed)
        self.assertEqual(resp.content, "不是json")

    @mock.patch("netops_ai.llm.client.urllib.request.urlopen")
    def test_api返回error字段抛LLMError(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"error": {"message": "bad request"}})
        client = LLMClient(base_url="http://fake/v1", api_key="k", model="m")
        with self.assertRaises(LLMError):
            client.complete([{"role": "user", "content": "hi"}])

    @mock.patch("netops_ai.llm.client.urllib.request.urlopen")
    def test_请求带上UA避免被前置WAF拦(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}
        )
        client = LLMClient(base_url="http://fake/v1", api_key="k", model="m")
        client.complete([{"role": "user", "content": "hi"}])
        sent_req = mock_urlopen.call_args.args[0]
        self.assertTrue(sent_req.get_header("User-agent"))
        self.assertIn("Bearer k", sent_req.get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()


class TestRetry(unittest.TestCase):
    """93763：Gemini 返回 503「high demand」，一次就放弃，这条告警没出结论。"""

    def _err(self, code):
        import io
        import urllib.error

        return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b'{"error":"busy"}'))

    @mock.patch("netops_ai.llm.client.time.sleep")
    @mock.patch("netops_ai.llm.client.urllib.request.urlopen")
    def test_503之后重试成功(self, mock_urlopen, mock_sleep):
        ok = _fake_response({"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}], "usage": {}})
        mock_urlopen.side_effect = [self._err(503), ok]
        r = LLMClient(base_url="http://x", api_key="k", model="m").complete([{"role": "user", "content": "q"}])
        self.assertEqual(r.content, "hi")
        self.assertEqual(r.attempts, 2)
        self.assertEqual(r.retry_count, 1)
        mock_sleep.assert_called_once()

    @mock.patch("netops_ai.llm.client.time.sleep")
    @mock.patch("netops_ai.llm.client.urllib.request.urlopen")
    def test_读超时后重试成功(self, mock_urlopen, mock_sleep):
        ok = _fake_response({"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}], "usage": {}})
        mock_urlopen.side_effect = [socket.timeout("timed out"), ok]
        r = LLMClient(base_url="http://x", api_key="k", model="m").complete([{"role": "user", "content": "q"}])
        self.assertEqual(r.content, "hi")
        self.assertEqual(r.retry_count, 1)
        mock_sleep.assert_called_once()

    @mock.patch("netops_ai.llm.client.time.sleep")
    @mock.patch("netops_ai.llm.client.urllib.request.urlopen")
    def test_400不重试(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = [self._err(400)]
        with self.assertRaises(LLMError) as cm:
            LLMClient(base_url="http://x", api_key="k", model="m").complete([{"role": "user", "content": "q"}])
        self.assertEqual(cm.exception.status_code, 400)
        mock_sleep.assert_not_called()

    @mock.patch("netops_ai.llm.client.time.sleep")
    @mock.patch("netops_ai.llm.client.urllib.request.urlopen")
    def test_一直503最后报错(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = [self._err(503)] * 4
        with self.assertRaises(LLMError):
            LLMClient(base_url="http://x", api_key="k", model="m").complete([{"role": "user", "content": "q"}])
        self.assertEqual(mock_urlopen.call_count, 4)

    @mock.patch("netops_ai.llm.client.time.sleep")
    @mock.patch("netops_ai.llm.client.urllib.request.urlopen")
    def test_一直503最后报错带重试审计(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = [self._err(503), self._err(503), self._err(503), self._err(503)]
        with self.assertRaises(LLMError) as cm:
            LLMClient(base_url="http://x", api_key="k", model="m").complete([{"role": "user", "content": "q"}])
        self.assertEqual(cm.exception.attempts, 4)
        self.assertEqual(cm.exception.retry_count, 3)

    @mock.patch("netops_ai.llm.client.urllib.request.urlopen")
    def test_退避和retry回调可注入(self, mock_urlopen):
        ok = _fake_response({"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}], "usage": {}})
        mock_urlopen.side_effect = [self._err(503), self._err(502), self._err(504), ok]
        sleeps = []
        retries = []
        client = LLMClient(
            base_url="http://x",
            api_key="k",
            model="m",
            sleep=sleeps.append,
            jitter=lambda: 0.0,
        )
        r = client.complete(
            [{"role": "user", "content": "q"}],
            on_retry=lambda attempt, exc: retries.append((attempt, type(exc).__name__)),
        )
        self.assertEqual(r.content, "hi")
        self.assertEqual(sleeps, [1.0, 3.0, 8.0])
        self.assertEqual(retries, [(1, "HTTPError"), (2, "HTTPError"), (3, "HTTPError")])

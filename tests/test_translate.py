"""AI 生成内容按需翻译：中文原样直通、英文调模型并落盘缓存、缓存命中不再调模型。"""

import json
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from netops_ai.api import translate
from netops_ai.api.app import app
from netops_ai.llm.client import LLMError, LLMResponse

client = TestClient(app)


def _fake_response(content: str) -> LLMResponse:
    return LLMResponse(content=content, parsed=None, usage={}, finish_reason="stop", raw={})


class TranslateTextTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._cache_path = Path(tempfile.mkdtemp()) / "_translation_cache.json"
        self._patch = mock.patch.object(translate, "CACHE_PATH", self._cache_path)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()

    def test_中文原样直通不调模型(self) -> None:
        with mock.patch("netops_ai.api.translate.LLMClient") as mock_client_cls:
            out = translate.translate_text("接口 down 了", "zh")
            self.assertEqual(out, "接口 down 了")
            mock_client_cls.assert_not_called()

    def test_英文调模型并写入缓存(self) -> None:
        with mock.patch("netops_ai.api.translate.LLMClient") as mock_client_cls:
            mock_client_cls.return_value.complete.return_value = _fake_response("Interface is down.")
            out = translate.translate_text("接口 down 了", "en")
            self.assertEqual(out, "Interface is down.")
            self.assertTrue(self._cache_path.exists())
            cached = json.loads(self._cache_path.read_text(encoding="utf-8"))
            self.assertEqual(len(cached), 1)

    def test_缓存命中不再调模型(self) -> None:
        with mock.patch("netops_ai.api.translate.LLMClient") as mock_client_cls:
            mock_client_cls.return_value.complete.return_value = _fake_response("Interface is down.")
            translate.translate_text("接口 down 了", "en")
            mock_client_cls.return_value.complete.reset_mock()
            out2 = translate.translate_text("接口 down 了", "en")
            self.assertEqual(out2, "Interface is down.")
            mock_client_cls.return_value.complete.assert_not_called()

    def test_模型调用失败抛TranslateError(self) -> None:
        with mock.patch("netops_ai.api.translate.LLMClient") as mock_client_cls:
            mock_client_cls.return_value.complete.side_effect = LLMError("网络不通")
            with self.assertRaises(translate.TranslateError):
                translate.translate_text("接口 down 了", "en")


class TranslateEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._cache_path = Path(tempfile.mkdtemp()) / "_translation_cache.json"
        self._patch = mock.patch.object(translate, "CACHE_PATH", self._cache_path)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()

    def test_接口正常返回翻译(self) -> None:
        with mock.patch("netops_ai.api.translate.LLMClient") as mock_client_cls:
            mock_client_cls.return_value.complete.return_value = _fake_response("Interface is down.")
            resp = client.post("/api/translate", json={"text": "接口 down 了", "lang": "en"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["translated"], "Interface is down.")

    def test_模型失败返回502不是500(self) -> None:
        with mock.patch("netops_ai.api.translate.LLMClient") as mock_client_cls:
            mock_client_cls.return_value.complete.side_effect = LLMError("网络不通")
            resp = client.post("/api/translate", json={"text": "接口 down 了", "lang": "en"})
            self.assertEqual(resp.status_code, 502)

    def test_lang不是zh或en被拒(self) -> None:
        resp = client.post("/api/translate", json={"text": "接口 down 了", "lang": "fr"})
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()

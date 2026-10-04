"""Zabbix 只读客户端的测试。

两类测试都要有（README 定的规矩）：
- 不依赖网络的：用假响应测闸门有没有被绕过、请求/响应有没有解析对
- 依赖网络的：能连真 Zabbix 就跑真的，连不上就跳过，不算失败
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from netops_ai.zabbix.client import ZabbixAPIError, ZabbixClient


def _fake_response(payload: dict):
    """造一个 urlopen() 的返回值：一个支持 context manager 的假 HTTP 响应。"""
    body = json.dumps(payload).encode("utf-8")
    resp = mock.MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class TestGateNeverBypassed(unittest.TestCase):
    """核心红线：白名单挡下来的方法，请求必须根本没发出去。"""

    @mock.patch("netops_ai.zabbix.client.urllib.request.urlopen")
    def test_写方法在发请求前就被拦(self, mock_urlopen):
        client = ZabbixClient(url="http://fake", user="Admin", password="x")
        with self.assertRaises(PermissionError):
            client._call("host.create", {"host": "evil"})
        mock_urlopen.assert_not_called()

    @mock.patch("netops_ai.zabbix.client.urllib.request.urlopen")
    def test_script_execute也拦(self, mock_urlopen):
        client = ZabbixClient(url="http://fake", user="Admin", password="x")
        with self.assertRaises(PermissionError):
            client._call("script.execute", {})
        mock_urlopen.assert_not_called()


class TestCallParsing(unittest.TestCase):
    """请求/响应的编解码，全部用假响应，不碰网络。"""

    @mock.patch("netops_ai.zabbix.client.urllib.request.urlopen")
    def test_login拿到token后续请求带上Bearer头(self, mock_urlopen):
        login_resp = _fake_response({"jsonrpc": "2.0", "result": "tok-123", "id": 1})
        hosts_resp = _fake_response(
            {"jsonrpc": "2.0", "result": [{"hostid": "1", "host": "V1"}], "id": 2}
        )
        mock_urlopen.side_effect = [login_resp, hosts_resp]

        client = ZabbixClient(url="http://fake", user="Admin", password="x")
        token = client.login()
        self.assertEqual(token, "tok-123")

        hosts = client.list_hosts()
        self.assertEqual(hosts, [{"hostid": "1", "host": "V1"}])

        # 第二次请求（list_hosts）必须带上第一次登录拿到的 token
        second_call_req = mock_urlopen.call_args_list[1].args[0]
        self.assertEqual(second_call_req.get_header("Authorization"), "Bearer tok-123")

    @mock.patch("netops_ai.zabbix.client.urllib.request.urlopen")
    def test_ensure_login只在没token时才登录(self, mock_urlopen):
        login_resp = _fake_response({"jsonrpc": "2.0", "result": "tok-1", "id": 1})
        problems_resp = _fake_response({"jsonrpc": "2.0", "result": [], "id": 2})
        mock_urlopen.side_effect = [login_resp, problems_resp]

        client = ZabbixClient(url="http://fake", user="Admin", password="x")
        client.ensure_login()
        client.get_active_problems()  # 不该再触发一次 login

        self.assertEqual(mock_urlopen.call_count, 2)

    @mock.patch("netops_ai.zabbix.client.urllib.request.urlopen")
    def test_error字段抛ZabbixAPIError(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response(
            {
                "jsonrpc": "2.0",
                "error": {"code": -32602, "message": "Invalid params.", "data": "bad"},
                "id": 1,
            }
        )
        client = ZabbixClient(url="http://fake", user="Admin", password="x")
        with self.assertRaises(ZabbixAPIError):
            client.login()

    @mock.patch("netops_ai.zabbix.client.urllib.request.urlopen")
    def test_不合法JSON抛ZabbixAPIError而不是裸异常(self, mock_urlopen):
        resp = mock.MagicMock()
        resp.read.return_value = b"<html>not json</html>"
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        mock_urlopen.return_value = resp

        client = ZabbixClient(url="http://fake", user="Admin", password="x")
        with self.assertRaises(ZabbixAPIError):
            client.login()

    def test_没配url直接报错不发请求(self):
        client = ZabbixClient(url="", user="Admin", password="x")
        with self.assertRaises(ZabbixAPIError):
            client.login()

    @mock.patch("netops_ai.zabbix.client.urllib.request.urlopen")
    def test_get_history把history类型传下去(self, mock_urlopen):
        login_resp = _fake_response({"jsonrpc": "2.0", "result": "tok-1", "id": 1})
        hist_resp = _fake_response({"jsonrpc": "2.0", "result": [{"value": "1"}], "id": 2})
        mock_urlopen.side_effect = [login_resp, hist_resp]

        client = ZabbixClient(url="http://fake", user="Admin", password="x")
        client.ensure_login()
        client.get_history("12345", history_type=3, limit=5)

        sent = json.loads(mock_urlopen.call_args_list[1].args[0].data)
        self.assertEqual(sent["params"]["history"], 3)
        self.assertEqual(sent["params"]["itemids"], "12345")
        self.assertEqual(sent["params"]["limit"], 5)


def _load_dotenv(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _real_client_or_none() -> ZabbixClient | None:
    """有真 Zabbix 能连就返回一个已登录的客户端，连不上返回 None（不是抛异常）。"""
    env = {**_load_dotenv(Path(__file__).resolve().parent.parent / ".env"), **os.environ}
    url, user, pw = env.get("ZABBIX_URL"), env.get("ZABBIX_USER"), env.get("ZABBIX_PASSWORD")
    if not (url and user and pw):
        return None
    client = ZabbixClient(url=url, user=user, password=pw, timeout=5)
    try:
        client.login()
    except Exception:
        return None
    return client


class TestRealZabbixSmoke(unittest.TestCase):
    """能连真 Zabbix 才跑；连不上（比如在 Mac 上跑）就整体跳过，不算失败。"""

    @classmethod
    def setUpClass(cls):
        cls.client = _real_client_or_none()
        if cls.client is None:
            raise unittest.SkipTest("连不上真实 Zabbix（没有 .env 或者网络不通），跳过真实取数测试")

    @classmethod
    def tearDownClass(cls):
        if cls.client is not None:
            cls.client.logout()

    def test_真实列主机(self):
        hosts = self.client.list_hosts()
        self.assertIsInstance(hosts, list)
        self.assertGreater(len(hosts), 0, "M0 装完 Zabbix 应该至少有一台主机")

    def test_真实取当前告警(self):
        problems = self.client.get_active_problems()
        self.assertIsInstance(problems, list)  # spike-1 已恢复，这里应该是空列表

    def test_写方法对真服务器也被本地拦下不发网络请求(self):
        with self.assertRaises(PermissionError):
            self.client._call("host.create", {"host": "should-never-reach-server"})


if __name__ == "__main__":
    unittest.main()

"""telnet 设备适配层。不依赖网络的部分用假 socket 测；真连设备的部分
连不上就跳过，不算失败（同一台设备，跟 ssh.py 共用一份真实凭据）。
"""

from __future__ import annotations

import os
import socket
import unittest
from pathlib import Path
from unittest import mock

from netops_ai.devices.telnet import TelnetDeviceAdapter


class _FakeSocket:
    """按顺序吐预先准备好的字节块；吐完之后 recv() 一律超时，模拟"这一轮
    没有更多数据了"，配合 `_read_until_quiet` 的循环读取逻辑。
    """

    def __init__(self, recv_queue: list[bytes]):
        self._queue = list(recv_queue)
        self.sent: list[str] = []
        self.closed = False

    def settimeout(self, timeout):
        pass

    def sendall(self, data: bytes) -> None:
        self.sent.append(data.decode())

    def recv(self, bufsize: int) -> bytes:
        if not self._queue:
            raise socket.timeout()
        item = self._queue.pop(0)
        if item is None:  # 显式的"这一轮读完了"边界，逼下一次 recv() 超时
            raise socket.timeout()
        return item

    def close(self) -> None:
        self.closed = True


def _patched_socket(chunks: list[bytes]):
    fake = _FakeSocket(chunks)
    return mock.patch("netops_ai.devices.telnet.socket.create_connection", return_value=fake), fake


class TestGateBeforeConnect(unittest.TestCase):
    def test_被拒的命令不会触发connect(self):
        adapter = TelnetDeviceAdapter("cisco", host="10.0.0.99", username="ai-readonly", password="x")
        with mock.patch.object(adapter, "_ensure_connected") as m:
            result = adapter.run("configure terminal")
            self.assertFalse(result.allowed)
            m.assert_not_called()


class TestLoginAndExec(unittest.TestCase):
    def test_登录成功后放行命令真的执行了(self):
        chunks = [
            b"Username: ",  # banner / 用户名提示（丢弃，不校验）
            b"Password: ",  # 密码提示（丢弃）
            b"\r\nV1>",  # 登录后的提示符，_ensure_connected 靠这个判断登录成功
            b"\r\nV1>",  # terminal length 0 的响应（丢弃）
            b"show version\r\nCisco IOS Software...\r\nV1>\r\nV1>",  # 命令回显 + 真实输出 + 提示符
        ]
        patcher, fake = _patched_socket(chunks)
        with patcher:
            adapter = TelnetDeviceAdapter(
                "cisco", host="10.0.0.99", username="ai-readonly", password="x"
            )
            result = adapter.run("show version")
            self.assertTrue(result.ok, result.error)
            self.assertIn("Cisco IOS Software", result.output)
            # 命令回显和结尾提示符都应该被剥掉，不留在最终输出里
            self.assertNotIn("V1>", result.output)

    def test_登录失败没拿到提示符就报错(self):
        chunks = [b"Username: ", b"Password: ", b"\r\n% Login invalid\r\n", None]
        patcher, fake = _patched_socket(chunks)
        with patcher:
            adapter = TelnetDeviceAdapter(
                "cisco", host="10.0.0.99", username="ai-readonly", password="wrong"
            )
            result = adapter.run("show version")
            self.assertTrue(result.allowed)
            self.assertFalse(result.ok)
            self.assertIn("登录失败", result.error)

    def test_没配host直接报错不会去connect(self):
        adapter = TelnetDeviceAdapter("cisco", host="", username="ai-readonly", password="x")
        result = adapter.run("show version")
        self.assertTrue(result.allowed)
        self.assertFalse(result.ok)
        self.assertIn("DEVICE_HOST", result.error)


class TestStripEchoAndPrompt(unittest.TestCase):
    def test_剥离命令回显和结尾提示符(self):
        raw = "show version\r\nCisco IOS Software...\r\nV1>\r\nV1>"
        out = TelnetDeviceAdapter._strip_echo_and_prompt(raw, "show version")
        self.assertEqual(out, "Cisco IOS Software...")

        self.assertTrue(TelnetDeviceAdapter._looks_complete("\r\n[V1]"))

    def test_长内容结尾不会被误删(self):
        # 确认"看起来像提示符"但其实是正常输出一部分的长行不会被误砍
        raw = "show run\r\n" + "x" * 60 + ">\r\nV1#"
        out = TelnetDeviceAdapter._strip_echo_and_prompt(raw, "show run")
        self.assertTrue(out.startswith("x" * 60))


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


class TestRealDeviceSmoke(unittest.TestCase):
    """能真连设备才跑。telnet 是给这台机器 SSH(paramiko)连不上时的备用通道，
    见 `netops_ai/devices/telnet.py` 模块说明。
    """

    @classmethod
    def setUpClass(cls):
        env = {**_load_dotenv(Path(__file__).resolve().parent.parent / ".env"), **os.environ}
        host, user, pw = env.get("DEVICE_HOST"), env.get("DEVICE_USERNAME"), env.get("DEVICE_PASSWORD")
        vendor = env.get("DEVICE_VENDOR", "cisco")
        if not (host and user and pw):
            raise unittest.SkipTest("DEVICE_PASSWORD 没配，跳过真实设备连接测试")
        cls.adapter = TelnetDeviceAdapter(
            vendor, host=host, port=int(env.get("DEVICE_TELNET_PORT", "23")), username=user, password=pw, timeout=8
        )
        try:
            cls.adapter._ensure_connected()
        except Exception as exc:
            raise unittest.SkipTest(f"telnet 连不上真实设备，跳过：{exc}")

    @classmethod
    def tearDownClass(cls):
        cls.adapter.close()

    def test_真实设备上跑show_version(self):
        result = self.adapter.run("show version")
        self.assertTrue(result.ok, result.error)
        self.assertIn("Cisco IOS", result.output)

    def test_真实设备上configure_terminal必须被拒(self):
        result = self.adapter.run("configure terminal")
        self.assertFalse(result.allowed)


if __name__ == "__main__":
    unittest.main()

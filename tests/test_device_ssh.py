"""SSH 设备适配层。不依赖网络的部分 mock 系统 ssh 子进程；真连设备的部分
只有本机 `.env` 配好时才跑。
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from netops_ai.devices.ssh import SSHDeviceAdapter


class TestGateBeforeConnect(unittest.TestCase):
    """核心红线：白名单挡下来的命令，根本不应该触发一次 SSH 子进程。"""

    def test_被拒的命令不会触发subprocess(self):
        adapter = SSHDeviceAdapter("cisco", host="10.0.0.99", username="ai-readonly", password="x")
        with mock.patch("netops_ai.devices.ssh.subprocess.run") as m:
            result = adapter.run("configure terminal")
            self.assertFalse(result.allowed)
            m.assert_not_called()


class TestExecWithMockedSubprocess(unittest.TestCase):
    def test_放行命令真的调用系统ssh(self):
        completed = subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout="Cisco IOS Software...\n", stderr=""
        )
        with mock.patch("netops_ai.devices.ssh.subprocess.run", return_value=completed) as m:
            adapter = SSHDeviceAdapter("cisco", host="10.0.0.99", username="ai-readonly", password="secret")
            result = adapter.run("show version")

        self.assertTrue(result.ok)
        self.assertIn("Cisco IOS Software", result.output)
        argv = m.call_args.args[0]
        kwargs = m.call_args.kwargs
        self.assertEqual(argv[0], "ssh")
        self.assertIn("KexAlgorithms=+diffie-hellman-group14-sha1", argv)
        self.assertIn("HostKeyAlgorithms=+ssh-rsa", argv)
        self.assertIn("PubkeyAcceptedAlgorithms=+ssh-rsa", argv)
        self.assertIn("StrictHostKeyChecking=accept-new", argv)
        self.assertIn("ai-readonly@10.0.0.99", argv)
        self.assertEqual(argv[-1], "show version")
        self.assertNotIn("secret", argv)
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["env"]["NETOPS_AI_SSH_PASSWORD"], "secret")

    def test_askpass_helper文件不包含密码(self):
        adapter = SSHDeviceAdapter("cisco", host="10.0.0.99", username="ai-readonly", password="secret")
        helper = Path(adapter._askpass_helper())
        try:
            self.assertTrue(helper.exists())
            self.assertNotIn("secret", helper.read_text(encoding="utf-8"))
        finally:
            adapter.close()
        self.assertFalse(helper.exists())

    def test_stderr有内容且stdout为空时算失败(self):
        completed = subprocess.CompletedProcess(
            args=["ssh"], returncode=255, stdout="", stderr="% Invalid input detected"
        )
        with mock.patch("netops_ai.devices.ssh.subprocess.run", return_value=completed):
            adapter = SSHDeviceAdapter("cisco", host="10.0.0.99", username="ai-readonly", password="x")
            result = adapter.run("show version")
        self.assertTrue(result.allowed)
        self.assertFalse(result.ok)
        self.assertIn("Invalid input", result.error)

    def test_没配host直接报错不会去run(self):
        adapter = SSHDeviceAdapter("cisco", host="", username="ai-readonly", password="x")
        with mock.patch("netops_ai.devices.ssh.subprocess.run") as m:
            result = adapter.run("show version")
        self.assertTrue(result.allowed)
        self.assertFalse(result.ok)
        self.assertIn("DEVICE_HOST", result.error)
        m.assert_not_called()


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
    """能真连设备才跑；用于确认系统 ssh + legacy 算法开关能连 V1。"""

    @classmethod
    def setUpClass(cls):
        env = {**_load_dotenv(Path(__file__).resolve().parent.parent / ".env"), **os.environ}
        host, user, pw = env.get("DEVICE_HOST"), env.get("DEVICE_USERNAME"), env.get("DEVICE_PASSWORD")
        vendor = env.get("DEVICE_VENDOR", "cisco")
        if not (host and user and pw):
            raise unittest.SkipTest("DEVICE_PASSWORD 没配，跳过真实设备连接测试")
        cls.adapter = SSHDeviceAdapter(vendor, host=host, username=user, password=pw, timeout=12)

    @classmethod
    def tearDownClass(cls):
        cls.adapter.close()

    def test_真实设备上跑show_ip_interface_brief(self):
        result = self.adapter.run("show ip interface brief")
        self.assertTrue(result.ok, result.error)
        self.assertIn("Interface", result.output)

    def test_真实设备上configure_terminal必须被拒(self):
        result = self.adapter.run("configure terminal")
        self.assertFalse(result.allowed)


if __name__ == "__main__":
    unittest.main()

"""定时任务入口：各任务只写草稿 / 报告、不发送；出错退出码 1；缺配置如实说。设备和 Zabbix 用假的。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from netops_ai import jobs as J
from netops_ai import outbox as OB
from tests.test_outbox_delivery import FLAPPY, FlapZbx
from tests.test_traffic_report import FakeZbx

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
VER = "Cisco IOS Software, IOSv Software (VIOS-ADVENTERPRISEK9-M), Version 15.9(3)M8, RELEASE SOFTWARE (fc1)"


class Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.root = Path(self._d.name)


class TestJobs(Base):
    def links(self):
        p = self.root / "links.yaml"
        p.write_text("- name: wan\n  host: R1\n  interface: Gi0/1\n  email: [noc@isp.example]\n  circuit_id: C1\n", encoding="utf-8")
        return p

    def test_isp_flap_出草稿_不发送(self):
        lines = J.run_isp_flap(FlapZbx(FLAPPY), self.links(), now=NOW, root=self.root / "ob")
        self.assertIn("draft created", lines[0])
        drafts = OB.list_drafts(root=self.root / "ob")
        self.assertEqual([d["status"] for d in drafts], ["pending"])

    def test_isp_flap_没配链路_如实说(self):
        self.assertIn("no links configured", J.run_isp_flap(FlapZbx([]), self.root / "none.yaml")[0])

    def test_流量任务_出PDF和待确认草稿(self):
        lines = J.run_traffic("weekly", FakeZbx(), self.root / "rep", channel="feishu", now=NOW, root=self.root / "ob")
        self.assertTrue(Path(lines[0].split(": ", 1)[1]).read_bytes().startswith(b"%PDF"))
        self.assertEqual(OB.list_drafts(root=self.root / "ob")[0]["kind"], "traffic-weekly")

    def test_邮件通道没有收件人_报错而不是出个发不了的草稿(self):
        with self.assertRaises(ValueError):
            J.run_traffic("daily", FakeZbx(), self.root / "rep", channel="email", to=[], now=NOW, root=self.root / "ob")
        self.assertEqual(OB.list_drafts(root=self.root / "ob"), [])

    def test_版本提醒_有落后才出草稿(self):
        class Ad:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def run(self, cmd):
                return SimpleNamespace(allowed=True, denial_reason="", error="", output=VER)
        policy = self.root / "p.yaml"
        policy.write_text('- product: ios\n  target: "15.9(3)M9"\n', encoding="utf-8")
        lines = J.run_version_reminder({"R1": 1}, lambda d: Ad(), policy, root=self.root / "ob")
        self.assertIn("behind target: R1", lines[0])
        self.assertEqual(len(OB.list_drafts(root=self.root / "ob")), 1)
        policy.write_text('- product: ios\n  target: "15.9(3)M8"\n', encoding="utf-8")
        lines = J.run_version_reminder({"R1": 1}, lambda d: Ad(), policy, root=self.root / "ob2")
        self.assertIn("none", lines[0])
        self.assertEqual(OB.list_drafts(root=self.root / "ob2"), [])
        self.assertIn("no version policy", J.run_version_reminder({"R1": 1}, lambda d: Ad(), self.root / "x.yaml")[0])


class TestMain(unittest.TestCase):
    def test_出错退出码1_不抛(self):
        with mock.patch.object(J, "_zabbix", side_effect=RuntimeError("zabbix down")), mock.patch("sys.stderr"):
            self.assertEqual(J.main(["isp-flap"]), 1)

    def test_成功退出码0(self):
        with mock.patch.object(J, "run_version_reminder", return_value=["ok"]), mock.patch("netops_ai.topology.load_topology", return_value={}), \
                mock.patch("builtins.print") as pr:
            self.assertEqual(J.main(["version-reminder"]), 0)
        pr.assert_called_with("ok")

    def test_收件人环境变量(self):
        with mock.patch.dict("os.environ", {"REPORT_CHANNEL": "Email", "REPORT_TO": "a@example.com, b@example.com"}):
            self.assertEqual(J._recipients(), ("email", ["a@example.com", "b@example.com"]))
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(J._recipients(), ("feishu", []))


if __name__ == "__main__":
    unittest.main()

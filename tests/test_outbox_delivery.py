"""对外发送：草稿箱（只写草稿、人确认才发、收件人白名单）、外网链路翻滚检测出草稿、给管理层的总结草稿、接口。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from netops_ai import outbox as OB
from netops_ai.inspection import isp_flap as ISP
from netops_ai.inspection import management_summary as MS
from tests.test_traffic_report import FakeZbx

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.root = Path(self._d.name)


class TestOutbox(Base):
    def make(self, **kw):
        return OB.create(channel="email", kind="t", subject="s", body="b", to=["noc@isp.example"], root=self.root, **kw)

    def test_创建只是草稿_不发(self):
        sent = []
        rec = self.make()
        self.assertEqual(rec["status"], "pending")
        self.assertEqual(sent, [])
        self.assertEqual(len(OB.list_drafts("pending", self.root)), 1)

    def test_确认才发_留痕_白名单内(self):
        sent = []
        rec = self.make()
        out = OB.approve_and_send(rec["id"], "owner", {"email": sent.append}, allowlist="@isp.example", root=self.root)
        self.assertEqual((out["status"], out["approved_by"]), ("sent", "owner"))
        self.assertTrue(out["sent_at"])
        self.assertEqual(len(sent), 1)
        with self.assertRaises(OB.OutboxError):  # 已发的不能再发
            OB.approve_and_send(rec["id"], "owner", {"email": sent.append}, allowlist="@isp.example", root=self.root)

    def test_收件人不在白名单_不发(self):
        rec = self.make()
        for al in ("", "someone@else.example", "@else.example"):
            with self.assertRaises(OB.OutboxError):
                OB.approve_and_send(rec["id"], "owner", {"email": lambda r: None}, allowlist=al, root=self.root)
        self.assertEqual(OB.get(rec["id"], self.root)["status"], "pending")

    def test_必须有确认人_没配通道不假装成功(self):
        rec = self.make()
        with self.assertRaises(OB.OutboxError):
            OB.approve_and_send(rec["id"], " ", {"email": lambda r: None}, allowlist="@isp.example", root=self.root)
        with self.assertRaises(OB.OutboxError):
            OB.approve_and_send(rec["id"], "owner", {}, allowlist="@isp.example", root=self.root)
        with self.assertRaises(OB.OutboxError):  # 没配 SMTP
            OB.email_sender({})({"to": ["a@b.example"], "subject": "s", "body": "b", "attachments": []})

    def test_发送失败_记failed带原因_可重试(self):
        rec = self.make()

        def boom(r):
            raise ConnectionError("smtp down")
        out = OB.approve_and_send(rec["id"], "owner", {"email": boom}, allowlist="@isp.example", root=self.root)
        self.assertEqual(out["status"], "failed")
        self.assertIn("smtp down", out["error"])
        ok = OB.approve_and_send(rec["id"], "owner", {"email": lambda r: None}, allowlist="@isp.example", root=self.root)
        self.assertEqual(ok["status"], "sent")

    def test_丢弃_只能丢pending(self):
        rec = self.make()
        self.assertEqual(OB.discard(rec["id"], "owner", root=self.root)["status"], "discarded")
        with self.assertRaises(OB.OutboxError):
            OB.discard(rec["id"], "owner", root=self.root)
        with self.assertRaises(OB.OutboxError):
            OB.approve_and_send(rec["id"], "owner", {"email": lambda r: None}, allowlist="@isp.example", root=self.root)

    def test_邮件草稿要有收件人_id不能带路径(self):
        with self.assertRaises(OB.OutboxError):
            OB.create(channel="email", kind="t", subject="s", body="b", to=[], root=self.root)
        self.assertIsNone(OB.get("../x", self.root))

    def test_SMTP发送_带附件_走starttls_登录(self):
        att = self.root / "r.pdf"
        att.write_bytes(b"%PDF-1.4 test")
        rec = {"to": ["noc@isp.example"], "subject": "s", "body": "b", "attachments": [str(att)]}
        with mock.patch("smtplib.SMTP") as smtp:
            inst = smtp.return_value.__enter__.return_value
            OB.email_sender({"SMTP_HOST": "mail.example", "SMTP_FROM": "ops@example.com", "SMTP_USER": "u", "SMTP_PASSWORD": "p"})(rec)
        smtp.assert_called_once_with("mail.example", 587, timeout=20)
        inst.starttls.assert_called_once()
        inst.login.assert_called_once_with("u", "p")
        msg = inst.send_message.call_args[0][0]
        self.assertEqual((msg["To"], msg["From"]), ("noc@isp.example", "ops@example.com"))
        self.assertEqual([p.get_filename() for p in msg.iter_attachments()], ["r.pdf"])


class FlapZbx:
    """R1 Gi0/1 的状态历史：给定 (分钟偏移, 值) 序列。"""

    def __init__(self, series, with_item=True):
        self.series, self.with_item = series, with_item

    def list_hosts(self):
        return [{"hostid": "1", "host": "R1"}]

    def list_items(self, host_id):
        return [{"itemid": "9", "name": "Interface Gi0/1(): Operational status", "status": "0"}] if self.with_item else []

    def get_history(self, item_id, *, history_type=0, time_from=None, time_till=None, limit=100):
        return [{"clock": str(int((NOW + timedelta(minutes=m)).timestamp())), "value": str(v)} for m, v in self.series]


LINK = {"name": "wan-primary", "host": "R1", "interface": "Gi0/1", "circuit_id": "CN-BJ-000123", "isp": "Example Telecom",
        "email": ["noc@isp.example"], "threshold": 3, "window_minutes": 60, "cooldown_hours": 6, "signature": "NetOps"}
FLAPPY = [(-50, 1), (-45, 2), (-40, 1), (-30, 2), (-25, 1), (-10, 2), (-5, 1)]  # 3 次 down


class TestIspFlap(Base):
    def test_检测_数up到非up的次数(self):
        det = ISP.detect(FlapZbx(FLAPPY), LINK, NOW)
        self.assertEqual(det["flaps"], 3)
        self.assertEqual([s for _, s in det["events"]], ["down", "up"] * 3)
        self.assertEqual(ISP.detect(FlapZbx([(-50, 1), (-10, 1)]), LINK, NOW)["flaps"], 0)

    def test_达到阈值出草稿_只是草稿_内容是事实(self):
        res = ISP.check_links(FlapZbx(FLAPPY), [LINK], now=NOW, root=self.root)
        self.assertEqual((res[0]["action"], res[0]["flaps"]), ("draft created", 3))
        d = OB.get(res[0]["draft"], self.root)
        self.assertEqual((d["status"], d["to"], d["kind"]), ("pending", ["noc@isp.example"], "isp-flap"))
        self.assertIn("CN-BJ-000123", d["subject"])
        self.assertIn("Down events: 3", d["body"])
        self.assertIn("down", d["body"])

    def test_设备日志里的闪断_采样看不见也能抓到(self):
        def line(m, state, extra=""):
            at = NOW + timedelta(minutes=m)
            return {"clock": str(int(at.timestamp()) + 5), "value": f"{at:%b %d %H:%M:%S} 192.0.2.1 17: *{at:%b %d %H:%M:%S}.000: "
                    f"%LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet0/1, changed state to {state}"}

        class LogZbx(FlapZbx):
            def list_items(self, host_id):
                return super().list_items(host_id) + [{"itemid": "77", "name": "Syslog from 192.0.2.1", "status": "0", "value_type": "2"}]

            def get_history(self, item_id, *, history_type=0, time_from=None, time_till=None, limit=100):
                if item_id == "77":
                    return [line(-50, "down"), line(-49, "up"), line(-30, "down"), line(-29, "up"), line(-10, "down"), line(-9, "up"),
                            {"clock": "1", "value": "Oct 17 00:00:00 x: %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet0/2, changed state to down"}]
                return [{"clock": str(int((NOW + timedelta(minutes=m)).timestamp())), "value": "1"} for m in (-55, -20, -2)]  # 采样一直是 up
        det = ISP.detect(LogZbx([]), LINK, NOW)
        self.assertEqual((det["flaps"], det["source"]), (3, "device log (LINEPROTO-UPDOWN)"))  # 别的接口（Gi0/2）的日志不算
        self.assertEqual(ISP._canon("GigabitEthernet 0/1"), ISP._canon("Gi0/1"))

    def test_时钟偏差_事件比现在还晚几分钟也算_窗口结束时间不早于事件(self):
        series = [(-30, 1), (-20, 2), (-10, 1), (+2, 2), (+3, 1)]  # 后面两个事件在「现在」之后（Zabbix 服务器时钟超前）
        det = ISP.detect(FlapZbx(series), LINK, NOW)
        self.assertEqual(det["flaps"], 2)
        self.assertGreaterEqual(det["end"], det["events"][-1][0])

    def test_没到阈值_不出草稿(self):
        res = ISP.check_links(FlapZbx([(-30, 1), (-20, 2), (-10, 1)]), [LINK], now=NOW, root=self.root)
        self.assertEqual(res[0]["action"], "ok")
        self.assertEqual(OB.list_drafts(root=self.root), [])

    def test_冷却期内不重复出_丢弃后可再出(self):
        first = ISP.check_links(FlapZbx(FLAPPY), [LINK], now=datetime.now(timezone.utc), root=self.root)
        self.assertEqual(first[0]["action"], "draft created")
        again = ISP.check_links(FlapZbx(FLAPPY), [LINK], now=datetime.now(timezone.utc), root=self.root)
        self.assertIn("cooldown", again[0]["action"])
        OB.discard(first[0]["draft"], "owner", root=self.root)
        third = ISP.check_links(FlapZbx(FLAPPY), [LINK], now=datetime.now(timezone.utc), root=self.root)
        self.assertEqual(third[0]["action"], "draft created")

    def test_找不到监控项_取数出错_如实报(self):
        self.assertIn("no status item", ISP.check_links(FlapZbx(FLAPPY, with_item=False), [LINK], now=NOW, root=self.root)[0]["action"])

        class Bad(FlapZbx):
            def get_history(self, *a, **k):
                raise RuntimeError("zabbix down")
        self.assertIn("error", ISP.check_links(Bad(FLAPPY), [LINK], now=NOW, root=self.root)[0]["action"])

    def test_链路清单校验(self):
        p = self.root / "l.yaml"
        p.write_text("- name: a\n  host: R1\n  interface: Gi0/1\n  email: x@isp.example\n", encoding="utf-8")
        links = ISP.load_links(p)
        self.assertEqual((links[0]["threshold"], links[0]["email"]), (3, ["x@isp.example"]))
        p.write_text("- name: a\n  host: R1\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            ISP.load_links(p)
        self.assertEqual(ISP.load_links(self.root / "none.yaml"), [])


class FakeLLM:
    def __init__(self, text="本周整体平稳，R1 Gi0/1 峰值利用率较高。", fail=False):
        self.text, self.fail, self.calls = text, fail, []

    def complete(self, messages, **kw):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("llm down")
        return SimpleNamespace(content=self.text)


class TestManagementSummary(Base):
    def test_草稿_带PDF和AI摘要_不发送(self):
        llm = FakeLLM()
        res = MS.create_draft("weekly", FakeZbx(), self.root / "rep", llm=llm, to=["boss@example.com"], now=NOW, root=self.root / "ob")
        d = res["draft"]
        self.assertEqual((d["status"], d["channel"], d["kind"]), ("pending", "email", "traffic-weekly"))
        self.assertIn("整体平稳", d["body"])
        self.assertTrue(d["meta"]["ai_summary"])
        self.assertTrue(Path(d["attachments"][0]).read_bytes().startswith(b"%PDF"))
        sys_prompt = llm.calls[0][0]["content"]
        self.assertIn("不要编造数字", sys_prompt)
        self.assertIn("R1 Gi0/1", llm.calls[0][1]["content"])  # 模型只拿到事实清单

    def test_模型不可用_报告照出_正文退回事实清单(self):
        res = MS.create_draft("daily", FakeZbx(), self.root / "rep", llm=FakeLLM(fail=True), to=["a@example.com"], now=NOW, root=self.root / "ob")
        self.assertFalse(res["draft"]["meta"]["ai_summary"])
        self.assertIn("未生成 AI 摘要", res["draft"]["body"])
        res2 = MS.create_draft("daily", FakeZbx(), self.root / "rep", llm=None, to=["a@example.com"], now=NOW, root=self.root / "ob")
        self.assertFalse(res2["draft"]["meta"]["ai_summary"])


class TestApi(Base):
    def setUp(self):
        super().setUp()
        import os

        from fastapi.testclient import TestClient

        from netops_ai.api.app import app

        self._old = os.environ.get("NETOPS_OUTBOX_DIR")
        os.environ["NETOPS_OUTBOX_DIR"] = str(self.root)
        self.addCleanup(lambda: os.environ.__setitem__("NETOPS_OUTBOX_DIR", self._old) if self._old is not None else os.environ.pop("NETOPS_OUTBOX_DIR", None))
        self.c = TestClient(app)

    def test_列出_确认_丢弃_白名单(self):
        from netops_ai.api import outbox as API

        rec = OB.create(channel="email", kind="t", subject="s", body="b", to=["noc@isp.example"])
        self.assertEqual(self.c.get("/api/outbox", params={"status": "pending"}).json()["drafts"][0]["id"], rec["id"])
        self.assertEqual(self.c.get("/api/outbox", params={"status": "bad"}).status_code, 400)
        self.assertEqual(self.c.get("/api/outbox/nope").status_code, 404)
        sent = []
        with mock.patch.object(API, "senders", lambda: {"email": sent.append}), mock.patch.object(API, "_env", lambda: {"OUTBOX_ALLOWED_RECIPIENTS": ""}):
            r = self.c.post(f"/api/outbox/{rec['id']}/approve", json={"approver": "owner"})
            self.assertEqual(r.status_code, 400)  # 白名单为空
            self.assertIn("allowlist", r.json()["message"])
        with mock.patch.object(API, "senders", lambda: {"email": sent.append}), mock.patch.object(API, "_env", lambda: {"OUTBOX_ALLOWED_RECIPIENTS": "@isp.example"}):
            self.assertEqual(self.c.post(f"/api/outbox/{rec['id']}/approve", json={}).status_code, 400)  # 没有确认人
            r = self.c.post(f"/api/outbox/{rec['id']}/approve", json={"approver": "owner"})
            self.assertEqual((r.status_code, r.json()["status"]), (200, "sent"))
        self.assertEqual(len(sent), 1)
        rec2 = OB.create(channel="email", kind="t", subject="s2", body="b", to=["noc@isp.example"])
        self.assertEqual(self.c.post(f"/api/outbox/{rec2['id']}/discard", json={}).json()["status"], "discarded")

    def test_生成总结草稿接口(self):
        from netops_ai.api import traffic_reports as TRA

        class Ctx(FakeZbx):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        with mock.patch.object(TRA, "_zabbix", lambda: Ctx()), mock.patch.object(TRA, "report_dir", lambda: self.root / "rep"):
            r = self.c.post("/api/reports/traffic/weekly/draft", json={"channel": "email", "to": ["boss@example.com"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(OB.get(r.json()["id"])["status"], "pending")
        self.assertEqual(self.c.post("/api/reports/traffic/yearly/draft", json={}).status_code, 400)


if __name__ == "__main__":
    unittest.main()

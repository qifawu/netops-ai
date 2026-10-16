"""脚本库（待审 → 批准 → 取用）与两层分流。脚本库一律写临时目录，不碰真实 records。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from netops_ai.inspection import check_script as CS
from netops_ai.inspection import layer_router as LR
from netops_ai.inspection import script_registry as SR
from tests.test_check_script import DIFF_SCRIPT

NL = chr(10)
OK_TEST = {"ok": True, "plausible": True, "cases": [1, 2, 3], "problems": []}


class Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.root = Path(self._d.name)


class TestRegistry(Base):
    def test_待审不能取用_批准后可取_留痕齐全(self):
        rec = SR.submit("p.rt", DIFF_SCRIPT, goal="路由表逐条比", model="m1", self_test=OK_TEST, root=self.root)
        self.assertEqual((rec["status"], rec["version"], rec["created_by"]), ("pending", 1, "assistant"))
        self.assertIsNone(SR.get_approved("p.rt", root=self.root))
        ap = SR.approve("p.rt", 1, "owner", root=self.root)
        self.assertEqual((ap["status"], ap["approved_by"]), ("approved", "owner"))
        got = SR.get_approved("p.rt", root=self.root)
        self.assertEqual((got["sha256"], got["goal"], got["model"]), (CS.sha256(DIFF_SCRIPT), "路由表逐条比", "m1"))
        self.assertTrue(got["approved_at"] and got["history"])

    def test_自测没过_不能批准(self):
        SR.submit("p.rt", DIFF_SCRIPT, goal="g", self_test={"ok": False}, root=self.root)
        with self.assertRaises(SR.RegistryError):
            SR.approve("p.rt", 1, "owner", root=self.root)

    def test_批准必须有批准人(self):
        SR.submit("p.rt", DIFF_SCRIPT, goal="g", self_test=OK_TEST, root=self.root)
        with self.assertRaises(SR.RegistryError):
            SR.approve("p.rt", 1, " ", root=self.root)

    def test_静态检查不过的源码进不了库(self):
        with self.assertRaises(SR.RegistryError):
            SR.submit("p.bad", "import os" + NL + "def evaluate(ctx):" + NL + "    return {}", goal="g", root=self.root)

    def test_文件被改_源码或签名或状态_就当没批准(self):
        SR.submit("p.rt", DIFF_SCRIPT, goal="g", self_test=OK_TEST, root=self.root)
        SR.approve("p.rt", 1, "owner", root=self.root)
        path = self.root / "p.rt" / "v1.json"
        rec = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**rec, "source": rec["source"] + NL + "# tampered"}), encoding="utf-8")
        self.assertIsNone(SR.get_approved("p.rt", root=self.root))
        path.write_text(json.dumps({**rec, "signature": "0" * 64}), encoding="utf-8")
        self.assertIsNone(SR.get_approved("p.rt", root=self.root))
        path.write_text(json.dumps({**rec, "status": "pending"}), encoding="utf-8")
        self.assertIsNone(SR.get_approved("p.rt", root=self.root))

    def test_改源码是新版本_旧批准版不受影响_撤销后不可用(self):
        SR.submit("p.rt", DIFF_SCRIPT, goal="g", self_test=OK_TEST, root=self.root)
        SR.approve("p.rt", 1, "owner", root=self.root)
        v2 = SR.submit("p.rt", DIFF_SCRIPT + NL + "# v2", goal="g2", self_test=OK_TEST, root=self.root)
        self.assertEqual((v2["version"], v2["status"]), (2, "pending"))
        self.assertEqual(SR.get_approved("p.rt", root=self.root)["version"], 1)
        SR.approve("p.rt", 2, "owner", root=self.root)
        self.assertEqual(SR.get_approved("p.rt", root=self.root)["version"], 2)
        SR.revoke("p.rt", 2, "owner", "回退", root=self.root)
        self.assertEqual(SR.get_approved("p.rt", root=self.root)["version"], 1)
        SR.revoke("p.rt", 1, "owner", root=self.root)
        self.assertIsNone(SR.get_approved("p.rt", root=self.root))

    def test_相同源码不重复建版本_拒绝后不能再批准(self):
        a = SR.submit("p.rt", DIFF_SCRIPT, goal="g", self_test=OK_TEST, root=self.root)
        b = SR.submit("p.rt", DIFF_SCRIPT, goal="g", self_test=OK_TEST, root=self.root)
        self.assertEqual(a["version"], b["version"])
        SR.reject("p.rt", 1, "owner", "不要", root=self.root)
        with self.assertRaises(SR.RegistryError):
            SR.approve("p.rt", 1, "owner", root=self.root)

    def test_非法id和列表(self):
        with self.assertRaises(SR.RegistryError):
            SR.submit("../evil", DIFF_SCRIPT, goal="g", root=self.root)
        SR.submit("p.rt", DIFF_SCRIPT, goal="g", self_test=OK_TEST, root=self.root)
        SR.approve("p.rt", 1, "owner", root=self.root)
        rows = SR.list_scripts(self.root)
        self.assertEqual((rows[0]["script_id"], rows[0]["usable_version"]), ("p.rt", 1))
        self.assertEqual(SR.script_key("My Plan", "Route Diff"), "my-plan.route-diff")


class TestRouter(Base):
    def test_一次性且不能规则化_走AI(self):
        key = LR.fingerprint("show ip route", "anomaly")
        r = LR.decide(key, root=self.root)
        self.assertEqual((r["layer"], r["via"]), ("ai", "ai"))

    def test_重复达到阈值_沉淀脚本(self):
        key = LR.fingerprint("show ip arp", "arp-change")
        for _ in range(3):
            LR.record(key, root=self.root)
        r = LR.decide(key, root=self.root)
        self.assertEqual((r["layer"], r["via"], r["repeats"]), ("script", "script", 3))

    def test_窗口外的不算(self):
        key = LR.fingerprint("show ip arp", "x")
        old = datetime.now(timezone.utc) - timedelta(days=LR.WINDOW_DAYS + 5)
        for _ in range(5):
            LR.record(key, now=old, root=self.root)
        self.assertEqual(LR.count(key, root=self.root), 0)
        self.assertEqual(LR.decide(key, root=self.root)["layer"], "ai")

    def test_可规则化_或_现成模板能表达(self):
        key = LR.fingerprint("show ip route", "route-diff")
        self.assertEqual(LR.decide(key, rule_expressible=True, root=self.root)["via"], "script")
        self.assertEqual(LR.decide(key, template_covers=True, root=self.root)["via"], "template")

    def test_已有批准脚本_直接复用(self):
        key = LR.fingerprint("show ip route", "route-diff")
        SR.submit(key, DIFF_SCRIPT, goal="g", self_test=OK_TEST, root=self.root)
        SR.approve(key, 1, "owner", root=self.root)
        r = LR.decide(key, root=self.root)
        self.assertEqual((r["via"], r["script_version"]), ("script", 1))


class TestSandboxCsv(unittest.TestCase):
    def test_csv可用_读数据不碰文件(self):
        src = NL.join(["import csv", "def evaluate(ctx):", "    rows = list(csv.reader(ctx['output'].splitlines()))",
                       "    return {'status': 'pass', 'summary': str(len(rows)), 'metrics': {'rows': len(rows)}}"])
        r = CS.run_script(src, {"output": "a,b" + NL + "c,d", "previous": None, "previous_run": None, "device": "D", "params": {}})
        self.assertEqual(r["metrics"]["rows"], 2)


if __name__ == "__main__":
    unittest.main()

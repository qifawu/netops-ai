"""软件版本合规提醒：版本比较、按清单核对、报告块、提醒草稿（进草稿箱不发送）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from netops_ai import outbox
from netops_ai.inspection import version_policy as VP


def v(raw, product="ios"):
    return {"product": product, "version": raw.lower(), "raw": raw}


POLICY = [{"devices": ["A1"], "product": "ios", "target": "15.2(7)E4", "note": "接入层"},
          {"devices": ["*"], "product": "ios", "target": "15.9(3)M9", "note": ""}]


class TestCompare(unittest.TestCase):
    def test_按段比较_数字段比大小(self):
        self.assertEqual(VP.compare("15.9(3)m8", "15.9(3)m9"), -1)
        self.assertEqual(VP.compare("15.9(3)m10", "15.9(3)m9"), 1)  # 10 > 9，不是字符串比较
        self.assertEqual(VP.compare("15.9(3)m9", "15.9(3)m9"), 0)
        self.assertEqual(VP.compare("15.8(3)m9", "15.9(3)m1"), -1)
        self.assertEqual(VP.compare("17.3.4a", "17.3.4"), 1)

    def test_认不出的不比(self):
        self.assertIsNone(VP.compare("abc", "15.9"))
        self.assertIsNone(VP.compare("15.9", ""))


class TestEvaluate(unittest.TestCase):
    def test_落后_达标_更高_无法比较_没设目标_取版本失败(self):
        rows = VP.evaluate({"D1": v("15.9(3)M8"), "D2": v("15.9(3)M9"), "D3": v("15.9(3)M10"), "A1": v("15.2(7)E3"),
                            "X1": v("17.3.4a", "ios_xe"), "E1": {"error": "unreachable"}}, POLICY)
        by = {r["device"]: r for r in rows}
        self.assertEqual([by[k]["status"] for k in ("D1", "D2", "D3")], ["behind", "ok", "ahead"])
        self.assertEqual((by["A1"]["status"], by["A1"]["target"], by["A1"]["note"]), ("behind", "15.2(7)E4", "接入层"))  # 先匹配到的条目生效
        self.assertEqual(by["X1"]["status"], "no_policy")  # 清单里没有 ios_xe
        self.assertEqual((by["E1"]["status"], by["E1"]["note"]), ("unknown", "unreachable"))

    def test_清单校验(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.yaml"
            p.write_text('- product: ios\n  target: "15.9(3)M9"\n', encoding="utf-8")
            self.assertEqual(VP.load_policy(p)[0]["devices"], ["*"])
            p.write_text("- product: ios\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                VP.load_policy(p)
            self.assertEqual(VP.load_policy(Path(d) / "none.yaml"), [])


class TestReport(unittest.TestCase):
    def rows(self):
        return VP.evaluate({"D1": v("15.9(3)M8"), "D2": v("15.9(3)M9")}, POLICY)

    def test_块和提醒文字(self):
        blocks = VP.to_blocks(self.rows(), {"D1": 31, "D2": 31})
        self.assertIn("落后于目标版本 1 台", blocks[1][1])
        table = blocks[2]
        self.assertEqual(table[1][4], "命中 CVE")
        self.assertEqual([r[3] for r in table[2]], ["落后", "达标"])
        txt = VP.reminder_text(self.rows(), {"D1": 31})
        self.assertIn("D1：当前 15.9(3)M8，目标 15.9(3)M9，该版本命中 31 条 CVE", txt)
        self.assertIn("不会自动升级", txt)

    def test_提醒草稿_只在有落后时出_不发送(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ok = VP.evaluate({"D2": v("15.9(3)M9")}, POLICY)
            self.assertIsNone(VP.create_reminder_draft(ok, root=root))
            draft = VP.create_reminder_draft(self.rows(), channel="feishu", root=root)
            self.assertEqual((draft["status"], draft["kind"], draft["channel"]), ("pending", "version-reminder", "feishu"))
            self.assertEqual(len(outbox.list_drafts("pending", root)), 1)


if __name__ == "__main__":
    unittest.main()

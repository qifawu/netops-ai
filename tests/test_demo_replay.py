"""离线回放工具 `tools/demo_replay.py` 和随仓库带的三条合成示例。不连任何外部系统。"""
import json
import unittest
from pathlib import Path

from tools import demo_replay


def _load(name_part: str) -> dict:
    path = next(p for p in sorted(demo_replay.DEFAULT_DIR.glob("*.json")) if name_part in p.name)
    return json.loads(path.read_text(encoding="utf-8"))


class TestExamples(unittest.TestCase):
    def test_有三条示例(self):
        self.assertEqual(len(list(demo_replay.DEFAULT_DIR.glob("*.json"))), 3)

    def test_正常样例证据全部逐字核过且业务规则无违反(self):
        r = demo_replay.replay(_load("interface-admin-down"))
        self.assertEqual([v.grade for v in r["verdicts"]], ["verbatim", "verbatim"])
        self.assertEqual(r["violations"], [])

    def test_判不出的样例诚实承认_卡片是橙色_规则无违反(self):
        r = demo_replay.replay(_load("ospf-neighbor-timeout"))
        self.assertEqual(r["violations"], [])
        self.assertEqual(r["card"]["header"]["template"], "orange")
        self.assertTrue(all(v.verified for v in r["verdicts"]))

    def test_反例里编造的证据必须被抓出来(self):
        r = demo_replay.replay(_load("fabricated-evidence"))
        grades = [v.grade for v in r["verdicts"]]
        self.assertEqual(grades.count("fabricated"), 1)
        self.assertEqual(grades.count("verbatim"), 1)
        self.assertTrue(r["violations"], "置信度 high 但还有没排除的方向，业务规则必须报")

    def test_所有示例的证据原文都能在上下文里找到_除了故意编造的那条(self):
        for path in sorted(demo_replay.DEFAULT_DIR.glob("*.json")):
            rec = json.loads(path.read_text(encoding="utf-8"))
            fabricated = sum(1 for v in demo_replay.replay(rec)["verdicts"] if v.grade == "fabricated")
            self.assertEqual(fabricated, 1 if "fabricated" in path.name else 0, path.name)

    def test_命令行入口跑得通(self):
        self.assertEqual(demo_replay.main([]), 0)
        self.assertEqual(demo_replay.main(["--card"]), 0)

    def test_没有记录时返回2(self):
        import tempfile

        empty = Path(tempfile.mkdtemp())
        original = demo_replay.DEFAULT_DIR
        demo_replay.DEFAULT_DIR = empty
        try:
            self.assertEqual(demo_replay.main([]), 2)
        finally:
            demo_replay.DEFAULT_DIR = original


if __name__ == "__main__":
    unittest.main()

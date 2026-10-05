"""Offline replay tool `tools/demo_replay.py` and the synthetic examples shipped with the repo. No external systems."""
import json
import tempfile
import unittest
from pathlib import Path

from tools import demo_replay


def _load(name_part: str) -> dict:
    path = next(p for p in sorted(demo_replay.DEFAULT_DIR.glob("*.json")) if name_part in p.name)
    return json.loads(path.read_text(encoding="utf-8"))


class TestExamples(unittest.TestCase):
    def test_至少有两条示例(self):
        self.assertGreaterEqual(len(list(demo_replay.DEFAULT_DIR.glob("*.json"))), 2)

    def test_正常样例渲染出绿色卡片(self):
        r = demo_replay.replay(_load("interface-admin-down"))
        self.assertEqual(r["card"]["header"]["template"], "green")
        self.assertTrue(r["analysis"]["evidence"])

    def test_判不出的样例卡片是橙色(self):
        r = demo_replay.replay(_load("ospf-neighbor-timeout"))
        self.assertEqual(r["card"]["header"]["template"], "orange")

    def test_命令行入口跑得通(self):
        self.assertEqual(demo_replay.main([]), 0)
        self.assertEqual(demo_replay.main(["--card"]), 0)

    def test_没有记录时返回2(self):
        empty = Path(tempfile.mkdtemp())
        original = demo_replay.DEFAULT_DIR
        demo_replay.DEFAULT_DIR = empty
        try:
            self.assertEqual(demo_replay.main([]), 2)
        finally:
            demo_replay.DEFAULT_DIR = original


if __name__ == "__main__":
    unittest.main()

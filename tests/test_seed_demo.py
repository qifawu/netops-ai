"""`tools/seed_demo.py`: fill records/ with the synthetic examples. No external systems."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from tools import seed_demo


class TestSeedDemo(unittest.TestCase):
    def test_写入全部示例且不覆盖已有文件(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp)
            self.assertEqual(seed_demo.main(["--records-dir", str(dst)]), 0)
            written = sorted(p.name for p in dst.glob("alert-*.json"))
            self.assertGreaterEqual(len(written), 2)
            first = dst / written[0]
            first.write_text('{"keep": true}', encoding="utf-8")
            self.assertEqual(seed_demo.main(["--records-dir", str(dst)]), 0)
            self.assertEqual(json.loads(first.read_text(encoding="utf-8")), {"keep": True})

    def test_时间戳平移到最近几小时(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp)
            seed_demo.main(["--records-dir", str(dst)])
            now = time.time()
            for path in dst.glob("alert-*.json"):
                rec = json.loads(path.read_text(encoding="utf-8"))
                age_hours = (now - rec["alert_clock"]) / 3600
                self.assertTrue(0.5 <= age_hours <= 7, (path.name, age_hours))
                self.assertEqual(rec["webhook_payload"]["clock"], str(rec["alert_clock"]))

    def test_没有示例时返回2(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(seed_demo.main(["--examples-dir", tmp, "--records-dir", tmp]), 2)


if __name__ == "__main__":
    unittest.main()

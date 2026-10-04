"""巡检历史 / 对比 / 导出。全部写临时目录，不碰 records/。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from netops_ai.api import dashboard
from netops_ai.api.app import app
from netops_ai.inspection import export, history


def _trend(*keys, at="2026-10-03 10:00:00 UTC"):
    return {
        "scanned_at": at, "scanned_hosts": 8, "scanned_items": 100, "items_with_data": 90,
        "findings": [{"kind": "trend", "host_name": h, "item_name": f"name-{k}", "item_key": k,
                      "finding": {"huge": "x" * 100}, "series": [[1, 2]] * 50} for h, k in keys],
    }


def _snap(status, at="2026-10-03 10:00:00 UTC"):
    check = {"check": "ospf", "status": status, "summary": f"邻居 {status}", "evidence": ["4.4.4.4  0 EXSTART/DR  10.0.1.18  Gi0/3"], "command": "show ip ospf neighbor"}
    return {"checked_at": at, "devices": [{"name": "D1", "role": "aggregation", "reachable": True, "checks": [check]}],
            "counters": {}, "summary": {"devices": 1, "bad": int(status == "bad"), "warn": int(status == "warn"), "ok": int(status == "ok"), "skip": 0, "unreachable": 0}}


class TestHistory(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netops_insp_hist_"))

    def _save(self, kind, payload, minutes):
        return history.save(kind, payload, self.dir, at=datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes))

    def test_趋势历史是压缩版_不带曲线点(self):
        path = self._save("trend", _trend(("V1", "k1")), 0)
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("series", text)
        self.assertNotIn("huge", text)
        self.assertIn("k1", text)

    def test_最近两份新的在前_坏文件跳过(self):
        for i in range(3):
            self._save("status", _snap("ok", at=f"t{i}"), i)
        (self.dir / "status-99999999T999999000000Z.json").write_text("{坏", encoding="utf-8")
        recent = history.load_recent("status", self.dir, 2)
        self.assertEqual(len(recent), 2)  # 坏文件字典序最新，被跳过
        self.assertEqual([r["checked_at"] for r in recent], ["t2", "t1"])

    def test_趋势对比_新增和消失(self):
        prev, cur = history.compact_trend(_trend(("V1", "a"), ("V1", "b"))), history.compact_trend(_trend(("V1", "b"), ("D1", "c")))
        d = history.diff_trend(prev, cur)
        self.assertEqual((d["new_count"], d["gone_count"]), (1, 1))
        self.assertEqual(d["new"][0]["key"], "c")
        self.assertEqual(d["gone"][0]["key"], "a")
        self.assertIsNone(history.diff_trend(None, cur))  # 没有上一份就不画对比，不是「全是新增」

    def test_状态对比_变差和恢复(self):
        worse = history.diff_status(_snap("ok"), _snap("bad"))
        self.assertEqual(len(worse["worse"]), 1)
        self.assertEqual(worse["worse"][0]["to"], "bad")
        better = history.diff_status(_snap("bad"), _snap("ok"))
        self.assertEqual(len(better["better"]), 1)
        same = history.diff_status(_snap("ok"), _snap("ok"))
        self.assertEqual((same["worse"], same["better"]), ([], []))

    def test_列表两类混排_新的在前(self):
        self._save("trend", _trend(("V1", "a"), at="2026-10-03 10:00:00 UTC"), 0)
        self._save("status", _snap("bad", at="2026-10-03 11:00:00 UTC"), 60)
        runs = history.list_runs(self.dir)
        self.assertEqual([r["kind"] for r in runs], ["status", "trend"])
        self.assertIn("1 项异常", runs[0]["note"])


class TestExport(unittest.TestCase):
    def _view(self):
        return {
            "scanned_at": "2026-10-03 10:00:00 UTC", "scanned_hosts": 8, "scanned_items": 100, "finding_count": 1,
            "groups": [{"label": "持续走坏", "why": "一直往坏的方向走", "rows": [{"host": "V1", "item": "CPU | util", "key": "cpu", "detail": "上升 <20%>"}]}],
            "advice": {"summary": "总结", "needs_attention": [{"host": "V1", "item_key": "cpu", "severity": "要紧", "why": "因为", "suggestion": "看日志", "evidence": "原话一行"}],
                       "ignorable": [], "cannot_tell": []},
            "status": _snap("bad", at="2026-10-03 10:05:00 UTC"),
            "diff": {"status": history.diff_status(_snap("ok"), _snap("bad")), "trend": None},
        }

    def test_markdown含各节_证据原样放代码块_竖线转义(self):
        md = export.render_markdown(self._view())
        for part in ("# 网络巡检报告", "## 3. 状态巡检", "## 2. 与上一次对比", "变差：D1", "4.4.4.4  0 EXSTART/DR  10.0.1.18  Gi0/3", "原话一行", "CPU \\| util"):
            self.assertIn(part, md)

    def test_html自包含且转义(self):
        html = export.render_html(self._view())
        self.assertIn("<style>", html)
        self.assertIn("&lt;20%&gt;", html)
        self.assertNotIn("<20%>", html)
        self.assertIn('class="bad"', html)

    def test_没有任何记录也能渲染(self):
        self.assertIn("还没有巡检记录", export.render_markdown({}))


class TestApiAndView(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="netops_insp_api_"))
        self.patches = [
            mock.patch.object(dashboard, "RECORDS_DIR", self.tmp),
            mock.patch.object(dashboard, "INSPECTION_LATEST_PATH", self.tmp / "inspection-latest.json"),
            mock.patch.object(dashboard, "INSPECTION_STATUS_PATH", self.tmp / "inspection-status-latest.json"),
        ]
        for p in self.patches:
            p.start()
        self.client = TestClient(app)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_视图带状态_对比_历史_第二轮才有对比(self):
        raw = {**_trend(("V1", "a")), "errors": []}
        dashboard._save_history("trend", raw)
        dashboard._write_json_atomic(dashboard.INSPECTION_STATUS_PATH, _snap("ok"))
        dashboard._save_history("status", _snap("ok"))
        view = dashboard.build_inspection_view(raw)
        self.assertEqual(view["status"]["summary"]["ok"], 1)
        self.assertIsNone(view["diff"]["trend"])  # 只有一轮：不画对比
        self.assertEqual(len(view["history"]), 2)
        raw2 = {**_trend(("V1", "a"), ("D1", "b"), at="2026-10-03 11:00:00 UTC"), "errors": []}
        dashboard._save_history("trend", raw2)
        dashboard._save_history("status", _snap("bad", at="2026-10-03 11:00:00 UTC"))
        view2 = dashboard.build_inspection_view(raw2)
        self.assertEqual(view2["diff"]["trend"]["new_count"], 1)
        self.assertEqual(len(view2["diff"]["status"]["worse"]), 1)

    def test_没状态快照时视图仍正常(self):
        view = dashboard.build_inspection_view({**_trend(("V1", "a")), "errors": []})
        self.assertIsNone(view["status"])

    def test_导出接口(self):
        raw = {**_trend(("V1", "a")), "errors": []}
        (self.tmp / "inspection-latest.json").write_text(__import__("json").dumps(raw), encoding="utf-8")
        md = self.client.get("/api/inspection/export", params={"format": "md"})
        self.assertEqual(md.status_code, 200)
        self.assertIn("attachment", md.headers["content-disposition"])
        self.assertIn("# 网络巡检报告", md.text)
        html = self.client.get("/api/inspection/export", params={"format": "html"})
        self.assertIn("<!doctype html>", html.text)
        self.assertEqual(self.client.get("/api/inspection/export", params={"format": "pdf"}).status_code, 400)

    def test_没跑过巡检导出404(self):
        self.assertEqual(self.client.get("/api/inspection/export").status_code, 404)

    def test_完整巡检一边失败不影响另一边(self):
        with mock.patch.object(dashboard, "run_inspection_and_persist", side_effect=RuntimeError("zbx down")), \
                mock.patch.object(dashboard, "run_status_and_persist", return_value={}) as status:
            errors = dashboard.run_full_inspection()
        self.assertIn("zbx down", errors["trend"])
        self.assertNotIn("status", errors)
        status.assert_called_once()


if __name__ == "__main__":
    unittest.main()

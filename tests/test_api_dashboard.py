"""M4 看板的数据层：`list_records`/`load_record`/`render_markdown_report`
纯读文件、纯字符串拼接，不碰网络，用临时目录测。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from netops_ai.api import dashboard


def _write_record(records_dir: Path, eventid: str, **overrides) -> None:
    record = {
        "eventid": eventid,
        "started_at": "2026-09-19T00:00:00Z",
        "finished_at": overrides.pop("finished_at", "2026-09-19T00:01:00Z"),
        "device_fetch_error": overrides.pop("device_fetch_error", ""),
        "zabbix_fetch_error": "",
        "analysis_parsed": overrides.pop("analysis_parsed", {"root_cause": "x", "confidence": "high", "evidence": []}),
        "analysis_error": overrides.pop("analysis_error", ""),
    }
    (records_dir / f"alert-{eventid}.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


class TestListRecords(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.records_dir = Path(self.tmp.name)
        self._patch = mock.patch.object(dashboard, "RECORDS_DIR", self.records_dir)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_没有记录时返回空列表(self):
        self.assertEqual(dashboard.list_records(), [])

    def test_按finished_at倒序(self):
        _write_record(self.records_dir, "1", finished_at="2026-09-19T00:01:00Z")
        _write_record(self.records_dir, "2", finished_at="2026-09-19T00:02:00Z")
        records = dashboard.list_records()
        self.assertEqual([r["eventid"] for r in records], ["2", "1"])

    def test_坏掉的json文件跳过不崩(self):
        (self.records_dir / "alert-broken.json").write_text("not json", encoding="utf-8")
        _write_record(self.records_dir, "1")
        records = dashboard.list_records()
        self.assertEqual(len(records), 1)

    def test_low置信度带undistinguishable标记(self):
        _write_record(
            self.records_dir, "1",
            analysis_parsed={
                "root_cause": "x", "confidence": "low",
                "undistinguishable_candidates": [{"candidates": ["a", "b"]}],
            },
        )
        records = dashboard.list_records()
        self.assertTrue(records[0]["has_undistinguishable"])


class TestLoadRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.records_dir = Path(self.tmp.name)
        self._patch = mock.patch.object(dashboard, "RECORDS_DIR", self.records_dir)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_找不到返回None(self):
        self.assertIsNone(dashboard.load_record("999"))

    def test_找到返回全文不做摘要(self):
        _write_record(self.records_dir, "1", analysis_parsed={"root_cause": "详细结论", "confidence": "high", "evidence": [{"claim": "c", "source": "s", "source_from": "zabbix"}]})
        record = dashboard.load_record("1")
        self.assertEqual(record["analysis_parsed"]["evidence"][0]["source"], "s")


class TestRenderMarkdownReport(unittest.TestCase):
    def test_基本字段都出现在md里(self):
        record = {
            "eventid": "1",
            "started_at": "t0",
            "finished_at": "t1",
            "analysis_parsed": {
                "root_cause": "Gi0/1 被人为 shutdown",
                "confidence": "high",
                "evidence": [{"claim": "c1", "source": "s1", "source_from": "device"}],
                "hypothesis_checklist": {
                    "local_action": {
                        "status": "supported", "reason": "r",
                        "counter_evidence": [],
                    }
                },
                "undistinguishable_candidates": [],
            },
        }
        md = dashboard.render_markdown_report(record)
        self.assertIn("Gi0/1 被人为 shutdown", md)
        self.assertIn("c1", md)
        self.assertIn("本端有人动过配置", md)
        self.assertNotIn("local_action", md)

    def test_没有分析结果也不崩(self):
        record = {"eventid": "1", "started_at": "t0", "finished_at": "t1", "analysis_error": "限流了"}
        md = dashboard.render_markdown_report(record)
        self.assertIn("（无结论）", md)


class TestInspectionPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.records_dir = Path(self.tmp.name)
        self._patch_dir = mock.patch.object(dashboard, "RECORDS_DIR", self.records_dir)
        self._patch_dir.start()
        self.addCleanup(self._patch_dir.stop)
        self._patch_path = mock.patch.object(dashboard, "INSPECTION_LATEST_PATH", self.records_dir / "inspection-latest.json")
        self._patch_path.start()
        self.addCleanup(self._patch_path.stop)

    def test_没跑过巡检返回None(self):
        self.assertIsNone(dashboard.load_latest_inspection())

    def test_跑完之后能读回来(self):
        fake_report = mock.MagicMock(scanned_hosts=1, scanned_items=2, items_with_data=2, errors=[], findings=[])
        with mock.patch("netops_ai.inspection.scan.scan_all_hosts", return_value=fake_report), \
             mock.patch("netops_ai.zabbix.client.ZabbixClient") as mock_zbx_cls:
            mock_zbx_cls.return_value.__enter__.return_value = mock.MagicMock()
            dashboard.run_inspection_and_persist()
        latest = dashboard.load_latest_inspection()
        self.assertEqual(latest["scanned_hosts"], 1)

    def test_finding顶层带kind字段(self):
        # M2.4 F2：判方指出之前 findings 只有 host_name/item_name/item_key/finding
        # 四个字段，统计 trend/flap 各多少条得挖进嵌套的 finding 字段
        from netops_ai.inspection.detectors import SelfHealingFlapFinding
        from netops_ai.inspection.scan import ItemFinding, ScanReport

        finding = SelfHealingFlapFinding(
            kind="self_healing_flap", flap_count=2, avg_recovery_seconds=200, max_recovery_seconds=300, reason="r"
        )
        fake_report = ScanReport(
            scanned_hosts=1, scanned_items=1, items_with_data=1, errors=[],
            findings=[ItemFinding(host_name="V1", item_name="Gi0/1", item_key="k", finding=finding)],
        )
        with mock.patch("netops_ai.inspection.scan.scan_all_hosts", return_value=fake_report), \
             mock.patch("netops_ai.zabbix.client.ZabbixClient") as mock_zbx_cls:
            mock_zbx_cls.return_value.__enter__.return_value = mock.MagicMock()
            dashboard.run_inspection_and_persist()
        latest = dashboard.load_latest_inspection()
        self.assertEqual(latest["findings"][0]["kind"], "self_healing_flap")


if __name__ == "__main__":
    unittest.main()

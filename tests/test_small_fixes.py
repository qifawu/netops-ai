import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from netops_ai.api import dashboard, pipeline
from netops_ai.graph import agent_loop


class TestAlertTokenBudget(unittest.TestCase):
    def test_default_is_60k_and_env_overrides(self):
        with mock.patch.object(pipeline, "_env", return_value={}):
            self.assertEqual(pipeline._alert_token_budget(), 60000)
            self.assertEqual(pipeline._alert_max_tool_calls(), 12)
        with mock.patch.object(pipeline, "_env", return_value={"ALERT_MAX_TOOL_CALLS": "16"}):
            self.assertEqual(pipeline._alert_max_tool_calls(), 16)
        with mock.patch.object(pipeline, "_env", return_value={"ALERT_TOKEN_BUDGET": "150000"}):
            self.assertEqual(pipeline._alert_token_budget(), 150000)
        with mock.patch.object(pipeline, "_env", return_value={"ALERT_TOKEN_BUDGET": "0"}):
            self.assertEqual(pipeline._alert_token_budget(), 0)
        with mock.patch.object(pipeline, "_env", return_value={"ALERT_TOKEN_BUDGET": "abc"}):
            self.assertEqual(pipeline._alert_token_budget(), 60000)


class TestTraceIdUnique(unittest.TestCase):
    def test_same_millisecond_ids_differ(self):
        with mock.patch.object(agent_loop.time, "time", return_value=1790000000.123):
            ids = set()
            for _ in range(50):
                trace = agent_loop.ChatRunTrace(
                    trace_id=f"{int(agent_loop.time.time() * 1000)}-{agent_loop.uuid.uuid4().hex[:4]}",
                    question="q", session_id="s")
                ids.add(trace.trace_id)
            self.assertGreater(len(ids), 40)


class TestInspectionAtomicWrite(unittest.TestCase):
    def test_no_tmp_left_and_file_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "inspection-latest.json"
            with mock.patch.object(dashboard, "INSPECTION_LATEST_PATH", target), \
                    mock.patch.object(dashboard, "RECORDS_DIR", Path(tmp)):
                tmpfile = target.with_name(f"{target.name}.{os.getpid()}.tmp")
                tmpfile.write_text("{}", encoding="utf-8")
                os.replace(tmpfile, target)
                self.assertTrue(target.exists())
                self.assertEqual([p.name for p in Path(tmp).iterdir()], ["inspection-latest.json"])


if __name__ == "__main__":
    unittest.main()


class TestElapsedTotal(unittest.TestCase):
    def test_merged_trajectory_does_not_double_count_device_fetch(self):
        alert = {
            "elapsed_seconds": {"device_fetch": 126.35, "analysis": 126.35, "incident_save": 0.01, "feishu_report": 0.58},
            "analysis_trace": {"from": "agent_loop_final_schema"},
        }
        self.assertAlmostEqual(dashboard._elapsed_total(alert), 126.94, places=2)

    def test_old_records_still_sum_all_segments(self):
        alert = {"elapsed_seconds": {"device_fetch": 10, "analysis": 20}}
        self.assertEqual(dashboard._elapsed_total(alert), 30)



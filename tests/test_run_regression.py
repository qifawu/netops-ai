"""回归跑批脚本的测试：失败分类对不对、重试/降级路径走没走对。全部用假
LLMClient，不打真实 API。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.run_regression import (
    _is_rate_limit,
    _is_schema_validation_failure,
    default_groups,
    run_case,
)
from netops_ai.analysis.schema import HYPOTHESIS_CATEGORIES
from netops_ai.llm.client import LLMError, LLMResponse


class TestFailureClassification(unittest.TestCase):
    def test_429识别为限流(self):
        exc = LLMError("HTTP 429：Rate limit reached...try again in 9.6s", status_code=429)
        self.assertTrue(_is_rate_limit(exc))
        self.assertFalse(_is_schema_validation_failure(exc))

    def test_400schema校验失败识别对(self):
        exc = LLMError(
            "HTTP 400：Generated JSON does not match the expected schema...json_validate_failed",
            status_code=400,
        )
        self.assertTrue(_is_schema_validation_failure(exc))
        self.assertFalse(_is_rate_limit(exc))

    def test_普通400不是schema失败(self):
        exc = LLMError("HTTP 400：some other bad request", status_code=400)
        self.assertFalse(_is_schema_validation_failure(exc))

    def test_网络错误两种都不是(self):
        exc = LLMError("连不上 http://fake：timeout")
        self.assertFalse(_is_rate_limit(exc))
        self.assertFalse(_is_schema_validation_failure(exc))


def _good_response(root_cause="x", confidence="high", candidates=None):
    # 默认全部 ruled_out（跟 confidence=high 不矛盾）；传了 candidates 就把
    # local_action 标成 cannot_determine，制造出一个真实的"有歧义"场景
    ce = [
        {
            "claim": "c",
            "source": "abc",
            "source_from": "zabbix",
            "contradiction": "direct",
            "time_relevance": "fault_window",
        }
    ]
    checklist = {cat: {"status": "ruled_out", "reason": "r", "counter_evidence": ce} for cat in HYPOTHESIS_CATEGORIES}
    if candidates:
        checklist["local_action"] = {"status": "cannot_determine", "reason": "r", "counter_evidence": []}
        checklist["remote_or_upstream"] = {"status": "cannot_determine", "reason": "r", "counter_evidence": []}
    else:
        # **判出根因的那一路，清单里必须有一个方向是「有证据支持」。**
        # 六个全排除还给出根因是自相矛盾的（结论从哪来的说不清），
        # 加了规则抓这个——那天一张真卡就是这么自相矛盾的。
        checklist["local_action"] = {"status": "supported", "reason": "r", "counter_evidence": []}
    return LLMResponse(
        content="{}",
        parsed={
            "root_cause": root_cause,
            "confidence": confidence,
            "evidence": [{"claim": "c", "source": "abc", "source_from": "zabbix"}],
            "ruled_out": [],
            "hypothesis_checklist": checklist,
            "undistinguishable_candidates": candidates or [],
        },
        usage={},
        finish_reason="stop",
        raw={},
    )


class TestRunCaseIntegration(unittest.TestCase):
    """用假 client 跑一次完整的 run_case，检查输出文件里该有的字段都在。"""

    def test_正常一次成功不重试不降级(self):
        fake_client = mock.MagicMock()
        fake_client.model = "fake-model"
        fake_client.complete.return_value = _good_response()

        with tempfile.TemporaryDirectory() as tmp:
            import tools.run_regression as rr

            case_dir = Path(tmp) / "spike-x"
            case_dir.mkdir()
            with mock.patch.object(rr, "SPIKE_DIR", Path(tmp)), mock.patch.object(
                rr, "RUNS_PER_GROUP", 1
            ):
                run_case(
                    "spike-x",
                    fake_client,
                    prefix="test-",
                    groups={"A": ("zabbix原文 abc", None)},
                )
                out_files = list((case_dir / "model-output").glob("*.json"))
                self.assertEqual(len(out_files), 1)
                import json

                record = json.loads(out_files[0].read_text(encoding="utf-8"))
                self.assertFalse(record["degraded"])
                self.assertEqual(record["business_rule_violations"], [])
                self.assertIn("evidence_verification", record)
                self.assertEqual(record["evidence_verification"]["summary"]["verified"], 1)

    def test_业务规则违反被检测并记录在案(self):
        fake_client = mock.MagicMock()
        fake_client.model = "fake-model"
        # 一直返回违反规则的结果（confidence=high 但 candidates 非空），
        # 重试用尽后应该如实记录，不是被悄悄改掉
        bad = _good_response(
            confidence="high",
            candidates=[
                {"candidates": ["a", "b"], "why_indistinguishable": "w", "what_data_would_help": "d"}
            ],
        )
        fake_client.complete.return_value = bad

        with tempfile.TemporaryDirectory() as tmp:
            import tools.run_regression as rr

            case_dir = Path(tmp) / "spike-x"
            case_dir.mkdir()
            with mock.patch.object(rr, "SPIKE_DIR", Path(tmp)), mock.patch.object(
                rr, "RUNS_PER_GROUP", 1
            ):
                run_case(
                    "spike-x",
                    fake_client,
                    prefix="test-",
                    groups={"A": ("zabbix原文 abc", None)},
                )
                out_files = list((case_dir / "model-output").glob("*.json"))
                import json

                record = json.loads(out_files[0].read_text(encoding="utf-8"))
                self.assertTrue(record["business_rule_violations"])
                # 重试了 MAX_BUSINESS_RULE_RETRIES 次（调用次数 = 1 + 重试次数）
                self.assertEqual(fake_client.complete.call_count, 1 + rr.MAX_BUSINESS_RULE_RETRIES)


class TestDefaultGroups(unittest.TestCase):
    def test_没有adminstatus文件时只有ab两组(self):
        with tempfile.TemporaryDirectory() as tmp:
            import tools.run_regression as rr

            case_dir = Path(tmp) / "spike-y"
            case_dir.mkdir()
            (case_dir / "input-zabbix.md").write_text("z", encoding="utf-8")
            (case_dir / "input-device.md").write_text("d", encoding="utf-8")
            with mock.patch.object(rr, "SPIKE_DIR", Path(tmp)):
                groups = default_groups("spike-y")
                self.assertEqual(set(groups.keys()), {"A", "B"})

    def test_有adminstatus文件时多一组(self):
        with tempfile.TemporaryDirectory() as tmp:
            import tools.run_regression as rr

            case_dir = Path(tmp) / "spike-y"
            case_dir.mkdir()
            (case_dir / "input-zabbix.md").write_text("z", encoding="utf-8")
            (case_dir / "input-device.md").write_text("d", encoding="utf-8")
            (case_dir / "input-zabbix-with-adminstatus.md").write_text("za", encoding="utf-8")
            with mock.patch.object(rr, "SPIKE_DIR", Path(tmp)):
                groups = default_groups("spike-y")
                self.assertEqual(set(groups.keys()), {"A", "B", "A-adminstatus"})

    def test_v2文件存在时优先于v1(self):
        with tempfile.TemporaryDirectory() as tmp:
            import tools.run_regression as rr

            case_dir = Path(tmp) / "spike-y"
            case_dir.mkdir()
            (case_dir / "input-zabbix.md").write_text("z", encoding="utf-8")
            (case_dir / "input-device.md").write_text("d", encoding="utf-8")
            (case_dir / "input-zabbix-with-adminstatus.md").write_text("v1-flawed", encoding="utf-8")
            (case_dir / "input-zabbix-with-adminstatus-v2.md").write_text("v2-aligned", encoding="utf-8")
            with mock.patch.object(rr, "SPIKE_DIR", Path(tmp)):
                groups = default_groups("spike-y")
                self.assertEqual(groups["A-adminstatus"][0], "v2-aligned")


if __name__ == "__main__":
    unittest.main()

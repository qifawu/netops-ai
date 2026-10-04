"""假设清单的 status 对模型用英文枚举、出来转回中文。

真跑 + 重放：中文枚举下 qwen3.8-flash 六个方向全标同一个值（5/5 全「已排除」，
包括它自己写「确认为根因」的那个），置信度被压成 low；英文枚举 4/4 判别正确。
"""
import unittest

from netops_ai.analysis import analyzer, schema


class StatusWireTests(unittest.TestCase):
    def test_schema_给模型的status枚举是英文(self):
        st = schema._HYPOTHESIS_ITEM_SCHEMA["properties"]["status"]
        self.assertEqual(st["enum"], ["supported", "ruled_out", "cannot_determine"])

    def test_normalize_statuses把英文转回中文(self):
        parsed = {"hypothesis_checklist": {
            "local_action": {"status": "supported"},
            "remote_or_upstream": {"status": "ruled_out"},
            "link_or_path_quality": {"status": "cannot_determine"},
            "local_hardware_or_resource": {"status": "有证据支持"},  # 已经是中文的原样保留
        }}
        schema.normalize_statuses(parsed)
        got = {k: v["status"] for k, v in parsed["hypothesis_checklist"].items()}
        self.assertEqual(got["local_action"], schema.STATUS_SUPPORTED)
        self.assertEqual(got["remote_or_upstream"], schema.STATUS_RULED_OUT)
        self.assertEqual(got["link_or_path_quality"], schema.STATUS_CANNOT_DETERMINE)
        self.assertEqual(got["local_hardware_or_resource"], schema.STATUS_SUPPORTED)

    def test_分析出口统一转中文(self):
        parsed = {"hypothesis_checklist": {"local_action": {"status": "supported", "reason": "x", "counter_evidence": []}}}
        out = analyzer._with_derived_confidence(parsed)
        self.assertEqual(out["hypothesis_checklist"]["local_action"]["status"], schema.STATUS_SUPPORTED)

    def test_不是dict或没有清单时不崩(self):
        self.assertIsNone(schema.normalize_statuses(None))
        self.assertEqual(schema.normalize_statuses({"x": 1}), {"x": 1})


if __name__ == "__main__":
    unittest.main()

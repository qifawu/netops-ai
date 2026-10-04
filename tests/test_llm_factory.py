from __future__ import annotations

import unittest

from netops_ai.llm.factory import analysis_schema_use_refs, detect_llm_route, key_prefix_provider


class TestLLMRouteDetection(unittest.TestCase):
    def test_explicit_provider_wins_over_base_url(self):
        route = detect_llm_route(
            {
                "LLM_PROVIDER": "gemini:native",
                "LLM_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai",
                "LLM_API_KEY": "AIza-example",
            }
        )
        self.assertEqual((route.provider, route.transport, route.source), ("gemini", "native", "LLM_PROVIDER"))

    def test_gemini_openai_compat_is_distinct_from_native(self):
        compat = detect_llm_route(
            {
                "LLM_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai",
                "LLM_API_KEY": "AIza-example",
            }
        )
        native = detect_llm_route(
            {
                "LLM_BASE_URL": "https://generativelanguage.googleapis.com/v1beta",
                "LLM_API_KEY": "AIza-example",
            }
        )
        self.assertEqual((compat.provider, compat.transport), ("gemini", "openai-compatible"))
        self.assertEqual((native.provider, native.transport), ("gemini", "native"))

    def test_key_prefix_is_diagnostic_only(self):
        route = detect_llm_route(
            {
                "LLM_BASE_URL": "https://generativelanguage.googleapis.com/v1beta",
                "LLM_API_KEY": "sk-ant-api03-example",
            }
        )
        self.assertEqual((route.provider, route.transport), ("gemini", "native"))
        self.assertEqual(route.key_prefix, "anthropic")
        self.assertTrue(route.warnings)

    def test_ambiguous_sk_prefix_is_unknown(self):
        self.assertEqual(key_prefix_provider("sk-example"), "unknown")

    def test_openai_uses_ref_schema(self):
        self.assertTrue(
            analysis_schema_use_refs(
                {
                    "LLM_PROVIDER": "openai",
                    "LLM_BASE_URL": "https://api.openai.com/v1",
                    "LLM_API_KEY": "sk-example",
                }
            )
        )

    def test_verified_gemini_openai_compat_uses_ref_schema(self):
        self.assertTrue(
            analysis_schema_use_refs(
                {
                    "LLM_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai",
                    "LLM_API_KEY": "AIza-example",
                }
            )
        )

    def test_unverified_generic_compat_provider_uses_expanded_schema(self):
        self.assertFalse(
            analysis_schema_use_refs(
                {
                    "LLM_BASE_URL": "https://llm.example.test/v1",
                    "LLM_API_KEY": "sk-example",
                }
            )
        )


if __name__ == "__main__":
    unittest.main()

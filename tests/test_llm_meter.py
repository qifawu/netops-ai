from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from netops_ai.llm.meter import (
    TokenBudget,
    TokenUsage,
    cost_cny,
    cost_usd,
    price_for,
    record_usage,
)


class TestPricing(unittest.TestCase):
    def test_longest_fragment_wins(self):
        """`gpt-4o` 不能把 `gpt-4o-mini` 抢走——两者差 16 倍价。"""
        self.assertEqual(price_for("gpt-4o-mini-2024-07-18"), (0.15, 0.60))
        self.assertEqual(price_for("gpt-4o-2024-11-20"), (2.50, 10.00))

    def test_date_suffix_tolerated(self):
        self.assertEqual(price_for("claude-haiku-4-5-20251001"), (1.00, 5.00))

    def test_unknown_model_returns_none_not_a_guess(self):
        """查不到价格必须返回 None。

        猜一个价格出来会被写进报告、拿去和人工成本比、用来做决策，而且没人
        会回头质疑它。没有数字比有个错数字安全。
        """
        self.assertIsNone(price_for("某个没见过的模型"))
        self.assertIsNone(cost_usd(TokenUsage(model="某个没见过的模型", prompt_tokens=10000)))
        self.assertIsNone(cost_cny(TokenUsage(model="某个没见过的模型", prompt_tokens=10000)))

    def test_env_override_beats_table(self):
        usage = TokenUsage(model="任意模型", prompt_tokens=1_000_000, completion_tokens=0)
        with mock.patch.dict(
            os.environ,
            {"LLM_PRICE_INPUT_USD_PER_M": "2", "LLM_PRICE_OUTPUT_USD_PER_M": "8"},
        ):
            self.assertAlmostEqual(cost_usd(usage), 2.0)

    def test_cost_math(self):
        usage = TokenUsage(model="gpt-4o-mini", prompt_tokens=1_000_000, completion_tokens=1_000_000)
        self.assertAlmostEqual(cost_usd(usage), 0.75)  # 0.15 + 0.60


class TestUsageParsing(unittest.TestCase):
    def test_openai_field_names(self):
        u = TokenUsage.from_api("m", {"prompt_tokens": 100, "completion_tokens": 20})
        self.assertEqual((u.prompt_tokens, u.completion_tokens, u.calls), (100, 20, 1))

    def test_anthropic_field_names(self):
        u = TokenUsage.from_api("m", {"input_tokens": 100, "output_tokens": 20})
        self.assertEqual((u.prompt_tokens, u.completion_tokens), (100, 20))

    def test_missing_usage_is_zero_calls(self):
        """没有 usage 就是 0 次调用，不能记成 1 次——否则账本会虚报调用数。"""
        self.assertEqual(TokenUsage.from_api("m", None).calls, 0)
        self.assertEqual(TokenUsage.from_api("m", {}).calls, 0)

    def test_add_accumulates(self):
        a = TokenUsage("m", 10, 2, 1)
        b = TokenUsage("m", 30, 5, 1)
        self.assertEqual(a.add(b).as_dict()["total_tokens"], 47)
        self.assertEqual(a.add(b).calls, 2)



class TestTokenBudget(unittest.TestCase):
    def test_charge_and_remaining(self):
        b = TokenBudget(limit=1000)
        b.charge(TokenUsage(prompt_tokens=300, completion_tokens=100))
        self.assertEqual(b.remaining, 600)
        self.assertFalse(b.exhausted)

    def test_would_exceed_predicts_before_spending(self):
        """闸门要能在发请求**之前**拦住，而不是花完了才说超了。"""
        b = TokenBudget(limit=1000)
        b.charge(TokenUsage(prompt_tokens=800))
        self.assertTrue(b.would_exceed(300))
        self.assertFalse(b.would_exceed(100))

    def test_hops_budget_would_not_catch_this(self):
        """4 跳不多，但每跳重发全量上下文，总量是首跳的 10 倍。

        这就是 hops 预算拦不住的那种爆炸：它数次数，不数体积。
        """
        b = TokenBudget(limit=60_000)
        for hop in range(1, 5):
            b.charge(TokenUsage(prompt_tokens=20_000 * hop, completion_tokens=500))
        self.assertTrue(b.exhausted)
        self.assertEqual(b.spent, 20_000 * 10 + 2_000)


class TestLedger(unittest.TestCase):
    def test_每次调用追加一行(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            with mock.patch.dict(os.environ, {"TOKEN_LEDGER_PATH": str(path)}):
                for _ in range(3):
                    record_usage(TokenUsage("gemini-2.5-flash", 8000, 1000, 1), eventid="1", tag="t")
            rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["total_tokens"], 9000)

    def test_recording_never_raises_even_if_path_is_unwritable(self):
        """记账失败绝不许把正事搞挂——和设备连不上、飞书发不出一个待遇。"""
        with mock.patch.dict(os.environ, {"TOKEN_LEDGER_PATH": "/nonexistent-root/x.jsonl"}):
            record_usage(TokenUsage("gpt-4o-mini", 10, 1, 1))  # 不抛就算过

    def test_empty_usage_is_not_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            with mock.patch.dict(os.environ, {"TOKEN_LEDGER_PATH": str(path)}):
                record_usage(TokenUsage())
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()


class TestEventidAttribution(unittest.TestCase):
    def test_告警管道设了当前告警_账本每行都带上(self):
        from netops_ai.llm.meter import CURRENT_EVENTID

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "l.jsonl"
            with mock.patch.dict(os.environ, {"TOKEN_LEDGER_PATH": str(path)}):
                token = CURRENT_EVENTID.set("93999")
                try:
                    record_usage(TokenUsage("m", 1, 1, 1))
                finally:
                    CURRENT_EVENTID.reset(token)
                record_usage(TokenUsage("m", 1, 1, 1))
            rows = [json.loads(l) for l in path.read_text().splitlines()]
        self.assertEqual([r["eventid"] for r in rows], ["93999", ""])

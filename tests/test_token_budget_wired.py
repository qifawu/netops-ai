"""token 预算必须真的接在跑着的循环上，不只是"这个类写好了"。

**这组测试存在的理由是一次真实的翻车。**

的 R2 验收发现：`meter.TokenBudget` 这个类写好了、有 5 条单测、
`env.example` 里也写了 `ANALYSIS_TOKEN_BUDGET`、文档里声称"超了就带着已有
证据出残缺结论"——**但全仓库没有任何运行代码 import 过它**。那道"防循环
跑飞"的防线一直是摆设，真正兜住失控循环的是 `max_iterations`。

更糟的是，开发机还把这个不存在的控制写进了 R 段任务书，让对面照着它设
预算。**给机制写单测证明不了机制被接上了。**

所以下面的测试分成两类，第二类才是这次真正缺的那类：

1. `TestBudgetMechanics` —— 预算逻辑本身（和以前一样）
2. `TestBudgetIsActuallyWired` —— **接线**：默认值从环境变量来、计数回调
 挂得上、消耗真的落进存档
3. `TestBudgetInsideTheRealLoop` —— **在真 `run_agent_loop` 里跑一遍**，
 证明前两类连成了一条线。只有这一类能挡住"写了但没接"这种失败。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from netops_ai.graph.agent_loop import (
    AgentLoopAbort,
    AgentLoopBudget,
    ChatRunTrace,
    ToolCallRecord,
    _attach_token_counter,
    _default_token_budget,
    save_trace,
)


def _trace(**kw) -> ChatRunTrace:
    return ChatRunTrace(trace_id="t", question="q", session_id="s", **kw)


class TestBudgetMechanics(unittest.TestCase):
    def test_charge_accumulates(self):
        t = _trace()
        t.charge_tokens(100, 20)
        t.charge_tokens(300, 50)
        self.assertEqual(t.tokens_spent, 470)

    def test_negative_usage_is_clamped(self):
        """有的适配器在异常路径上会返回负数或 None 转出来的怪值。"""
        t = _trace()
        t.charge_tokens(-5, 10)
        self.assertEqual(t.tokens_spent, 10)

    def test_aborts_when_over_budget(self):
        t = _trace()
        t.charge_tokens(50_000, 0)
        with self.assertRaises(AgentLoopAbort) as ctx:
            t.record_tool_call(
                ToolCallRecord(tool="device_show", args={}, ok=True),
                AgentLoopBudget(max_tokens=40_000),
            )
        self.assertIn("token 预算已用完", str(ctx.exception))
        self.assertTrue(t.incomplete)

    def test_zero_budget_means_unlimited(self):
        t = _trace()
        t.charge_tokens(10_000_000, 0)
        t.record_tool_call(  # 不抛就算过
            ToolCallRecord(tool="device_show", args={}, ok=True),
            AgentLoopBudget(max_tokens=0),
        )

    def test_token_budget_checked_before_tool_call_budget(self):
        """两个预算同时超时，要报 token 那个——**贵的是体积不是次数**，
        报错信息指错方向会让人去调 max_tool_calls，白忙。
        """
        t = _trace()
        t.charge_tokens(99_999, 0)
        budget = AgentLoopBudget(max_tokens=1000, max_tool_calls=1)
        with self.assertRaises(AgentLoopAbort) as ctx:
            t.record_tool_call(ToolCallRecord(tool="x", args={}, ok=True), budget)
        self.assertIn("token 预算", str(ctx.exception))

    def test_hop_growth_is_what_this_catches(self):
        """4 跳不多，但每跳重发全量上下文，总量是首跳的 10 倍。

        `max_iterations` 数次数不数体积，拦不住这种增长——这就是为什么
        光有轮数上限不够。
        """
        t = _trace()
        budget = AgentLoopBudget(max_tokens=60_000, max_tool_calls=99)
        with self.assertRaises(AgentLoopAbort):
            for hop in range(1, 6):
                t.charge_tokens(20_000 * hop, 500)
                t.record_tool_call(ToolCallRecord(tool="x", args={}, ok=True), budget)
        self.assertLess(len(t.tool_calls), 5, "应该在跑满 5 跳之前就被拦下")


class TestBudgetIsActuallyWired(unittest.TestCase):
    """**这一类才是上次缺的。** 机制对不对是一回事，接没接上是另一回事。"""

    def test_default_comes_from_env(self):
        with mock.patch.dict(os.environ, {"ANALYSIS_TOKEN_BUDGET": "45000"}):
            self.assertEqual(_default_token_budget(), 45_000)
            self.assertEqual(AgentLoopBudget().max_tokens, 45_000)

    def test_unset_env_means_unlimited(self):
        with mock.patch.dict(os.environ, {"ANALYSIS_TOKEN_BUDGET": ""}):
            self.assertEqual(_default_token_budget(), 0)

    def test_garbage_env_does_not_crash(self):
        """配错了退回"不限"，而不是把整条分析链路炸掉。"""
        for bad in ("abc", "-1", "0"):
            with self.subTest(bad=bad), mock.patch.dict(os.environ, {"ANALYSIS_TOKEN_BUDGET": bad}):
                self.assertEqual(_default_token_budget(), 0)

    def test_explicit_budget_overrides_env(self):
        with mock.patch.dict(os.environ, {"ANALYSIS_TOKEN_BUDGET": "45000"}):
            self.assertEqual(AgentLoopBudget(max_tokens=1234).max_tokens, 1234)

    def test_counter_callback_attaches_to_the_model(self):
        """回调必须真挂到模型上——**这就是上次漏掉的那一步**。"""
        class _Model:
            callbacks = None

        t = _trace()
        model = _attach_token_counter(_Model(), t)
        self.assertTrue(model.callbacks, "回调没挂上，预算就永远是 0，形同虚设")

    def test_counter_callback_charges_the_trace(self):
        """挂上还不够，回调触发时要真的把消耗记进这个 trace。"""
        class _Model:
            callbacks = None

        class _Msg:
            usage_metadata = {"input_tokens": 1207, "output_tokens": 1343}

        class _Gen:
            message = _Msg()

        class _Result:
            llm_output = {"model_name": "gemini-2.5-flash"}
            generations = [[_Gen()]]

        t = _trace()
        model = _attach_token_counter(_Model(), t)
        model.callbacks[-1].on_llm_end(_Result())
        self.assertEqual(t.tokens_spent, 1207 + 1343)

    def test_counter_never_breaks_the_run(self):
        """usage 拿不到、字段变了、适配器换了——计数失败绝不许炸穿真实分析。"""
        class _Model:
            callbacks = None

        t = _trace()
        model = _attach_token_counter(_Model(), t)
        model.callbacks[-1].on_llm_end(object())  # 完全不是 LLMResult
        self.assertEqual(t.tokens_spent, 0)

    def test_attaching_preserves_existing_callbacks(self):
        """`factory._with_usage_ledger()` 那个记账回调已经挂在那儿了，
        不能被这个覆盖掉——两个各干各的：一个落盘算钱，一个当场刹车。
        """
        sentinel = object()

        class _Model:
            callbacks = [sentinel]

        model = _attach_token_counter(_Model(), _trace())
        self.assertIn(sentinel, model.callbacks)
        self.assertEqual(len(model.callbacks), 2)

    def test_spent_amount_is_visible_afterwards(self):
        """花了多少要能事后看见，否则"有预算"和"没预算"在记录里长得一样。"""
        import json
        import tempfile
        from pathlib import Path

        t = _trace()
        t.charge_tokens(1000, 200)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("netops_ai.graph.agent_loop.TRACE_DIR", Path(tmp)):
                path = save_trace(t)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["tokens_spent"], 1200)



class TestBudgetInsideTheRealLoop(unittest.TestCase):
    """跑真正的 `run_agent_loop`，验预算真的会中断循环并产出残缺结论。

 **这条才是上次真正缺的那种测试。** 上面那些证明"预算逻辑对"和"回调挂
 得上"，但证明不了它们在跑着的循环里连成一条线——R2 验收
 发现的就是这个断点：类写好了、单测全绿、文档说它生效，实际没接。

 这里只替掉两样东西：模型（不调真 LLM）和 `create_agent` 的返回值
 （不依赖真模型驱动 LangChain 的工具循环）。`run_agent_loop` 本身、
 `record_tool_call` 的预算检查、`AgentLoopAbort` 的捕获、残缺结论的
 产出、trace 存档——全是真代码。

 写这条的时候连着踩了两个坑，都记在这儿免得下次再踩：
 1. 第一版用假模型驱动 `create_agent`，`invoke` 被调了、token 也累加了，
 但**工具一次都没执行**——假模型不够格让 LangChain 识别 tool_calls。
 2. 第二版假工具直接返回结果，没照真实工具那样在 `finally` 里过
 `record_tool_call`——**工具不走这一步，任何预算都拦不住它**。
    """

    PER_HOP = 30_000

    def _run(self, limit: int, hops: int = 8):
        from langchain_core.messages import AIMessage
        from langchain_core.tools import StructuredTool

        from netops_ai.graph import agent_loop as loop_mod
        from netops_ai.graph.agent_loop import run_agent_loop

        per_hop = self.PER_HOP

        def factory(trace, budget):
            def show(command: str) -> str:
                trace.charge_tokens(per_hop, 200)
                try:
                    return f"output of {command}"
                finally:
                    # 照真实工具的形状：干完活过 record_tool_call
                    trace.record_tool_call(
                        ToolCallRecord(tool="device_show", args={"command": command}, ok=True),
                        budget,
                    )

            return [StructuredTool.from_function(
                func=show, name="device_show", description="read-only show")]

        class _StubAgent:
            def __init__(self, tools):
                self.tool = {t.name: t for t in tools}["device_show"]

            def invoke(self, payload, config=None):
                msgs = list(payload.get("messages", []))
                for i in range(hops):
                    self.tool.func(command=f"show run {i}")
                msgs.append(AIMessage(content="查完了"))
                return {"messages": msgs}

        with mock.patch.object(loop_mod, "_build_loop_model", return_value=object()), \
             mock.patch.object(loop_mod, "_attach_token_counter", side_effect=lambda m, t: m), \
             mock.patch.object(loop_mod, "create_agent",
                               side_effect=lambda model, tools, system_prompt: _StubAgent(tools)):
            return run_agent_loop(
                factory,
                question="Gi0/1 怎么了",
                # 这组测的是 **token 预算**。stub 连敲同一个工具 8 次，会撞上
                # 无进展检测——那是另一道闸，这里显式关掉，
                # 否则测出来的是「被无进展拦下」，不是 token 预算的行为。
                budget=AgentLoopBudget(max_tokens=limit, max_tool_calls=50, max_iterations=50,
                                       no_progress_threshold=0,
                                       # 收尾调用会发真 HTTP 请求，单测里必须关掉
                                       wrap_up_on_abort=False),
            )

    def test_unlimited_runs_all_hops(self):
        r = self._run(0)
        self.assertEqual(len(r.trace.tool_calls), 8)
        self.assertFalse(r.incomplete)

    def test_budget_stops_the_loop_early(self):
        r = self._run(70_000)
        self.assertTrue(r.incomplete)
        self.assertIn("token 预算已用完", r.termination_reason)
        self.assertLess(len(r.trace.tool_calls), 8)

    def test_partial_conclusion_instead_of_a_crash(self):
        """超预算要**带着已有证据收尾**，不是把整条链路炸掉——
        已经花掉的 token 不能白花。
        """
        r = self._run(70_000)
        self.assertTrue(r.answer.startswith("（没查完："))
        self.assertTrue(r.trace.tool_calls, "收尾时要保留已经查到的步骤")
        self.assertTrue(r.trace_path.exists(), "存档要照常落盘")

if __name__ == "__main__":
    unittest.main()

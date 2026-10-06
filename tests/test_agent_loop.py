from __future__ import annotations

import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool

from netops_ai.graph import agent_loop


class FakeAgent:
    def __init__(self, messages):
        self.messages = messages
        self.config = None

    def invoke(self, payload, config=None):
        self.config = config
        return {"messages": [*payload["messages"], *self.messages]}


@contextmanager
def _patch_trace_dir():
    tmp = Path(tempfile.mkdtemp())
    with mock.patch.object(agent_loop, "TRACE_DIR", tmp):
        yield


class TestAgentLoop(unittest.TestCase):
    def test_message_text_extracts_gemini_text_parts(self):
        msg = AIMessage(content=[{"type": "text", "text": "正文", "extras": {"signature": "sig"}}])
        self.assertEqual(agent_loop.message_text(msg), "正文")

    def test_plan_is_injected_and_free_text_answer_is_saved(self):
        fake_agent = FakeAgent([AIMessage(content="查完了")])
        with (
            mock.patch("netops_ai.graph.agent_loop._build_loop_model", return_value=object()),
            mock.patch("netops_ai.graph.agent_loop.create_agent", return_value=fake_agent) as create_agent,
            _patch_trace_dir(),
        ):
            result = agent_loop.run_agent_loop(
                [],
                question="q",
                plan="先查 Zabbix，再查接口详情",
                budget=agent_loop.AgentLoopBudget(max_iterations=7),
                trace_id="unit-plan",
            )

        self.assertEqual(result.answer, "查完了")
        self.assertEqual(fake_agent.config["recursion_limit"], 7)
        self.assertIn("先查 Zabbix", create_agent.call_args.kwargs["system_prompt"])
        payload = json.loads(result.trace_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["final_answer"], "查完了")
        self.assertEqual(payload["model_call_count"], 1)
        self.assertEqual(payload["plan"], "先查 Zabbix，再查接口详情")

    def test_unbounded_budget_zeros_out_the_three_extra_gates(self) -> None:
        budget = agent_loop.AgentLoopBudget.unbounded()

        self.assertEqual(budget.max_iterations, 0)
        self.assertEqual(budget.max_tool_calls, 0)
        self.assertEqual(budget.no_progress_threshold, 0)

    def test_unbounded_budget_does_not_pass_recursion_limit_to_langgraph(self) -> None:
        """max_iterations=0：交给 LangGraph 自己的默认 recursion_limit，不传这个键。"""
        fake_agent = FakeAgent([AIMessage(content="查完了")])
        with (
            mock.patch("netops_ai.graph.agent_loop._build_loop_model", return_value=object()),
            mock.patch("netops_ai.graph.agent_loop.create_agent", return_value=fake_agent),
            _patch_trace_dir(),
        ):
            agent_loop.run_agent_loop(
                [],
                question="q",
                budget=agent_loop.AgentLoopBudget.unbounded(),
                trace_id="unit-unbounded",
            )

        self.assertNotIn("recursion_limit", fake_agent.config)

    def test_final_schema_runs_one_strict_convergence_call(self):
        fake_agent = FakeAgent([AIMessage(content="已有取数上下文")])
        fake_parsed = {"root_cause": "证据不足", "confidence": "low"}
        fake_client = mock.MagicMock()
        fake_client.complete.return_value = mock.MagicMock(parsed=fake_parsed)
        schema = {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}}
        with (
            mock.patch("netops_ai.graph.agent_loop._build_loop_model", return_value=object()),
            mock.patch("netops_ai.graph.agent_loop.create_agent", return_value=fake_agent),
            mock.patch("netops_ai.graph.agent_loop.LLMClient", return_value=fake_client),
            _patch_trace_dir(),
        ):
            result = agent_loop.run_agent_loop([], question="q", final_schema=schema, trace_id="unit-schema")

        self.assertEqual(result.structured, fake_parsed)
        self.assertEqual(json.loads(result.answer), fake_parsed)
        self.assertEqual(fake_client.complete.call_args.kwargs["response_format"], schema)
        self.assertEqual(result.trace.model_call_count, 2)

    def test_tool_call_budget_aborts_and_records_trace(self):
        def tools(trace, budget):
            def fail_tool(x: str) -> str:
                trace.record_tool_call(
                    agent_loop.ToolCallRecord(tool="fail_tool", args={"x": x}, ok=True, result="ok"),
                    budget,
                )
                return "ok"

            return [StructuredTool.from_function(func=fail_tool, name="fail_tool", description="test")]

        captured_tools = {}

        def create_agent_side_effect(**kwargs):
            captured_tools["tool"] = kwargs["tools"][0]

            class Agent:
                def invoke(self, payload, config=None):
                    captured_tools["tool"].invoke({"x": "1"})
                    return {"messages": [AIMessage(content="never")]}

            return Agent()

        with (
            mock.patch("netops_ai.graph.agent_loop._build_loop_model", return_value=object()),
            mock.patch("netops_ai.graph.agent_loop.create_agent", side_effect=create_agent_side_effect),
            # 收尾调用「没配模型就不发请求」——本机 .env 配了模型，单测就会去连真 endpoint，
            # 答案变成模型现编的一段话（收尾时在 Win 上翻出来），所以这里显式当没配。
            mock.patch("netops_ai.graph.agent_loop.env", return_value={}),
            _patch_trace_dir(),
        ):
            result = agent_loop.run_agent_loop(
                tools,
                question="q",
                budget=agent_loop.AgentLoopBudget(max_tool_calls=1),
                trace_id="unit-budget",
            )

        self.assertTrue(result.incomplete)
        self.assertIn("（没查完：工具调用预算已用完", result.answer)
        self.assertIn("fail_tool", result.answer)
        payload = json.loads(result.trace_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["tool_call_count"], 1)
        self.assertTrue(payload["incomplete"])

    def test_repeated_failure_aborts_same_tool_and_args(self):
        captured_tools = {}

        def tools(trace, budget):
            def failing_tool(command: str) -> str:
                record = agent_loop.ToolCallRecord(
                    tool="device_show",
                    args={"command": command},
                    ok=False,
                    error="PermissionError: denied",
                )
                trace.record_tool_call(record, budget)
                return json.dumps({"error": record.error})

            return [StructuredTool.from_function(func=failing_tool, name="device_show", description="test")]

        def create_agent_side_effect(**kwargs):
            captured_tools["tool"] = kwargs["tools"][0]

            class Agent:
                def invoke(self, payload, config=None):
                    captured_tools["tool"].invoke({"command": "show logging"})
                    captured_tools["tool"].invoke({"command": "show logging"})
                    return {"messages": [AIMessage(content="never")]}

            return Agent()

        with (
            mock.patch("netops_ai.graph.agent_loop._build_loop_model", return_value=object()),
            mock.patch("netops_ai.graph.agent_loop.create_agent", side_effect=create_agent_side_effect),
            _patch_trace_dir(),
        ):
            result = agent_loop.run_agent_loop(
                tools,
                question="q",
                budget=agent_loop.AgentLoopBudget(max_tool_calls=10, no_progress_threshold=2),
                trace_id="unit-repeat",
            )

        self.assertTrue(result.incomplete)
        # 合并后统一叫「没带来新信息」，失败只是其中一种
        self.assertIn("没带来新信息", result.answer)
        self.assertIn("这一轮没查到任何东西", result.answer)
        self.assertEqual(len(result.trace.tool_calls), 2)

    def test_iteration_limit_error_is_reported_as_incomplete(self):
        class Agent:
            def invoke(self, payload, config=None):
                raise RuntimeError("Recursion limit of 3 reached")

        with (
            mock.patch("netops_ai.graph.agent_loop._build_loop_model", return_value=object()),
            mock.patch("netops_ai.graph.agent_loop.create_agent", return_value=Agent()),
            _patch_trace_dir(),
        ):
            result = agent_loop.run_agent_loop(
                [],
                question="q",
                budget=agent_loop.AgentLoopBudget(max_iterations=3),
                trace_id="unit-iterations",
            )

        self.assertTrue(result.incomplete)
        self.assertIn("max_iterations=3", result.answer)
        self.assertIn("这一轮没查到任何东西", result.answer)

    def test_final_schema_runs_after_agent_loop_abort_with_trace_transcript(self):
        schema = {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}}
        parsed = {"root_cause": "证据不足", "confidence": "medium"}
        trace_holder = {}

        def tools(trace, budget):
            trace_holder["trace"] = trace
            return []

        class Agent:
            def invoke(self, payload, config=None):
                trace_holder["trace"].last_model_text = "探路小结：A1 Gi0/1 相关"
                trace_holder["trace"].tool_calls.append(
                    agent_loop.ToolCallRecord(
                        tool="device_show",
                        args={"device": "A1", "command": "show interfaces Gi0/1"},
                        ok=True,
                        result="GigabitEthernet0/1 is administratively down\nline protocol is down",
                    )
                )
                raise agent_loop.AgentLoopAbort("工具调用预算已用完：max_tool_calls=12。")

        with (
            mock.patch("netops_ai.graph.agent_loop._build_loop_model", return_value=object()),
            mock.patch("netops_ai.graph.agent_loop.create_agent", return_value=Agent()),
            mock.patch("netops_ai.graph.agent_loop._final_schema_call", return_value=parsed) as final_call,
            _patch_trace_dir(),
        ):
            result = agent_loop.run_agent_loop(tools, question="q", final_schema=schema, trace_id="unit-abort-schema")

        final_call.assert_called_once()
        transcript = final_call.call_args.kwargs["transcript"]
        self.assertIn("device_show", transcript)
        self.assertIn("show interfaces Gi0/1", transcript)
        self.assertIn("GigabitEthernet0/1 is administratively down\nline protocol is down", transcript)
        self.assertIn("探路小结", transcript)
        self.assertEqual(result.trace.final_structured, parsed)
        self.assertEqual(result.structured, parsed)
        self.assertEqual(result.trace.final_schema_from, "agent_loop_final_schema_after_abort")
        self.assertIn("max_tool_calls=12", result.trace.final_schema_abort_reason)

    def test_final_schema_failure_after_abort_keeps_both_reasons(self):
        schema = {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}}
        trace_holder = {}

        def tools(trace, budget):
            trace_holder["trace"] = trace
            return []

        class Agent:
            def invoke(self, payload, config=None):
                trace_holder["trace"].tool_calls.append(
                    agent_loop.ToolCallRecord(tool="zbx_problems", args={"host": "A1"}, ok=True, result="raw result")
                )
                raise agent_loop.AgentLoopAbort("工具调用预算已用完：max_tool_calls=12。")

        with (
            mock.patch("netops_ai.graph.agent_loop._build_loop_model", return_value=object()),
            mock.patch("netops_ai.graph.agent_loop.create_agent", return_value=Agent()),
            mock.patch("netops_ai.graph.agent_loop._final_schema_call", side_effect=ValueError("schema parse failed")),
            mock.patch("netops_ai.graph.agent_loop.env", return_value={}),
            _patch_trace_dir(),
        ):
            result = agent_loop.run_agent_loop(tools, question="q", final_schema=schema, trace_id="unit-abort-schema-fail")

        self.assertIsNone(result.trace.final_structured)
        self.assertIn("工具调用预算已用完", result.trace.error)
        self.assertIn("final_schema_after_abort failed: ValueError: schema parse failed", result.trace.error)
        self.assertIn("（没查完：工具调用预算已用完", result.answer)

    def test_abort_without_final_schema_keeps_old_abort_answer_path(self):
        class Agent:
            def invoke(self, payload, config=None):
                raise agent_loop.AgentLoopAbort("工具调用预算已用完：max_tool_calls=12。")

        with (
            mock.patch("netops_ai.graph.agent_loop._build_loop_model", return_value=object()),
            mock.patch("netops_ai.graph.agent_loop.create_agent", return_value=Agent()),
            mock.patch("netops_ai.graph.agent_loop._final_schema_call") as final_call,
            _patch_trace_dir(),
        ):
            result = agent_loop.run_agent_loop([], question="q", trace_id="unit-abort-no-schema")

        final_call.assert_not_called()
        self.assertIsNone(result.trace.final_structured)
        self.assertIn("（没查完：工具调用预算已用完", result.answer)

    def test_agent_invoke_connection_error_retries_then_succeeds_and_emits_trace_event(self):
        events = []

        class Agent:
            def __init__(self):
                self.calls = 0

            def invoke(self, payload, config=None):
                self.calls += 1
                if self.calls < 3:
                    raise ConnectionError("temporary OpenAIConnectionError")
                return {"messages": [AIMessage(content="查完了")]}

        agent = Agent()
        sleeps = []
        with (
            mock.patch("netops_ai.graph.agent_loop._build_loop_model", return_value=object()),
            mock.patch("netops_ai.graph.agent_loop.create_agent", return_value=agent),
            mock.patch("netops_ai.graph.agent_loop.LLM_RETRY_SLEEP", side_effect=sleeps.append),
            mock.patch("netops_ai.llm.retry.random.random", return_value=0),
            _patch_trace_dir(),
        ):
            result = agent_loop.run_agent_loop([], question="q", trace_id="unit-agent-retry", on_event=events.append)

        self.assertEqual(result.answer, "查完了")
        self.assertEqual(agent.calls, 3)
        self.assertEqual(sleeps, [1.0, 3.0])
        retry_events = [event for event in events if event["kind"] == "llm_retry"]
        self.assertEqual([event["attempt"] for event in retry_events], [1, 2])
        self.assertIn("OpenAIConnectionError", retry_events[0]["error"])

    def test_agent_invoke_logic_error_is_not_retried(self):
        class Agent:
            def __init__(self):
                self.calls = 0

            def invoke(self, payload, config=None):
                self.calls += 1
                raise ValueError("schema parse failed")

        agent = Agent()
        with (
            mock.patch("netops_ai.graph.agent_loop._build_loop_model", return_value=object()),
            mock.patch("netops_ai.graph.agent_loop.create_agent", return_value=agent),
            mock.patch("netops_ai.graph.agent_loop.LLM_RETRY_SLEEP") as sleep,
            _patch_trace_dir(),
        ):
            result = agent_loop.run_agent_loop([], question="q", trace_id="unit-agent-logic-error")

        self.assertEqual(agent.calls, 1)
        sleep.assert_not_called()
        self.assertIn("ValueError: schema parse failed", result.trace.error)

    def test_connection_error_exhausted_with_tool_calls_runs_final_schema_after_abort(self):
        schema = {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}}
        parsed = {"root_cause": "证据不足", "confidence": "low"}
        trace_holder = {}

        def tools(trace, budget):
            trace_holder["trace"] = trace
            return []

        class Agent:
            def __init__(self):
                self.calls = 0

            def invoke(self, payload, config=None):
                self.calls += 1
                if not trace_holder["trace"].tool_calls:
                    trace_holder["trace"].tool_calls.append(
                        agent_loop.ToolCallRecord(tool="zbx_problems", args={"host": "A1"}, ok=True, result="raw")
                    )
                raise ConnectionError("OpenAIConnectionError: Connection error.")

        agent = Agent()
        with (
            mock.patch("netops_ai.graph.agent_loop._build_loop_model", return_value=object()),
            mock.patch("netops_ai.graph.agent_loop.create_agent", return_value=agent),
            mock.patch("netops_ai.graph.agent_loop._final_schema_call", return_value=parsed) as final_call,
            mock.patch("netops_ai.graph.agent_loop.LLM_RETRY_SLEEP"),
            _patch_trace_dir(),
        ):
            result = agent_loop.run_agent_loop(tools, question="q", final_schema=schema, trace_id="unit-conn-final")

        self.assertEqual(agent.calls, 4)
        final_call.assert_called_once()
        self.assertEqual(result.structured, parsed)
        self.assertEqual(result.trace.final_schema_from, "agent_loop_final_schema_after_abort")
        self.assertEqual(result.trace.final_schema_abort_reason, "LLM 连接失败（重试 3 次）")
        self.assertIn("取证阶段: ConnectionError", result.trace.error)

    def test_connection_error_exhausted_and_final_schema_failure_keeps_both_stages(self):
        schema = {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}}
        trace_holder = {}

        def tools(trace, budget):
            trace_holder["trace"] = trace
            return []

        class Agent:
            def invoke(self, payload, config=None):
                if not trace_holder["trace"].tool_calls:
                    trace_holder["trace"].tool_calls.append(
                        agent_loop.ToolCallRecord(tool="zbx_problems", args={}, ok=True, result="raw")
                    )
                raise ConnectionError("OpenAIConnectionError: Connection error.")

        with (
            mock.patch("netops_ai.graph.agent_loop._build_loop_model", return_value=object()),
            mock.patch("netops_ai.graph.agent_loop.create_agent", return_value=Agent()),
            mock.patch("netops_ai.graph.agent_loop._final_schema_call", side_effect=ConnectionError("final 503")),
            mock.patch("netops_ai.graph.agent_loop.env", return_value={}),
            mock.patch("netops_ai.graph.agent_loop.LLM_RETRY_SLEEP"),
            _patch_trace_dir(),
        ):
            result = agent_loop.run_agent_loop(tools, question="q", final_schema=schema, trace_id="unit-conn-final-fail")

        self.assertIn("取证阶段: ConnectionError", result.trace.error)
        self.assertIn("收尾阶段: ConnectionError: final 503", result.trace.error)
        self.assertIn("（没查完：LLM 连接失败（重试 3 次）", result.answer)


if __name__ == "__main__":
    unittest.main()


class TestPartialAnswerKept(unittest.TestCase):
    """qwen 5/5 撞满工具调用预算，但结论早就写完了。以前这里把小结整段丢掉。"""

    def test_预算掐断时保留模型已经写好的小结(self):
        from netops_ai.graph.agent_loop import AgentLoopAbort, AgentLoopBudget, ChatRunTrace, run_agent_loop

        trace_holder = {}

        def _tools(trace, budget):
            trace_holder["t"] = trace
            trace.last_model_text = "探路小结：D2 的 Et0/2 被配进了 area 1"
            return []

        with mock.patch("netops_ai.graph.agent_loop.build_chat_model") as model, \
             mock.patch("netops_ai.graph.agent_loop.create_agent") as agent:
            model.return_value = mock.MagicMock()
            agent.return_value.invoke.side_effect = AgentLoopAbort("工具调用预算已用完：max_tool_calls=10。")
            result = run_agent_loop(_tools, question="q", budget=AgentLoopBudget())

        self.assertIn("area 1", result.answer)
        self.assertIn("没查完", result.answer)


class Test无进展检测(unittest.TestCase):
    """业界说的第四道闸（action hash window / no-progress）。

 **从三个计数器合并成一个。** 原来是：同一工具连续失败 2 次 /
 同一「工具+参数」第 3 次 / 同一工具连调 6 次。三条防的是同一件事——
 这一跳没让我们比上一跳知道得更多——却各有各的阈值，互相还留着缝。

 合并后的判据是三选一：调用失败、工具自报返回是空的、这个「工具+参数」问过了。
    """

    def _trace(self):
        return agent_loop.ChatRunTrace(trace_id="t", question="q", session_id="s")

    def _record(self, trace, budget, tool, args, ok=True, result="ok"):
        trace.record_tool_call(
            agent_loop.ToolCallRecord(tool=tool, args=args, ok=ok, result=result), budget
        )

    def test_同一个调用复读到第三次没进展就停(self):
        """阈值数的是**连续几跳没带来新信息**，不是同一个调用出现了几次。

        第 1 次是新问题（有进展），第 2、3、4 次才是"问过了"。
        所以放过两次复读，第三次复读停手——偶尔想复看一个计数器变没变算合理，
        连着三次就是打转了。
        """
        t, b = self._trace(), agent_loop.AgentLoopBudget(max_tool_calls=50)
        self._record(t, b, "zbx_hosts", {"host": "V1"})   # 新问题
        self._record(t, b, "zbx_hosts", {"host": "V1"})   # 复读一
        self._record(t, b, "zbx_hosts", {"host": "V1"})   # 复读二
        with self.assertRaises(agent_loop.AgentLoopAbort):
            self._record(t, b, "zbx_hosts", {"host": "V1"})
        self.assertIn("已经问过了", t.termination_reason)
        self.assertTrue(t.incomplete)

    def test_参数不同就不算重复调用(self):
        t, b = self._trace(), agent_loop.AgentLoopBudget(max_tool_calls=50)
        for host in ("V1", "V2", "D1", "D2", "A1"):
            self._record(t, b, "zbx_hosts", {"host": host})
        self.assertEqual(len(t.tool_calls), 5)

    def test_连扫四台接入是合法的不能拦(self):
        """Q1 真实轨迹：`topology_neighbors` 连查 A1~A4 一次扫完四台接入。

        **这条测试当场打回过一版实现。** 判据一度写成「返回跟之前某次一模一样」，
        四台要是都干净，返回就是四份一样的空，第三台就被拦下。
        换成按「问过没问过」比才对：换了参数就是在问新东西，答案一样也是新知识。
        """
        t, b = self._trace(), agent_loop.AgentLoopBudget(max_tool_calls=50)
        for host in ("A1", "A2", "A3", "A4"):
            self._record(t, b, "topology_neighbors", {"host": host}, result="一样的返回")
        self.assertEqual(len(t.tool_calls), 4)
        self.assertFalse(t.incomplete)

    def test_工具自报空时换参数也拦得住(self):
        """Win 那次：`zbx_top_talkers` 返回全零榜单，模型去挨个
 `zbx_items` 翻了 12 步，**每次 item_id 不同、每次都成功**，
 一路烧到 max_tool_calls 才停。

 **这条要靠工具配合**——工具自己说返回是空的，循环层才拦得住。
 参数一直在换，光看调用本身看不出在打转。
 对应 `chat_agent._tool_reply` 的 `empty` 标记和
 `zabbix/cli.py` 里全零榜单自己承认是空的那段。
        """
        t, b = self._trace(), agent_loop.AgentLoopBudget(max_tool_calls=50)
        self._record(t, b, "zbx_top_talkers", {"window": "1h"})
        with self.assertRaises(agent_loop.AgentLoopAbort):
            for i in range(12):
                self._record(t, b, "zbx_items", {"item_id": 52900 + i},
                             result={"items": [], "empty": True})
        self.assertIn("返回是空的", t.termination_reason)
        # 撞的是第 3 次，不是烧到 12 次才停
        self.assertEqual(len(t.tool_calls), 1 + 3)

    def test_中间查到一次有内容就重新计数(self):
        t, b = self._trace(), agent_loop.AgentLoopBudget(max_tool_calls=50)
        for i in range(2):
            self._record(t, b, "zbx_items", {"item_id": i}, result={"empty": True})
        self._record(t, b, "device_show", {"cmd": "show logging"}, result={"output": "有东西"})
        for i in range(2):
            self._record(t, b, "zbx_items", {"item_id": 100 + i}, result={"empty": True})
        self.assertFalse(t.incomplete)

    def test_闸能关掉(self):
        t = self._trace()
        b = agent_loop.AgentLoopBudget(max_tool_calls=50, no_progress_threshold=0)
        for _ in range(10):
            self._record(t, b, "zbx_hosts", {"host": "V1"})
        self.assertEqual(len(t.tool_calls), 10)

    def test_max_tool_calls为0时工具调用数量闸也关掉(self):
        """A16：告警这条线先暂时取消轮数限制，只留 LangChain 自己的上限。"""
        t, b = self._trace(), agent_loop.AgentLoopBudget.unbounded()
        for i in range(20):  # 超过旧默认值 12，仍然不该被拦
            self._record(t, b, "zbx_hosts", {"host": f"V{i}"})
        self.assertEqual(len(t.tool_calls), 20)
        self.assertFalse(t.incomplete)


class Test历史查询去重(unittest.TestCase):
    def _trace(self):
        return agent_loop.ChatRunTrace(trace_id="t", question="q", session_id="s")

    def _remember(self, trace, tool, args, *, budget=None):
        trace.record_tool_call(
            agent_loop.ToolCallRecord(tool=tool, args=args, ok=True, result={"points": [1]}),
            budget or agent_loop.AgentLoopBudget(max_tool_calls=50),
        )

    def test_同itemid相同和重叠窗口会被拦(self):
        t = self._trace()
        self._remember(t, "zbx_history", {"item_id": "54089", "since": "100000", "until": "120000"},)
        note = t.dedupe_history_query(
            "zbx_history", {"item_id": "54089", "since": "110000", "until": "130000"}, now=140000
        )
        self.assertIn("itemid 54089", note)
        self.assertIn("#1 号调用", note)
        self.assertIn("不消耗 max_tool_calls", note)

    def test_同itemid不重叠窗口放行(self):
        t = self._trace()
        self._remember(t, "zbx_history", {"item_id": "54089", "since": "100", "until": "200"})
        self.assertEqual(
            t.dedupe_history_query("zbx_history", {"item_id": "54089", "since": "200", "until": "300"}),
            "",
        )

    def test_解析不了的窗口放行(self):
        t = self._trace()
        self._remember(t, "zbx_history", {"item_id": "54089", "since": "100000", "until": "120000"})
        self.assertEqual(
            t.dedupe_history_query("zbx_history", {"item_id": "54089", "since": "yesterday", "until": ""}),
            "",
        )

    def test_不同itemid放行(self):
        t = self._trace()
        self._remember(t, "zbx_history", {"item_id": "54089", "since": "100", "until": "200"})
        self.assertEqual(
            t.dedupe_history_query("zbx_history", {"item_id": "54090", "since": "100", "until": "200"}),
            "",
        )

    def test_history和trends同itemid重叠也会被拦(self):
        t = self._trace()
        self._remember(t, "zbx_history", {"item_id": "54089", "since": "100", "until": "200"})
        self.assertIn(
            "itemid 54089",
            t.dedupe_history_query("zbx_trends", {"item_id": "54089", "since": "150", "until": "250"}),
        )

    def test_syslog同主机重叠窗口被拦_include不同放行(self):
        t = self._trace()
        self._remember(t, "zbx_syslog", {"host": "A1", "since": "100", "until": "200", "include": "LINK"})
        self.assertIn(
            "syslog",
            t.dedupe_history_query("zbx_syslog", {"host": "a1", "since": "150", "until": "250", "include": "LINK"}),
        )
        self.assertEqual(
            t.dedupe_history_query("zbx_syslog", {"host": "a1", "since": "150", "until": "250", "include": "OSPF"}),
            "",
        )

    def test_查到现在且超过60秒算新窗口放行(self):
        t = self._trace()
        self._remember(t, "zbx_history", {"item_id": "54089", "since": "-6h", "until": ""})
        self.assertEqual(
            t.dedupe_history_query("zbx_history", {"item_id": "54089", "since": "-6h", "until": ""}, now=int(time.time()) + 61),
            "",
        )

    def test_deduped记录不消耗工具预算但计入无进展(self):
        t = self._trace()
        self._remember(t, "zbx_history", {"item_id": "54089", "since": "100", "until": "200"})
        tight_budget = agent_loop.AgentLoopBudget(max_tool_calls=1, no_progress_threshold=3)
        for _ in range(2):
            t.record_tool_call(
                agent_loop.ToolCallRecord(
                    tool="zbx_history",
                    args={"item_id": "54089", "since": "150", "until": "180"},
                    ok=True,
                    result="重复查询已拦截",
                    deduped=True,
                ),
                tight_budget,
            )
        self.assertEqual(len(t.tool_calls), 3)
        with self.assertRaises(agent_loop.AgentLoopAbort):
            t.record_tool_call(
                agent_loop.ToolCallRecord(
                    tool="zbx_history",
                    args={"item_id": "54089", "since": "150", "until": "180"},
                    ok=True,
                    result="重复查询已拦截",
                    deduped=True,
                ),
                tight_budget,
            )
        self.assertIn("重复历史查询已拦截", t.termination_reason)


class Test工具返回有界且自述(unittest.TestCase):
    """工具层对策。维护者 「所以我听下来 工具层也需要做好对策」。

 `_json_safe(max_text=6000)` **只用在落盘那一侧**，返回给模型的一直是
 裸 `json.dumps(data)`，没有上限。
    """

    def test_空结果说清是空的(self):
        from netops_ai.graph.chat_agent import _tool_reply
        out = json.loads(_tool_reply([]))
        self.assertTrue(out["empty"])
        # 拿到 `[]` 和拿到「查了但没有」，模型区分不了，多半换个参数再查一遍
        self.assertIn("没有数据", out["note"])

    def test_空结果回显查询参数且不给改法(self):
        """工具返回保持中性——空就说空、回显查的是什么，不带 try_instead。
 原来带的「把窗口放宽」跟提示词的「不要换时间窗反复查」打过架（外部 agent 反馈第 3 条）。
        """
        from netops_ai.graph.chat_agent import _tool_reply
        out = json.loads(_tool_reply({}, query={"host": "A1", "since": "100"}))
        self.assertTrue(out["empty"])
        self.assertEqual(out["query"], {"host": "A1", "since": "100"})
        self.assertNotIn("try_instead", out)

    def test_超长截断并说明被截了(self):
        from netops_ai.graph.chat_agent import TOOL_RESULT_MAX_CHARS, _tool_reply
        data = ["x" * 200] * 200
        out = json.loads(_tool_reply(data))
        self.assertTrue(out["truncated"])
        self.assertEqual(len(out["head"]), TOOL_RESULT_MAX_CHARS)
        # 不说被截了的话，模型会把半截数据当成全部：总长度和返回长度都要说出来
        self.assertIn(f"共 {len(json.dumps(data, ensure_ascii=False))} 字符", out["note"])
        self.assertIn(f"前 {TOOL_RESULT_MAX_CHARS} 字符", out["note"])
        self.assertIn("不完整", out["note"])
        # 只陈述事实，不教它怎么查
        self.assertNotIn("收窄", out["note"])

    def test_正常结果原样返回(self):
        from netops_ai.graph.chat_agent import _tool_reply
        self.assertEqual(json.loads(_tool_reply({"a": 1})), {"a": 1})


class TestSopRuntimeState(unittest.TestCase):
    def _data(self):
        return {
            "matched": True,
            "playbook": "interface-link-down",
            "matched_because": ["告警名含 Link down"],
            "steps": [
                {
                    "id": "triage_interface",
                    "why": "看接口状态。",
                    "main": True,
                    "action": {"tool": "device_show", "command": "show interfaces GigabitEthernet0/1"},
                    "branches": [
                        {"when": "output_contains('administratively down')", "goto": "local_shutdown_evidence"},
                        {"when": "default", "goto": "__ai__"},
                    ],
                },
                {
                    "id": "local_shutdown_evidence",
                    "why": "看故障窗口配置日志。",
                    "main": True,
                    "action": {"tool": "zbx_syslog", "host": "D1"},
                    "branches": [{"when": "default", "goto": "__end__"}],
                },
            ],
        }

    def test_next_step_hint_is_appended_after_matching_tool(self):
        trace = agent_loop.ChatRunTrace(
            trace_id="t", question="q", session_id="s", sop=agent_loop.SopRuntimeState(self._data())
        )
        record = agent_loop.ToolCallRecord(
            tool="device_show",
            args={"command": "show interfaces GigabitEthernet0/1"},
            ok=True,
            result={"allowed": True, "ok": True, "output": "GigabitEthernet0/1 is administratively down"},
        )

        hint = trace.sop_hint_for_tool_result(record)
        trace.record_tool_call(record, agent_loop.AgentLoopBudget(max_tool_calls=50))

        self.assertIn("SOP 下一步：local_shutdown_evidence", hint)
        # 下一步按 action 原样摆（工具名 + 真实参数名），不再只挑一个值叫「命令」
        self.assertIn("tool=zbx_syslog; host=D1", hint)
        self.assertEqual(trace.sop_usage["steps"][0]["status"], "done")
        self.assertEqual(trace.sop_usage["steps"][0]["tool_call_index"], 1)

    def test_denied_or_empty_sop_step_is_invalid_and_skipped(self):
        trace = agent_loop.ChatRunTrace(
            trace_id="t", question="q", session_id="s", sop=agent_loop.SopRuntimeState(self._data())
        )
        record = agent_loop.ToolCallRecord(
            tool="device_show",
            args={"command": "show interfaces GigabitEthernet0/1"},
            ok=False,
            result={"allowed": False, "denial_reason": "whitelist"},
        )

        hint = trace.sop_hint_for_tool_result(record)
        trace.record_tool_call(record, agent_loop.AgentLoopBudget(max_tool_calls=50, no_progress_threshold=0))

        self.assertIn("这一步报错或被拒绝，按 error 分支", hint)
        self.assertIn("triage_interface", trace.sop_usage["invalid_steps"])
        self.assertEqual(trace.sop_usage["steps"][0]["status"], "denied")

    def test_sop_usage_adherence_branches(self):
        not_matched = agent_loop.SopRuntimeState({"matched": False, "steps": []}).usage()
        self.assertEqual(not_matched["adherence"], "not_matched")

        matched = agent_loop.SopRuntimeState(self._data()).usage()
        self.assertEqual(matched["adherence"], "matched_not_used")

        used = agent_loop.SopRuntimeState(self._data())
        used.hint_for(
            agent_loop.ToolCallRecord(
                tool="device_show",
                args={"command": "show interfaces GigabitEthernet0/1"},
                ok=True,
                result={"allowed": True, "ok": True, "output": "administratively down"},
            ),
            tool_call_index=1,
        )
        used.hint_for(
            agent_loop.ToolCallRecord(tool="zbx_syslog", args={"host": "D1"}, ok=True, result={"lines": ["x"]}),
            tool_call_index=2,
        )
        self.assertEqual(used.usage()["adherence"], "used")

        deviated = agent_loop.SopRuntimeState(self._data())
        deviated.hint_for(
            agent_loop.ToolCallRecord(tool="zbx_items", args={"host": "D1"}, ok=True, result={"items": [1]}),
            tool_call_index=1,
        )
        deviated.hint_for(
            agent_loop.ToolCallRecord(
                tool="device_show",
                args={"command": "show interfaces GigabitEthernet0/1"},
                ok=True,
                result={"allowed": True, "ok": True, "output": "administratively down"},
            ),
            tool_call_index=2,
        )
        deviated.hint_for(
            agent_loop.ToolCallRecord(tool="zbx_syslog", args={"host": "D1"}, ok=True, result={"lines": ["x"]}),
            tool_call_index=3,
        )
        self.assertEqual(deviated.usage()["adherence"], "used_with_deviation")


class TestSopRuntimeW36b(unittest.TestCase):
    """验证（A1 Gi0/1 console shutdown，Trap 告警名不带接口）外部 agent 反馈的第 1、3 条。"""

    def _data(self):
        from netops_ai.playbooks.lookup import sop_lookup

        # 真实 SOP + 真实告警名：`Trap: linkDown received on A1 …` 里没有接口
        return sop_lookup("Trap: linkDown received on A1 (viosl2 switch, 接入层)", [], "cisco", alert_clock=1790396461)

    @staticmethod
    def _dev(command, output):
        return agent_loop.ToolCallRecord(
            tool="device_show", args={"command": command}, ok=True,
            result={"allowed": True, "ok": True, "output": output},
        )

    def test_verification_sequence_does_not_point_back(self):
        sop = agent_loop.SopRuntimeState(self._data())
        hints = [
            sop.hint_for(self._dev("show interfaces GigabitEthernet0/1",
                                   "GigabitEthernet0/1 is administratively down, line protocol is down"),
                         tool_call_index=1),
            sop.hint_for(agent_loop.ToolCallRecord(
                tool="zbx_syslog", args={"host": "A1-viosl2", "since": "1790395561", "until": "1790396761"},
                ok=True, result={"empty": True, "lines": []}), tool_call_index=2),
            # 不带过滤的 show logging（被截断的那次），输出里有 administratively down
            sop.hint_for(self._dev("show logging", "changed state to administratively down"), tool_call_index=3),
            sop.hint_for(self._dev("show logging | include GigabitEthernet0/1|CONFIG_I|RESTART",
                                   "%SYS-5-CONFIG_I: Configured from console by console"), tool_call_index=4),
        ]
        self.assertIn("SOP 下一步：local_shutdown_evidence", hints[0])
        # 空结果按 default 走（error/default 这里都指 local_log_buffer，看前缀区分），命令带占位符摆出来
        self.assertIn("返回为空，按 default 分支", hints[1])
        self.assertIn("SOP 下一步：local_log_buffer", hints[1])
        self.assertIn("command=show logging | include CONFIG_I|<接口>", hints[1])
        for hint in hints[2:]:
            self.assertNotIn("local_shutdown_evidence", hint)
            self.assertNotIn("triage_interface", hint)
        usage = sop.usage()
        status = {s["id"]: s["status"] for s in usage["steps"]}
        self.assertEqual(status["triage_interface"], "done")
        self.assertEqual(status["local_shutdown_evidence"], "empty")
        self.assertEqual(status["local_log_buffer"], "done")

    def test_placeholder_command_does_not_match_other_device_commands(self):
        sop = agent_loop.SopRuntimeState(self._data())
        self.assertEqual(sop.hint_for(self._dev("show clock", "administratively down"), tool_call_index=1), "")
        self.assertEqual(sop.statuses["triage_interface"], "not_reached")

    def test_branch_back_to_visited_step_ends_sop(self):
        data = {
            "matched": True,
            "steps": [
                {"id": "a", "action": {"tool": "device_show", "command": "show version"},
                 "branches": [{"when": "default", "goto": "b"}]},
                {"id": "b", "why": "看时钟。", "action": {"tool": "device_show", "command": "show clock"},
                 "branches": [{"when": "default", "goto": "a"}]},
            ],
        }
        sop = agent_loop.SopRuntimeState(data)
        self.assertIn("SOP 下一步：b", sop.hint_for(self._dev("show version", "x"), tool_call_index=1))
        hint = sop.hint_for(self._dev("show clock", "y"), tool_call_index=2)
        self.assertNotIn("SOP 下一步", hint)
        self.assertIn("a 已在第 1 次工具调用走过", hint)

    def test_empty_goes_default_and_error_goes_error(self):
        data = {
            "matched": True,
            "steps": [
                {"id": "logs", "action": {"tool": "zbx_syslog"},
                 "branches": [{"when": "error", "goto": "on_error"}, {"when": "default", "goto": "on_default"}]},
                {"id": "on_error", "why": "报错时。", "action": {"tool": "device_show", "command": "show version"}},
                {"id": "on_default", "why": "空或有数据时。", "action": {"tool": "device_show", "command": "show clock"}},
            ],
        }
        empty = agent_loop.SopRuntimeState(data).hint_for(
            agent_loop.ToolCallRecord(tool="zbx_syslog", args={}, ok=True, result={"empty": True}), tool_call_index=1)
        self.assertIn("SOP 下一步：on_default", empty)
        failed = agent_loop.SopRuntimeState(data).hint_for(
            agent_loop.ToolCallRecord(tool="zbx_syslog", args={}, ok=False, error="ZabbixAPIError: x"),
            tool_call_index=1)
        self.assertIn("SOP 下一步：on_error", failed)


class TestSopRuntimeW36c(unittest.TestCase):
    """device-unreachable 的 value 分支按工具真实返回走；去掉写死实例值后的 SOP 运行时认得出步骤。"""

    ICMP_ITEMS = [
        {"itemid": "1", "name": "ICMP ping", "key_": "icmpping", "value_type": "3", "lastvalue": "0"},
        {"itemid": "2", "name": "ICMP loss", "key_": "icmppingloss", "value_type": "0", "lastvalue": "100"},
        {"itemid": "3", "name": "ICMP response time", "key_": "icmppingsec", "value_type": "0", "lastvalue": "0"},
    ]

    @staticmethod
    def _lookup(alert_name):
        from netops_ai.playbooks.lookup import sop_lookup

        return sop_lookup(alert_name, ["component=network"], "cisco", alert_clock=1790396461)

    def _items(self, lastvalue, *, key="icmpping", items=None):
        rows = [dict(row) for row in (self.ICMP_ITEMS if items is None else items)]
        for row in rows:
            if row["key_"] == "icmpping":
                row["lastvalue"] = lastvalue
        return agent_loop.ToolCallRecord(
            tool="zbx_items", args={"host": "A1-viosl2", "key": key, "limit": 50}, ok=True,
            result={"host": {"hostid": "10", "host": "A1-viosl2"}, "items": rows, "count": len(rows)},
        )

    @staticmethod
    def _dev(command, output):
        return agent_loop.ToolCallRecord(
            tool="device_show", args={"command": command}, ok=True,
            result={"allowed": True, "ok": True, "output": output},
        )



    def test_value_branches_are_really_taken(self):
        """真 SOP 里 '1' 和 default 都去 __ai__，看不出分支真走了；这里给三条分支不同的去处。"""
        data = {
            "matched": True,
            "steps": [
                {"id": "ping", "action": {"tool": "zbx_items", "key": "icmpping"},
                 "branches": [{"when": "value == '0'", "goto": "down"},
                              {"when": "value == '1'", "goto": "up"},
                              {"when": "default", "goto": "unknown"}]},
                {"id": "down", "why": "不通。", "action": {"tool": "device_show", "command": "show clock"}},
                {"id": "up", "why": "通。", "action": {"tool": "device_show", "command": "show version"}},
                {"id": "unknown", "why": "不知道。", "action": {"tool": "device_show", "command": "show users"}},
            ],
        }
        self.assertIn("SOP 下一步：down", agent_loop.SopRuntimeState(data).hint_for(self._items("0"), tool_call_index=1))
        self.assertIn("SOP 下一步：up", agent_loop.SopRuntimeState(data).hint_for(self._items("1"), tool_call_index=1))
        self.assertIn("SOP 下一步：up", agent_loop.SopRuntimeState(data).hint_for(self._items("1.0"), tool_call_index=1))
        self.assertIn("SOP 下一步：unknown",
                      agent_loop.SopRuntimeState(data).hint_for(self._items("0", items=[]), tool_call_index=1))


    def test_bgp_peer_route_step_uses_agent_filled_peer_address(self):
        data = self._lookup("Syslog: BGP neighbor down")
        self.assertEqual(data["playbook"], "bgp-session")
        steps = {s["id"]: s for s in data["steps"]}
        self.assertEqual(steps["check_peer_reachability"]["action"]["command"], "show ip route <对端地址>")
        self.assertEqual(steps["check_bgp_neighbor_detail"]["action"]["command"], "show ip bgp neighbors")
        self.assertEqual(steps["check_peer_side"]["action"], {"tool": "topology_neighbors"})
        sop = agent_loop.SopRuntimeState(data)
        sop.hint_for(self._dev("show logging | begin Sep 26 04", "%BGP-5-ADJCHANGE: neighbor 10.0.0.2 Down"), tool_call_index=1)
        hint = sop.hint_for(self._dev("show ip bgp summary", "10.0.0.2 4 65000 0 0 1 0 0 00:01:02 Idle"), tool_call_index=2)
        self.assertIn("command=show ip route <对端地址>", hint)
        hint = sop.hint_for(self._dev("show ip route 10.0.0.2", "% Network not in table"), tool_call_index=3)
        self.assertEqual(sop.statuses["check_peer_reachability"], "done")
        self.assertIn("SOP 下一步：confirm_underlay", hint)

    def test_ospf_steps_without_literal_interface_are_told_apart(self):
        data = self._lookup("Syslog: OSPF neighbor down")
        self.assertEqual(data["playbook"], "ospf-adjacency")
        steps = {s["id"]: s for s in data["steps"]}
        self.assertEqual(steps["check_ospf_config"]["action"]["command"], "show ip ospf interface")
        self.assertEqual(steps["confirm_link_layer"]["action"]["command"], "show interfaces <接口>")
        self.assertEqual(steps["check_peer_side"]["action"], {"tool": "topology_neighbors", "interface": "<接口>"})
        sop = agent_loop.SopRuntimeState(data)
        sop.hint_for(self._dev("show ip ospf neighbor", ""), tool_call_index=1)
        hint = sop.hint_for(self._dev("show ip ospf interface brief", "Gi0/1 1 0 10.0.0.1/30 1 DOWN 0/0"),
                            tool_call_index=2)
        self.assertEqual(sop.statuses["check_ospf_interface_state"], "done")
        self.assertEqual(sop.statuses["check_ospf_config"], "not_reached")
        self.assertIn("SOP 下一步：confirm_link_layer", hint)
        sop.hint_for(self._dev("show interfaces GigabitEthernet0/1", "GigabitEthernet0/1 is down"), tool_call_index=3)
        self.assertEqual(sop.statuses["confirm_link_layer"], "done")


class Test撞预算时带着证据收尾(unittest.TestCase):
    """Win 实测：同一个问题连跑 4 次，**2 次撞 6 万 token 预算
 只回一句「没查完」**，查到的东西全浪费了。

 根因不是收尾逻辑不存在，而是 `last_model_text` 一直是空的——
 **工具调用那一轮模型往往只输出 tool_calls、一个字正文都没有。**
 所以补一次不给工具的收尾调用。
    """

    def _trace_with_evidence(self):
        t = agent_loop.ChatRunTrace(trace_id="t", question="哪个口流量最大", session_id="s")
        t.tool_calls = [
            agent_loop.ToolCallRecord(tool="zbx_top_talkers", args={"window": "3h"},
                                      ok=True, result={"top": [{"host": "V1", "max": 2127792}]}),
            agent_loop.ToolCallRecord(tool="zbx_items", args={"item_id": "1"}, ok=False, error="boom"),
        ]
        return t

    def test_把已查到的东西整理成答案(self):
        fake = mock.MagicMock()
        fake.complete.return_value = mock.MagicMock(content="V1 Gi0/2 最大，约 2.1 Mbps。还差错包没查。")
        with mock.patch.object(agent_loop, "LLMClient", return_value=fake), \
             mock.patch.object(agent_loop, "env",
                               return_value={"LLM_BASE_URL": "http://x", "LLM_API_KEY": "k", "LLM_MODEL": "m"}):
            out = agent_loop._wrap_up_call("哪个口流量最大", self._trace_with_evidence(), "预算用完")
        self.assertIn("2.1 Mbps", out)
        # **失败的工具调用不该进收尾素材**，它没带来证据
        sent = fake.complete.call_args[0][0][-1]["content"]
        self.assertIn("zbx_top_talkers", sent)
        self.assertNotIn("zbx_items", sent)

    def test_一条证据都没有就不发请求(self):
        t = agent_loop.ChatRunTrace(trace_id="t", question="q", session_id="s")
        fake = mock.MagicMock()
        with mock.patch.object(agent_loop, "LLMClient", return_value=fake):
            self.assertEqual(agent_loop._wrap_up_call("q", t, "预算用完"), "这一轮没查到任何东西。")
        fake.complete.assert_not_called()

    def test_没配模型就不发请求(self):
        fake = mock.MagicMock()
        with mock.patch.object(agent_loop, "LLMClient", return_value=fake), \
             mock.patch.object(agent_loop, "env", return_value={}):
            out = agent_loop._wrap_up_call("q", self._trace_with_evidence(), "x")
            self.assertIn("已取得的证据摘要", out)
            self.assertIn("zbx_top_talkers", out)
        fake.complete.assert_not_called()

    def test_abort_answer_always_has_prefix_and_empty_evidence_text(self):
        t = agent_loop.ChatRunTrace(trace_id="t", question="q", session_id="s")
        out = agent_loop._abort_answer("q", t, "工具调用预算已用完：max_tool_calls=20。", agent_loop.AgentLoopBudget())
        self.assertTrue(out.startswith("（没查完：工具调用预算已用完"))
        self.assertIn("这一轮没查到任何东西", out)


class Test系统提示只放跨工具的规矩(unittest.TestCase):
    """维护者 「前置后置需要这么多离谱的控制吗？别我们出的教程误人子弟了」
 「假如我是接入 外部 agent 或者 外部 agent 到我的网络环境，它会怎么查怎么测」。

 照那把尺子量：原来 14 条规则里有 9 条是「什么时候用哪个工具」，
 其中 4 条工具描述里本来就写着——同一句话两个地方各写一遍，
 改一处另一处就成了反例。砍到 5 条，路由全部搬进工具自己的描述。

 **这不省 token**：工具描述跟系统提示一样每次请求全都要发。
 省的是"两处打架"和"读的人不知道该信哪个"。
    """

    def _tools(self):
        from netops_ai.graph import chat_agent
        return chat_agent.build_chat_tools(
            agent_loop.ChatRunTrace(trace_id="t", question="q", session_id="s"),
            agent_loop.AgentLoopBudget(),
        )

    def test_系统提示里不许出现工具名(self):
        """出现工具名就说明又在系统提示里做路由了。路由写在工具描述里。"""
        prompt = agent_loop.DEFAULT_SYSTEM_PROMPT
        leaked = sorted(t.name for t in self._tools() if t.name in prompt)
        self.assertEqual(leaked, [], f"这些工具的用法又被写回系统提示了：{leaked}")

    def test_系统提示包含跨轮记忆和查空对象红线(self):
        prompt = agent_loop.DEFAULT_SYSTEM_PROMPT
        self.assertIn("真实出现过的工具调用", prompt)
        self.assertIn("没有任何证据表明它存在过", prompt)
        self.assertIn("不许把“路由表里没有”推成“漏配”", prompt)

    def test_工具描述讲清它做什么(self):
        """砍系统提示不能把话砍没了——抽查几条。

 起工具描述**只写事实**（做什么、参数、返回、来源、限制），不写「什么时候用我、别用谁」，
 见 `tests/test_tool_neutrality.py`。这里核的是事实还在：看描述就知道这个工具能干什么。
        """
        desc = {t.name: t.description for t in self._tools()}
        self.assertIn("流量排行", desc["zbx_top_talkers"])
        self.assertIn("趋势图", desc["zbx_chart"])
        self.assertIn("白名单", desc["device_show"])
        self.assertIn("不执行任何设备或 Zabbix 动作", desc["sop_lookup"])


class Test设备和监控各管各的(unittest.TestCase):
    """维护者 （之后）：「其实登陆机器应该查更重要的东西」
 「日志 syslog server 都有现成的了」。

 五轮实测：gpt-5.5 拿着能登任意设备的通道，跑了四次 `show logging`，
 但 A1 的 buffer 只有 4096 字节，**它自己每次 SSH 登录写进去的记录
 正在把要找的证据挤出去**；决定性的 `%SYS-5-CONFIG_I` 五轮一次都没出现。

 分工：**Zabbix 管「过去发生了什么」，设备管「现在是什么样」。**
    """

    def _desc(self):
        from netops_ai.graph import chat_agent
        tools = chat_agent.build_chat_tools(
            agent_loop.ChatRunTrace(trace_id="t", question="q", session_id="s"),
            agent_loop.AgentLoopBudget(),
        )
        return {t.name: t.description for t in tools}

    # 改写：原来这组测试断言的是**路由**——device_show 的描述里要写「日志别在这儿找、用 zbx_syslog」，
    # zbx_syslog 要写「不要登设备跑 show logging」。实测这句劝阻让两个 agent 在 Zabbix 恰好
    # 没收到 syslog 时都没去看设备 buffer，错过了决定性证据。现在描述只写事实：数据从哪来、
    # 反映哪个时刻、有什么局限；「两个来源各有局限」写在取证提示里（见 test_api_pipeline）。

    def test_有专门取syslog的工具且说清数据来源(self):
        d = self._desc()
        self.assertIn("zbx_syslog", d)
        self.assertIn("Zabbix 收到", d["zbx_syslog"])
        self.assertIn("不是设备本地的日志 buffer", d["zbx_syslog"])

    def test_登设备的描述说清输出是执行时刻的状态(self):
        d = self._desc()["device_show"]
        self.assertIn("执行时刻", d)
        self.assertIn("more system:running-config", d)

    def test_登设备的描述说清本地buffer的局限(self):
        for name in ("device_show", "device_show_many"):
            with self.subTest(tool=name):
                d = self._desc()[name]
                self.assertIn("show logging", d)
                self.assertIn("buffer", d)
                self.assertIn("覆盖", d)
                # 不再在工具之间做路由
                self.assertNotIn("zbx_syslog", d)

    def test_ai_readonly登录记录是取证自己产生的(self):
        d = self._desc()
        for name in ("device_show", "device_show_many", "zbx_syslog"):
            with self.subTest(tool=name):
                self.assertIn("ai-readonly", d[name])
                self.assertIn("取证动作本身", d[name])
        # 「不能当流量来源证据」是怎么用的建议，挪进了两条线的提示词
        from netops_ai.api import pipeline
        self.assertIn("ai-readonly", pipeline.FORENSICS_SYSTEM_PROMPT)

    def test_不许再让模型自己拼itemid取日志(self):
        """原来是四步：列监控项→认出 value_type=2→按 itemid 取历史→合并排序。
        gpt-5.5 都没走通，所以包成了一个工具。"""
        from netops_ai.api import pipeline
        self.assertNotIn("zbx_history", pipeline.FORENSICS_QUESTION)


class TestWrapUpHardening(unittest.TestCase):
    """实测换来的三条收尾约束。

 Win 跑了 12 轮 外部 agent 扮演用户的对话，暴露出三件事：
 ①没撞任何闸门、模型最后一轮只发 tool_calls 正文为空，我们把空串当答案放出去
 （2/12，且都出现在用户质疑之后那一轮）；
 ②闸门收尾 5 次里 3 次只写了「下一步打算」，一条已查到的证据都没列；
 ③R0b 第 2 轮在收尾里编了「BVI11 admin down 导致该网段不发布」还附修复建议。
    """

    def test_没撞闸门但一个字没答也要走收尾(self):
        """用户看到的是「它不理我了」，而前面查到的东西全浪费。"""
        import netops_ai.graph.agent_loop as loop

        trace = loop.ChatRunTrace(trace_id="t", question="q", session_id="s")
        trace.tool_calls.append(
            loop.ToolCallRecord(tool="device_show", args={"command": "show ip route"},
                                ok=True, result={"output": "10.1.3.0 is directly connected"})
        )
        with mock.patch.object(loop, "_wrap_up_call", return_value="收尾答案") as wrap:
            # 直接验分支条件：有工具调用 + 答案为空 → 必须补收尾
            answer = ""
            if not answer and trace.tool_calls:
                answer = loop._wrap_up_call("q", trace, "模型最后没有给出文字回答")
        self.assertEqual(answer, "收尾答案")
        wrap.assert_called_once()

    def test_收尾提示把顺序写死了(self):
        """「能答多少答多少」会被读成「说说下一步打算」，得把三段顺序写死。"""
        import inspect

        import netops_ai.graph.agent_loop as loop

        src = inspect.getsource(loop._wrap_up_call)
        self.assertIn("三段都不许省", src)
        self.assertIn("已经查到了什么", src)
        self.assertIn("目前最可能的结论", src)
        self.assertIn("还差什么", src)
        # 证据不足时允许直说，不要硬凑一个
        self.assertIn("现有证据还不足以判断", src)

    def test_收尾不许编因果也不许给改配置建议(self):
        import inspect

        import netops_ai.graph.agent_loop as loop

        src = inspect.getsource(loop._wrap_up_call)
        self.assertIn("没有直接证据的因果关系一个字都不许写", src)
        self.assertIn("不要给出任何修改配置的建议", src)


class TestTraceRedactionKeepsPlainWords(unittest.TestCase):
    """落盘的取证轨迹也要脱敏，但不能把设备输出里的常用词切碎（`Administrative` → `<redacted>istrative`）。"""

    CFG = {"ZABBIX_USER": "Admin", "DEVICE_PASSWORD": "S3cretPw!", "DEVICE_HOST": "192.0.2.50", "DEVICE_USERNAME": "ai-readonly"}

    def _r(self, text):
        with mock.patch.object(agent_loop, "env", return_value=self.CFG):
            return agent_loop._redact_env_values(text)

    def test_纯字母用户名不替换(self):
        self.assertEqual(self._r("Idle (Admin) Administrative Shutdown"), "Idle (Admin) Administrative Shutdown")

    def test_密码主机和带连字符的用户名照旧遮掉(self):
        self.assertEqual(self._r("pw S3cretPw! host 192.0.2.50 user ai-readonly"),
                         "pw <redacted> host <redacted> user <redacted>")

    def test_只换独立出现的值(self):
        self.assertEqual(self._r("x192.0.2.500y 192.0.2.50"), "x192.0.2.500y <redacted>")

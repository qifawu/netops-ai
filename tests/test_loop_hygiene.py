"""循环卫生：逐轮进度、完全相同调用的缓存、上下文压缩。

三件都是**中性**的：给模型看的文字只陈述事实（数字、「未重新执行」「已省略」），
不写建议/劝阻/催促。判据复用 `test_tool_neutrality.advisory_phrases`。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool

from netops_ai.api import pipeline
from netops_ai.graph import agent_loop, chat_agent
from netops_ai.zabbix import cli as zbx_cli
from tests.test_tool_neutrality import advisory_phrases

ON = agent_loop.LoopHygiene()


@contextmanager
def _patch_trace_dir():
    with mock.patch.object(agent_loop, "TRACE_DIR", Path(tempfile.mkdtemp())):
        yield


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _trace() -> agent_loop.ChatRunTrace:
    trace = agent_loop.ChatRunTrace(trace_id="t", question="q", session_id="s")
    trace.clock = _Clock()
    return trace


def _recording_tool(trace, budget, name: str, *, result_of=None, calls: list | None = None, fail: bool = False):
    """跟真工具一样：自己在 finally 里 `record_tool_call()`。"""
    calls = calls if calls is not None else []

    def run(command: str, limit: int = 50) -> str:
        calls.append((command, limit))
        record = agent_loop.ToolCallRecord(tool=name, args={"command": command, "limit": limit}, ok=False)
        try:
            if fail:
                raise TimeoutError("device unreachable")
            data = result_of(command) if result_of else {"output": f"{command} #{len(calls)}"}
            record.result = data
            record.ok = not (isinstance(data, dict) and data.get("allowed") is False)
            return json.dumps(data, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            record.error = f"{type(exc).__name__}: {exc}"
            return json.dumps({"error": record.error}, ensure_ascii=False)
        finally:
            trace.record_tool_call(record, budget)

    return StructuredTool.from_function(func=run, name=name, description="test"), calls


def _wrapped(trace, budget, name="device_show", hygiene=ON, **kwargs):
    tool, calls = _recording_tool(trace, budget, name, **kwargs)
    return agent_loop.apply_loop_hygiene([tool], trace, budget, hygiene)[0], calls


def _split(reply: str) -> tuple[dict, str]:
    body, _, progress = reply.partition("\n[进度] ")
    return json.loads(body), progress


class Test逐轮进度(unittest.TestCase):
    def test_数字与预算和计数一致(self):
        trace, budget = _trace(), agent_loop.AgentLoopBudget(max_tool_calls=6, no_progress_threshold=3, max_tokens=0)
        tool, _ = _wrapped(trace, budget)
        reply = tool.invoke({"command": "show version"})
        self.assertTrue(reply.endswith("\n[进度] 本次分析已调用工具 1 次（上限 6）；连续无新信息 0 次（上限 3）"), reply)
        tool.invoke({"command": "show clock"})
        reply = tool.invoke({"command": "show clock"})  # 缓存返回：不计调用次数，计一次无进展
        self.assertIn("本次分析已调用工具 2 次（上限 6）", reply)
        self.assertIn("连续无新信息 1 次（上限 3）", reply)
        self.assertIn("另有 1 次缓存或去重返回未计入调用次数", reply)
        self.assertEqual(trace._no_progress_streak, 1)

    def test_没有上限写无上限_有token上限时带上token(self):
        trace = _trace()
        trace.tokens_spent = 1234
        budget = agent_loop.AgentLoopBudget.unbounded(max_tokens=50000)
        tool, _ = _wrapped(trace, budget)
        reply = tool.invoke({"command": "show version"})
        self.assertIn("本次分析已调用工具 1 次（无上限）；连续无新信息 0 次（无上限）", reply)
        self.assertIn("token 已用 1234 / 上限 50000", reply)

    def test_关开关后不出现(self):
        with mock.patch.object(agent_loop, "env", return_value={"AGENT_LOOP_PROGRESS": "off"}):
            hygiene = agent_loop.LoopHygiene.from_env()
        self.assertFalse(hygiene.progress)
        self.assertTrue(hygiene.cache)
        trace, budget = _trace(), agent_loop.AgentLoopBudget()
        tool, _ = _wrapped(trace, budget, hygiene=hygiene)
        self.assertNotIn("[进度]", tool.invoke({"command": "show version"}))
        self.assertNotIn("[进度]", tool.invoke({"command": "show version"}))  # 缓存返回也不带

    def test_默认值_进度开缓存开压缩关(self):
        with mock.patch.object(agent_loop, "env", return_value={}):
            hygiene = agent_loop.LoopHygiene.from_env()
        self.assertEqual((hygiene.progress, hygiene.cache, hygiene.compact), (True, True, False))
        self.assertEqual((hygiene.cache_ttl, hygiene.compact_keep), (60.0, 3))


    def test_告警路径的无上限预算(self):
        replies: list[str] = []

        def tools(trace, budget):
            return [_recording_tool(trace, budget, "device_show")[0]]

        def create_agent_side_effect(**kwargs):
            tool = kwargs["tools"][0]

            class Agent:
                def invoke(self, payload, config=None):
                    replies.append(tool.invoke({"command": "show version"}))
                    return {"messages": [AIMessage(content="done")]}

            return Agent()

        with (
            mock.patch.object(agent_loop, "_build_loop_model", return_value=object()),
            mock.patch.object(agent_loop, "create_agent", side_effect=create_agent_side_effect),
            mock.patch.object(agent_loop, "env", return_value={}),
            _patch_trace_dir(),
        ):
            agent_loop.run_agent_loop(tools, question="q", budget=agent_loop.AgentLoopBudget.unbounded(max_tokens=0))
        self.assertIn("本次分析已调用工具 1 次（无上限）", replies[0])


class Test完全相同调用的缓存(unittest.TestCase):
    def test_同参数第二次不触发handler(self):
        trace, budget = _trace(), agent_loop.AgentLoopBudget()
        tool, calls = _wrapped(trace, budget)
        first, _ = _split(tool.invoke({"command": "show interfaces Gi0/4"}))
        trace.clock.t += 12
        second, _ = _split(tool.invoke({"command": "show interfaces Gi0/4"}))
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            {k: second[k] for k in ("cached", "same_as_call", "age_seconds")},
            {"cached": True, "same_as_call": 1, "age_seconds": 12},
        )
        self.assertEqual(second["note"], "与第 1 次调用的工具和参数相同，未重新执行；result 是那次的返回。")
        self.assertEqual(second["result"], first)
        record = trace.tool_calls[-1]
        self.assertTrue(record.cached and record.deduped and record.ok)
        # trace 里原来那次的 result 不动
        self.assertEqual(trace.tool_calls[0].result, first)

    def test_参数顺序不同或默认值显式写出也命中(self):
        trace, budget = _trace(), agent_loop.AgentLoopBudget()
        tool, calls = _wrapped(trace, budget)
        tool.invoke({"command": "show version"})
        reply, _ = _split(tool.invoke({"limit": 50, "command": "  show version "}))
        self.assertTrue(reply["cached"])
        self.assertEqual(len(calls), 1)
        tool.invoke({"command": "show version", "limit": 10})  # 参数真不同
        self.assertEqual(len(calls), 2)

    def test_TTL内命中_过期后重新执行(self):
        trace, budget = _trace(), agent_loop.AgentLoopBudget(no_progress_threshold=0)
        tool, calls = _wrapped(trace, budget, hygiene=agent_loop.LoopHygiene(cache_ttl=60))
        tool.invoke({"command": "show logging"})
        trace.clock.t += 60
        self.assertTrue(_split(tool.invoke({"command": "show logging"}))[0]["cached"])
        trace.clock.t += 1  # 距第一次 61 秒
        reply, _ = _split(tool.invoke({"command": "show logging"}))
        self.assertNotIn("cached", reply)
        self.assertEqual(len(calls), 2)
        # 过期重查后缓存指向新的那次（第 3 次调用）
        self.assertEqual(_split(tool.invoke({"command": "show logging"}))[0]["same_as_call"], 3)

    def test_台账类本次分析内一直有效(self):
        trace, budget = _trace(), agent_loop.AgentLoopBudget()
        tool, calls = _wrapped(trace, budget, name="nb_devices")
        tool.invoke({"command": "A1"})
        trace.clock.t += 86400
        self.assertTrue(_split(tool.invoke({"command": "A1"}))[0]["cached"])
        self.assertEqual(len(calls), 1)

    def test_新鲜度分类(self):
        ttl = 60.0
        cases = {
            ("device_show", ()): ttl,
            ("device_show_many", ()): ttl,
            ("zbx_problems", ()): ttl,
            ("zbx_items", ()): ttl,
            ("zbx_hosts", ()): ttl,
            ("zbx_syslog", (("since", "1790391000"), ("until", "1790392000"))): None,
            ("zbx_syslog", (("since", "-30m"), ("until", ""))): ttl,
            ("zbx_syslog", (("since", "1790391000"), ("until", "-5m"))): ttl,
            ("zbx_history", (("item_id", "1"), ("since", "1790391000"))): ttl,  # 没有 until：结束于 now
            ("zbx_trends", (("item_id", "1"), ("since", "-7d"))): ttl,
            ("nb_devices", ()): None,
            ("topology_neighbors", ()): None,
            ("sop_lookup", ()): None,
            ("doc_search", ()): None,
            ("list_analyses", ()): None,
            ("get_analysis", ()): None,
            ("zbx_chart", ()): 0,
            ("some_new_tool", ()): ttl,
        }
        for (tool, args), expected in cases.items():
            with self.subTest(tool=tool, args=args):
                self.assertEqual(agent_loop.cache_ttl(tool, dict(args), ttl), expected)

    def test_被拒命令重复发送按缓存返回上次的拒绝(self):
        trace, budget = _trace(), agent_loop.AgentLoopBudget(no_progress_threshold=0)
        denied = {"command": "conf t", "allowed": False, "ok": False, "output": "", "error": "",
                  "denial_reason": "not in whitelist"}
        tool, calls = _wrapped(trace, budget, result_of=lambda _c: denied)
        tool.invoke({"command": "conf t"})
        reply, _ = _split(tool.invoke({"command": "conf t"}))
        self.assertEqual(len(calls), 1)
        self.assertTrue(reply["cached"])
        self.assertEqual(reply["result"]["denial_reason"], "not in whitelist")
        # 不做意图归一：换个写法就是新调用
        tool.invoke({"command": "configure terminal"})
        self.assertEqual(len(calls), 2)

    def test_报错不缓存(self):
        trace, budget = _trace(), agent_loop.AgentLoopBudget(no_progress_threshold=0)
        tool, calls = _wrapped(trace, budget, fail=True)
        tool.invoke({"command": "show version"})
        reply, _ = _split(tool.invoke({"command": "show version"}))
        self.assertNotIn("cached", reply)
        self.assertEqual(len(calls), 2)

    def test_缓存返回计入无进展且不消耗工具预算(self):
        trace = _trace()
        budget = agent_loop.AgentLoopBudget(max_tool_calls=2, no_progress_threshold=3)
        tool, calls = _wrapped(trace, budget)
        tool.invoke({"command": "show logging | include LINK"})
        tool.invoke({"command": "show logging | include LINK"})
        tool.invoke({"command": "show logging | include LINK"})
        with self.assertRaises(agent_loop.AgentLoopAbort):
            tool.invoke({"command": "show logging | include LINK"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(trace.tool_calls), 4)
        self.assertIn("连续 3 跳没带来新信息", trace.termination_reason)
        self.assertIn("返回的是缓存", trace.termination_reason)

    def test_关开关后行为等同旧版(self):
        trace, budget = _trace(), agent_loop.AgentLoopBudget(no_progress_threshold=0)
        with mock.patch.object(agent_loop, "env", return_value={"AGENT_LOOP_CACHE": "off", "AGENT_LOOP_PROGRESS": "off"}):
            hygiene = agent_loop.LoopHygiene.from_env()
        raw_tool, calls = _recording_tool(trace, budget, "device_show")
        wrapped = agent_loop.apply_loop_hygiene([raw_tool], trace, budget, hygiene)
        self.assertIs(wrapped[0], raw_tool)  # 两个都关：工具原样注册
        wrapped[0].invoke({"command": "show version"})
        wrapped[0].invoke({"command": "show version"})
        self.assertEqual(len(calls), 2)
        self.assertFalse(any(c.cached for c in trace.tool_calls))
        # 只关缓存、进度开着：handler 照样每次执行
        tool, calls = _wrapped(_trace(), budget, hygiene=agent_loop.LoopHygiene(cache=False))
        tool.invoke({"command": "show version"})
        tool.invoke({"command": "show version"})
        self.assertEqual(len(calls), 2)

    def test_缓存的是去掉SOP提示和进度行的原文(self):
        trace, budget = _trace(), agent_loop.AgentLoopBudget()
        hint = "\n[SOP 下一步：flap_history——看日志；tool=zbx_syslog]"

        def run(command: str) -> str:
            record = agent_loop.ToolCallRecord(tool="device_show", args={"command": command}, ok=True, result={"output": "x"})
            try:
                trace._last_sop_hint = hint
                return json.dumps({"output": "x"}) + hint
            finally:
                trace.record_tool_call(record, budget)

        tool = agent_loop.apply_loop_hygiene(
            [StructuredTool.from_function(func=run, name="device_show", description="t")], trace, budget, ON
        )[0]
        self.assertIn("SOP 下一步", tool.invoke({"command": "show version"}))
        reply = tool.invoke({"command": "show version"})
        body, progress = _split(reply)
        self.assertEqual(body["result"], {"output": "x"})
        self.assertNotIn("SOP", reply)
        self.assertEqual(reply.count("[进度]"), 1)

    def test_缓存在历史重叠窗口拦截之前(self):
        """完全相同 → 缓存；窗口重叠但参数不同 → 仍走原来的 dedupe_history_query。"""
        handler_calls = []

        def fake_history(ns):
            handler_calls.append(ns.since)
            return [{"clock": "1790391000", "value": "1"}]

        trace, budget = _trace(), agent_loop.AgentLoopBudget(no_progress_threshold=0)
        with mock.patch.object(zbx_cli, "cmd_history", fake_history):
            tools = {t.name: t for t in chat_agent.build_zabbix_tools(trace, budget)}
        history = agent_loop.apply_loop_hygiene([tools["zbx_history"]], trace, budget, ON)[0]
        history.invoke({"item_id": 54089, "since": "-1h"})
        cached, _ = _split(history.invoke({"item_id": "54089", "since": "-1h", "limit": 100}))
        overlap, _ = _split(history.invoke({"item_id": "54089", "since": "-30m"}))
        self.assertEqual(handler_calls, ["-1h"])
        self.assertTrue(cached["cached"])
        self.assertTrue(overlap["deduped"])
        self.assertNotIn("cached", overlap)
        self.assertEqual([(c.cached, c.deduped) for c in trace.tool_calls], [(False, False), (True, True), (False, True)])

    def test_落盘trace带cached字段(self):
        trace, budget = _trace(), agent_loop.AgentLoopBudget()
        tool, _ = _wrapped(trace, budget)
        tool.invoke({"command": "show version"})
        tool.invoke({"command": "show version"})
        with _patch_trace_dir():
            payload = json.loads(agent_loop.save_trace(trace).read_text(encoding="utf-8"))
        self.assertEqual([c["cached"] for c in payload["tool_calls"]], [False, True])


def _tool_turns(n: int, lengths) -> list[Any]:
    messages: list[Any] = [HumanMessage(content="A1 Gi0/4 down")]
    for i in range(1, n + 1):
        call_id = f"call_{i}"
        messages.append(AIMessage(content="", tool_calls=[{"id": call_id, "name": "device_show",
                                                            "args": {"command": f"show x {i}"}}]))
        messages.append(ToolMessage(content=f"R{i}:" + "y" * (lengths(i) - len(f"R{i}:")),
                                    tool_call_id=call_id, name="device_show"))
    return messages


class Test上下文压缩(unittest.TestCase):
    def test_最近K个原文保留_更早的换成占位_原消息不动(self):
        messages = _tool_turns(5, lambda i: 1000)
        snapshot = [m.model_copy(deep=True) for m in messages]
        out = agent_loop.compact_tool_messages(messages, keep=3)
        tools_out = [m for m in out if isinstance(m, ToolMessage)]
        self.assertTrue(tools_out[0].content.startswith("[第 1 次工具调用（device_show，{\"command\": \"show x 1\"}）的结果"))
        self.assertIn("共 1000 字符，已省略 600 字符；开头 400 字符：R1:yyy", tools_out[0].content)
        self.assertTrue(tools_out[1].content.startswith("[第 2 次工具调用"))
        self.assertEqual([m.content for m in tools_out[2:]], [m.content for m in messages[6::2]])
        self.assertEqual(tools_out[0].tool_call_id, "call_1")
        # 原列表和原消息对象一个都没改
        self.assertEqual([m.content for m in messages], [m.content for m in snapshot])
        self.assertIsNot(out, messages)

    def test_占位里的字符数正确(self):
        messages = _tool_turns(2, lambda i: 5000 + i)
        placeholder = agent_loop.compact_tool_messages(messages, keep=1)[2].content
        self.assertIn("共 5001 字符，已省略 4601 字符；开头 400 字符：", placeholder)
        head = placeholder.split("开头 400 字符：", 1)[1][:-1]
        self.assertEqual(head, messages[2].content[:400])

    def test_短结果不压(self):
        messages = _tool_turns(5, lambda i: 300)
        self.assertEqual([m.content for m in agent_loop.compact_tool_messages(messages, keep=1)],
                         [m.content for m in messages])

    def test_默认关时不给create_agent传middleware(self):
        with (
            mock.patch.object(agent_loop, "_build_loop_model", return_value=object()),
            mock.patch.object(agent_loop, "create_agent", return_value=mock.Mock(invoke=lambda *a, **k: {"messages": []})) as ca,
            mock.patch.object(agent_loop, "env", return_value={}),
            _patch_trace_dir(),
        ):
            agent_loop.run_agent_loop([], question="q")
        self.assertNotIn("middleware", ca.call_args.kwargs)

    def test_真实create_agent里只压模型看到的_trace和state全量(self):
        """用真的 `create_agent` + 一个假模型：模型每次收到的 messages 记下来。"""
        seen: list[list[Any]] = []

        class FakeModel(BaseChatModel):
            @property
            def _llm_type(self) -> str:
                return "fake"

            def bind_tools(self, tools, **kwargs):  # noqa: ANN001
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
                seen.append(list(messages))
                n = len(seen)
                if n <= 5:
                    msg = AIMessage(content="", tool_calls=[{"id": f"c{n}", "name": "device_show",
                                                             "args": {"command": f"show x {n}"}}])
                else:
                    msg = AIMessage(content="done")
                return ChatResult(generations=[ChatGeneration(message=msg)])

        def tools(trace, budget):
            def device_show(command: str) -> str:
                text = f"{command}:" + "z" * 2000
                record = agent_loop.ToolCallRecord(tool="device_show", args={"command": command}, ok=True, result=text)
                try:
                    return text
                finally:
                    trace.record_tool_call(record, budget)

            return [StructuredTool.from_function(func=device_show, name="device_show", description="t")]

        with (
            mock.patch.object(agent_loop, "_build_loop_model", return_value=FakeModel()),
            mock.patch.object(agent_loop, "env", return_value={}),
            _patch_trace_dir(),
        ):
            result = agent_loop.run_agent_loop(
                tools, question="q", budget=agent_loop.AgentLoopBudget(no_progress_threshold=0),
                hygiene=agent_loop.LoopHygiene(progress=False, compact=True, compact_keep=2),
            )
        self.assertEqual(result.answer, "done")
        last = [m for m in seen[-1] if isinstance(m, ToolMessage)]
        self.assertEqual(len(last), 5)
        self.assertTrue(all(m.content.startswith("[第 ") for m in last[:3]))
        self.assertTrue(all(m.content.endswith("z" * 100) for m in last[3:]))
        # state 里的消息（final transcript 的底本）和 trace 都是全量原文
        state_tools = [m for m in result.messages if isinstance(m, ToolMessage)]
        self.assertTrue(all(len(m.content) > 2000 for m in state_tools))
        self.assertTrue(all(len(c.result) > 2000 for c in result.trace.tool_calls))
        self.assertNotIn("已省略", agent_loop.build_transcript(result.messages))
        self.assertNotIn("已省略", agent_loop.build_transcript_from_trace(result.trace))

    def test_离线度量_W33规模的失控轨迹(self):
        """60 次调用、每次 3000~8000 字符：累计送给模型的工具结果字符数，压缩前后。"""
        before, after = offline_measure(60, keep=3)
        self.assertLess(after, before * 0.2, (before, after))


def _w33_length(i: int) -> int:
    return 3000 + (i * 1373) % 5001  # 确定性地铺满 3000~8000


def offline_measure(n: int, *, keep: int) -> tuple[int, int]:
    """第 k 次模型调用看到前 k-1 个工具结果；n 次工具调用共 n+1 次模型调用。"""
    messages = _tool_turns(n, _w33_length)
    total_before = total_after = 0
    for k in range(n + 1):
        prefix = messages[: 1 + 2 * k]
        total_before += sum(len(m.content) for m in prefix if isinstance(m, ToolMessage))
        total_after += sum(
            len(m.content) for m in agent_loop.compact_tool_messages(prefix, keep=keep) if isinstance(m, ToolMessage)
        )
    return total_before, total_after


class Test新增文案是中性的(unittest.TestCase):
    def test_进度行缓存说明压缩占位里没有劝阻或催促(self):
        trace = _trace()
        trace.tokens_spent = 99
        texts = [
            trace.progress_line(agent_loop.AgentLoopBudget(max_tokens=100)),
            trace.progress_line(agent_loop.AgentLoopBudget.unbounded(max_tokens=0)),
            agent_loop._cache_note(3),
            agent_loop.compaction_placeholder(4, "device_show", {"command": "show logging"}, "x" * 900),
        ]
        trace.tool_calls.append(agent_loop.ToolCallRecord(tool="t", args={}, ok=True, deduped=True))
        texts.append(trace.progress_line(agent_loop.AgentLoopBudget()))
        extra = ("应该", "请", "收尾", "重查", "需要")
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(advisory_phrases(text), [])
                self.assertFalse([w for w in extra if w in text], text)

    def test_取证提示里不再要求在思考里写清(self):
        self.assertNotIn("想清楚", pipeline.FORENSICS_SYSTEM_PROMPT)
        self.assertIn("可以自己补查", pipeline.FORENSICS_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()

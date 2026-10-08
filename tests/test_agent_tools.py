"""智能体工具注册（`graph/chat_agent.py`）：Zabbix / 设备 / 拓扑 / SOP / 文档检索工具的生成、白名单闸门、trace 记录。
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from netops_ai.graph import chat_agent


class TestChatAgentTools(unittest.TestCase):
    def test_zabbix_tools_are_generated_from_cli_specs(self):
        trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")
        tools = chat_agent.build_zabbix_tools(trace)
        self.assertEqual(
            {t.name for t in tools},
            {"zbx_" + s.name.replace("-", "_") for s in chat_agent.zbx_cli.COMMANDS},
        )

    def test_zabbix_tool_calls_cli_handler_and_records_trace(self):
        trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")
        tool = next(t for t in chat_agent.build_zabbix_tools(trace) if t.name == "zbx_hosts")
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.list_hosts.return_value = [{"hostid": "1", "host": "V1", "name": "V1"}]
        with mock.patch("netops_ai.zabbix.cli._client", return_value=fake):
            raw = tool.invoke({"query": "V1", "limit": 5})
        data = json.loads(raw)
        self.assertEqual(data["hosts"][0]["host"], "V1")
        self.assertEqual(trace.tool_calls[0].tool, "zbx_hosts")
        self.assertTrue(trace.tool_calls[0].ok)

    def test_zabbix_history_dedup_skips_cli_handler_and_records_trace(self):
        trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")
        budget = chat_agent.AgentLoopBudget(max_tool_calls=10, no_progress_threshold=0)
        handler = mock.MagicMock(return_value={"points": [{"clock": 100, "value": "1"}]})
        with mock.patch("netops_ai.graph.chat_agent.zbx_cli.cmd_history", handler):
            tool = next(t for t in chat_agent.build_zabbix_tools(trace, budget) if t.name == "zbx_history")
            first = tool.invoke({"item_id": "54089", "since": "100", "limit": 10})
            second = tool.invoke({"item_id": "54089", "since": "150", "limit": 10})

        self.assertEqual(json.loads(first)["points"][0]["value"], "1")
        self.assertIn("itemid 54089", second)
        # （外部 agent 反馈）：拦截时把上次的结果原样带回，并注明是第几次调用的同一查询
        dup = json.loads(second)
        self.assertTrue(dup["deduped"])
        self.assertEqual(dup["same_as_call"], 1)
        self.assertIn("#1 号调用", dup["note"])
        self.assertEqual(dup["previous_result"], json.loads(first))
        self.assertEqual(handler.call_count, 1)
        self.assertEqual(len(trace.tool_calls), 2)
        self.assertFalse(trace.tool_calls[0].deduped)
        self.assertTrue(trace.tool_calls[1].deduped)
        self.assertTrue(trace.tool_calls[1].ok)

    def test_device_tool_uses_adapter_run_gate(self):
        trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")
        tool = next(t for t in chat_agent.build_device_tools(trace) if t.name == "device_show")
        adapter = mock.MagicMock()
        adapter.__enter__.return_value = adapter
        adapter.run.return_value = mock.MagicMock(
            command="configure terminal",
            allowed=False,
            ok=False,
            output="",
            error="",
            denial_reason="命中禁令动词",
        )
        with mock.patch("netops_ai.graph.chat_agent._device_adapter", return_value=adapter):
            raw = tool.invoke({"command": "configure terminal"})
        data = json.loads(raw)
        self.assertFalse(data["allowed"])
        adapter.run.assert_called_once_with("configure terminal")

    def test_device_show_ok_requires_device_execution_not_just_whitelist_allow(self):
        """审查发现（真实数据坐实，一条真实告警）：以前 `record.ok` 只看 `allowed`
 （有没有发给设备），不看 `payload["ok"]`（设备是不是真的执行成功）。那条真实记录里
 `show logging`/`show running-config` 被白名单放行、但设备回显
 "Line has invalid autocommand"（执行失败），trace 却记成了 ok=true——循环卫生和
 审计看到的是「成功拿到材料」，实际是一次没拿到任何东西的失败调用。
        """
        trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")
        tool = next(t for t in chat_agent.build_device_tools(trace) if t.name == chat_agent.DEVICE_SHOW_TOOL_NAME)
        adapter = mock.MagicMock()
        adapter.__enter__.return_value = adapter
        adapter.run.return_value = mock.MagicMock(
            command="show logging", allowed=True, ok=False,
            output="", error='Line has invalid autocommand "show logging"', denial_reason="",
        )
        with mock.patch("netops_ai.graph.chat_agent._device_adapter", return_value=adapter):
            raw = tool.invoke({"command": "show logging"})

        data = json.loads(raw)
        self.assertTrue(data["allowed"])   # 白名单放行了
        self.assertFalse(data["ok"])       # 但设备没有真的执行成功
        self.assertFalse(trace.tool_calls[0].ok)  # trace 记录跟着 ok，不能只看 allowed

    def test_device_many_uses_one_adapter_session_and_keeps_per_command_results(self):
        trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")
        tool = next(t for t in chat_agent.build_device_tools(trace) if t.name == "device_show_many")
        adapter = mock.MagicMock()
        adapter.__enter__.return_value = adapter
        adapter.run.side_effect = [
            mock.MagicMock(
                command="show ip interface brief",
                allowed=True,
                ok=True,
                output="Gi0/1 up",
                error="",
                denial_reason="",
            ),
            mock.MagicMock(
                command="configure terminal",
                allowed=False,
                ok=False,
                output="",
                error="",
                denial_reason="命中禁令动词",
            ),
        ]
        with mock.patch("netops_ai.graph.chat_agent._device_adapter", return_value=adapter):
            raw = tool.invoke({"commands": ["show ip interface brief", "configure terminal"]})

        data = json.loads(raw)
        self.assertFalse(data["truncated"])
        self.assertEqual(data["received_count"], 2)
        self.assertEqual(data["executed_count"], 2)
        self.assertEqual([r["command"] for r in data["results"]], ["show ip interface brief", "configure terminal"])
        self.assertTrue(data["results"][0]["allowed"])
        self.assertFalse(data["results"][1]["allowed"])
        adapter.__enter__.assert_called_once()
        self.assertEqual(adapter.run.call_count, 2)
        self.assertEqual(trace.tool_calls[0].tool, "device_show_many")
        # 审查发现并修：批量调用以前不管每条命令是否被拒/执行失败，只要循环没抛异常
        # 就记 record.ok=True。这批里第二条被白名单拒了，trace 层面不该显得跟全部成功一样。
        self.assertFalse(trace.tool_calls[0].ok)

    def test_device_many_truncates_with_explicit_metadata(self):
        trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")
        tool = next(t for t in chat_agent.build_device_tools(trace) if t.name == "device_show_many")
        adapter = mock.MagicMock()
        adapter.__enter__.return_value = adapter
        adapter.run.side_effect = [
            mock.MagicMock(
                command="show version",
                allowed=True,
                ok=True,
                output="version",
                error="",
                denial_reason="",
            ),
            mock.MagicMock(
                command="show logging",
                allowed=True,
                ok=True,
                output="logging",
                error="",
                denial_reason="",
            ),
        ]
        commands = ["show version", "show logging", "show ip route"]
        with (
            mock.patch("netops_ai.graph.chat_agent._device_adapter", return_value=adapter),
            mock.patch("netops_ai.graph.chat_agent.env", return_value={"DEVICE_BATCH_MAX_COMMANDS": "2"}),
        ):
            raw = tool.invoke({"commands": commands})

        data = json.loads(raw)
        self.assertTrue(data["truncated"])
        self.assertEqual(data["received_count"], 3)
        self.assertEqual(data["executed_count"], 2)
        self.assertEqual(data["max_commands"], 2)
        self.assertEqual([r["command"] for r in data["results"]], commands[:2])
        self.assertEqual(adapter.run.call_count, 2)

    def test_build_chat_tools_keeps_single_and_batch_device_tools(self):
        trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")
        names = {t.name for t in chat_agent.build_chat_tools(trace, chat_agent.AgentLoopBudget())}
        self.assertIn("device_show", names)
        self.assertIn("device_show_many", names)

    def test_include_doc_search_true覆盖环境变量打开知识库工具(self):
        """A16：告警这条线显式传 include_doc_search=True，不管 DOC_SEARCH 有没有配。"""
        trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")
        with mock.patch("netops_ai.graph.chat_agent.env", return_value={}):
            names = {
                t.name
                for t in chat_agent.build_chat_tools(trace, chat_agent.AgentLoopBudget(), include_doc_search=True)
            }
        self.assertIn("doc_search", names)

    def test_include_doc_search_false覆盖环境变量关掉知识库工具(self):
        trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")
        with mock.patch("netops_ai.graph.chat_agent.env", return_value={"DOC_SEARCH": "on"}):
            names = {
                t.name
                for t in chat_agent.build_chat_tools(trace, chat_agent.AgentLoopBudget(), include_doc_search=False)
            }
        self.assertNotIn("doc_search", names)

    def test_include_doc_search不传时仍然吃环境变量(self):
        """对话那条线没有跟着 A16 变——不传这个参数就是老行为。"""
        trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")
        with mock.patch("netops_ai.graph.chat_agent.env", return_value={}):
            names = {t.name for t in chat_agent.build_chat_tools(trace, chat_agent.AgentLoopBudget())}
        self.assertNotIn("doc_search", names)


class TestToolLabelsNeverDriftFromRegisteredNames(unittest.TestCase):
    """**同一个工具只能有一个名字。**

    这条测试是被一个发作了三次的 bug 逼出来的：`build_zabbix_tools()`
    落盘时手写 `"zbx-cli " + spec.name`，而注册给模型的是
    `zabbix_tool_name(spec.name)`。两个名字、分别手写在两处、没有任何
    东西强制它们一致——

    1. 取图表数据的过滤器按注册名去匹配落盘标签，恒为空，"画个图"
       从来没通过；
    2. 第一次"修复"把过滤器改成了另一个错的写法；
    3. 第二次"修复"又只改了其中一侧。真实环境验证才发现两侧从没对齐过。

    """

    def test_recorded_label_equals_registered_tool_name(self):
        from netops_ai.graph.agent_loop import AgentLoopBudget, ChatRunTrace
        from netops_ai.graph.chat_agent import build_zabbix_tools
        from netops_ai.graph.tool_names import zabbix_tool_name
        from netops_ai.zabbix import cli as zbx_cli

        trace = ChatRunTrace(trace_id="t", question="q", session_id="s")
        tools = build_zabbix_tools(trace, AgentLoopBudget())
        registered = {tool.name for tool in tools}
        expected = {zabbix_tool_name(spec.name) for spec in zbx_cli.COMMANDS}
        self.assertEqual(registered, expected, "注册名必须由 zabbix_tool_name() 统一生成")



class TestDeviceShowOnNeighbor(unittest.TestCase):
    """#17：告警那台连不上时，要能去拓扑里的邻居上看。只认拓扑里登记的设备。"""

    def setUp(self):
        pass
        self.trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")
        self.adapter = mock.MagicMock()
        self.adapter.__enter__.return_value = self.adapter
        self.adapter.run.return_value = mock.MagicMock(
            command="show ip ospf neighbor", allowed=True, ok=True, output="x", error="", denial_reason="")

    def _tool(self, name="device_show"):
        return next(t for t in chat_agent.build_device_tools(self.trace, device_host="192.0.2.52") if t.name == name)

    def test_不填device就是告警那台(self):
        with mock.patch("netops_ai.graph.chat_agent._device_adapter", return_value=self.adapter) as m:
            self._tool().invoke({"command": "show ip ospf neighbor"})
        m.assert_called_once_with("192.0.2.52")

    def test_填拓扑名字或别名就去那台(self):
        with mock.patch("netops_ai.graph.chat_agent._device_adapter", return_value=self.adapter) as m:
            self._tool().invoke({"command": "show ip ospf neighbor", "device": "D2-vios"})
            self._tool("device_show_many").invoke({"commands": ["show ip ospf neighbor"], "device": "D2"})
        self.assertEqual([c.args[0] for c in m.call_args_list], ["192.0.2.53", "192.0.2.53"])
        self.assertEqual(self.trace.tool_calls[0].args["device"], "D2-vios")

    def test_拓扑里没有的不连_把可选名字摆出来(self):
        with mock.patch("netops_ai.graph.chat_agent._device_adapter") as m:
            raw = self._tool().invoke({"command": "show version", "device": "10.9.9.9"})
        m.assert_not_called()
        self.assertIn("可选", json.loads(raw)["error"])


class TestW36bGetAnalysisAcceptsIntEventid(unittest.TestCase):
    def test_int_eventid(self):
        trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")
        tool = next(t for t in chat_agent.build_history_tools(trace) if t.name == "get_analysis")
        with mock.patch("netops_ai.api.dashboard.load_record", return_value={"eventid": "110418"}) as load:
            data = json.loads(tool.invoke({"eventid": 110418}))
        load.assert_called_once_with("110418")
        self.assertEqual(data["eventid"], "110418")


if __name__ == "__main__":
    unittest.main()

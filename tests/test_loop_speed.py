"""告警主线提速：最后一段简短 / SOP 计划只列主步骤 / AGENT_LOOP_THINKING 作用范围 / 批内接口。、内部文档。

"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from langchain_core.messages import AIMessage

from netops_ai.api import pipeline
from netops_ai.graph import agent_loop
from netops_ai.playbooks.lookup import sop_lookup

TRAP_ONLY = "Trap: linkDown received on A1 (viosl2 switch, 接入层)"
#: 领头告警不带接口，同批告警名和 trap 的 ifDescr 带接口
BATCH_TEXT = (
    '## 告警原文\n{"name": "' + TRAP_ONLY + '"}\n\n## 标签\n[]\n\n'
    '## 同批告警\n[{"name": "Cisco IOS: Interface Gi0/1(): Link down"}]\n'
    '1.3.6.1.2.1.2.2.1.2.2 "GigabitEthernet0/1"\n'
)
NO_INTERFACE_TEXT = '## 告警原文\n{"name": "' + TRAP_ONLY + '"}\n\n## 标签\n[]\n'


def _dev(command: str, output: str) -> agent_loop.ToolCallRecord:
    return agent_loop.ToolCallRecord(
        tool="device_show", args={"command": command}, ok=True,
        result={"allowed": True, "ok": True, "output": output},
    )


class Test最后一段简短(unittest.TestCase):
    def test_只加在告警主线_图的explore不受影响(self):
        self.assertIn(pipeline.ALERT_CLOSING_NOTE, pipeline.ALERT_FORENSICS_SYSTEM_PROMPT)
        self.assertNotIn("写几句就够", pipeline.FORENSICS_SYSTEM_PROMPT)
        self.assertIn("结构化步骤", pipeline.ALERT_CLOSING_NOTE)

    def test_措辞中性(self):
        for word in ("不要", "必须", "不许", "禁止"):
            self.assertNotIn(word, pipeline.ALERT_CLOSING_NOTE)

    def test_eventid关联没丢(self):
        # 短小结里点名 eventid；问题原文（也进结构化收尾的「原始问题」）写「在结论里写明」
        self.assertIn("eventid", pipeline.ALERT_CLOSING_NOTE)
        self.assertIn("就在结论里写明它的 eventid", pipeline.FORENSICS_QUESTION)

    @mock.patch("netops_ai.api.pipeline.run_agent_loop")
    def test_告警主线用的是带收尾说明的提示词(self, mock_loop):
        mock_loop.return_value = mock.Mock(
            incomplete=False, termination_reason="",
            trace=mock.Mock(tool_calls=[], final_structured=None, transcript="轨迹原文"),
        )
        pipeline._run_ai_exploration(zabbix_text=NO_INTERFACE_TEXT, plan=None, host_filter="", alert_clock=1790396461)
        self.assertEqual(mock_loop.call_args.kwargs["system_prompt"], pipeline.ALERT_FORENSICS_SYSTEM_PROMPT)


class TestSop计划只列主步骤(unittest.TestCase):
    def test_真实SOP_辅助步骤不进初始计划(self):
        data = sop_lookup("Cisco IOS: Interface Gi0/1(): Link down", [], "cisco", alert_clock=1790396461)
        plan = pipeline._render_sop_plan(data)
        for aux in ("local_log_buffer", "l2_errdisabled_check", "l2_stp_check", "device_level_check"):
            # 主步骤的 branches 里会出现辅助步骤的 id（去向），但不再有它自己的一行
            self.assertNotIn(f"id={aux}", plan)
        for main in ("triage_interface", "local_shutdown_evidence", "physical_peer", "l2_peer_check", "flap_history"):
            self.assertIn(f"id={main}", plan)
        self.assertNotIn("辅助步骤（只在某条分支指向它时才走）", plan)
        self.assertLess(len(plan), 3000)

    def test_走到辅助步骤时SOP下一步里有完整命令和去向(self):
        data = sop_lookup(TRAP_ONLY, [], "cisco", alert_clock=1790396461)
        sop = agent_loop.SopRuntimeState(data)
        sop.hint_for(_dev("show interfaces GigabitEthernet0/1", "GigabitEthernet0/1 is administratively down"),
                     tool_call_index=1)
        hint = sop.hint_for(agent_loop.ToolCallRecord(
            tool="zbx_syslog", args={"host": "A1-viosl2", "since": "1790395561", "until": "1790396761"},
            ok=True, result={"empty": True, "lines": []}), tool_call_index=2)
        self.assertIn("SOP 下一步：local_log_buffer", hint)
        self.assertIn("tool=device_show; command=show logging | include CONFIG_I|<接口>", hint)
        self.assertIn("branches: output_contains('%SYS-5-CONFIG_I')→__end__, error→__ai__, default→__ai__", hint)
        self.assertIn("expect=", hint)
        # why 不再只摘第一句
        self.assertIn("buffer 容量有限", hint)

    def test_why超长时封顶_命令和分支不截(self):
        data = {
            "matched": True,
            "steps": [
                {"id": "a", "main": True, "action": {"tool": "device_show", "command": "show version"},
                 "branches": [{"when": "default", "goto": "aux"}]},
                {"id": "aux", "main": False, "why": "长" * 1000, "expect": "期" * 500,
                 "action": {"tool": "device_show", "command": "show logging | include X" + "Y" * 300},
                 "branches": [{"when": "output_contains('Z')", "goto": "__end__"}, {"when": "default", "goto": "__ai__"}]},
            ],
        }
        hint = agent_loop.SopRuntimeState(data).hint_for(_dev("show version", "x"), tool_call_index=1)
        self.assertIn("show logging | include X" + "Y" * 300, hint)
        self.assertIn("default→__ai__", hint)
        self.assertNotIn("长" * (agent_loop.SOP_HINT_WHY_LIMIT + 1), hint)
        self.assertNotIn("期" * (agent_loop.SOP_HINT_EXPECT_LIMIT + 1), hint)

    def test_超出max_main_steps的主步骤也按计划外给全(self):
        steps = [
            {"id": f"m{i}", "main": True, "why": f"第{i}步。补充", "action": {"tool": "device_show", "command": f"show m{i}"},
             "branches": [{"when": "default", "goto": f"m{i + 1}"}]}
            for i in range(3)
        ]
        data = {"matched": True, "limits": {"max_main_steps": 2}, "steps": steps}
        sop = agent_loop.SopRuntimeState(data)
        in_plan = sop.hint_for(_dev("show m0", "x"), tool_call_index=1)
        self.assertNotIn("计划外", in_plan)  # m1 在计划里：维持原来的简短提示
        out_of_plan = sop.hint_for(_dev("show m1", "x"), tool_call_index=2)
        self.assertIn("SOP 下一步：m2（计划外步骤）", out_of_plan)
        self.assertIn("why=第2步。补充", out_of_plan)


class Test批内接口用到计划(unittest.TestCase):
    def _plan(self, zabbix_text: str) -> str:
        return pipeline._render_sop_plan(pipeline._sop_data_from_zabbix_text(zabbix_text, 1790396461))

    def test_领头不带接口_同批有接口时用真实命令(self):
        plan = self._plan(BATCH_TEXT)
        self.assertIn("command=show interfaces GigabitEthernet0/1", plan)
        self.assertNotIn("<接口>", plan)

    def test_领头自带接口时以告警名为准(self):
        data = sop_lookup("Cisco IOS: Interface Gi0/2(): Link down", [], "cisco",
                          alert_clock=1790396461, interface_hint="GigabitEthernet0/1")
        commands = [s["action"].get("command", "") for s in data["steps"]]
        self.assertIn("show interfaces GigabitEthernet0/2", commands)
        self.assertNotIn("show interfaces GigabitEthernet0/1", commands)

    def test_两处都没有接口时仍是占位符(self):
        self.assertIn("command=show interfaces <接口>", self._plan(NO_INTERFACE_TEXT))

    def test_对话工具签名不变(self):
        trace = agent_loop.ChatRunTrace(trace_id="t", question="q", session_id="s")
        from netops_ai.graph import chat_agent

        tools = chat_agent.build_chat_tools(trace, agent_loop.AgentLoopBudget())
        tool = next(t for t in tools if t.name == "sop_lookup")
        self.assertEqual(set(tool.args), {"alert_name", "tags"})


class TestThinking开关作用范围(unittest.TestCase):
    """`AGENT_LOOP_THINKING=off` 只改 `_build_loop_model()` 造出来的循环模型。"""

    BASE_ENV = {"LLM_BASE_URL": "https://example.invalid/v1", "LLM_API_KEY": "k", "LLM_MODEL": "m"}

    def _loop_model(self, extra_env: dict) -> SimpleNamespace:
        model = SimpleNamespace(extra_body={"keep": 1})
        with (
            mock.patch.object(agent_loop, "env", return_value={**self.BASE_ENV, **extra_env}),
            mock.patch.object(agent_loop, "detect_llm_route", return_value=SimpleNamespace(provider="openai")),
            mock.patch.object(agent_loop, "build_chat_model", return_value=model),
        ):
            return agent_loop._build_loop_model()

    def test_默认不设_行为不变(self):
        self.assertEqual(self._loop_model({}).extra_body, {"keep": 1})

    def test_off_只给循环模型加enable_thinking(self):
        self.assertEqual(self._loop_model({"AGENT_LOOP_THINKING": "off"}).extra_body,
                         {"keep": 1, "enable_thinking": False})

    def test_收尾结构化调用不受影响(self):
        def payload(extra_env: dict):
            with (
                mock.patch.object(agent_loop, "env", return_value={**self.BASE_ENV, **extra_env}),
                mock.patch.object(agent_loop.LLMClient, "complete", return_value=SimpleNamespace(parsed={})) as complete,
            ):
                agent_loop._final_schema_call("q", [], {"type": "json_schema"}, plan=None, transcript="t")
            return complete.call_args

        self.assertEqual(payload({}), payload({"AGENT_LOOP_THINKING": "off"}))
        self.assertNotIn("extra_body", payload({"AGENT_LOOP_THINKING": "off"}).kwargs)



if __name__ == "__main__":
    unittest.main()

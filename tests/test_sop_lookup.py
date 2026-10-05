from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import yaml

from netops_ai.devices.whitelist import check as check_device_command
from netops_ai.graph import chat_agent
from netops_ai.graph.agent_loop import AgentLoopBudget, ChatRunTrace
from netops_ai.graph.tool_names import registered_readonly_tool_names
from netops_ai.playbooks.lookup import (
    _alert_log_prefix,
    _render_warnings,
    _step_for_agent,
    load_validated_playbooks,
    parse_alert_interface,
    sop_lookup,
    validate_playbook_actions,
)


class TestSopLookup(unittest.TestCase):
    def test_alert_log_prefix_uses_utc_and_two_space_single_digit_day(self):
        clock = datetime(2026, 9, 4, 9, 5, tzinfo=timezone.utc).timestamp()

        self.assertEqual(_alert_log_prefix(clock), "Sep  4 09")

    def test_alert_log_prefix_subtracts_five_minutes_across_hour(self):
        clock = datetime(2026, 9, 24, 15, 2, tzinfo=timezone.utc).timestamp()

        self.assertEqual(_alert_log_prefix(clock), "Sep 24 14")

    def test_alert_log_prefix_subtracts_five_minutes_across_day(self):
        clock = datetime(2026, 9, 1, 0, 2, tzinfo=timezone.utc).timestamp()

        self.assertEqual(_alert_log_prefix(clock), "Aug 31 23")

    def test_no_alert_clock_uses_current_utc(self):
        with mock.patch("netops_ai.playbooks.lookup.time.time", return_value=1788512700):
            self.assertEqual(_alert_log_prefix(None), "Sep  4 09")

    def test_matching_alert_returns_real_steps_with_mapped_tools(self):
        data = sop_lookup("Cisco IOS: Interface Gi0/1(): Link down", ["component=network"])

        self.assertTrue(data["matched"])
        self.assertEqual(data["playbook"], "interface-link-down")
        self.assertGreaterEqual(len(data["steps"]), 2)
        self.assertEqual(data["steps"][0]["id"], "triage_interface")
        self.assertEqual(data["steps"][0]["action"]["tool"], "device_show")
        self.assertEqual(data["steps"][0]["action"]["command"], "show interfaces GigabitEthernet0/1")
        self.assertEqual(data["steps"][0]["provenance"]["source"], "ai_proposed_human_approved")

    def test_parse_alert_interface_normalizes_common_cisco_names(self):
        cases = {
            "Cisco IOS: Interface Gi0/1(): Link down": ("GigabitEthernet0/1", "Gi0/1"),
            "Syslog: Interface Gi0/1 line protocol down": ("GigabitEthernet0/1", "Gi0/1"),
            "Interface Ethernet0/1(uplink)": ("Ethernet0/1", "Et0/1"),
            "Interface BV11(SRV1-SRV2-GW): Link down": ("BVI11", "BV11"),
            "Interface Vl1(): Link down": ("Vlan1", "Vl1"),
            "Interface GigabitEthernet0/2(): Link down": ("GigabitEthernet0/2", "Gi0/2"),
            "Interface Port-channel10(): Link down": ("Port-channel10", "Po10"),
        }
        for alert_name, expected in cases.items():
            with self.subTest(alert_name=alert_name):
                self.assertEqual(parse_alert_interface(alert_name, []), expected)

        self.assertIsNone(parse_alert_interface("Cisco IOS: Interface Link down", []))

    def test_parse_alert_interface_falls_back_to_interface_tag(self):
        """判断类 bug 审查坐实（真实记录 100615 等）：告警名不带接口、接口只在
 Zabbix tag 里时，以前这个函数完全不看 tags，SOP 会把 {alert_interface} 渲染成
 占位符——明明有真实值却被当成缺失。
        """
        dict_tags = [{"tag": "interface", "value": "Gi0/1"}]
        self.assertEqual(
            parse_alert_interface("Syslog: BGP neighbor down", dict_tags),
            ("GigabitEthernet0/1", "Gi0/1"),
        )
        # 告警名里的解析优先于 tag（哪怕 tag 说的是别的接口）。
        self.assertEqual(
            parse_alert_interface("Interface Gi0/2(): Link down", dict_tags),
            ("GigabitEthernet0/2", "Gi0/2"),
        )
        # 字符串形式的 tag（`sop_lookup` 工具签名走的这条）也要认。
        self.assertEqual(
            parse_alert_interface("Syslog: BGP neighbor down", ["interface=Gi0/1"]),
            ("GigabitEthernet0/1", "Gi0/1"),
        )
        # 没有 interface 这个 tag、或 tag 值本身解析不出接口，仍然如实返回 None，不瞎猜。
        self.assertIsNone(parse_alert_interface("Syslog: BGP neighbor down", [{"tag": "component", "value": "network"}]))
        self.assertIsNone(parse_alert_interface("Syslog: BGP neighbor down", [{"tag": "interface", "value": "not-an-interface"}]))

    def test_sop_lookup_renders_real_command_from_interface_tag_when_name_has_none(self):
        """端到端：Trap 类告警名（`Trap: linkDown received on A1`）不带接口，SOP 里
        {alert_interface} 该渲染成 tag 里的真实值，不是占位符 `<接口>`。
        tags 用 `key=value` 字符串形式——这是 `sop_lookup` 真实调用方（告警主线的
        `_alert_name_and_tags_from_zabbix_text`、对话工具签名）传参的样子。"""
        data = sop_lookup("Trap: linkDown received on A1", ["interface=Gi0/1"])

        self.assertTrue(data["matched"])
        commands = [
            step["action"].get("command", "")
            for step in data["steps"]
            if step["action"].get("tool") == "device_show"
        ]
        self.assertIn("show interfaces GigabitEthernet0/1", commands)
        self.assertFalse(any("<接口>" in c for c in commands))

    def test_interface_template_without_interface_marks_needs_interface(self):
        data = sop_lookup("Cisco IOS: Interface Link down", ["component=network"])

        self.assertTrue(data["matched"])
        triage = data["steps"][0]
        self.assertEqual(triage["id"], "triage_interface")
        self.assertTrue(triage["needs_interface"])
        # 命令不再整条拿掉，接口位置用占位符（验证 外部 agent 反馈第 4 条：只剩 tool=device_show）
        self.assertEqual(triage["action"]["command"], "show interfaces <接口>")
        self.assertIn("告警里没有接口名", triage["why"])
        buffer = next(s for s in data["steps"] if s["id"] == "local_log_buffer")
        self.assertEqual(buffer["action"]["command"], "show logging | include CONFIG_I|<接口>")

    def test_unmatched_alert_returns_note(self):
        data = sop_lookup("Completely unknown lab alert", [])

        self.assertFalse(data["matched"])
        self.assertEqual(data["steps"], [])
        self.assertIn("没有匹配到团队 SOP", data["note"])


    def test_unknown_action_tool_fails_at_load_time(self):
        bad = {
            "name": "bad",
            "steps": [{"id": "s1", "action": {"tool": "write_memory"}, "provenance": {"source": "human"}}],
        }

        with self.assertRaisesRegex(ValueError, "write_memory"):
            validate_playbook_actions([bad], known_tools=registered_readonly_tool_names())

    def test_registered_tool_names_include_all_chat_readonly_tools(self):
        trace = ChatRunTrace(trace_id="t", question="q", session_id="s")
        chat_tool_names = {tool.name for tool in chat_agent.build_chat_tools(trace, AgentLoopBudget())}

        self.assertLessEqual(chat_tool_names, registered_readonly_tool_names())

    def test_load_validated_playbooks_checks_directory(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.yaml"
            path.write_text(
                "name: bad\nsteps:\n  - id: s1\n    action:\n      tool: write_memory\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "write_memory"):
                load_validated_playbooks(Path(td))

    def test_chat_tool_registers_and_records_trace(self):
        trace = ChatRunTrace(trace_id="t", question="q", session_id="s")
        tool = next(t for t in chat_agent.build_chat_tools(trace, AgentLoopBudget()) if t.name == "sop_lookup")

        raw = tool.invoke({"alert_name": "Cisco IOS: Interface Gi0/1(): Link down", "tags": ["component=network"]})
        data = json.loads(raw)

        self.assertTrue(data["matched"])
        self.assertEqual(trace.tool_calls[0].tool, "sop_lookup")
        self.assertTrue(trace.tool_calls[0].ok)

    def test_alert_clock_renders_sop_command_and_trace_keeps_rendered_result(self):
        # interface-link-down 的 flap_history 步骤也改成按关键字过滤了（跟
        # ospf-adjacency/bgp-session 同样的理由：设备时钟带 * 未同步，按时间前缀 begin
        # 常在窗口内查不到），三份现役 SOP 都不再用 {alert_log_prefix}。`_render_command`
        # 这个模板变量本身还在（备用），直接测渲染函数，不再依赖某份现役 SOP 恰好用它。
        from netops_ai.playbooks.lookup import _render_command

        rendered, warning = _render_command("show logging | begin {alert_log_prefix}", alert_clock=1788512700)
        self.assertEqual(rendered, "show logging | begin Sep  4 09")
        self.assertEqual(warning, "")

        trace = ChatRunTrace(trace_id="t", question="q", session_id="s")
        tool = next(
            t for t in chat_agent.build_sop_tools(trace, alert_clock=1788512700)
            if t.name == "sop_lookup"
        )

        raw = tool.invoke(
            {
                "alert_name": "Cisco IOS: Interface GigabitEthernet0/1(): Link down",
                "tags": ["component=network"],
            }
        )
        data = json.loads(raw)

        self.assertNotIn("{alert_log_prefix}", raw)
        self.assertEqual(trace.tool_calls[0].result["steps"], data["steps"])

    def test_unknown_template_variable_is_preserved_and_marks_trace_result(self):
        trace = ChatRunTrace(trace_id="t", question="q", session_id="s")
        tool = next(t for t in chat_agent.build_sop_tools(trace) if t.name == "sop_lookup")

        def broken_lookup(*args, **kwargs):
            result = {
                "matched": True,
                "playbook": "broken",
                "steps": [
                    _step_for_agent(
                        {
                            "id": "bad_log",
                            "action": {
                                "tool": "device",
                                "command": "show logging | begin {alert_log_prefix} {missing}",
                            },
                        },
                        alert_clock=None,
                    )
                ],
            }
            result["render_warnings"] = _render_warnings(result)
            return result

        with mock.patch(
            "netops_ai.graph.chat_agent.sop_lookup",
            side_effect=broken_lookup,
        ):
            raw = tool.invoke({"alert_name": "x", "tags": []})

        data = json.loads(raw)
        self.assertTrue(data["steps"][0]["action"]["command"].startswith("show logging | begin "))
        self.assertIn("{missing}", data["steps"][0]["action"]["command"])
        self.assertNotIn("{alert_log_prefix}", data["steps"][0]["action"]["command"])
        self.assertIn("render_warnings", data)
        self.assertIn("missing", data["render_warnings"][0])
        self.assertTrue(trace.tool_calls[0].ok)
        self.assertIn("render_warnings", trace.tool_calls[0].result)

    def test_rendered_logging_commands_pass_cisco_device_whitelist(self):
        paths = sorted(Path("playbooks").glob("*.yaml"))
        self.assertEqual(
            {path.stem for path in paths} & {"interface-link-down", "bgp-session", "ospf-adjacency"},
            {"interface-link-down", "bgp-session", "ospf-adjacency"},
        )
        logging_commands = []
        for path in paths:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            for step in doc.get("steps") or []:
                command = ((step.get("action") or {}).get("command") or "")
                if "show logging" not in command:
                    continue
                rendered = _step_for_agent(
                    step,
                    alert_clock=datetime(2026, 9, 4, 9, 5, tzinfo=timezone.utc).timestamp(),
                    alert_interface=("GigabitEthernet0/1", "Gi0/1"),
                )
                output = (rendered.get("action") or {}).get("command") or ""
                logging_commands.append((path.stem, output))
                self.assertRegex(output, r"^show logging \| (begin|include) ")
                self.assertNotIn("{", output)
                self.assertTrue(check_device_command("cisco", output).allowed, output)

        self.assertTrue(
            {"interface-link-down", "bgp-session", "ospf-adjacency"} <= {name for name, _ in logging_commands}
        )
        # interface-link-down 多了一条 local_log_buffer（Zabbix 侧没有日志时看设备 buffer）
        self.assertGreaterEqual(len(logging_commands), 4)


if __name__ == "__main__":
    unittest.main()

"""分析层的测试：prompt 拼得对不对、结构化输出解析对不对。全部用假 LLMClient，
不打真实 API。"""

from __future__ import annotations

import unittest
from unittest import mock

from netops_ai.analysis.analyzer import analyze, analyze_degraded, build_user_prompt
from netops_ai.analysis.schema import ANALYSIS_JSON_SCHEMA, analysis_json_schema
from netops_ai.llm.client import LLMResponse


class TestBuildPrompt(unittest.TestCase):
    def test_只有zabbix数据时不提设备(self):
        prompt = build_user_prompt("zabbix原文", None)
        self.assertIn("## Zabbix 侧候选告警", prompt)
        self.assertIn("zabbix原文", prompt)
        self.assertIn("没有设备侧数据", prompt)
        self.assertNotIn("## 设备侧数据", prompt)

    def test_有设备数据时组织成统一时间线(self):
        prompt = build_user_prompt(
            "zabbix原文", "设备原文", fault_time_epoch=1789694011, device_collected_epoch=1789701342
        )
        self.assertIn("## Zabbix 侧候选告警", prompt)
        self.assertIn("候选告警 1（eventid: unknown）", prompt)
        self.assertIn("zabbix原文", prompt)
        self.assertIn("取数时刻", prompt)
        self.assertIn("这是取数时刻的设备快照", prompt)
        self.assertIn("设备原文", prompt)

    def test_多候选zabbix数据按eventid分块(self):
        prompt = build_user_prompt(
            [
                {"eventid": "52152", "text": "Gi0/1 Link down 原文"},
                {"eventid": "52229", "text": "ICMP unreachable 原文"},
            ],
            None,
        )
        self.assertIn("候选告警 1（eventid: 52152）", prompt)
        self.assertIn("Gi0/1 Link down 原文", prompt)
        self.assertIn("候选告警 2（eventid: 52229）", prompt)
        self.assertIn("ICMP unreachable 原文", prompt)

    def test_多候选各自带设备时间线(self):
        prompt = build_user_prompt(
            [
                {"eventid": "52152", "text": "Gi0/1 Link down 原文"},
                {"eventid": "52229", "text": "ICMP unreachable 原文"},
            ],
            "设备原文",
            fault_time_epoch=1789694011,
            device_collected_epoch=1789701342,
        )
        # 设备现场对两条候选都要出现，判 alert_roles 常常要参照同一份设备快照
        self.assertEqual(prompt.count("设备原文"), 2)
        self.assertEqual(prompt.count("这是取数时刻的设备快照"), 2)


class TestAnalyze(unittest.TestCase):
    def test_把schema和两段prompt传给client(self):
        fake_client = mock.MagicMock()
        fake_client.complete.return_value = LLMResponse(
            content='{"root_cause":"x","confidence":"high","evidence":[],"ruled_out":[]}',
            parsed={"root_cause": "x", "confidence": "high", "evidence": [], "ruled_out": []},
            usage={},
            finish_reason="stop",
            raw={},
        )
        fake_client.analysis_schema_use_refs.return_value = True
        result = analyze("zabbix原文", "设备原文", client=fake_client)

        self.assertEqual(result.parsed["root_cause"], "x")
        call_kwargs = fake_client.complete.call_args.kwargs
        self.assertEqual(call_kwargs["response_format"], ANALYSIS_JSON_SCHEMA)
        messages = fake_client.complete.call_args.args[0]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("可处置粒度", messages[0]["content"])
        self.assertIn("alert_roles", messages[0]["content"])
        self.assertIn("grouping", messages[0]["content"])
        self.assertIn("zabbix原文", messages[1]["content"])
        self.assertIn("设备原文", messages[1]["content"])

    def test_provider不支持refs时传展开schema(self):
        fake_client = mock.MagicMock()
        fake_client.analysis_schema_use_refs.return_value = False
        fake_client.complete.return_value = LLMResponse(
            content="{}", parsed={}, usage={}, finish_reason="stop", raw={}
        )
        analyze("zabbix原文", "设备原文", client=fake_client)
        call_kwargs = fake_client.complete.call_args.kwargs
        self.assertEqual(call_kwargs["response_format"], analysis_json_schema(use_refs=False))

    def test_a组不传设备数据(self):
        fake_client = mock.MagicMock()
        fake_client.complete.return_value = LLMResponse(
            content="{}", parsed={}, usage={}, finish_reason="stop", raw={}
        )
        fake_client.analysis_schema_use_refs.return_value = True
        analyze("zabbix原文", None, client=fake_client)
        messages = fake_client.complete.call_args.args[0]
        self.assertNotIn("## 设备侧数据", messages[1]["content"])


class TestAnalyzeDegraded(unittest.TestCase):
    def test_用宽松json_object不是strict_schema(self):
        fake_client = mock.MagicMock()
        fake_client.complete.return_value = LLMResponse(
            content="{}", parsed={}, usage={}, finish_reason="stop", raw={}
        )
        analyze_degraded("zabbix原文", "设备原文", client=fake_client)
        call_kwargs = fake_client.complete.call_args.kwargs
        self.assertEqual(call_kwargs["response_format"], {"type": "json_object"})

    def test_降级prompt里带了json形状说明(self):
        fake_client = mock.MagicMock()
        fake_client.complete.return_value = LLMResponse(
            content="{}", parsed={}, usage={}, finish_reason="stop", raw={}
        )
        analyze_degraded("zabbix原文", None, client=fake_client)
        messages = fake_client.complete.call_args.args[0]
        self.assertIn("root_cause", messages[0]["content"])
        self.assertIn("alert_roles", messages[0]["content"])
        self.assertIn("只输出 JSON", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()


class TestSyslogReachesJudgmentPrompt(unittest.TestCase):
    """**故障窗口 syslog 必须进研判的 prompt，而且要按时间戳进故障时间线。**

 的真实事故：`administratively down` + `Configured from console`
 这两条铁证在记录的 `device_context_text` 里，**在真正送给研判的 prompt 里没有**。
 syslog 段的标题 `### 故障窗口 syslog` 没有反引号，不匹配命令标题正则，
 里面每一行都被跳过；末尾的原文兜底又只在一个命令块都没切出来时才补。
 模型说"缺少故障时刻的设备日志"是实话。

 同一条记录、同一个模型各跑 3 次：丢掉 syslog 时三次都是 low；带上 syslog
 三次都是 high 并判对（真实原因就是人手敲的 shutdown）。
    """

    DEVICE = (
        "### 故障窗口 syslog\n\n```\n本次 syslog 检索覆盖……\n\n"
        "Sep 21 14:10:14 192.0.2.52 112: *Sep 21 14:10:13.337: %LINK-5-CHANGED: "
        "Interface Ethernet0/1, changed state to administratively down\n"
        "Sep 21 14:10:17 192.0.2.52 114: *Sep 21 14:10:16.761: %SYS-5-CONFIG_I: "
        "Configured from console by console\n```\n\n"
        "### `show ip interface brief`（AI 探路）\n\n```\nEthernet0/1  unassigned  YES unset  administratively down down\n```\n"
    )

    def _prompt(self):
        from datetime import datetime, timezone
        from netops_ai.analysis.analyzer import build_user_prompt
        fault = datetime(2026, 9, 21, 14, 10, 28, tzinfo=timezone.utc).timestamp()
        return build_user_prompt("## 告警原文\nLink down", self.DEVICE,
                                 fault_time_epoch=fault, device_collected_epoch=fault + 60)

    def test_铁证进了prompt(self):
        p = self._prompt()
        self.assertIn("Configured from console", p)
        self.assertIn("changed state to administratively down", p)

    def test_按设备自己的时间戳进故障时间线而不是当快照(self):
        # rsyslog 落盘的行前面多一截接收时间和来源 IP，设备自己的时间戳在 `*` 后面。
        # 当成快照的话会被贴上"说明不了故障当时的状态"，等于白给。
        p = self._prompt()
        near = p.split("【故障时刻附近】")[1].split("【")[0]
        self.assertIn("14:10:13", near)
        self.assertIn("Configured from console", near)


class TestPromptCap(unittest.TestCase):
    """95276：52 条候选 × 21.6 万字符上下文，模型「输入超长」400，整个 incident 没结论。"""

    def test_超长上下文截断并说明(self):
        from netops_ai.analysis import analyzer

        out = analyzer.build_user_prompt("头" + "x" * 300_000 + "尾", None)
        self.assertLessEqual(len(out), analyzer.PROMPT_MAX_CHARS + 200)
        self.assertTrue(out.startswith("## Zabbix"), out[:20])
        self.assertIn("中间省略", out)
        self.assertTrue(out.rstrip().endswith("尾"), out[-20:])

    def test_没超就一个字不动(self):
        from netops_ai.analysis import analyzer

        self.assertNotIn("中间省略", analyzer.build_user_prompt("短文本", None))

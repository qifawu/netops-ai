"""工具说明保持中性（维护者 ）。

> 工具需要保持中性，而不是「不要做这个做那个」的说明。没见过用锤子前告诉你不应该用螺丝刀的。

实测：`zbx_syslog` 的说明写「不要去登设备跑 show logging」，SOP 的 flap_history 步骤却写
`device_show … show logging`；Zabbix 那次恰好没收到 A1 的 syslog，两个 agent 都因为这句劝阻
没看设备 buffer，错过了 `%SYS-5-CONFIG_I`。「怎么查」只写在提示词或 SOP 一处。

判据（刻意只抓措辞，不抓事实）：
- 劝阻：不要 / 不许 / 不能用 / 不能当 / 不能拿 / 不该 / 不应 / 禁止 / 「别」作动词
 （「别名」「别的」「区别」「分别」「类别」「识别」「级别」是名词/事实，放过）；
- 路由/代替：请用 / 改用 / 换用 / 代替 / 取而代之 / 而不是用 / 用这个 / 用它；
- 顺序/优先：优先 / 先用 / 先调 / 先查 / 应先 / 先 … 再（同一分句内）/ 只在 … 时用；
- 情绪化劝阻：挤出去。

「不是」「不含」「不会」「不接受」「没有」这类否定是在陈述事实（「返回的是厂商文档，不是实测证据」
「不接受任意 IP」），不在判据里。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from netops_ai.graph import agent_loop, chat_agent

REPO_ROOT = Path(__file__).resolve().parents[1]

ADVISORY_PATTERNS = {
    "劝阻": re.compile(r"不要|不许|不能用|不能当|不能拿|不该|不应|禁止|切勿|(?<![区分类识级辨鉴差个性])别(?![名的])"),
    "路由/代替": re.compile(r"请用|改用|换用|代替|取而代之|而不是用|用这个|用它"),
    "顺序/优先": re.compile(r"优先|先用|先调|先查|应先|先[^。；，,;\n]{0,15}再|只在[^。；\n]{0,20}时用"),
    "情绪化": re.compile(r"挤出去"),
}


def advisory_phrases(text: str) -> list[str]:
    hits = []
    for kind, pattern in ADVISORY_PATTERNS.items():
        for match in pattern.finditer(text or ""):
            start = max(0, match.start() - 12)
            hits.append(f"{kind}: …{text[start:match.end() + 12]}…")
    return hits


def _registered_tools():
    trace = agent_loop.ChatRunTrace(trace_id="t", question="q", session_id="s")
    return chat_agent.build_chat_tools(trace, agent_loop.AgentLoopBudget(), include_doc_search=True)


class Test判据本身(unittest.TestCase):
    """判据太宽会误伤事实描述，太窄会放过劝阻。两头各钉几条。"""

    def test_抓得到劝阻和路由(self):
        for text in (
            "要查故障时刻设备上发生了什么，用这个，不要去登设备跑 show logging",
            "别一台台调 topology_neighbors 把预算耗光",
            "一个月这类问题应优先用 trends",
            "长窗口请用 trends",
            "分析告警类任务应先调用",
            "先用 list_analyses 找到 eventid",
            "只在用户明确要最新结果时用",
            "正在把你要找的证据挤出去",
        ):
            with self.subTest(text=text):
                self.assertTrue(advisory_phrases(text), text)

    def test_不误伤事实描述(self):
        for text in (
            "设备名或 Zabbix 别名",
            "对应 zbx-cli hosts，只读风险级别 read-only。",
            "返回的是厂商文档，不是这台设备的实测证据",
            "不接受任意 IP，名字不在拓扑里时返回可选设备名",
            "已经恢复的问题不在结果里",
            "net.if.* 只取流量项本身，不含 errors/discards",
            "新日志会覆盖旧日志",
            "0 浮点/1 字符/2 日志/3 整数/4 文本",
        ):
            with self.subTest(text=text):
                self.assertEqual(advisory_phrases(text), [], text)


class Test注册工具的说明是中性的(unittest.TestCase):
    def test_description和参数说明里没有劝阻或路由措辞(self):
        offenders = {}
        for tool in _registered_tools():
            texts = [tool.description or ""]
            if tool.args_schema is not None:
                props = tool.args_schema.model_json_schema().get("properties") or {}
                texts.extend(str(p.get("description") or "") for p in props.values())
            hits = [hit for text in texts for hit in advisory_phrases(text)]
            if hits:
                offenders[tool.name] = hits
        self.assertEqual(offenders, {}, f"工具说明里又出现了使用建议/劝阻/路由：{offenders}")

    def test_工具返回里不再带try_instead(self):
        """空结果只说「没有数据」并回显查询条件，不教它换个方式再查。"""
        for rel in ("netops_ai/zabbix/cli.py", "netops_ai/topology_cli.py", "netops_ai/graph/chat_agent.py",
                    "netops_ai/topology.py"):
            with self.subTest(file=rel):
                source = (REPO_ROOT / rel).read_text(encoding="utf-8")
                self.assertNotIn('"try_instead"', source)
                self.assertNotIn("['try_instead']", source)


if __name__ == "__main__":
    unittest.main()

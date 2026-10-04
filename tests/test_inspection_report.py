"""巡检报告渲染 + 巡检工具。

**为什么有这个文件**：巡检代码 M5 那轮就写好了，但一直只挂在 HTTP 接口上，
对话里问「做一次巡检」agent 调不到。A5 把它注册成工具，这里锁住两件事：
工具在清单里、报告不许把好消息报成坏消息。
"""

import json
import unittest

from netops_ai.graph.agent_loop import AgentLoopBudget, ChatRunTrace
from netops_ai.graph.chat_agent import build_inspection_tools
from netops_ai.graph.tool_names import RUN_INSPECTION_TOOL_NAME, registered_readonly_tool_names
from netops_ai.inspection.report import render_inspection_markdown


def _payload(**kw):
    base = {"scanned_hosts": 2, "scanned_items": 2015, "items_with_data": 1796, "errors": [], "findings": []}
    base.update(kw)
    return base


class RenderTests(unittest.TestCase):
    def test_没有发现也要写清楚扫了什么(self):
        md = render_inspection_markdown(_payload())
        self.assertIn("2 台主机", md)
        self.assertIn("2015 个数值监控项", md)
        self.assertIn("没有发现值得关注的趋势", md)
        self.assertIn("这不等于网络没问题", md)

    def test_好转的趋势不许被标成走坏(self):
        """真实数据里 ICMP ping 从 0.06 涨到 1.0 是设备恢复了。
        早先一律按 kind=trend 标「持续走坏」，把好消息报成了坏消息。"""
        md = render_inspection_markdown(_payload(findings=[{
            "kind": "trend", "host_name": "V1", "item_name": "ICMP ping", "item_key": "icmpping",
            "finding": {"kind": "trend", "direction": "rising", "health_impact": "improving",
                        "first_segment_mean": 0.06, "last_segment_mean": 1.0,
                        "relative_change": 15.1, "monotonic_fraction": 0.99, "reason": "恢复了"},
        }]))
        self.assertIn("持续好转", md)
        self.assertNotIn("持续走坏", md)

    def test_走坏的照样标走坏(self):
        md = render_inspection_markdown(_payload(findings=[{
            "kind": "trend", "host_name": "V1", "item_name": "CPU", "item_key": "cpu",
            "finding": {"kind": "trend", "direction": "rising", "health_impact": "worsening",
                        "first_segment_mean": 10, "last_segment_mean": 80,
                        "relative_change": 7.0, "monotonic_fraction": 0.95, "reason": "一路涨"},
        }]))
        self.assertIn("持续走坏", md)

    def test_错误多了要截断并说明还有多少条(self):
        md = render_inspection_markdown(_payload(errors=[f"e{i}" for i in range(25)]))
        self.assertIn("取数出错 25 条", md)
        self.assertIn("另外还有 5 条", md)


class ToolTests(unittest.TestCase):
    def test_巡检在只读工具清单里(self):
        self.assertIn(RUN_INSPECTION_TOOL_NAME, registered_readonly_tool_names())

    def test_默认读缓存不真扫(self):
        """真扫一次要遍历两千多个监控项，对话里默认必须读缓存。"""
        from netops_ai.api import dashboard

        calls = {"rescan": 0}
        orig_load, orig_run = dashboard.load_latest_inspection, dashboard.run_inspection_and_persist
        dashboard.load_latest_inspection = lambda: _payload(findings=[])
        dashboard.run_inspection_and_persist = lambda **kw: calls.__setitem__("rescan", calls["rescan"] + 1) or _payload()
        try:
            trace = ChatRunTrace(trace_id="t", question="巡检", session_id="s")
            tool = build_inspection_tools(trace, AgentLoopBudget())[0]
            out = json.loads(tool.func())
            self.assertEqual(calls["rescan"], 0, "默认不许真扫")
            self.assertIn("markdown", out)
            self.assertEqual(out["source"], "最近一次巡检的缓存")
            self.assertEqual(len(trace.tool_calls), 1, "工具调用要记进 trace")
        finally:
            dashboard.load_latest_inspection, dashboard.run_inspection_and_persist = orig_load, orig_run

    def test_没有缓存时老实说没有(self):
        from netops_ai.api import dashboard

        orig = dashboard.load_latest_inspection
        dashboard.load_latest_inspection = lambda: None
        try:
            trace = ChatRunTrace(trace_id="t", question="巡检", session_id="s")
            out = json.loads(build_inspection_tools(trace, AgentLoopBudget())[0].func())
            self.assertIn("rescan=true", out["error"])
        finally:
            dashboard.load_latest_inspection = orig


if __name__ == "__main__":
    unittest.main()

"""对照样例：同一套流程写成 LangGraph 的图。测它是为了别让样例烂掉（框架升级会动 API）。"""

from __future__ import annotations

import unittest

from tools.pipeline_graph_demo import MAX_PROBES, build_graph


class TestGraphDemo(unittest.TestCase):
    def test_证据不够会走回取证节点再查一轮(self):
        final = build_graph().invoke({"eventid": "1", "zabbix_text": "x"})
        self.assertEqual(final["steps"], ["取证", "补查", "研判", "校验", "落盘发卡"])
        self.assertLessEqual(final["probes"], MAX_PROBES)

    def test_断点续跑_同一条告警接得上(self):
        from langgraph.checkpoint.memory import MemorySaver

        saver = MemorySaver()
        graph = build_graph(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "alert-1"}}
        for chunk in graph.stream({"eventid": "1", "zabbix_text": "x"}, cfg, stream_mode="updates"):
            if "judge" in chunk:
                break  # 模拟进程被杀
        self.assertEqual(saver.get(cfg)["channel_values"]["steps"], ["取证", "补查", "研判"])

        final = graph.invoke(None, cfg)  # 从 checkpoint 接着跑
        self.assertEqual(final["steps"][-2:], ["校验", "落盘发卡"])

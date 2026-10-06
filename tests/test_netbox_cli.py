from __future__ import annotations

import json
import unittest
from unittest import mock

from netops_ai import netbox_cli as nb_cli
from netops_ai.graph import chat_agent
from netops_ai.graph.agent_loop import AgentLoopBudget
from netops_ai.graph.tool_names import registered_readonly_tool_names
from netops_ai.topology import TopologyDevice, TopologyLink


def _lab():
    """跟真实 lab 同形状：两核心、两汇聚、四接入，接入全是单上联。"""
    def dev(name, role, host, alias, links):
        return TopologyDevice(name=name, host=host, role=role, aliases=(alias,),
                              links=tuple(TopologyLink(local_interface=a, peer=p, peer_interface=b) for a, p, b in links))
    return {
        "V1": dev("V1", "core", "192.0.2.50", "V1-vios", [("Gi0/1", "V2", "Gi0/1"), ("Gi0/2", "D1", "Et0/1")]),
        "V2": dev("V2", "core", "192.0.2.51", "V2-vios", [("Gi0/1", "V1", "Gi0/1"), ("Gi0/2", "D1", "Et0/2")]),
        "D1": dev("D1", "aggregation", "192.0.2.52", "D1-vios",
                  [("Et0/1", "V1", "Gi0/2"), ("Et0/2", "V2", "Gi0/2"), ("Et1/0", "A1", "Et0/1")]),
        "A1": dev("A1", "access", "192.0.2.54", "A1-viosl2", [("Et0/1", "D1", "Et1/0")]),
    }


class TestNbCli(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("netops_ai.netbox_cli.topo.load_topology", side_effect=_lab)
        patcher.start()
        self.addCleanup(patcher.stop)
        source = mock.patch("netops_ai.netbox_cli.topo.LAST_SOURCE", "topology.yaml")
        error = mock.patch("netops_ai.netbox_cli.topo.LAST_NETBOX_ERROR", "")
        source.start()
        error.start()
        self.addCleanup(source.stop)
        self.addCleanup(error.stop)

    def test_台账列出名字角色管理ip和zabbix别名(self):
        out = nb_cli.cmd_devices(mock.Mock(query="", role=""))
        self.assertEqual(out["count"], 4)
        a1 = next(d for d in out["devices"] if d["name"] == "A1")
        self.assertEqual((a1["role"], a1["mgmt_ip"], a1["zabbix_host"]), ("access", "192.0.2.54", "A1-viosl2"))

    def test_按zabbix别名也能过滤出来(self):
        # 模型手上的名字常常来自 Zabbix，不是拓扑名。
        out = nb_cli.cmd_devices(mock.Mock(query="a1-viosl2", role=""))
        self.assertEqual([d["name"] for d in out["devices"]], ["A1"])

    def test_全图摘要一次答出哪些设备只有一条上联(self):
        # **这条是这个命令存在的理由。** 以前要判"哪些接入单归一"得一台台问，
        # 8 台就是 8 次工具调用，而一轮预算只有 6~12 次，光问拓扑就打满了。
        out = nb_cli.cmd_topology(mock.Mock())
        self.assertEqual(out["single_homed"], ["A1"])
        d1 = next(d for d in out["devices"] if d["name"] == "D1")
        self.assertEqual(d1["uplinks"], ["V1", "V2"])

    def test_核心没有上联但不算单归一(self):
        out = nb_cli.cmd_topology(mock.Mock())
        self.assertEqual(next(d for d in out["devices"] if d["name"] == "V1")["uplinks"], [])
        self.assertNotIn("V1", out["single_homed"])

    def test_摘要不探对端(self):
        # 摘要是台账问题，不是可达性问题。混在一起既慢又容易被读成故障。
        out = nb_cli.cmd_topology(mock.Mock())
        self.assertNotIn("peer_reachable", json.dumps(out))
        self.assertIn("没有探测对端", out["note"])

    def test_devices空查询标empty并回显过滤条件(self):
        """空结果只陈述事实并回显查询条件，不带 try_instead。"""
        out = nb_cli.cmd_devices(mock.Mock(query="NO_SUCH", role=""))
        self.assertTrue(out["empty"])
        self.assertEqual(out["devices"], [])
        self.assertIn("没有匹配的设备", out["note"])
        self.assertIn("no_such", out["note"])
        self.assertNotIn("try_instead", out)

    def test_topology真源失败且fallback为空时不能说全网无设备(self):
        with (
            mock.patch("netops_ai.netbox_cli.topo.load_topology", return_value={}),
            mock.patch("netops_ai.netbox_cli.topo.LAST_SOURCE", "topology.yaml"),
            mock.patch("netops_ai.netbox_cli.topo.LAST_NETBOX_ERROR", "连不上 NetBox"),
        ):
            out = nb_cli.cmd_topology(mock.Mock())
        self.assertTrue(out["empty"])
        self.assertEqual(out["count"], 0)
        # 事实说全：NetBox 报了什么错、退回了哪个源——读的人据此就知道这不是「全网没有设备」
        self.assertIn("NetBox 不可用（连不上 NetBox）", out["note"])
        self.assertIn("已退回 topology.yaml", out["note"])
        self.assertNotIn("try_instead", out)


class TestSpecTableIsTheSingleSource(unittest.TestCase):
    """这套 spec 表存在的全部理由就是"名字只有一个出处"。"""

    def test_三个工具都从spec表生成且名字对得上(self):
        trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")
        tools = chat_agent.build_topology_tools(trace, AgentLoopBudget())
        self.assertEqual({t.name for t in tools}, {s.tool_name for s in nb_cli.COMMANDS})

    def test_neighbors的工具名必须还是topology_neighbors(self):
        # SOP 引擎和剧本引用着这个名字，跟着 CLI 改名会**静默**让它们查不到。
        spec = next(s for s in nb_cli.COMMANDS if s.name == "neighbors")
        self.assertEqual(spec.tool_name, "topology_neighbors")

    def test_白名单从spec表算不是手写的(self):
        # 手写的症状是：加了新工具忘了来补一行，调用被拒，
        # 而拒绝理由长得像"模型编了个不存在的工具"。
        self.assertTrue({s.tool_name for s in nb_cli.COMMANDS} <= registered_readonly_tool_names())

    def test_落盘标签等于注册名(self):
        trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")
        with mock.patch("netops_ai.netbox_cli.topo.load_topology", side_effect=_lab):
            tool = next(t for t in chat_agent.build_topology_tools(trace, AgentLoopBudget()) if t.name == "nb_topology")
            tool.invoke({})
        self.assertEqual(trace.tool_calls[-1].tool, "nb_topology")


if __name__ == "__main__":
    unittest.main()

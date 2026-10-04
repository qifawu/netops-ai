from __future__ import annotations

import tempfile

import json
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from netops_ai.devices.base import DeviceAdapter
from netops_ai.graph import chat_agent
from netops_ai.topology import (
    known_device_labels,
    load_topology,
    resolve_device,
    topology_neighbors,
)

# **这一整份测的是 file provider，所以把 provider 钉死。**
# 不钉的话，绿不绿取决于跑测试的人有没有配 NetBox：在 Mac 上配了
# `NETBOX_URL` 之后，这里 11 条当场红——不是代码坏了，是这些用例一直在读
# 环境里恰好没有的东西。开发机做完 也会撞上同一件事。
# 真的要验 provider 开关的用例在 `tests/test_netbox_topology.py`。
try:
    import netops_ai.netbox  # noqa: F401
except ImportError:  # 开源版没有 NetBox，topology 本来就只读本地 yaml，不需要钉 provider
    class _no_netbox:  # noqa: N801
        start = stop = staticmethod(lambda: None)
else:
    _no_netbox = mock.patch("netops_ai.netbox.netbox_enabled", return_value=False)


def setUpModule():
    _no_netbox.start()


def tearDownModule():
    _no_netbox.stop()


class _FakeDevice(DeviceAdapter):
    def __init__(self, vendor: str = "cisco", *, output: str = "Interface IP-Address OK? Method Status Protocol"):
        super().__init__(vendor)
        self.output = output
        self.commands: list[str] = []

    def _execute_one(self, command: str) -> str:
        self.commands.append(command)
        return self.output


def _write_topology(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "devices": [
                    {
                        "name": "V1",
                        "host": "192.0.2.50",
                        "links": [
                            {
                                "local_interface": "GigabitEthernet0/1",
                                "peer": "V2",
                                "peer_interface": "GigabitEthernet0/1",
                            }
                        ],
                    },
                    {
                        "name": "V2",
                        "host": "10.0.0.2",
                        "links": [
                            {
                                "local_interface": "GigabitEthernet0/1",
                                "peer": "V1",
                                "peer_interface": "GigabitEthernet0/1",
                            }
                        ],
                    },
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


class TestTopology(unittest.TestCase):
    def test_load_topology_parses_devices_and_links(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "topology.yaml"
            _write_topology(path)

            topology = load_topology(path)

        self.assertEqual(topology["V1"].host, "192.0.2.50")
        self.assertEqual(topology["V1"].links[0].peer, "V2")
        self.assertEqual(topology["V1"].links[0].peer_interface, "GigabitEthernet0/1")

    def test_missing_device_returns_explicit_note(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "topology.yaml"
            _write_topology(path)

            data = json.loads(topology_neighbors("NO_SUCH", "GigabitEthernet0/1", topology_path=path))

        self.assertFalse(data["found"])
        self.assertIn("没有拓扑记录", data["note"])
        self.assertIn("NO_SUCH", data["note"])

    def test_missing_interface_returns_explicit_note(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "topology.yaml"
            _write_topology(path)

            data = json.loads(topology_neighbors("V1", "GigabitEthernet0/9", topology_path=path))

        self.assertFalse(data["found"])
        self.assertIn("没有拓扑记录", data["note"])
        self.assertIn("GigabitEthernet0/9", data["note"])

    def test_topology_neighbors_returns_peer_and_read_only_evidence(self):
        fake = _FakeDevice(output="GigabitEthernet0/1 10.0.0.2 YES manual up up")

        with TemporaryDirectory() as td:
            path = Path(td) / "topology.yaml"
            _write_topology(path)

            raw = topology_neighbors(
                "V1",
                "GigabitEthernet0/1",
                topology_path=path,
                adapter_factory=lambda _device: fake,
            )

        data = json.loads(raw)
        neighbor = data["neighbors"][0]
        self.assertTrue(data["found"])
        self.assertEqual(neighbor["peer"], "V2")
        self.assertEqual(neighbor["peer_interface"], "GigabitEthernet0/1")
        self.assertTrue(neighbor["peer_reachable"])
        self.assertEqual(neighbor["evidence"]["command"], "show ip interface brief")
        self.assertEqual(fake.commands, ["show ip interface brief"])

    def test_chat_tools_register_topology_neighbors(self):
        trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")
        tools = chat_agent.build_chat_tools(trace, chat_agent.AgentLoopBudget())

        self.assertIn("topology_neighbors", {tool.name for tool in tools})


class TestZabbixNameMismatchIsTheRealBug(unittest.TestCase):
    """的真实故障：agent 拿 Zabbix 主机名 `V2-vios` 查拓扑，
 topology.yaml 登记的是 `V2`，三次全部查空。V2 侧因此只能给出
 「低置信、需要人工介入」——而同一次故障 V1 侧是高置信的。
 **一个命名不一致，直接让半个故障判不出来。**
    """

    def test_zabbix_hostname_resolves_through_aliases(self):
        topology = load_topology()
        for name in ("V2-vios", "V1-vios"):
            with self.subTest(name=name):
                device = resolve_device(topology, name)
                self.assertIsNotNone(device, f"{name} 必须能解析——这正是当初查空的那个名字")
                self.assertEqual(device.name, name.split("-")[0])

    def test_resolution_ignores_case_and_padding(self):
        topology = load_topology()
        self.assertEqual(resolve_device(topology, "  v2-VIOS  ").name, "V2")

    def test_unknown_name_is_not_guessed(self):
        """不做模糊匹配。猜错了没人知道，别名写错了是配置错误，看得见。"""
        topology = load_topology()
        self.assertIsNone(resolve_device(topology, "V9-vios"))
        self.assertIsNone(resolve_device(topology, "vios"))

    def test_repo_topology_registers_the_real_zabbix_names(self):
        labels = {label.lower() for label in known_device_labels(load_topology())}
        self.assertIn("v1-vios", labels)
        self.assertIn("v2-vios", labels)
        self.assertIn("a1-viosl2", labels)
        self.assertNotIn("a1-iol", labels)

    def test_legacy_zabbix_hostname_still_resolves_for_records(self):
        topology = load_topology()
        self.assertEqual(resolve_device(topology, "A1-iol").name, "A1")
        self.assertEqual(resolve_device(topology, "A2-iol").name, "A2")
        self.assertEqual(resolve_device(topology, "A3-iol").name, "A3")
        self.assertEqual(resolve_device(topology, "  a1-IOL  ").name, "A1")

    def test_ambiguous_alias_is_rejected_at_load_time(self):
        """一个名字指向两台设备，解析就成了掷骰子。加载时就得炸。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "topology.yaml"
            path.write_text(
                "devices:\n"
                "  - name: A\n    host: 1.1.1.1\n    aliases: [shared]\n"
                "  - name: B\n    host: 1.1.1.2\n    aliases: [shared]\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                load_topology(path)
        self.assertIn("命名冲突", str(ctx.exception))


class TestFailureTellsTheAgentHowToRecover(unittest.TestCase):
    """查不到的时候只说「不在 topology.yaml 中」，agent 没有任何可操作
    信息，只能放弃——S 段那次就是这么放弃的。"""

    def test_unknown_device_returns_the_list_of_valid_names(self):
        data = json.loads(topology_neighbors("V2-vios-typo", "GigabitEthernet0/1"))
        self.assertFalse(data["found"])
        self.assertIn("V2", data["known_devices"])
        self.assertIn("V2-vios", data["known_devices"])
        self.assertIn("known_devices", data["note"])

    def test_unknown_interface_returns_the_interfaces_that_do_exist(self):
        data = json.loads(topology_neighbors("V1", "GigabitEthernet0/9"))
        self.assertFalse(data["found"])
        self.assertEqual(
            data["known_interfaces"],
            ["GigabitEthernet0/1", "GigabitEthernet0/2", "GigabitEthernet0/3"],
        )

    def test_hit_reports_which_name_was_queried(self):
        """用别名查进来的，要让 agent 看见它查的名字被解析成了谁，
        否则结论里会写 Zabbix 主机名，跟拓扑对不上。"""
        data = json.loads(
            topology_neighbors("V2-vios", "GigabitEthernet0/1", adapter_factory=_never_probe)
        )
        self.assertTrue(data["found"])
        self.assertEqual(data["device"], "V2")
        self.assertEqual(data["queried_as"], "V2-vios")


class _StubResult:
    def __init__(self):
        self.allowed = False
        self.ok = False
        self.command = "show ip interface brief"
        self.output = ""
        self.error = "测试不连真设备"
        self.denial_reason = ""


class _StubAdapter:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, command):
        return _StubResult()


def _never_probe(device):
    return _StubAdapter()


if __name__ == "__main__":
    unittest.main()


class TestAgentFriendliness(unittest.TestCase):
    """工具返回是给模型读的，不是给人读的。

 这两条锁的是 维护者问「现在是不是 AI agent 友好」之后改的两件事。
    """

    def test_没配设备账号就不探对端也不给peer_reachable(self):
        # 探了只会给每个邻居挂一条一模一样的"没配 DEVICE_USERNAME"：
        # D1 五条链路光这堆重复报错就占掉响应一半，而且五个
        # `peer_reachable: false` 很容易被读成"这台邻居全断了"。
        # **"探不了"和"探过了不通"必须分得开。**
        with mock.patch("netops_ai.topology.env", return_value={}):
            data = json.loads(topology_neighbors("V1"))
        self.assertTrue(data["found"])
        for n in data["neighbors"]:
            self.assertNotIn("peer_reachable", n)
            self.assertNotIn("evidence", n)
        self.assertIn("没有探测对端", data["note"])
        self.assertIn("不代表对端不可达", data["note"])

    def test_说明里的真源跟着provider走不写死yaml(self):
        # 写死成 topology.yaml 会被模型照抄：数据来自 NetBox，模型回答第一行
        # 却写"（来自 topology.yaml / netbox）"，因为它在复述这句话。
        with mock.patch("netops_ai.topology.env", return_value={}):
            data = json.loads(topology_neighbors("V1"))
        self.assertIn(data["source"], data["note"])

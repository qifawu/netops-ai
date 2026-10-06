"""NetBox 作为拓扑真源。夹具是**真实响应录下来的**，不是手编的
（`tests/fixtures/netbox/`，NetBox 4.7.1，8 台三层拓扑，只把主机地址换成了占位）。

锁三件事：NetBox 的线缆能还原成邻居、Zabbix 主机名能解析到台账里的设备、
以及 NetBox 挂了要退回 yaml **但必须留下原因**。
"""

import json
import unittest
from pathlib import Path

from netops_ai import netbox, topology

FIX = Path(__file__).parent / "fixtures" / "netbox"


def _fx(name: str) -> list[dict]:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))["results"]


class BuildTopologyTests(unittest.TestCase):
    def setUp(self):
        self.topo = netbox.build_topology(_fx("devices"), _fx("interfaces"))

    def test_八台设备带层级角色(self):
        self.assertEqual(len(self.topo), 8)
        roles = {n: d.role for n, d in self.topo.items()}
        self.assertEqual(roles["C1"], "core")
        self.assertEqual(roles["D1"], "aggregation")
        self.assertEqual(roles["A1"], "access")

    def test_离线设备不进拓扑也不当邻居(self):
        devices = _fx("devices")
        victim = "A1"
        for d in devices:
            if d.get("name") == victim:
                d["status"] = {"value": "offline", "label": "Offline"}
        topo = netbox.build_topology(devices, _fx("interfaces"))
        self.assertNotIn(victim, topo)
        self.assertEqual(len(topo), 7)
        for dev in topo.values():
            self.assertTrue(all(link.peer != victim for link in dev.links))

    def test_线缆还原成邻居(self):
        d1 = self.topo["D1"]
        peers = {(l.local_interface, l.peer, l.peer_interface) for l in d1.links}
        self.assertIn(("Gi0/4", "A1", "Gi0/1"), peers)
        self.assertIn(("Gi0/1", "C1", "Gi0/2"), peers)
        self.assertEqual(len(self.topo["A1"].links), 1, "接入层只有一条上联")

    def test_zabbix主机名能解析到设备(self):
        """真实事故：Zabbix 叫 V2-vios、台账里写 V2，
 三次查不到对端，半个故障只能判「需要人工介入」。八台会放大四倍。
        """
        self.assertEqual(topology.resolve_device(self.topo, "D1-vios").name, "D1")
        self.assertEqual(topology.resolve_device(self.topo, "A1-viosl2").name, "A1")

    def test_管理地址去掉掩码(self):
        self.assertEqual(self.topo["C1"].host, "192.0.2.11")

    def test_设备详情和接口清单来自只读响应(self):
        a1 = self.topo["A1"]
        self.assertEqual(a1.model, "vIOS-L2")
        self.assertEqual(a1.site, "netops lab")
        self.assertEqual(a1.serial, "")
        self.assertEqual(a1.platform, "")
        self.assertEqual(a1.interfaces[0].name, "Gi0/0")
        self.assertEqual(a1.interfaces[0].description, "带外管理口")
        self.assertEqual(a1.interfaces[1].peer, "D1")
        self.assertEqual(a1.interfaces[1].peer_interface, "Gi0/4")


class AuthHeaderTests(unittest.TestCase):
    def test_v2_token_走_bearer(self):
        """NetBox 4.7 起 token 是 v2：`Bearer nbt_<key>.<plaintext>`。
        照旧文档写 `Token <key>` 会 403，返回 `{"detail":"Invalid v1 token"}`。"""
        self.assertEqual(netbox.auth_header("nbt_abc.def"), "Bearer nbt_abc.def")

    def test_老token还按v1走(self):
        self.assertEqual(netbox.auth_header("0123456789"), "Token 0123456789")


class FallbackTests(unittest.TestCase):
    def test_netbox挂了退回yaml但要留下原因(self):
        """**静默退回会把配置错误吃掉。** 真踩过：header 拼错，
        它一直读旧 yaml，只有两台设备，从外面看像是 NetBox 里就这么多。"""
        orig_enabled, orig_load = netbox.netbox_enabled, netbox.load_topology_from_netbox
        netbox.netbox_enabled = lambda cfg=None: True

        def boom():
            raise netbox.NetBoxError("连不上")

        netbox.load_topology_from_netbox = boom
        try:
            data = topology.load_topology()
            self.assertTrue(data, "退回之后还得有拓扑可用")
            self.assertEqual(topology.LAST_SOURCE, "topology.yaml")
            self.assertIn("连不上", topology.LAST_NETBOX_ERROR)
        finally:
            netbox.netbox_enabled, netbox.load_topology_from_netbox = orig_enabled, orig_load


if __name__ == "__main__":
    unittest.main()

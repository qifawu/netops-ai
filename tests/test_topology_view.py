from __future__ import annotations

import time
import unittest
from pathlib import Path
from unittest import mock

from netops_ai.api import dashboard
from netops_ai.topology import TopologyDevice, TopologyLink


#: **测试读 fixture，不读真实 records/**（真实目录一直在长，断言会随数据漂）。
#: 说明见 tests/fixtures/records/README.md。
_FIXTURE_RECORDS = Path(__file__).parent / "fixtures" / "records"
_records_patch = mock.patch.object(dashboard, "RECORDS_DIR", _FIXTURE_RECORDS)


def setUpModule():
    _records_patch.start()


def tearDownModule():
    _records_patch.stop()



def _lab():
    def dev(name, role, host, alias, links, **kwargs):
        return TopologyDevice(name=name, host=host, role=role, aliases=(alias,),
                              links=tuple(TopologyLink(local_interface=a, peer=p, peer_interface=b) for a, p, b in links),
                              **kwargs)
    return {
        "V1": dev("V1", "core", "192.0.2.50", "V1-vios", [("Gi0/2", "D1", "Et0/1")]),
        "D1": dev("D1", "aggregation", "192.0.2.52", "D1-vios",
                  [("Et0/1", "V1", "Gi0/2"), ("Et1/0", "A1", "Et0/1")],
                  model="vIOS", site="netops lab"),
        "A1": dev("A1", "access", "192.0.2.54", "A1-viosl2", [("Et0/1", "D1", "Et1/0")]),
    }


class TestTopologyView(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("netops_ai.topology.load_topology", side_effect=_lab)
        patcher.start()
        self.addCleanup(patcher.stop)
        import netops_ai.topology as topo
        topo.LAST_SOURCE = "topology.yaml"
        topo.LAST_NETBOX_ERROR = ""

    def _view(self, incidents):
        return dashboard.build_topology_view(incidents)

    def test_一条线只画一次不画两遍(self):
        # V1→D1 和 D1→V1 是同一条线。不去重的话图上每条线都画两遍，
        # 肉眼看不出来，但点一台高亮的时候会闪。
        self.assertEqual(len(self._view([])["links"]), 2)

    def test_告警按zabbix别名对得上拓扑名(self):
        # **records 里的 host 是 Zabbix 主机名（D1-vios），拓扑里叫 D1。**
        # 不走别名解析就永远叠不上告警——那次半个故障判不出就是这个根因。
        now = int(time.time())
        v = self._view([{"host": "D1-vios", "clock": now, "root_cause": "重启", "incident_id": "x"}])
        d1 = next(d for d in v["devices"] if d["name"] == "D1")
        self.assertEqual(d1["incident_count"], 1)
        self.assertEqual(d1["latest_root_cause"], "重启")

    def test_窗口之外的故障不算(self):
        old = int(time.time()) - 72 * 3600
        v = self._view([{"host": "D1-vios", "clock": old, "root_cause": "老的", "incident_id": "x"}])
        self.assertEqual(next(d for d in v["devices"] if d["name"] == "D1")["incident_count"], 0)

    def test_核心没有上联但不算单归一(self):
        v = self._view([])
        by = {d["name"]: d for d in v["devices"]}
        self.assertTrue(by["A1"]["single_homed"])
        self.assertFalse(by["V1"]["single_homed"])
        self.assertEqual(by["A1"]["uplinks"], ["D1"])

    def test_详情带台账字段和来源(self):
        d1 = next(d for d in self._view([])["devices"] if d["name"] == "D1")
        self.assertTrue(d1["managed"])
        self.assertEqual(d1["model"], "vIOS")
        self.assertEqual(d1["site"], "netops lab")
        self.assertEqual(d1["inventory_source"], "本地文件，不是 NetBox")
        self.assertTrue(d1["interfaces"])

    def test_未知对端是未纳管节点而不是纳管设备(self):
        lab = _lab()
        lab["A1"] = TopologyDevice(
            **{**lab["A1"].__dict__, "links": lab["A1"].links + (TopologyLink("Et0/2", "SRV", "eth0"),)}
        )
        with mock.patch("netops_ai.topology.load_topology", return_value=lab):
            view = dashboard.build_topology_view([])
        srv = next(d for d in view["devices"] if d["name"] == "SRV")
        self.assertFalse(srv["managed"])
        self.assertEqual(srv["role_cn"], "未纳管")
        self.assertFalse(next(l for l in view["links"] if l["b"] == "SRV")["managed"])


class TestStatusIsReal(unittest.TestCase):
    """**状态点必须是真探出来的。** 常亮的绿点第一次出事时还是绿的，比没有更坏。"""

    def test_没配模型时那个点是灭的(self):
        with mock.patch.object(dashboard, "_env", return_value={}), \
             mock.patch("netops_ai.topology.load_topology", side_effect=_lab):
            items = {i["key"]: i for i in dashboard.build_status()["items"]}
        self.assertFalse(items["llm"]["ok"])
        self.assertIn("没配", items["llm"]["label"])

    def test_退回本地yaml时拓扑那个点是灭的(self):
        with mock.patch.object(dashboard, "_env", return_value={}), \
             mock.patch("netops_ai.topology.load_topology", side_effect=_lab):
            import netops_ai.topology as topo
            topo.LAST_SOURCE = "topology.yaml"
            items = {i["key"]: i for i in dashboard.build_status()["items"]}
        self.assertFalse(items["topology"]["ok"])




if __name__ == "__main__":
    unittest.main()

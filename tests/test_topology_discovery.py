"""拓扑自动发现：CDP / LLDP 解析、连线还原（两端互看、共享网段、外部邻居）、对照已登记拓扑、报告和图。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from netops_ai.inspection import topology_discovery as TD
from netops_ai.topology import TopologyDevice, TopologyLink

NL = chr(10)


def dev(name, role, *links, aliases=()):
    return TopologyDevice(name=name, host="192.0.2.1", links=tuple(TopologyLink(*l) for l in links), aliases=tuple(aliases), role=role)


KNOWN = {
    "R1": dev("R1", "core", ("GigabitEthernet0/1", "R2", "GigabitEthernet0/1"), ("GigabitEthernet0/2", "S1", "GigabitEthernet0/1"), aliases=("R1-vios",)),
    "R2": dev("R2", "core", ("GigabitEthernet0/1", "R1", "GigabitEthernet0/1")),
    "S1": dev("S1", "access", ("GigabitEthernet0/1", "R1", "GigabitEthernet0/2")),
}


def row(local, peer, peer_if, ip=""):
    return {"local_if": local, "peer": peer, "peer_if": peer_if, "mgmt_ip": ip}


def obs(**kw):
    return {d: {"rows": rows, "source": "cdp", "error": ""} for d, rows in kw.items()}


CDP = NL.join([
    "-------------------------", "Device ID: R2.example.local", "Entry address(es): ", "  IP address: 192.0.2.2",
    "Platform: Cisco IOSv,  Capabilities: Router", "Interface: GigabitEthernet0/1,  Port ID (outgoing port): GigabitEthernet0/1",
    "Holdtime : 179 sec", "", "Version :", "Cisco IOS Software, Version 15.9(3)M9", "", "advertisement version: 2", "Management address(es): ",
    "  IP address: 192.0.2.2", "", "-------------------------", "Device ID: S1.example.local", "Entry address(es): ", "  IP address: 192.0.2.3",
    "Platform: Cisco IOSv,  Capabilities: Switch", "Interface: GigabitEthernet0/2,  Port ID (outgoing port): GigabitEthernet0/1",
    "Holdtime : 170 sec", "", "Management address(es): ", "  IP address: 192.0.2.3", ""])


class FakeAdapter:
    def __init__(self, outputs, allowed=True):
        self.outputs, self.allowed, self.cmds = outputs, allowed, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cmd):
        self.cmds.append(cmd)
        return SimpleNamespace(allowed=self.allowed, denial_reason="denied", error="", output=self.outputs.get(cmd, ""))


class TestCollect(unittest.TestCase):
    def test_解析CDP_名字去域名_有CDP就不再查LLDP(self):
        ad = FakeAdapter({"show cdp neighbors detail": CDP})
        got = TD.collect({"R1": 1}, lambda d: ad)
        self.assertEqual(got["R1"]["source"], "cdp")
        self.assertEqual([(r["local_if"], r["peer"], r["peer_if"]) for r in got["R1"]["rows"]],
                         [("GigabitEthernet0/1", "R2", "GigabitEthernet0/1"), ("GigabitEthernet0/2", "S1", "GigabitEthernet0/1")])
        self.assertEqual(ad.cmds, ["show cdp neighbors detail"])

    def test_CDP没有输出_退回LLDP(self):
        lldp = NL.join(["Local Intf: Gi0/1", "Chassis id: 0001.0001.0001", "Port id: Gi0/1", "Port Description: uplink", "System Name: R2.example.local", "",
                        "System Description: ", "Cisco IOS Software", "", "Time remaining: 100 seconds", "System Capabilities: B,R", "Enabled Capabilities: R",
                        "Management Addresses:", "    IP: 192.0.2.2", "Auto Negotiation - not supported", "Physical media capabilities - not advertised",
                        "Media Attachment Unit type - not advertised", "Vlan ID: - not advertised", "", "Total entries displayed: 1", ""])
        ad = FakeAdapter({"show lldp neighbors detail": lldp})
        got = TD.collect({"R1": 1}, lambda d: ad)
        self.assertEqual(ad.cmds, ["show cdp neighbors detail", "show lldp neighbors detail"])
        self.assertEqual(got["R1"]["source"], "lldp")
        self.assertEqual(got["R1"]["rows"][0]["peer"], "R2")

    def test_命令被拒_设备不可达_如实记录_不影响其它(self):
        def factory(d):
            if d == 2:
                raise OSError("unreachable")
            return FakeAdapter({}, allowed=False)
        got = TD.collect({"R1": 1, "R2": 2}, factory)
        self.assertEqual(got["R1"]["rows"], [])
        self.assertIn("denied", got["R1"]["error"])
        self.assertIn("unreachable", got["R2"]["error"])

    def test_名字和接口规整(self):
        self.assertEqual(TD.short_name("A1.netops-ai.local"), "A1")
        self.assertEqual(TD.short_name("192.0.2.9"), "192.0.2.9")
        self.assertEqual(TD.canon_if("GigabitEthernet 0/1"), TD.canon_if("Gi0/1"))


class TestBuild(unittest.TestCase):
    def test_两端互看_和已登记完全一致(self):
        res = TD.build(obs(R1=[row("Gi0/1", "R2", "Gi0/1"), row("Gi0/2", "S1", "Gi0/1")], R2=[row("Gi0/1", "R1", "Gi0/1")], S1=[row("Gi0/1", "R1", "Gi0/2")]), KNOWN)
        self.assertEqual(len(res["links"]), 2)
        self.assertTrue(all(l["both_sides"] for l in res["links"]))
        self.assertEqual((res["extra"], res["missing"], res["external"], res["shared"]), ([], [], {}, []))

    def test_没看到的已登记连线_新发现的连线_单向(self):
        res = TD.build(obs(R1=[row("Gi0/1", "R2", "Gi0/1"), row("Gi0/3", "S1", "Gi0/2")], R2=[], S1=[row("Gi0/2", "R1", "Gi0/3")]), KNOWN)
        self.assertEqual(res["missing"], ["R1 GigabitEthernet0/2 — S1 GigabitEthernet0/1"])
        self.assertEqual([l["a"] + " | " + l["b"] for l in res["extra"]], ["R1 gi0/3 | S1 gi0/2"])
        self.assertEqual([l["a"] for l in res["unidirectional"]], ["R1 gi0/1"])  # R2 没数据，只有 R1 一端看到
        self.assertEqual(res["unreachable"], ["R2"])

    def test_共享网段不当连线_外部邻居单列_别名能认出来(self):
        mgmt = [row("Gi0/0", n, "Gi0/0") for n in ("R2", "S1", "X9")]
        res = TD.build(obs(R1=mgmt + [row("Gi0/5", "EXT1", "Fa0/1")], R2=[row("Gi0/1", "R1-vios", "Gi0/1")]), KNOWN)
        self.assertEqual(res["shared"][0]["interface"], "gi0/0")
        self.assertEqual(sorted(res["external"]), ["EXT1"])  # X9 在共享网段里，不单列
        self.assertEqual([(l["a"], l["b"]) for l in res["links"]], [("R1 gi0/1", "R2 gi0/1")])  # R1-vios 认成 R1

    def test_报告_建议yaml_图(self):
        res = TD.build(obs(R1=[row("Gi0/1", "R2", "Gi0/1"), row("Gi0/3", "S1", "Gi0/2")], R2=[row("Gi0/1", "R1", "Gi0/1")], S1=[]), KNOWN)
        md = "\n".join(str(b) for b in TD.to_blocks(res))
        self.assertIn("没看到", md)
        self.assertIn("新发现", md)
        self.assertIn('a: "R1 gi0/1"', TD.to_yaml(res))
        with tempfile.TemporaryDirectory() as d:
            p = TD.render_png(res, KNOWN, Path(d) / "t.png")
            self.assertTrue(p.read_bytes().startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()

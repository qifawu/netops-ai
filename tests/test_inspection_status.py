"""状态巡检（inspection/status.py）。设备输出用真机抓下来的原文（vIOS 15.9，EVE-NG），
适配器用假的——**不连任何真设备，也不碰 records/**。
"""
from __future__ import annotations

import unittest

from netops_ai.devices.base import CommandResult
from netops_ai.inspection import status as st
from netops_ai.topology import TopologyDevice, TopologyLink

BRIEF_D1 = """
Interface                  IP-Address      OK? Method Status                Protocol
GigabitEthernet0/0         192.0.2.52       YES NVRAM  up                    up
GigabitEthernet0/1         10.0.1.2        YES NVRAM  up                    up
GigabitEthernet0/2         10.0.1.10       YES NVRAM  administratively down down
GigabitEthernet0/4         unassigned      YES NVRAM  up                    up
"""
BRIEF_D1_OK = BRIEF_D1.replace("administratively down down    ", "up                    up      ")
BRIEF_V1 = """
Interface              IP-Address      OK? Method Status                Protocol
GigabitEthernet0/0     192.0.2.50       YES NVRAM  up                    up
GigabitEthernet0/1     10.0.1.1        YES NVRAM  up                    up
GigabitEthernet0/2     10.0.1.5        YES NVRAM  up                    up
"""
OSPF_FULL = """
Neighbor ID     Pri   State           Dead Time   Address         Interface
4.4.4.4           0   FULL/  -        00:00:31    10.0.1.18       GigabitEthernet0/3
1.1.1.1           0   FULL/  -        00:00:37    10.0.1.1        GigabitEthernet0/1
"""
OSPF_EXSTART = OSPF_FULL.replace("FULL/  -        00:00:37", "EXSTART/DR   00:00:37")
BGP_OK = """
Neighbor        V           AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd
10.0.0.2        4        65000    8404    8385        1    0    0 5d07h           0
"""
BGP_ACTIVE = BGP_OK.replace("5d07h           0", "never    Active")
SHOW_INT = """GigabitEthernet0/1 is up, line protocol is up
     0 input errors, 0 CRC, 0 frame, 0 overrun, 0 ignored
     0 output errors, 0 collisions, 1 interface resets
GigabitEthernet0/2 is up, line protocol is up
     5 input errors, 3 CRC, 0 frame, 0 overrun, 0 ignored
     2 output errors, 0 collisions, 0 interface resets
"""


def _dev(name, host, links):
    return TopologyDevice(name=name, host=host, links=tuple(TopologyLink(*link) for link in links))


class FakeAdapter:
    def __init__(self, outputs, fail=False):
        self.outputs, self.fail, self.commands = outputs, fail, []

    def __enter__(self):
        if self.fail:
            raise TimeoutError("ssh timeout")
        return self

    def __exit__(self, *a):
        return False

    def run(self, command):
        self.commands.append(command)
        return CommandResult(command=command, allowed=True, output=self.outputs.get(command, ""))


class TestParsers(unittest.TestCase):
    def test_brief(self):
        rows = st.parse_brief(BRIEF_D1)
        self.assertEqual(rows["GigabitEthernet0/2"]["status"], "administratively down")
        self.assertEqual(rows["GigabitEthernet0/4"]["ip"], "unassigned")

    def test_ospf_bgp_errors(self):
        self.assertEqual([n["state"] for n in st.parse_ospf(OSPF_EXSTART)], ["FULL", "EXSTART"])
        self.assertEqual(st.parse_bgp(BGP_ACTIVE)[0]["state"], "Active")
        self.assertEqual(st.parse_bgp(BGP_OK)[0]["state"], "0")
        admin = "10.0.0.2        4        65000       0       0        1    0    0 00:00:24 Idle (Admin)"  # 真机故障注入抓到的原文
        self.assertEqual(st.parse_bgp(admin)[0]["state"], "Idle (Admin)")
        c = st.parse_errors(SHOW_INT)
        self.assertEqual((c["GigabitEthernet0/2"]["input_errors"], c["GigabitEthernet0/2"]["output_errors"]), (5, 2))


class TestCleanError(unittest.TestCase):
    def test_报错里的整条ssh命令行不进结果(self):
        raw = "RuntimeError: Command '['ssh', '-o', 'KexAlgorithms=+x', '-p', '22', 'someuser@192.0.2.99', 'show version']' timed out after 10.0 seconds"
        cleaned = st._clean_error(raw)
        self.assertNotIn("someuser", cleaned)
        self.assertNotIn("KexAlgorithms", cleaned)
        self.assertIn("timed out after 10.0 seconds", cleaned)


class TestChecks(unittest.TestCase):
    def setUp(self):
        self.d1 = _dev("D1", "192.0.2.52", [("GigabitEthernet0/1", "V1", "GigabitEthernet0/2"), ("GigabitEthernet0/2", "V2", "GigabitEthernet0/2")])

    def test_管理关闭的线缆接口是问题且带原话(self):
        c = st.check_interfaces(self.d1, st.parse_brief(BRIEF_D1), "show ip interface brief")
        self.assertEqual(c["status"], "bad")
        self.assertIn("管理关闭", c["summary"])
        self.assertIn(c["evidence"][0], BRIEF_D1)  # 证据逐字出自设备输出

    def test_ospf邻居不是full(self):
        c = st.check_ospf(OSPF_EXSTART, 2, "x")
        self.assertEqual(c["status"], "bad")
        self.assertTrue(all(line in OSPF_EXSTART for line in c["evidence"]))

    def test_ospf邻居数少于拓扑预期(self):
        self.assertEqual(st.check_ospf(OSPF_FULL, 3, "x")["status"], "bad")
        self.assertEqual(st.check_ospf(OSPF_FULL, 2, "x")["status"], "ok")

    def test_没跑ospf或没有三层互联就跳过(self):
        self.assertEqual(st.check_ospf("", 0, "x")["status"], "skip")
        self.assertEqual(st.check_ospf("% OSPF: Router process 1 is not running", 2, "x")["status"], "skip")

    def test_bgp(self):
        self.assertEqual(st.check_bgp(BGP_OK, "x")["status"], "ok")
        bad = st.check_bgp(BGP_ACTIVE, "x")
        self.assertEqual(bad["status"], "bad")
        self.assertTrue(all(line in BGP_ACTIVE for line in bad["evidence"]))
        self.assertEqual(st.check_bgp("\n% BGP not active\n", "x")["status"], "skip")

    def test_错误计数只看增量_无基线只给关注(self):
        counters = st.parse_errors(SHOW_INT)
        no_base = st.check_errors(self.d1, counters, None, "x")
        self.assertEqual(no_base["status"], "warn")
        self.assertIn("没有上一次基线", no_base["summary"])
        base = {"GigabitEthernet0/2": {"input_errors": 5, "crc": 3, "output_errors": 2}}
        self.assertEqual(st.check_errors(self.d1, counters, base, "x")["status"], "ok")
        grew = {"GigabitEthernet0/2": {"input_errors": 1, "crc": 0, "output_errors": 0}}
        self.assertEqual(st.check_errors(self.d1, counters, grew, "x")["status"], "bad")
        reset = {"GigabitEthernet0/2": {"input_errors": 900, "crc": 0, "output_errors": 0}}  # 接口重启计数回零
        self.assertEqual(st.check_errors(self.d1, counters, reset, "x")["status"], "ok")


class TestRun(unittest.TestCase):
    def _topo(self):
        v1 = _dev("V1", "192.0.2.50", [("GigabitEthernet0/1", "D1", "GigabitEthernet0/1"), ("GigabitEthernet0/2", "D9", "GigabitEthernet0/1")])
        d1 = _dev("D1", "192.0.2.52", [("GigabitEthernet0/1", "V1", "GigabitEthernet0/1"), ("GigabitEthernet0/4", "A1", "GigabitEthernet0/1")])
        a1 = _dev("A1", "192.0.2.54", [("GigabitEthernet0/1", "D1", "GigabitEthernet0/4")])
        d9 = _dev("D9", "192.0.2.99", [])
        return {d.name: d for d in (v1, d1, a1, d9)}

    def test_整轮_连不上的设备是严重发现_不拖垮其它(self):
        outputs = {
            "V1": {"show ip interface brief": BRIEF_V1, "show ip ospf neighbor": OSPF_FULL, "show ip bgp summary": BGP_OK, "show interfaces": SHOW_INT},
            "D1": {"show ip interface brief": BRIEF_D1_OK, "show ip ospf neighbor": OSPF_FULL, "show ip bgp summary": "% BGP not active", "show interfaces": SHOW_INT},
            "A1": {"show ip interface brief": "GigabitEthernet0/1     unassigned      YES unset  up                    up      ",
                   "show ip ospf neighbor": "", "show ip bgp summary": "% BGP not active", "show interfaces": ""},
        }

        def factory(device):
            return FakeAdapter(outputs.get(device.name, {}), fail=device.name == "D9")

        snap = st.run_status_inspection(topology=self._topo(), adapter_factory=factory)
        by = {d["name"]: d for d in snap["devices"]}
        self.assertFalse(by["D9"]["reachable"])
        self.assertEqual(by["D9"]["checks"][0]["status"], "bad")
        self.assertEqual(snap["summary"]["unreachable"], 1)
        self.assertNotIn("raw", by["V1"])  # 原始输出不落盘，只留检查结论 + 证据行
        # V1 的 Gi0/2 对端 D9 连不上 → 拿不到对端接口，不算三层互联，OSPF 预期只有 D1 那一条
        ospf = {c["check"]: c for c in by["V1"]["checks"]}["ospf"]
        self.assertEqual(ospf["status"], "ok")
        self.assertEqual(by["A1"]["checks"][1]["status"], "skip")
        self.assertIn("GigabitEthernet0/2", snap["counters"]["V1"])

    def test_只发白名单内的四条只读命令(self):
        seen = []

        def factory(device):
            a = FakeAdapter({"show ip interface brief": BRIEF_V1})
            seen.append(a)
            return a

        st.run_status_inspection(topology={"V1": _dev("V1", "h", [])}, adapter_factory=factory)
        self.assertEqual(seen[0].commands, list(st.COMMANDS.values()))
        for cmd in st.COMMANDS.values():
            self.assertTrue(cmd.startswith("show "))


if __name__ == "__main__":
    unittest.main()

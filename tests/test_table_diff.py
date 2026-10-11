"""通用表对表比较：命令输出解析成行记录表，按 key 字段对齐，报新增 / 消失 / 字段变化。
同一套规则换命令（路由表 / ARP / 自带模板）就能用，不是针对某一条命令写死的。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from netops_ai.inspection import checklist as CL
from netops_ai.inspection import plan_chat as PC
from netops_ai.inspection import table_diff as TD
from netops_ai.inspection import table_parse as TP

NL = chr(10)
ROUTES = NL.join([
    "Gateway of last resort is not set", "",
    "      198.51.100.0/24 is variably subnetted, 4 subnets, 2 masks",
    "C        198.51.100.0/30 is directly connected, GigabitEthernet0/1",
    "O        203.0.113.0/24 [110/2] via 198.51.100.2, 00:10:21, GigabitEthernet0/1",
    "                     [110/2] via 198.51.100.6, 00:10:21, GigabitEthernet0/2",
    "S        192.0.2.0/24 [1/0] via 198.51.100.2", ""])
ARP = NL.join([
    "Protocol  Address     Age (min)  Hardware Addr   Type   Interface",
    "Internet  198.51.100.1      5   aabb.cc00.0100  ARPA   GigabitEthernet0/1",
    "Internet  198.51.100.2      7   aabb.cc00.0200  ARPA   GigabitEthernet0/1", ""])
KEY_ROUTE = ["vrf", "network", "prefix_length", "nexthop_ip", "nexthop_if"]


def run(rule, command, outputs, vendor="cisco"):
    cl = {"name": "t", "vendor": vendor, "devices": [{"name": "V1", "host": "192.0.2.50"}],
          "checks": [{"id": "c", "command": command, "expect": [rule]}]}
    hist, res = [], []
    for o in outputs:
        class A:
            def run(self, c, o=o):
                return SimpleNamespace(allowed=True, denial_reason="", error="", output=o)

            def close(self):
                pass
        r = CL.run_checklist(cl, lambda d: A(), history=hist)
        hist.append(r)
        res.append(r["devices"][0]["checks"][0]["evaluations"][0])
    return res


class TestParse(unittest.TestCase):
    def test_管道过滤不影响命中模板_等价多路径各占一行(self):
        r = TP.parse("show ip route | begin Gateway", ROUTES)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(len([x for x in r.rows if x["network"] == "203.0.113.0"]), 2)
        self.assertIn("nexthop_if", r.fields)

    def test_没有解析器_说清楚(self):
        r = TP.parse("show something unknown", "x")
        self.assertFalse(r.ok)
        self.assertIsNone(TP.describe("show something unknown"))

    def test_自带模板(self):
        tpl = NL.join(["Value NAME (\S+)", "Value STATE (up|down)", "", "Start", "  ^${NAME} is ${STATE} -> Record"])
        r = TP.parse("show whatever", "eth0 is up" + NL + "eth1 is down", template=tpl)
        self.assertEqual([x["state"] for x in r.rows], ["up", "down"])

    def test_坏模板被静态检查拦下(self):
        self.assertTrue(TP.validate_template("not a template"))
        self.assertTrue(TP.validate_template(""))


class TestTableDiff(unittest.TestCase):
    def rule(self, **kw):
        return {"type": "table_diff_vs_previous", "key": KEY_ROUTE, "severity": "critical", **kw}

    def test_持平_老化时间变了不算(self):
        later = ROUTES.replace("00:10:21", "00:15:40")
        r = run(self.rule(), "show ip route", [ROUTES, later])
        self.assertEqual((r[0]["state"], r[1]["state"]), ("baseline", "pass"))

    def test_字段级变化_说出从什么变成什么(self):
        changed = ROUTES.replace("[110/2] via 198.51.100.6", "[110/9] via 198.51.100.6")
        r = run(self.rule(compare=["protocol", "distance", "metric"]), "show ip route | begin Gateway", [ROUTES, changed])
        self.assertEqual((r[1]["changed"], r[1]["added"], r[1]["removed"], r[1]["passed"]), (1, 0, 0, False))
        self.assertIn("metric: 2 -> 9", r[1]["changed_sample"][0])

    def test_新增和消失(self):
        gone = ROUTES.replace("S        192.0.2.0/24 [1/0] via 198.51.100.2", "S        203.0.113.128/25 [1/0] via 198.51.100.2")
        r = run(self.rule(), "show ip route", [ROUTES, gone])
        self.assertEqual((r[1]["added"], r[1]["removed"]), (1, 1))
        self.assertTrue(any("192.0.2.0" in x for x in r[1]["removed_sample"]))

    def test_阈值放宽就不报(self):
        gone = ROUTES.replace("S        192.0.2.0/24 [1/0] via 198.51.100.2", "")
        r = run(self.rule(max_removed=1), "show ip route", [ROUTES, gone])
        self.assertTrue(r[1]["passed"])

    def test_换一条命令同一套规则_ARP(self):
        moved = ARP.replace("aabb.cc00.0200", "aabb.cc00.0299").replace("      7 ", "      9 ")
        rule = {"type": "table_diff_vs_previous", "key": ["ip_address"], "severity": "warning"}
        r = run(rule, "show ip arp", [ARP, ARP.replace("      5 ", "     50 "), moved])
        self.assertEqual(r[1]["state"], "pass")  # age 是易变字段，默认不比
        self.assertEqual(r[2]["changed"], 1)
        self.assertIn("mac_address", r[2]["changed_sample"][0])

    def test_key字段不存在_报错并列出可用字段(self):
        r = run({"type": "table_diff_vs_previous", "key": ["nope"], "severity": "warning"}, "show ip arp", [ARP])
        self.assertEqual((r[0]["state"], r[0]["passed"]), ("error", False))
        self.assertIn("ip_address", r[0]["detail"])

    def test_没有解析器_默认报错_允许fallback才退回文本(self):
        rule = {"type": "table_diff_vs_previous", "key": ["x"], "severity": "warning"}
        r = run(rule, "show something unknown", ["a" + NL + "b"])
        self.assertEqual(r[0]["state"], "error")
        r = run({**rule, "fallback": "text"}, "show something unknown", ["a" + NL + "b", "a"])
        self.assertEqual(r[1]["removed"], 1)

    def test_自带模板的规则(self):
        tpl = NL.join(["Value NAME (\S+)", "Value STATE (up|down)", "", "Start", "  ^${NAME} is ${STATE} -> Record"])
        rule = {"type": "table_diff_vs_previous", "key": ["name"], "severity": "critical", "template": tpl}
        r = run(rule, "show custom", ["eth0 is up" + NL + "eth1 is up", "eth0 is up" + NL + "eth1 is down"])
        self.assertIn("state: up -> down", r[1]["changed_sample"][0])

    def test_保存前校验(self):
        cl = {"name": "t", "vendor": "cisco", "devices": [{"name": "V1", "host": "192.0.2.50"}],
              "checks": [{"id": "c", "command": "show ip arp", "expect": [{"type": "table_diff_vs_previous", "key": []}]}]}
        self.assertTrue(any("key" in p for p in CL.validate_checklist(cl)))
        cl["checks"][0]["expect"] = [{"type": "table_diff_vs_previous", "key": ["ip_address"], "template": "bad"}]
        self.assertTrue(any("template" in p for p in CL.validate_checklist(cl)))

    def test_模板库里的路由表明细对比走表对表_且不会静默放过(self):
        from netops_ai.inspection import plans as P

        chk = P.TEMPLATES["route_table_diff"]["check"]
        self.assertEqual(chk["expect"][0]["type"], "table_diff_vs_previous")
        cl = {"name": "t", "vendor": "cisco", "devices": [{"name": "V1", "host": "192.0.2.50"}], "checks": [{"id": "rt", **chk}]}
        self.assertEqual(CL.validate_checklist(cl), [])


class TestModelRule(unittest.TestCase):
    def test_模型给的字段名不对_问题带可用字段回给模型(self):
        rule = {"type": "table_diff_vs_previous", "key": ["ip"], "severity": "warning"}
        p = PC.table_rule_problems("checks[0].table_diff[0]", "show ip arp", rule, "cisco")
        self.assertTrue(p and "ip_address" in p[0])
        ok = PC.table_rule_problems("w", "show ip arp", {**rule, "key": ["ip_address"]}, "cisco")
        self.assertEqual(ok, [])

    def test_没有内置解析器_要求自带模板(self):
        p = PC.table_rule_problems("w", "show something unknown", {"key": ["a"]}, "cisco")
        self.assertIn("template", p[0])


if __name__ == "__main__":
    unittest.main()

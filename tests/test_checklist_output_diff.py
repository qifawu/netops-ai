"""输出明细对比（output_diff_vs_previous）：整张路由表跟上一次逐条比。

覆盖：首次建基线、新增 / 消失、易变字段（路由老化时间）不算变化、汇总行里的数字不算、等价多路径逐条算、
阈值、设备不可达 / 被拒不覆盖基线、输出被截断时只判看得全的一边、校验、路由表模板的真实回显。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from netops_ai.inspection import checklist as CL
from netops_ai.inspection import plans as P

TABLE_A = """Codes: C - connected, S - static, R - RIP, O - OSPF
Gateway of last resort is not set

      198.51.100.0/24 is variably subnetted, 4 subnets, 2 masks
C        198.51.100.0/30 is directly connected, GigabitEthernet0/1
O        198.51.100.4/30 [110/2] via 192.0.2.2, 00:12:44, GigabitEthernet0/1
O        203.0.113.0/24 [110/20] via 192.0.2.2, 00:12:44, GigabitEthernet0/1
                     [110/20] via 192.0.2.6, 00:12:40, GigabitEthernet0/2
S        203.0.113.128/25 [1/0] via 192.0.2.2
"""
# 同一张表，只是老化时间不一样、汇总行数字不一样（路由本身没变）
TABLE_A_LATER = TABLE_A.replace("00:12:44", "01:40:02").replace("00:12:40", "01:39:58").replace("4 subnets", "4 subnets, 9 total")
TABLE_MINUS = TABLE_A.replace("S        203.0.113.128/25 [1/0] via 192.0.2.2\n", "")
TABLE_PLUS = TABLE_A + "S        203.0.113.64/26 [1/0] via 192.0.2.2\n"
TABLE_ONE_PATH_LOST = TABLE_A.replace("                     [110/20] via 192.0.2.6, 00:12:40, GigabitEthernet0/2\n", "")


class Adapter:
    def __init__(self, out="", deny=False):
        self.out, self.deny = out, deny

    def run(self, command):
        if self.deny:
            return SimpleNamespace(allowed=False, denial_reason="refused", error="", output="")
        return SimpleNamespace(allowed=True, denial_reason="", error="", output=self.out)

    def close(self):
        pass


def rule(**kw):
    e = dict(P.TEMPLATES["route_table_diff"]["check"]["expect"][0])
    e.update(kw)
    return e


def checklist(e):
    return {"name": "rt", "vendor": "cisco", "devices": [{"name": "V1", "host": "192.0.2.50"}],
            "checks": [{"id": "table", "command": "show ip route | begin Gateway", "expect": [e]}]}


def run_seq(e, outputs):
    hist, res = [], []
    for o in outputs:
        if o is None:
            factory = mock.Mock(side_effect=OSError("timed out"))
        elif o == "deny":
            factory = lambda dev: Adapter(deny=True)  # noqa: E731
        else:
            factory = lambda dev, o=o: Adapter(o)  # noqa: E731
        r = CL.run_checklist(checklist(e), factory, history=hist)
        hist.append(r)
        res.append(r)
    return res


def ev(result):
    return result["devices"][0]["checks"][0]["evaluations"][0]


class TestOutputDiff(unittest.TestCase):
    def test_首次运行只建基线_不判(self):
        r = run_seq(rule(), [TABLE_A])
        e = ev(r[0])
        self.assertEqual(e["state"], "baseline")
        self.assertIsNone(e["passed"])
        self.assertEqual(r[0]["devices"][0]["checks"][0]["status"], "pass")

    def test_路由没变_老化时间和汇总行数字变了也算持平(self):
        r = run_seq(rule(), [TABLE_A, TABLE_A_LATER])
        e = ev(r[1])
        self.assertEqual((e["added"], e["removed"], e["state"]), (0, 0, "pass"))

    def test_消失一条路由_判失败_并列出样例(self):
        r = run_seq(rule(), [TABLE_A, TABLE_MINUS])
        e = ev(r[1])
        self.assertEqual((e["added"], e["removed"], e["passed"]), (0, 1, False))
        self.assertTrue(any("203.0.113.128/25" in x for x in e["removed_sample"]), e["removed_sample"])
        self.assertEqual(r[1]["devices"][0]["checks"][0]["status"], "fail")
        self.assertEqual(r[1]["devices"][0]["checks"][0]["severity"], "critical")

    def test_新增一条路由_默认也报警_阈值放宽就不报(self):
        r = run_seq(rule(), [TABLE_A, TABLE_PLUS])
        self.assertEqual((ev(r[1])["added"], ev(r[1])["passed"]), (1, False))
        r = run_seq(rule(max_added=2), [TABLE_A, TABLE_PLUS])
        self.assertTrue(ev(r[1])["passed"])

    def test_等价多路径丢了一条路径_看得出来(self):
        r = run_seq(rule(), [TABLE_A, TABLE_ONE_PATH_LOST])
        e = ev(r[1])
        self.assertEqual(e["removed"], 1, e)
        self.assertTrue(any("192.0.2.6" in x for x in e["removed_sample"]))

    def test_设备不可达或命令被拒_不覆盖基线(self):
        r = run_seq(rule(), [TABLE_A, None, "deny", TABLE_MINUS])
        last = ev(r[3])
        # 对照的是第一次成功采集的那份，不是中间失败的
        self.assertEqual(last["removed"], 1)
        self.assertEqual(last["reference_run_id"], r[0]["run_id"])

    def test_当前输出被截断_消失的不算(self):
        with mock.patch.object(CL, "DIFF_MAX_CHARS", 120):
            r = run_seq(rule(), [TABLE_A, TABLE_A])
        e = ev(r[1])
        self.assertTrue(e["truncated"]["current"])
        self.assertIsNone(e["removed"])  # 看不全，不能说「消失」

    def test_路由表模板的规则能通过校验(self):
        d = P.recommended_draft({}, [], devices=[], templates=[]) if False else None  # noqa: F841
        tpl = P.TEMPLATES["route_table_diff"]["check"]
        cl = {"name": "rt", "vendor": "cisco", "devices": [{"name": "V1", "host": "192.0.2.50"}],
              "checks": [{"id": "table", "command": tpl["command"], "expect": tpl["expect"]}]}
        self.assertEqual(CL.validate_checklist(cl), [])

    def test_非法规则被校验拦住(self):
        bad = checklist({"type": "output_diff_vs_previous", "max_added": -1, "ignore_regex": ["("]})
        problems = " ".join(CL.validate_checklist(bad))
        self.assertIn("max_added", problems)
        self.assertIn("ignore_regex", problems)


if __name__ == "__main__":
    unittest.main()

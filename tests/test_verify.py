"""证据校验器的测试。三个"复现判方发现的真实失败案例"是这份测试的重点——
上一轮我自己写的校验脚本（分块模糊匹配 + 两个来源合并成一个池子）会放过
这三种情况，这次改写之后必须能抓到，不然这个校验器本身就没达到目的。
"""

from __future__ import annotations

import unittest

from netops_ai.analysis.verify import summarize, verify_evidence

ZABBIX_TEXT = """
## Zabbix 数据
icmpping 历史：13:07:24 ~ 13:12:24，六次轮询全部是 1（up）
eventid 27470 "IOSv: Device has been replaced"
中间隔着别的告警，跟下面这条不是连续原文
eventid 27717 "System name has changed"
"""

DEVICE_TEXT = """
## 设备数据
show ip interface brief
GigabitEthernet0/1 administratively down down
"""


class TestExactMatch(unittest.TestCase):
    def test_逐字命中的zabbix证据通过(self):
        evidence = [
            {
                "claim": "x",
                "source": "六次轮询全部是 1（up）",
                "source_from": "zabbix",
            }
        ]
        results = verify_evidence(evidence, ZABBIX_TEXT, DEVICE_TEXT)
        self.assertTrue(results[0].verified)
        self.assertEqual(results[0].grade, "verbatim")

    def test_逐字命中的device证据通过(self):
        evidence = [
            {
                "claim": "x",
                "source": "GigabitEthernet0/1 administratively down down",
                "source_from": "device",
            }
        ]
        results = verify_evidence(evidence, ZABBIX_TEXT, DEVICE_TEXT)
        self.assertTrue(results[0].verified)
        self.assertEqual(results[0].grade, "verbatim")

    def test_中文枚举来源也能核对(self):
        """schema 枚举中文化之后 source_from 是「监控/设备」，验证器一度只认 zabbix/device，
        真跑时 3/3 条逐字引文全被判「来源不合法」。"""
        evidence = [
            {"claim": "x", "source": "六次轮询全部是 1（up）", "source_from": "监控"},
            {"claim": "y", "source": "GigabitEthernet0/1 administratively down down", "source_from": "设备"},
        ]
        results = verify_evidence(evidence, ZABBIX_TEXT, DEVICE_TEXT)
        self.assertEqual([r.verified for r in results], [True, True])

    def test_来源标错但逐字存在于另一来源算通过(self):
        """设备 syslog 经 Zabbix 收进来，模型标「设备」，原文其实在 Zabbix 文本里。"""
        evidence = [{"claim": "x", "source": "六次轮询全部是 1（up）", "source_from": "设备"}]
        r = verify_evidence(evidence, ZABBIX_TEXT, DEVICE_TEXT)[0]
        self.assertTrue(r.verified)
        self.assertEqual(r.grade, "cross_source")
        self.assertIn("来源标错", r.reason)

    def test_跨物理换行但内容没变靠空白折叠通过(self):
        # 模型可能把 markdown 里跨行的内容复制成一整段，物理换行变成空格
        evidence = [
            {
                "claim": "x",
                "source": "show ip interface brief GigabitEthernet0/1 administratively down down",
                "source_from": "device",
            }
        ]
        results = verify_evidence(evidence, ZABBIX_TEXT, DEVICE_TEXT)
        self.assertTrue(results[0].verified)
        self.assertEqual(results[0].grade, "reformatted")


class TestKnownFailureModes(unittest.TestCase):
    """判方独立核对 62 条证据时发现的 3 种真实失败模式，逐条复现验证能抓到。"""

    def test_掉字不能通过(self):
        # 真实案例：原文"全部是 1"被模型引用成"全部 1"，掉了"是"字
        evidence = [
            {
                "claim": "x",
                "source": "六次轮询全部 1（up）",  # 掉了"是"
                "source_from": "zabbix",
            }
        ]
        results = verify_evidence(evidence, ZABBIX_TEXT, DEVICE_TEXT)
        self.assertFalse(results[0].verified)
        self.assertEqual(results[0].grade, "fabricated")

    def test_跨来源拼接不能通过(self):
        # 真实案例：设备侧输出和 Zabbix 侧文本拼进了同一个 source 字段。
        # 关键点：即便把两份原文合并成一个大池子能找到这个子串，
        # 按 source_from 分别校验也必须失败——这正是上一版校验器的漏洞。
        stitched = "六次轮询全部是 1（up） GigabitEthernet0/1 administratively down down"
        for source_from in ("zabbix", "device"):
            with self.subTest(source_from=source_from):
                evidence = [{"claim": "x", "source": stitched, "source_from": source_from}]
                results = verify_evidence(evidence, ZABBIX_TEXT, DEVICE_TEXT)
                self.assertFalse(results[0].verified)
                self.assertEqual(results[0].grade, "fabricated")

    def test_跨行拼接不相邻内容不能通过(self):
        # 真实案例：把两条不相邻的 eventid 引用拼在一起当成一条连续原文
        evidence = [
            {
                "claim": "x",
                "source": 'eventid 27470 "IOSv: Device has been replaced" eventid 27717',
                "source_from": "zabbix",
            }
        ]
        # 这两段在原文里隔着一整行，折叠空白后也不是连续子串
        results = verify_evidence(evidence, ZABBIX_TEXT, DEVICE_TEXT)
        self.assertFalse(results[0].verified)
        self.assertEqual(results[0].grade, "fabricated")


class TestReformattedRealShapes(unittest.TestCase):
    def test_模型加了连接符(self):
        device_text = """
### `show interfaces Ethernet1/0`

```
Ethernet1/0 is up, line protocol is up
```
"""
        evidence = [
            {
                "claim": "x",
                "source": "show interfaces Ethernet1/0 ->  Ethernet1/0 is up, line protocol is up",
                "source_from": "device",
            }
        ]
        r = verify_evidence(evidence, "", device_text)[0]
        self.assertTrue(r.verified)
        self.assertEqual(r.grade, "reformatted")

    def test_抄了zbx_history渲染格式(self):
        zabbix_text = "Sep 21 22:20:49 192.0.2.50 57: *Sep 21 22:18:35.905: %BGP-3-NOTIFICATION"
        evidence = [
            {
                "claim": "x",
                "source": "1790029250=Sep 21 22:20:49 192.0.2.50 57: *Sep 21 22:18:35.905: %BGP-3-NOTIFICATION",
                "source_from": "zabbix",
            }
        ]
        r = verify_evidence(evidence, zabbix_text, "")[0]
        self.assertTrue(r.verified)
        self.assertEqual(r.grade, "reformatted")

    def test_抄了时间线渲染前缀(self):
        device_text = "Sep 21 18:14:15 192.0.2.52 159: *Sep 21 18:12:01.123: %SYS-5-CONFIG_I"
        evidence = [
            {
                "claim": "x",
                "source": "18:14:14  [device] Sep 21 18:14:15 192.0.2.52 159: *Sep 21 18:12:01.123: %SYS-5-CONFIG_I",
                "source_from": "device",
            }
        ]
        r = verify_evidence(evidence, "", device_text)[0]
        self.assertTrue(r.verified)
        self.assertEqual(r.grade, "reformatted")

    def test_diff片段被列表前缀影响(self):
        device_text = """
```
--- 前
+++ 后
@@ -1,4 +1,4 @@
!
-! Last configuration change at 18:12:01 UTC Sun Sep 21 2026
```
"""
        evidence = [
            {
                "claim": "x",
                "source": "- --- 前\n+++ 后\n@@ -1,4 +1,4 @@\n!\n-! Last configuration change at 18:12:01 UTC Sun Sep 21 2026",
                "source_from": "device",
            }
        ]
        r = verify_evidence(evidence, "", device_text)[0]
        self.assertTrue(r.verified)
        self.assertEqual(r.grade, "reformatted")

    def test_窗口内没变过拆开引用(self):
        zabbix_text = (
            "窗口内没变过：Interface Gi0/1(): Operational status=1；"
            "ICMP loss=0；"
            "Interface Gi0/1(): Inbound packets with errors=0；"
            "Interface Gi0/1(): Outbound packets with errors=0"
        )
        evidence = [
            {
                "claim": "x",
                "source": (
                    "Interface Gi0/1(): Operational status=1；"
                    "Interface Gi0/1(): Inbound packets with errors=0；"
                    "Interface Gi0/1(): Outbound packets with errors=0"
                ),
                "source_from": "zabbix",
            }
        ]
        r = verify_evidence(evidence, zabbix_text, "")[0]
        self.assertTrue(r.verified)
        self.assertEqual(r.grade, "reformatted")

    def test_真编造必须是fabricated(self):
        evidence = [{"claim": "x", "source": "原文里完全不存在的内容", "source_from": "zabbix"}]
        r = verify_evidence(evidence, "真实原文", "")[0]
        self.assertFalse(r.verified)
        self.assertEqual(r.grade, "fabricated")

    def test_两个不相邻片段拼在一起必须是fabricated(self):
        zabbix_text = "alpha 出现在第一行\n中间隔着别的告警，跟下面这条不是连续原文\nomega 出现在第三行"
        evidence = [{"claim": "x", "source": "alpha 出现在第一行；omega 出现在第三行", "source_from": "zabbix"}]
        r = verify_evidence(evidence, zabbix_text, "")[0]
        self.assertFalse(r.verified)
        self.assertEqual(r.grade, "fabricated")


class TestEdgeCases(unittest.TestCase):
    def test_source_from写错枚举值直接判不通过(self):
        evidence = [{"claim": "x", "source": "六次轮询全部是 1（up）", "source_from": "both"}]
        results = verify_evidence(evidence, ZABBIX_TEXT, DEVICE_TEXT)
        self.assertFalse(results[0].verified)
        self.assertIn("source_from", results[0].reason)

    def test_a组没有设备数据时标device来源直接判不通过(self):
        evidence = [{"claim": "x", "source": "GigabitEthernet0/1", "source_from": "device"}]
        results = verify_evidence(evidence, ZABBIX_TEXT, None)
        self.assertFalse(results[0].verified)

    def test_空source判不通过(self):
        evidence = [{"claim": "x", "source": "", "source_from": "zabbix"}]
        results = verify_evidence(evidence, ZABBIX_TEXT, DEVICE_TEXT)
        self.assertFalse(results[0].verified)


class TestSummarize(unittest.TestCase):
    def test_统计通过和不通过的数量(self):
        evidence = [
            {"claim": "a", "source": "六次轮询全部是 1（up）", "source_from": "zabbix"},
            {"claim": "b", "source": "编的内容", "source_from": "zabbix"},
        ]
        results = verify_evidence(evidence, ZABBIX_TEXT, DEVICE_TEXT)
        summary = summarize(results)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["verified"], 1)
        self.assertEqual(summary["unverified"], 1)
        self.assertEqual(summary["unverified_indices"], [1])
        self.assertEqual(summary["verbatim"], 1)
        self.assertEqual(summary["fabricated"], 1)


if __name__ == "__main__":
    unittest.main()

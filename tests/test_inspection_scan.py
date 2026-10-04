"""巡检编排层：用假 `ZabbixClient` 测，不碰真实网络。真实巡检的结果记在
内部文档，这里只测"取到数据之后编排逻辑对不对"。
"""

from __future__ import annotations

import unittest
from unittest import mock

from netops_ai.inspection.scan import scan_all_hosts, scan_host


def _hist(clock_value_pairs):
    return [{"clock": str(c), "value": str(v)} for c, v in clock_value_pairs]


class TestScanHost(unittest.TestCase):
    def test_跳过非数值型监控项(self):
        zbx = mock.MagicMock()
        zbx.list_items.return_value = [{"itemid": "1", "name": "log item", "key_": "some.log", "value_type": 2}]
        report = scan_host(zbx, "10", "V1")
        self.assertEqual(report.scanned_items, 0)
        zbx.get_history.assert_not_called()

    def test_数值型监控项没数据不算发现(self):
        zbx = mock.MagicMock()
        zbx.list_items.return_value = [{"itemid": "1", "name": "cpu", "key_": "system.cpu.util", "value_type": 0}]
        zbx.get_history.return_value = []
        report = scan_host(zbx, "10", "V1")
        self.assertEqual(report.scanned_items, 1)
        self.assertEqual(report.items_with_data, 0)
        self.assertEqual(report.findings, [])

    def test_状态类监控项走flap检测(self):
        zbx = mock.MagicMock()
        zbx.list_items.return_value = [
            {"itemid": "1", "name": "Gi0/1 status", "key_": "net.if.status[ifOperStatus.2]", "value_type": 0}
        ]
        series = []
        t = 0
        for _ in range(3):
            series.append((t, 2))
            t += 300
            series.append((t, 1))
            t += 600
        zbx.get_history.return_value = _hist(series)
        report = scan_host(zbx, "10", "V1")
        self.assertEqual(len(report.findings), 1)
        self.assertEqual(report.findings[0].finding.kind, "self_healing_flap")
        # F2：kind 提到顶层，不用再挖 finding.kind 才能拿到类型
        self.assertEqual(report.findings[0].kind, "self_healing_flap")
        self.assertEqual(report.findings[0].host_name, "V1")

    def test_普通数值型监控项走trend和spike检测不走flap(self):
        zbx = mock.MagicMock()
        zbx.list_items.return_value = [
            {"itemid": "1", "name": "CPU", "key_": "system.cpu.util", "value_type": 0}
        ]
        series = [(i * 7200, 10.0 + i * 2) for i in range(12)]
        zbx.get_history.return_value = _hist(series)
        report = scan_host(zbx, "10", "V1")
        kinds = {f.finding.kind for f in report.findings}
        self.assertIn("trend", kinds)
        self.assertNotIn("self_healing_flap", kinds)

    def test_operstate编码状态不走trend(self):
        zbx = mock.MagicMock()
        zbx.list_items.return_value = [
            {
                "itemid": "1",
                "name": "Interface vnet0: Operational status",
                "key_": 'vfs.file.contents["/sys/class/net/vnet0/operstate"]',
                "value_type": 3,
            }
        ]
        series = [(i * 7200, 2 if i < 4 else 6) for i in range(12)]
        zbx.get_history.return_value = _hist(series)
        report = scan_host(zbx, "10", "Zabbix server")
        self.assertEqual(report.findings, [])

    def test_icmpping上涨标记为变好而不是变坏(self):
        zbx = mock.MagicMock()
        zbx.list_items.return_value = [
            {"itemid": "1", "name": "ICMP ping", "key_": "icmpping", "value_type": 3}
        ]
        series = [(i * 7200, 0 if i < 4 else 1) for i in range(12)]
        zbx.get_history.return_value = _hist(series)
        report = scan_host(zbx, "10", "V1")
        self.assertEqual(len(report.findings), 1)
        self.assertEqual(report.findings[0].finding.kind, "trend")
        self.assertEqual(report.findings[0].finding.direction, "rising")
        self.assertEqual(report.findings[0].finding.health_impact, "improving")

    def test_list_items失败记录错误不崩(self):
        from netops_ai.zabbix.client import ZabbixAPIError

        zbx = mock.MagicMock()
        zbx.list_items.side_effect = ZabbixAPIError("连不上")
        report = scan_host(zbx, "10", "V1")
        self.assertTrue(report.errors)
        self.assertEqual(report.findings, [])

    def test_get_history失败记录错误继续扫下一个(self):
        from netops_ai.zabbix.client import ZabbixAPIError

        zbx = mock.MagicMock()
        zbx.list_items.return_value = [
            {"itemid": "1", "name": "a", "key_": "a", "value_type": 0},
            {"itemid": "2", "name": "b", "key_": "b", "value_type": 0},
        ]
        zbx.get_history.side_effect = [ZabbixAPIError("超时"), _hist([(i * 60, 10.0 + i * 2) for i in range(12)])]
        report = scan_host(zbx, "10", "V1")
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(report.scanned_items, 2)


class TestScanAllHosts(unittest.TestCase):
    def test_汇总多台主机(self):
        zbx = mock.MagicMock()
        zbx.list_hosts.return_value = [{"hostid": "10", "host": "V1"}, {"hostid": "11", "host": "V2"}]
        zbx.list_items.return_value = []
        report = scan_all_hosts(zbx)
        self.assertEqual(report.scanned_hosts, 2)


if __name__ == "__main__":
    unittest.main()

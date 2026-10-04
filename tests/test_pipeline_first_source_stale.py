"""一条真实告警 的 RCA：pipeline 的「第一信源」预取按 `alert_clock±15分钟` 过滤，
过滤的是「Zabbix 记这条历史的时刻」，没看这条历史『值』内部这行日志真实发生的时刻——
设备到日志采集之间的传输一断一续，补发的旧行会跟补发抵达那一刻共用同一个 clock，
结果 8 小时前的积压消息被当成「故障窗口内」证据塞给了模型。

修法：能从行首解析出真实时间（`parse_line_receive_time`）且明显在窗口外的，
第一信源里就不放；解析不出来的照旧保留（格式不认识不等于可疑）。
"""

from __future__ import annotations

import datetime
import unittest
from unittest import mock

from netops_ai.api import pipeline

REAL_ALERT_CLOCK = int(datetime.datetime(2026, 9, 27, 0, 0, 2, tzinfo=datetime.timezone.utc).timestamp())
STALE_LINE = "Sep 26 15:58:47 192.0.2.51 186: *Sep 26 15:43:35.728: %BGP-5-ADJCHANGE: neighbor 10.0.0.2 Down Admin. shutdown"
FRESH_LINE = "Sep 27 00:00:02 192.0.2.51 999: *Sep 27 00:00:02.000: %OSPF-5-ADJCHG: Process 1, Nbr 1.1.1.1 on GigabitEthernet0/1 from FULL to DOWN, Neighbor Down: Dead timer expired"
UNRECOGNIZED_LINE = "20260926.171505 PDU INFO: hostname=<UNKNOWN> source=192.0.2.51"


class TestFirstSourceDropsStaleBacklog(unittest.TestCase):
    def _ctx(self, mock_zbx_cls, *, history):
        mock_zbx = mock.MagicMock()
        mock_zbx_cls.return_value.__enter__.return_value = mock_zbx
        log_item = {"itemid": "53001", "name": "Syslog from 192.0.2.51", "key_": "logrt[...]", "value_type": "2"}
        mock_zbx._call.side_effect = [
            [{"eventid": "112212", "name": "Syslog: OSPF neighbor down on Gi0/1", "severity": "4",
              "clock": str(REAL_ALERT_CLOCK), "objectid": "5", "opdata": "", "tags": []}],
            [{"description": "OSPF neighbor down on Gi0/1", "expression": "{53001}=1",
              "hosts": [{"hostid": "10684", "host": "V2-vios"}], "items": [log_item]}],
            [],
            [],
        ]
        mock_zbx.get_active_problems.return_value = []
        mock_zbx.list_items.return_value = [log_item]
        mock_zbx.get_history.return_value = history
        return mock_zbx

    @mock.patch("netops_ai.api.pipeline.ZabbixClient")
    def test_backlog_line_outside_fault_window_is_dropped(self, mock_zbx_cls):
        self._ctx(mock_zbx_cls, history=[
            {"clock": REAL_ALERT_CLOCK, "value": STALE_LINE},
            {"clock": REAL_ALERT_CLOCK, "value": FRESH_LINE},
        ])

        text, *_ = pipeline.fetch_zabbix_context("112212")

        self.assertIn(FRESH_LINE, text)
        self.assertNotIn(STALE_LINE, text)

    @mock.patch("netops_ai.api.pipeline.ZabbixClient")
    def test_all_lines_stale_leaves_an_honest_note_not_an_empty_section(self, mock_zbx_cls):
        self._ctx(mock_zbx_cls, history=[{"clock": REAL_ALERT_CLOCK, "value": STALE_LINE}])

        text, *_ = pipeline.fetch_zabbix_context("112212")

        self.assertNotIn(STALE_LINE, text)
        self.assertIn("补发的旧消息", text)

    @mock.patch("netops_ai.api.pipeline.ZabbixClient")
    def test_unparseable_line_is_kept_not_treated_as_suspicious(self, mock_zbx_cls):
        """解析不出行内时间不代表这行可疑，只是格式不认识——照旧保留，不能因为读不出来就丢。"""
        self._ctx(mock_zbx_cls, history=[{"clock": REAL_ALERT_CLOCK, "value": UNRECOGNIZED_LINE}])

        text, *_ = pipeline.fetch_zabbix_context("112212")

        self.assertIn(UNRECOGNIZED_LINE, text)


if __name__ == "__main__":
    unittest.main()

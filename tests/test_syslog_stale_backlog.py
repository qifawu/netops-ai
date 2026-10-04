"""一条真实告警 的 RCA：设备到日志采集之间的传输一断一续，补发的旧行会跟补发抵达
那一刻共用 Zabbix 的同一个 `clock`。修之前，`cmd_syslog` 和 pipeline 的「第一信源」
预取都把 `clock` 当成「这行日志描述的事几时发生」，把 8 小时前的积压消息标成了
「故障窗口内」的证据，模型据此编出一条技术上不成立的跨协议因果（BGP shutdown
「导致」OSPF 掉线）。

`parse_line_receive_time` 从日志行自带的接收时间前缀（`Sep 26 15:58:47 <ip> 186:`）
解析出行内真实时间；两处消费方都改成按它判断，不再只信 Zabbix 的 `clock`。
"""

from __future__ import annotations

import argparse
import datetime
import unittest
from contextlib import contextmanager
from unittest import mock

from netops_ai.zabbix import cli
from netops_ai.zabbix.client import parse_line_receive_time

REAL_ALERT_CLOCK = int(datetime.datetime(2026, 9, 27, 0, 0, 2, tzinfo=datetime.timezone.utc).timestamp())
STALE_LINE = "Sep 26 15:58:47 192.0.2.51 186: *Sep 26 15:43:35.728: %BGP-5-ADJCHANGE: neighbor 10.0.0.2 Down Admin. shutdown"
FRESH_LINE = "Sep 27 00:00:02 192.0.2.51 999: *Sep 27 00:00:02.000: %OSPF-5-ADJCHG: Process 1, Nbr 1.1.1.1 on GigabitEthernet0/1 from FULL to DOWN, Neighbor Down: Dead timer expired"


class TestParseLineReceiveTime(unittest.TestCase):
    def test_reads_real_time_off_the_receive_prefix(self):
        got = parse_line_receive_time(STALE_LINE, REAL_ALERT_CLOCK)
        self.assertEqual(got, int(datetime.datetime(2026, 9, 26, 15, 58, 47, tzinfo=datetime.timezone.utc).timestamp()))

    def test_year_boundary_is_guessed_correctly(self):
        near = int(datetime.datetime(2026, 12, 31, 23, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
        got = parse_line_receive_time("Jan 1 00:05:00 host 1: whatever", near)
        self.assertEqual(datetime.datetime.fromtimestamp(got, tz=datetime.timezone.utc).year, 2027)

    def test_unrecognized_formats_return_none_not_a_guess(self):
        # SNMP trap 转发格式，没有这个前缀，不该被当成「补发的旧行」处理。
        trap = "20260926.171505 PDU INFO: hostname=<UNKNOWN> source=192.0.2.51"
        self.assertIsNone(parse_line_receive_time(trap, REAL_ALERT_CLOCK))
        self.assertIsNone(parse_line_receive_time("", REAL_ALERT_CLOCK))


def _args(**kw):
    base = dict(host="V2-vios", since="-3d", until="", include="", limit=200, dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


class _FakeZbx:
    def __init__(self, log_items, history_by_item):
        self._log_items, self._history_by_item = log_items, history_by_item

    def list_hosts(self, **_):
        return [{"hostid": "10684", "host": "V2-vios", "name": "V2"}]

    def list_items(self, _host_id, **_kw):
        return self._log_items

    def get_history(self, item_id, **_kw):
        return self._history_by_item.get(item_id, [])


@contextmanager
def _patched(fake):
    @contextmanager
    def _client():
        yield fake

    with mock.patch.object(cli, "_client", _client):
        yield


class TestCmdSyslogUsesLineTimeNotClock(unittest.TestCase):
    """复现 一条真实告警：一批补发的旧行跟一条真正实时的行，全部共用同一个 `clock`
 （补发抵达/真正写入 Zabbix 的那一刻）。
    """

    def _fake(self):
        log_items = [{"itemid": "53001", "name": "Syslog from 192.0.2.51", "key_": "logrt[...]", "value_type": "2"}]
        history = [
            {"clock": REAL_ALERT_CLOCK, "value": STALE_LINE},
            {"clock": REAL_ALERT_CLOCK, "value": FRESH_LINE},
        ]
        return _FakeZbx(log_items, {"53001": history})

    def test_stale_backlog_line_is_flagged_and_sorted_by_real_time(self):
        with _patched(self._fake()):
            out = cli.cmd_syslog(_args())
        # 两行都还在——修的是「怎么标注/排序」，不是把补发的旧行悄悄丢掉。
        self.assertEqual(out["returned"], 2)
        lines = out["lines"]
        self.assertEqual(lines[0]["line"], STALE_LINE)   # line_time 更早的排前面
        self.assertEqual(lines[1]["line"], FRESH_LINE)
        # 补发的那行：line_time 跟 clock 差出去一大截；真实事件那行：line_time 就是 clock。
        self.assertLess(lines[0]["line_time"], lines[0]["clock"] - 3600)
        self.assertEqual(lines[1]["line_time"], lines[1]["clock"])
        # 全局标出「有几行是补发的」，中性事实，不替模型下结论。
        self.assertEqual(out["stale_lines"], 1)
        self.assertIn("补发", out["stale_note"])

    def test_no_stale_lines_means_no_stale_note(self):
        fresh_only = _FakeZbx(
            [{"itemid": "1", "name": "x", "key_": "logrt[...]", "value_type": "2"}],
            {"1": [{"clock": REAL_ALERT_CLOCK, "value": FRESH_LINE}]},
        )
        with _patched(fresh_only):
            out = cli.cmd_syslog(_args())
        self.assertNotIn("stale_lines", out)
        self.assertNotIn("stale_note", out)


if __name__ == "__main__":
    unittest.main()

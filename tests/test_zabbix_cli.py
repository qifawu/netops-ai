"""zbx-cli 里那些「成功但没用」的返回。

维护者 「所以我听下来 工具层也需要做好对策」。

循环层的闸是下游止损，**真正引发打转的是工具骗了模型**：
`zbx_top_talkers` 返回「成功 + 全零榜单」，模型拿到一份看着合法的空数据，
去挨个 `zbx_items` 核对是理性行为——Win 那次就这么烧掉 12 步。
"""

from __future__ import annotations

import argparse
import json
import unittest
from contextlib import contextmanager
from unittest import mock

from netops_ai.zabbix import cli


def _args(**kw):
    base = dict(window="1h", key="net.if.in", limit=100, top=5, dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


class _FakeZbx:
    """只实现 top_talkers 用到的那几个方法。"""

    def __init__(self, items, trends=None, history=None):
        self._items, self._trends, self._history = items, trends or [], history or []
        self.history_calls = 0

    def list_hosts(self, **_):
        return [{"hostid": "1", "host": "V1-vios", "name": "V1"},
                {"hostid": "10084", "host": "Zabbix server", "name": "Zabbix server"}]

    def list_items(self, _host_id, **_kw):
        return self._items

    def get_trends(self, *_a, **_kw):
        return self._trends

    def get_history(self, *_a, **_kw):
        self.history_calls += 1
        return self._history


@contextmanager
def _patched(fake):
    @contextmanager
    def _client():
        yield fake

    with mock.patch.object(cli, "_client", _client):
        yield


class Test只留真正的流量项(unittest.TestCase):
    """**原来这条过滤只写死了 `net.if.in`。** 问出向流量时，
    `limit=100` 会被一堆 discard/error 计数项占满，真流量项挤不进来。"""

    ITEMS = [
        {"itemid": "1", "name": "Bits received", "key_": "net.if.out[ifHCOutOctets.3]", "value_type": "3"},
        {"itemid": "2", "name": "Out discards", "key_": "net.if.out.discards[ifOutDiscards.3]", "value_type": "3"},
        {"itemid": "3", "name": "Out errors", "key_": "net.if.out.errors[ifOutErrors.3]", "value_type": "3"},
    ]

    def test_出向也过滤掉discards和errors(self):
        fake = _FakeZbx(self.ITEMS, trends=[{"value_avg": "100", "value_max": "200"}])
        with _patched(fake):
            out = cli.cmd_top_talkers(_args(key="net.if.out"))
        keys = [r["key_"] for r in out["top"]]
        self.assertEqual(keys, ["net.if.out[ifHCOutOctets.3]"])
        self.assertEqual(out["scanned_items"], 1)

    def test_不统计Zabbix服务器自己的接口(self):
        fake = _FakeZbx(self.ITEMS, trends=[{"value_avg": "100", "value_max": "200"}])
        with _patched(fake):
            out = cli.cmd_top_talkers(_args(key="net.if.out"))
        self.assertEqual({r["host"] for r in out["top"]}, {"V1-vios"})


class Testtrends没数就回落history(unittest.TestCase):
    """Zabbix 的 trend 整点才落库。问「过去 1 小时谁流量最大」而这一小时
    还没走完时 trends 是空的，整张榜全零。history 是实时写的。"""

    ITEMS = [{"itemid": "1", "name": "Bits received", "key_": "net.if.in[ifHCInOctets.3]", "value_type": "3"}]

    def test_trends为空时用history算(self):
        fake = _FakeZbx(self.ITEMS, trends=[],
                        history=[{"value": "100"}, {"value": "300"}])
        with _patched(fake):
            out = cli.cmd_top_talkers(_args())
        self.assertEqual(fake.history_calls, 1)
        row = out["top"][0]
        self.assertEqual(row["source"], "history")
        self.assertEqual(row["max"], 300.0)
        self.assertEqual(row["max_avg"], 200.0)
        self.assertNotIn("empty", out)

    def test_trends有数就不去查history(self):
        fake = _FakeZbx(self.ITEMS, trends=[{"value_avg": "10", "value_max": "20"}])
        with _patched(fake):
            out = cli.cmd_top_talkers(_args())
        self.assertEqual(fake.history_calls, 0)
        self.assertEqual(out["top"][0]["source"], "trends")


class Test全零榜单自己承认是空的(unittest.TestCase):
    """结构上 `top` 非空，通用层的 `_tool_reply` 判不出来——只能工具自己说。"""

    ITEMS = [{"itemid": str(i), "name": f"if{i}", "key_": f"net.if.in[x{i}]", "value_type": "3"} for i in range(3)]

    def test_全零时标empty并说清扫了什么(self):
        """只陈述事实（扫了几个、窗口多大、全是 0），不再在返回里教它下一步。
 防「全零之后挨个翻 itemid」靠的是 empty=true 触发的无进展闸，不是这句话。
        """
        fake = _FakeZbx(self.ITEMS, trends=[], history=[])
        with _patched(fake):
            out = cli.cmd_top_talkers(_args())
        self.assertTrue(out["empty"])
        self.assertIn("所有值都是 0", out["note"])
        self.assertIn(f"扫了 {out['scanned_items']} 个", out["note"])
        self.assertNotIn("try_instead", out)

    def test_有数的时候不标empty(self):
        fake = _FakeZbx(self.ITEMS, trends=[{"value_avg": "0", "value_max": "5"}])
        with _patched(fake):
            out = cli.cmd_top_talkers(_args())
        self.assertNotIn("empty", out)


class _EmptyZbx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list_hosts(self, **_):
        return [{"hostid": "1", "host": "V1-vios", "name": "V1"},
                {"hostid": "10084", "host": "Zabbix server", "name": "Zabbix server"}]

    def list_items(self, *_a, **_kw):
        return [{"itemid": "52907", "name": "ICMP ping", "key_": "icmpping", "value_type": "3"}]

    def get_item_value_type(self, _item_id):
        return 3

    def get_history(self, *_a, **_kw):
        return []

    def get_trends(self, *_a, **_kw):
        return []


class _SeriesZbx(_EmptyZbx):
    def __init__(self, *, history=None, trends=None):
        self._history = history or []
        self._trends = trends or []

    def get_history(self, *_a, **_kw):
        return self._history

    def get_trends(self, *_a, **_kw):
        return self._trends


class Test空结果自己承认是空的(unittest.TestCase):
    """这些返回结构上都是对象，通用 `_tool_reply` 看不出里面的列表为空。

 空结果**只说「没有数据」并回显查询条件**，不再带 try_instead（维护者：工具保持中性）。
    """

    def _assert_neutral_empty(self, out, *echoed):
        self.assertTrue(out["empty"])
        self.assertIn("没有数据", out["note"])
        for text in echoed:
            self.assertIn(text, out["note"])
        self.assertNotIn("try_instead", out)

    def test_hosts空查询标empty并回显查询(self):
        with _patched(_EmptyZbx()):
            out = cli.cmd_hosts(argparse.Namespace(query="NO_SUCH", limit=50, dry_run=False))
        self._assert_neutral_empty(out, "NO_SUCH")

    def test_items空key标empty并回显查询(self):
        with _patched(_EmptyZbx()):
            out = cli.cmd_items(argparse.Namespace(host="V1", key="net.if", limit=50, dry_run=False))
        self._assert_neutral_empty(out, "V1-vios", "net.if")

    def test_history空点标empty并回显查询(self):
        with _patched(_EmptyZbx()):
            out = cli.cmd_history(argparse.Namespace(item_id="52907", since="-1h", limit=100, dry_run=False))
        self.assertEqual(out["points"], [])
        self._assert_neutral_empty(out, "52907", "-1h", str(out["from"]))

    def test_trends空点标empty并回显查询(self):
        with _patched(_EmptyZbx()):
            out = cli.cmd_trends(argparse.Namespace(item_id="52907", since="-30d", limit=720, dry_run=False))
        self.assertEqual(out["points"], 0)
        self.assertEqual(out["series"], [])
        self._assert_neutral_empty(out, "52907", "-30d")

    def test_chart空序列标empty并回显查询(self):
        with _patched(_EmptyZbx()):
            out = cli.cmd_chart(argparse.Namespace(item_id="52907", since="-1h", limit=336, dry_run=False))
        self.assertEqual(out["series"], [])
        self._assert_neutral_empty(out, "52907", "没有可画的数据")

    def test_syslog空窗口回显的是真实的起止时间(self):
        """原文：「1790391000 到现在窗口内没有匹配的行」——传了 until 也说「到现在」。"""
        class _LogItemZbx(_EmptyZbx):
            def list_items(self, *_a, **_kw):
                return [{"itemid": "60001", "name": "syslog", "key_": "log[syslog]", "value_type": "2"}]

        with _patched(_LogItemZbx()):
            out = cli.cmd_syslog(argparse.Namespace(host="V1", since="1790391000", until="1790391900",
                                                    include="", limit=200, dry_run=False))
        self._assert_neutral_empty(out, "1790391000~1790391900")
        self.assertNotIn("到现在", out["note"])


class Test长序列摘要和降采样(unittest.TestCase):
    def test_trends摘要在前且series降采样(self):
        rows = [
            {"clock": str(1000 + i), "value_min": str(i), "value_avg": str(i + 0.5), "value_max": str(i + 1)}
            for i in range(200)
        ]
        with _patched(_SeriesZbx(trends=rows)):
            out = cli.cmd_trends(argparse.Namespace(item_id="52907", since="-30d", limit=720, dry_run=False))

        self.assertEqual(out["points"], 200)
        self.assertEqual(out["min"], 0.0)
        self.assertEqual(out["max"], 200.0)
        self.assertEqual(out["first_at"], 1000)
        self.assertEqual(out["last_at"], 1199)
        self.assertEqual(out["downsample"]["original_points"], 200)
        self.assertLessEqual(len(out["series"]), 120)

    def test_chart摘要字段在截断前仍可见(self):
        rows = [{"clock": str(1000 + i), "value": str(i)} for i in range(200)]
        with _patched(_SeriesZbx(history=rows)):
            out = cli.cmd_chart(argparse.Namespace(item_id="52907", since="-1h", limit=336, dry_run=False))

        text = json.dumps(out, ensure_ascii=False)
        head = text[: text.index('"series"')]
        for key in ('"points"', '"min"', '"avg"', '"max"', '"first_at"', '"last_at"', '"downsample"'):
            self.assertIn(key, head)
        self.assertEqual(out["downsample"]["original_points"], 200)
        self.assertLessEqual(len(out["series"]), 120)


if __name__ == "__main__":
    unittest.main()

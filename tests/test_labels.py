from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from netops_ai.analysis.schema import HYPOTHESIS_CATEGORIES
from netops_ai.feishu.card import build_card
from netops_ai.labels import LABELS, humanize_text, label_for
try:
    from netops_ai.netbox_cli import COMMANDS as NETBOX_COMMANDS
except ImportError:  # 开源版没有 NetBox 台账工具
    from netops_ai.topology_cli import COMMANDS as NETBOX_COMMANDS
from netops_ai.zabbix.cli import COMMANDS as ZABBIX_COMMANDS


class LabelCoverageTests(unittest.TestCase):
    def test_schema每个根因分类都有中文(self):
        self.assertTrue(set(HYPOTHESIS_CATEGORIES).issubset(LABELS))
        for code in HYPOTHESIS_CATEGORIES:
            self.assertNotEqual(label_for(code), code)

    def test巡检来源和只读工具都有中文(self):
        for code in ("trend", "periodic_spike", "self_healing_flap", "ai_self", "sop"):
            self.assertNotEqual(label_for(code), code)
        for code in (
            "device_show",
            "device_show_many",
            "sop_lookup",
            *(f"zbx_{spec.name.replace('-', '_')}" for spec in ZABBIX_COMMANDS),
            *(spec.tool_name for spec in NETBOX_COMMANDS),
        ):
            with self.subTest(code=code):
                self.assertNotEqual(label_for(code), code)

    def test自由文本替换且不误伤更长标识符(self):
        self.assertEqual(
            humanize_text("remote_or_upstream 和 link_or_path_quality"),
            "对端或上游的问题 和 链路或路径质量问题",
        )
        self.assertEqual(humanize_text("xremote_or_upstreamy not_remote_or_upstream"), "xremote_or_upstreamy not_remote_or_upstream")




class Test不许改坏路径和标识符(unittest.TestCase):
    """真实事故：一张飞书卡上出现 `/var/log/监控/zabbix_server.log`。

 网络工程师照着这条路径敲，拿到的是 no such file。原因是原来的词边界只排除
 `[A-Za-z0-9_]`，`/` 算边界，路径中段的 `zabbix` 就被换成了「监控」
 （`zabbix_server` 因为后面跟着 `_` 反而躲过一劫，所以只坏了一半，更难发现）。
    """

    def test_路径原样保留(self):
        raw = "检查 /var/log/zabbix/zabbix_server.log 里有没有 timeout"
        self.assertEqual(humanize_text(raw), raw)

    def test_URL原样保留(self):
        raw = "看 https://zabbix.example.com/api_jsonrpc.php 的返回"
        self.assertEqual(humanize_text(raw), raw)

    def test_带连字符的标识符原样保留(self):
        raw = "zabbix-server 和 zabbix-agent2 两个进程"
        self.assertEqual(humanize_text(raw), raw)

    def test_句子里独立出现的还是要换(self):
        """修边界不能把该换的也一起放过。"""
        self.assertEqual(humanize_text("这是 zabbix 侧的数据"), "这是 监控 侧的数据")

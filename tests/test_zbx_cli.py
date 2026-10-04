from __future__ import annotations

import json
import unittest
from unittest import mock

import argparse

from netops_ai.zabbix import cli
from netops_ai.zabbix import cli as zbx_cli


class TestZbxCliSchema(unittest.TestCase):
    def test_tools_schema_matches_command_specs(self):
        payload = cli.tools_payload()
        by_name = {c["name"]: c for c in payload["commands"]}
        self.assertEqual(set(by_name), {s.name for s in cli.COMMANDS})
        for spec in cli.COMMANDS:
            schema = by_name[spec.name]["parameters"]
            self.assertEqual(set(schema["properties"]), {p.name for p in spec.params})
            self.assertEqual(set(schema["required"]), {p.name for p in spec.params if p.required})

    def test_schema_subcommand_returns_one_command(self):
        with mock.patch("builtins.print") as out:
            code = cli.main(["schema", "history"])
        self.assertEqual(code, 0)
        data = json.loads(out.call_args.args[0])
        self.assertEqual(data["name"], "history")
        self.assertIn("item_id", data["parameters"]["required"])

    def test_history_help_mentions_trends_difference(self):
        parser = cli.build_parser()
        with self.assertRaises(SystemExit), mock.patch("sys.stdout") as stdout:
            parser.parse_args(["history", "--help"])
        text = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn("history", text)
        self.assertIn("trends", text)
        self.assertIn("housekeeper", text)


class TestZbxCliCommands(unittest.TestCase):
    def test_parse_relative_time(self):
        self.assertEqual(cli.parse_time("-1h", now=10000), 6400)
        self.assertEqual(cli.parse_time("-7d", now=700000), 95200)

    def test_dry_run_does_not_open_client(self):
        with mock.patch("netops_ai.zabbix.cli._client") as make_client, mock.patch("builtins.print") as out:
            code = cli.main(["hosts", "--dry-run"])
        self.assertEqual(code, 0)
        make_client.assert_not_called()
        data = json.loads(out.call_args.args[0])
        self.assertEqual(data["dry_run"][0]["method"], "host.get")

    def test_hosts_uses_zabbix_client(self):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.list_hosts.return_value = [
            {"hostid": "1", "host": "V1", "name": "V1"},
            {"hostid": "2", "host": "Zabbix server", "name": "Zabbix server"},
        ]
        with mock.patch("netops_ai.zabbix.cli._client", return_value=fake), mock.patch("builtins.print") as out:
            code = cli.main(["hosts", "--query", "V1"])
        self.assertEqual(code, 0)
        data = json.loads(out.call_args.args[0])
        self.assertEqual(data["hosts"][0]["host"], "V1")
        fake.list_hosts.assert_called_once()

    def test_denied_exit_code(self):
        with mock.patch("netops_ai.zabbix.cli._client", side_effect=PermissionError("拒绝")):
            code = cli.main(["hosts"])
        self.assertEqual(code, cli.EXIT_DENIED)




class TestHistoryUsesTheRightValueTypeTable(unittest.TestCase):
    """**Zabbix 最阴的一个坑**：`history.get` 的 `history` 参数传错了不报错，
    只安静返回空列表。

    以前 `cmd_history`/`cmd_chart` 一律用默认的 0（float），而接口流量
    （`net.if.in` 这类计数器）在 SNMP 模板里是 unsigned=3——
    **"查一下这个口的流量历史"永远返回空，而且不报错**，
    看起来像"这个口没数据"，其实是问错了表。
    """

    class _Zbx:
        def __init__(self, value_type):
            self.value_type = value_type
            self.asked_history_type = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_item_value_type(self, item_id):
            return self.value_type

        def get_history(self, item_id, *, history_type=0, **kw):
            self.asked_history_type = history_type
            return [{"clock": "100", "value": "42"}]

        def get_trends(self, item_id, **kw):
            return []

    def _run(self, cmd, value_type, **kwargs):
        from unittest import mock

        zbx = self._Zbx(value_type)
        args = argparse.Namespace(item_id="52907", since="-1h", limit=100, dry_run=False, **kwargs)
        with mock.patch.object(zbx_cli, "_client", lambda: zbx):
            out = getattr(zbx_cli, cmd)(args)
        return zbx, out

    def test_unsigned_item_queries_the_unsigned_table(self):
        zbx, out = self._run("cmd_history", 3)
        self.assertEqual(zbx.asked_history_type, 3, "接口计数器是 unsigned=3，用 0 查会返回空")
        self.assertEqual(out["value_type"], 3)

    def test_float_item_queries_the_float_table(self):
        zbx, _ = self._run("cmd_history", 0)
        self.assertEqual(zbx.asked_history_type, 0)

    def test_text_item_queries_the_text_table(self):
        zbx, _ = self._run("cmd_history", 4)
        self.assertEqual(zbx.asked_history_type, 4)

    def test_unknown_value_type_falls_back_to_float(self):
        zbx, _ = self._run("cmd_history", None)
        self.assertEqual(zbx.asked_history_type, 0, "查不到 value_type 就退回 float，不要炸")

    def test_result_says_which_table_it_used(self):
        """空结果时，这是唯一能区分「真没数据」和「问错表」的线索。"""
        _, out = self._run("cmd_history", 3)
        self.assertIn("value_type", out)

    def test_chart_short_window_also_respects_value_type(self):
        zbx, _ = self._run("cmd_chart", 3)
        self.assertEqual(zbx.asked_history_type, 3, "chart 走 history 那条分支时同样会踩这个坑")


if __name__ == "__main__":
    unittest.main()


class TestW36bUnixSecondsAcceptIntOrString(unittest.TestCase):
    """验证 外部 agent 反馈第 2 条：SOP 渲染成 `since=1790395561`，外部 agent 照字面传整数，schema 只收字符串，ValidationError 白跑一次。"""

    def test_time_and_id_params_accept_integer_and_string(self):
        from netops_ai.graph import chat_agent

        trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")
        tools = {t.name: t for t in chat_agent.build_zabbix_tools(trace)}
        for tool_name, args in {
            "zbx_syslog": {"host": "A1", "since": 1790395561, "until": 1790396761},
            "zbx_history": {"item_id": 54623, "since": 1790395561},
            "zbx_trends": {"item_id": 54623, "since": "-7d"},
            "zbx_chart": {"item_id": "54623", "since": 1790395561},
        }.items():
            with self.subTest(tool=tool_name):
                parsed = tools[tool_name].args_schema.model_validate(args)
                for key, value in args.items():
                    if key != "host":
                        self.assertEqual(getattr(parsed, key), str(value))  # handler 那侧照旧拿到字符串

    def test_handler_gets_string_and_empty_note_echoes_window(self):
        from netops_ai.graph import chat_agent

        trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")
        tool = next(t for t in chat_agent.build_zabbix_tools(trace) if t.name == "zbx_syslog")
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.list_hosts.return_value = [{"hostid": "1", "host": "A1-viosl2", "name": "A1"}]
        fake.list_items.return_value = [{"itemid": "9", "name": "syslog", "key_": "log", "value_type": "2"}]
        fake.get_history.return_value = []
        with mock.patch("netops_ai.zabbix.cli._client", return_value=fake):
            data = json.loads(tool.invoke({"host": "A1", "since": 1790395561, "until": 1790396761}))
        self.assertTrue(data["empty"])
        self.assertIn("1790395561~1790396761", data["note"])

    def test_parse_time_accepts_int(self):
        self.assertEqual(cli.parse_time(1790395561, now=0), 1790395561)
        self.assertEqual(cli.parse_time(" 1790395561 ", now=0), 1790395561)

    def test_cli_flags_stay_strings(self):
        args = cli.build_parser().parse_args(["syslog", "--host", "A1", "--from", "1790395561", "--to=-5m"])
        self.assertEqual((args.since, args.until), ("1790395561", "-5m"))

    def test_schema_advertises_both_types(self):
        spec = next(s for s in cli.COMMANDS if s.name == "syslog")
        props = cli.command_schema(spec)["parameters"]["properties"]
        self.assertEqual(props["since"]["type"], ["integer", "string"])
        self.assertIn("整数或数字字符串", props["since"]["description"])


class TestW36bProblemsTableSaysItsTimezone(unittest.TestCase):
    """验证 外部 agent 反馈第 6 条：表里是跑进程那台机器的本地时间，原来表头没写。"""

    def test_header_carries_utc_offset(self):
        md = cli._problems_markdown([{"eventid": "1", "severity": "2", "name": "x", "clock": "1790396461"}], None)
        self.assertRegex(md, r"开始时间（UTC[+-]\d\d:\d\d）")

"""webhook 收到事件之后跑的那条后台链路。全部用假 Zabbix/设备/LLM/agent-loop
客户端，不碰真实网络——真实链路的验证是各轮任务本身要做的事（跑一次真告警），
不该混进日常单测。

**N/M 段集成之后的新结构**：`process_alert` 只负责拉这条告警
自己的 Zabbix 上下文、注册进按 hostid 分的 incident 窗口（第一条立即发回执，
后续缓冲）。真正的取数、分析、CASE 库落地、发结论卡在窗口关闭时统一做
（`_flush_incident_window` -> `_process_incident_batch`）。测试直接构造
`_PendingAlert` 调 `_process_incident_batch`，不去真的等 `threading.Timer`。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from netops_ai.analysis.analyzer import AnalysisRun
from netops_ai.llm.client import LLMError, LLMResponse
from netops_ai.zabbix.client import ZabbixAPIError

import netops_ai.api.pipeline as pipeline


def _good_analysis(root_cause="x", *, eventids=("999",), roles=None, groups=None):
    ce = [
        {
            "claim": "c",
            "source": "some text",
            "source_from": "zabbix",
            "contradiction": "direct",
            "time_relevance": "fault_window",
        }
    ]
    checklist = {
        "local_action": {"status": "ruled_out", "reason": "r", "counter_evidence": ce},
        "local_hardware_or_resource": {"status": "ruled_out", "reason": "r", "counter_evidence": ce},
        "remote_or_upstream": {"status": "ruled_out", "reason": "r", "counter_evidence": ce},
        "link_or_path_quality": {"status": "ruled_out", "reason": "r", "counter_evidence": ce},
        "monitoring_or_collection_artifact": {"status": "ruled_out", "reason": "r", "counter_evidence": ce},
    }
    roles = roles or [
        {"eventid": eid, "role": "root" if i == 0 else "independent", "reason": "r", "caused_by_eventid": ""}
        for i, eid in enumerate(eventids)
    ]
    groups = groups if groups is not None else [{"events": list(eventids), "why_same": "同一次故障"}]
    parsed = {
        "root_cause": root_cause,
        "confidence": "high",
        "evidence": [{"claim": "c", "source": "some text", "source_from": "zabbix"}],
        "ruled_out": [],
        "alert_roles": roles,
        "grouping": groups,
        "hypothesis_checklist": checklist,
        "undistinguishable_candidates": [],
    }
    return parsed


def _pending(eventid="999", *, hostid="10683", clock=1700000000, triggerid="38369", interfaces=None, name="test alert"):
    return pipeline._PendingAlert(
        eventid=eventid,
        payload={"eventid": eventid, "name": name},
        zabbix_text=f"zabbix 上下文文本 for {eventid}",
        hostid=hostid,
        clock=clock,
        triggerid=triggerid,
        tags=[],
        interfaces=list(interfaces or []),
        name=name,
        zbx_err="",
        started_at="2026-09-19T00:00:00+00:00",
        device_host="192.0.2.50",
    )


class TestProcessIncidentBatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._records_patch = mock.patch.object(pipeline, "RECORDS_DIR", Path(self.tmp.name))
        self._records_patch.start()
        self.addCleanup(self._records_patch.stop)
        self._db_patch = mock.patch(
            "netops_ai.api.pipeline.save_incident",
            side_effect=lambda incident, interfaces=None: incident,
        )
        # 真实落 incidents.db 到临时目录，不用假的存储层——只读查询函数照跑
        import netops_ai.incident as incident_mod

        self._incident_db_patch = mock.patch.object(
            incident_mod, "DEFAULT_DB_PATH", Path(self.tmp.name) / "incidents.db"
        )
        self._incident_db_patch.start()
        self.addCleanup(self._incident_db_patch.stop)

    @mock.patch("netops_ai.api.pipeline.report_alert_group")
    @mock.patch("netops_ai.api.pipeline.fetch_diagnostic_context")
    def test_单条告警全部成功走完整流程并存档(self, mock_diag, mock_report):
        mock_diag.return_value = pipeline.DiagnosticContextResult(
            text="设备输出文本",
            device_records=[{"command": "show version", "ok": True}],
            mode="sop",
            trace=[{"source": "sop"}],
        )
        mock_diag.return_value.analysis = _good_analysis()
        mock_diag.return_value.transcript = "some text 轨迹原文"
        mock_report.return_value = {"attempted": True, "sent": True, "error": "", "transport": "app"}

        result = pipeline._process_incident_batch("10683", [_pending("999")])

        self.assertEqual(result["eventids"], ["999"])
        self.assertEqual(result["groups"], 1)

        out_file = Path(self.tmp.name) / "alert-999.json"
        self.assertTrue(out_file.exists())
        saved = json.loads(out_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["eventid"], "999")
        self.assertEqual(saved["analysis_parsed"]["root_cause"], "x")
        self.assertEqual(saved["business_rule_violations"], [])
        # 证据逐字核对不在线上跑：不再产出核对结果，卡片也就没有「需核实」警告。
        self.assertIsNone(saved["evidence_verification"])
        self.assertTrue(saved["incident_id"])
        self.assertEqual(saved["incident_role"], "root")
        self.assertEqual(saved["status"], "done")
        self.assertEqual(saved["sop_usage"]["adherence"], "not_matched")
        self.assertEqual(result["headline"], "x")  # A16 follow-up 提示用的结论摘要

    @mock.patch("netops_ai.api.pipeline.report_alert_group")
    @mock.patch("netops_ai.api.pipeline.fetch_diagnostic_context")
    def test_followup_hint会拼进喂给agent的zabbix_text里(self, mock_diag, mock_report):
        """A16：follow-up 批要带上上一批的结论，不是另起一次不知情的盲跑。"""
        mock_diag.return_value = pipeline.DiagnosticContextResult(
            text="设备输出文本", device_records=[], mode="sop", trace=[],
        )
        mock_diag.return_value.analysis = _good_analysis()
        mock_diag.return_value.transcript = "some text 轨迹原文"
        mock_report.return_value = {"attempted": True, "sent": True, "error": "", "transport": "app"}
        hint = "**协同提示（A16 follow-up）**：同一台设备刚分析完，结论是：接口被人为 shutdown。"

        pipeline._process_incident_batch("10683", [_pending("999")], followup_hint=hint)

        self.assertIn(hint, mock_diag.call_args.kwargs["zabbix_text"])

    @mock.patch("netops_ai.api.pipeline.report_alert_group")
    @mock.patch("netops_ai.api.pipeline.fetch_diagnostic_context")
    def test_多条候选按grouping拆成一个incident两条记录都写(self, mock_diag, mock_report):
        mock_diag.return_value = pipeline.DiagnosticContextResult(text="设备文本")
        mock_diag.return_value.transcript = "some text 轨迹原文"
        mock_diag.return_value.analysis = _good_analysis(
            eventids=("101", "102"),
            roles=[
                {"eventid": "101", "role": "root", "reason": "接口先断", "caused_by_eventid": ""},
                {"eventid": "102", "role": "consequence", "reason": "邻居跟着断", "caused_by_eventid": "101"},
            ],
            groups=[{"events": ["101", "102"], "why_same": "同一条链路引发"}],
        )
        mock_report.return_value = {"attempted": True, "sent": True, "error": "", "transport": "app"}

        result = pipeline._process_incident_batch(
            "10683", [_pending("101", interfaces=["Gi0/1"]), _pending("102", interfaces=[])]
        )

        self.assertEqual(sorted(result["eventids"]), ["101", "102"])
        self.assertEqual(result["groups"], 1)
        self.assertEqual(len(result["incident_ids"]), 1)

        rec101 = json.loads((Path(self.tmp.name) / "alert-101.json").read_text(encoding="utf-8"))
        rec102 = json.loads((Path(self.tmp.name) / "alert-102.json").read_text(encoding="utf-8"))
        self.assertEqual(rec101["incident_role"], "root")
        self.assertEqual(rec102["incident_role"], "consequence")
        self.assertEqual(rec101["incident_id"], rec102["incident_id"])
        self.assertEqual(rec101["incident_grouped_with"], ["102"])
        self.assertEqual(rec102["incident_grouped_with"], ["101"])
        # 只应该合成一张结论卡，不是两条告警各发各的
        mock_report.assert_called_once()

    @mock.patch("netops_ai.api.pipeline.report_alert_group")
    @mock.patch("netops_ai.api.pipeline.fetch_diagnostic_context")
    def test_llm调用失败原样记录不吞掉(self, mock_diag, mock_report):
        """取证轨迹末尾没拿到结构化结论时，错误要原样落盘，不能静默出一张空卡。"""
        mock_diag.return_value = pipeline.DiagnosticContextResult(
            error="HTTP 429：限流了", analysis=None
        )

        pipeline._process_incident_batch("10683", [_pending("1001")])

        saved = json.loads((Path(self.tmp.name) / "alert-1001.json").read_text(encoding="utf-8"))
        self.assertIn("限流了", saved["analysis_error"])
        self.assertEqual(saved["device_fetch_error"], "HTTP 429：限流了")
        self.assertEqual(saved["status"], "analysis_failed")
        mock_report.assert_not_called()

    @mock.patch("netops_ai.api.pipeline.report_alert_group")
    @mock.patch("netops_ai.api.pipeline.fetch_diagnostic_context")
    def test_abort_after_final_schema_trace_metadata_is_recorded(self, mock_diag, mock_report):
        mock_diag.return_value = pipeline.DiagnosticContextResult(
            text="设备文本",
            analysis=_good_analysis(),
            transcript="some text 轨迹原文",
            analysis_trace={
                "from": "agent_loop_final_schema_after_abort",
                "abort_reason": "工具调用预算已用完：max_tool_calls=12。",
            },
        )
        mock_report.return_value = {"attempted": True, "sent": True, "error": "", "transport": "app"}

        pipeline._process_incident_batch("10683", [_pending("1003")])

        saved = json.loads((Path(self.tmp.name) / "alert-1003.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "done")
        self.assertEqual(saved["analysis_trace"]["from"], "agent_loop_final_schema_after_abort")
        self.assertIn("max_tool_calls=12", saved["analysis_trace"]["abort_reason"])

    @mock.patch("netops_ai.api.pipeline.report_alert_group")
    @mock.patch("netops_ai.api.pipeline.fetch_diagnostic_context")
    def test_abort_and_final_schema_failure_error_is_recorded(self, mock_diag, mock_report):
        mock_diag.return_value = pipeline.DiagnosticContextResult(
            analysis=None,
            analysis_error=(
                "工具调用预算已用完：max_tool_calls=12。\n"
                "final_schema_after_abort failed: ValueError: schema parse failed"
            ),
        )

        pipeline._process_incident_batch("10683", [_pending("1004")])

        saved = json.loads((Path(self.tmp.name) / "alert-1004.json").read_text(encoding="utf-8"))
        self.assertIn("工具调用预算已用完", saved["analysis_error"])
        self.assertIn("final_schema_after_abort failed", saved["analysis_error"])
        self.assertEqual(saved["status"], "analysis_failed")
        mock_report.assert_not_called()


    @mock.patch("netops_ai.api.pipeline.report_alert_group")
    @mock.patch("netops_ai.api.pipeline.fetch_diagnostic_context")
    def test_没有grouping时每条各成一组而不是全算一组(self, mock_diag, mock_report):
        """模型没表态时**兜底方向是拆开，不是合并**。

        把不相关的告警硬合成一件事，比拆开危险得多——出来一张卡说五件事，
        人看了也不知道哪件是真的。拆开最坏是多发几张卡。
        """
        mock_diag.return_value = pipeline.DiagnosticContextResult(text="设备文本")
        run = _good_analysis()
        run["grouping"] = []
        mock_diag.return_value.analysis = run
        mock_diag.return_value.transcript = "some text 轨迹原文"
        mock_report.return_value = {"attempted": True, "sent": True, "error": "", "transport": "app"}

        result = pipeline._process_incident_batch(
            "10683", [_pending("2001"), _pending("2002"), _pending("2003")]
        )
        self.assertEqual(result["groups"], 3)


class TestIncidentWindowRegistration(unittest.TestCase):
    """`process_alert` 只测"注册进窗口"这一段的决策：第一条发回执、后续缓冲、
    不真的等 `threading.Timer` 触发（拿到 timer 之后立刻手动取消，改用
    `flush_incident_window_now` 显式触发，跟真实定时器解耦）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._records_patch = mock.patch.object(pipeline, "RECORDS_DIR", Path(self.tmp.name))
        self._records_patch.start()
        self.addCleanup(self._records_patch.stop)
        pipeline._INCIDENT_WINDOWS.clear()
        pipeline._ACTIVE_RUN_HOSTS.clear()
        pipeline._FOLLOWUP_QUEUE.clear()

    def tearDown(self):
        with pipeline._INCIDENT_WINDOWS_LOCK:
            for window in pipeline._INCIDENT_WINDOWS.values():
                if window.timer is not None:
                    window.timer.cancel()
            pipeline._INCIDENT_WINDOWS.clear()
            pipeline._ACTIVE_RUN_HOSTS.clear()
            pipeline._FOLLOWUP_QUEUE.clear()

    @mock.patch("netops_ai.api.pipeline.fetch_zabbix_context")
    def test_第一条告警注册窗口且不发任何卡(self, mock_zbx):
        """回执卡 砍掉：一次故障发两张卡，结论卡 80~270 秒就到。"""
        mock_zbx.return_value = ("zabbix 文本", "10683", 1700000000, "", {"triggerid": "1", "tags": [], "interfaces": []})

        result = pipeline.process_alert({"eventid": "3001", "name": "x"})

        self.assertEqual(result["status"], "buffered_for_window")
        self.assertIn("10683", pipeline._INCIDENT_WINDOWS)
        # 取消真实 timer，避免测试进程里遗留一个几分钟后触发的后台线程
        pipeline._INCIDENT_WINDOWS["10683"].timer.cancel()

    @mock.patch("netops_ai.api.pipeline.fetch_zabbix_context")
    def test_同主机第二条告警进同一个窗口(self, mock_zbx):
        mock_zbx.side_effect = [
            ("zabbix 文本 1", "10683", 1700000000, "", {"triggerid": "1", "tags": [], "interfaces": []}),
            ("zabbix 文本 2", "10683", 1700000010, "", {"triggerid": "2", "tags": [], "interfaces": []}),
        ]

        first = pipeline.process_alert({"eventid": "3002", "name": "x"})
        second = pipeline.process_alert({"eventid": "3003", "name": "y"})

        self.assertEqual(first["status"], "buffered_for_window")
        self.assertEqual(second["status"], "buffered_for_window")
        self.assertEqual(len(pipeline._INCIDENT_WINDOWS["10683"].pending), 1)
        pipeline._INCIDENT_WINDOWS["10683"].timer.cancel()

    @mock.patch("netops_ai.api.pipeline._process_incident_batch")
    @mock.patch("netops_ai.api.pipeline.fetch_zabbix_context")
    def test_手动flush调用process_incident_batch(self, mock_zbx, mock_batch):
        mock_zbx.return_value = ("zabbix 文本", "10683", 1700000000, "", {"triggerid": "1", "tags": [], "interfaces": []})
        mock_batch.return_value = {"eventids": ["3004"], "groups": 1}

        pipeline.process_alert({"eventid": "3004", "name": "x"})
        pipeline._INCIDENT_WINDOWS["10683"].timer.cancel()
        result = pipeline.flush_incident_window_now("10683")

        self.assertEqual(result, {"eventids": ["3004"], "groups": 1})
        mock_batch.assert_called_once()
        self.assertNotIn("10683", pipeline._INCIDENT_WINDOWS)

    @mock.patch("netops_ai.api.pipeline.fetch_zabbix_context")
    def test_主机正在跑时新告警排队不占用incident_windows(self, mock_zbx):
        """A16 follow-up：不是另起一次盲跑。"""
        mock_zbx.return_value = ("zabbix 文本", "10683", 1700000000, "", {"triggerid": "1", "tags": [], "interfaces": []})
        pipeline._ACTIVE_RUN_HOSTS.add("10683")

        result = pipeline.process_alert({"eventid": "3005", "name": "x"})

        self.assertEqual(result["status"], "queued_followup")
        self.assertNotIn("10683", pipeline._INCIDENT_WINDOWS)
        self.assertIn("10683", pipeline._FOLLOWUP_QUEUE)
        self.assertEqual(pipeline._FOLLOWUP_QUEUE["10683"].first_alert.eventid, "3005")

    @mock.patch("netops_ai.api.pipeline.fetch_zabbix_context")
    def test_同主机在活跃期间连来两条都进同一个follow_up队列(self, mock_zbx):
        mock_zbx.side_effect = [
            ("v1", "10683", 1700000000, "", {"triggerid": "1", "tags": [], "interfaces": []}),
            ("v2", "10683", 1700000010, "", {"triggerid": "2", "tags": [], "interfaces": []}),
        ]
        pipeline._ACTIVE_RUN_HOSTS.add("10683")

        pipeline.process_alert({"eventid": "3006", "name": "x"})
        pipeline.process_alert({"eventid": "3007", "name": "y"})

        self.assertEqual(len(pipeline._FOLLOWUP_QUEUE["10683"].pending), 1)

    def test_flush跑完自动带上结论提示处理排队的follow_up(self):
        """模拟：`_process_incident_batch` 跑第一批时，同主机第二条告警排进了
        follow-up 队列——flush 不能标完「没在跑」就走人，得接着处理排队的那批，
        而且要把第一批的结论当提示喂给第二批（不是另起一次盲跑）。
        """
        calls: list[dict] = []
        first = pipeline._PendingAlert(
            eventid="3008", payload={}, zabbix_text="v1", hostid="10683", clock=1700000000,
            triggerid="1", tags=[], interfaces=[], name="x", zbx_err="", started_at="now",
        )
        second = pipeline._PendingAlert(
            eventid="3009", payload={}, zabbix_text="v2", hostid="10683", clock=1700000010,
            triggerid="2", tags=[], interfaces=[], name="y", zbx_err="", started_at="now",
        )
        pipeline._INCIDENT_WINDOWS["10683"] = pipeline._IncidentWindow(first_alert=first)

        def fake_process(host_key: str, alerts: list, *, followup_hint: str = "") -> dict:
            calls.append({"eventids": [a.eventid for a in alerts], "hint": followup_hint})
            if len(calls) == 1:
                # 第一批「正在跑」的时候，第二条告警到了：模拟 process_alert 那时会
                # 走到的分支（host_key 那会儿在 _ACTIVE_RUN_HOSTS 里）。
                pipeline._FOLLOWUP_QUEUE["10683"] = pipeline._IncidentWindow(first_alert=second)
                return {"eventids": ["3008"], "groups": 1, "headline": "接口被人为 shutdown"}
            return {"eventids": ["3009"], "groups": 1, "headline": ""}

        with mock.patch.object(pipeline, "_process_incident_batch", side_effect=fake_process):
            result = pipeline._flush_incident_window(host_key="10683")

        self.assertEqual([c["eventids"] for c in calls], [["3008"], ["3009"]])
        self.assertEqual(calls[0]["hint"], "")
        self.assertIn("接口被人为 shutdown", calls[1]["hint"])
        self.assertIn("10683", calls[1]["hint"])
        # 返回的是第一批的结果（webhook/回执关心的是它自己这条），不是最后一批的
        self.assertEqual(result["eventids"], ["3008"])
        self.assertNotIn("10683", pipeline._FOLLOWUP_QUEUE)
        self.assertNotIn("10683", pipeline._ACTIVE_RUN_HOSTS)

    @mock.patch("netops_ai.api.pipeline.fetch_zabbix_context")
    def test_zabbix取不到上下文就直接落错误记录不进窗口(self, mock_zbx):
        mock_zbx.return_value = ("", "", 0, "problem.get 查不到", {})

        result = pipeline.process_alert({"eventid": "1000", "name": "x"})

        self.assertEqual(result["status"], "error")
        self.assertEqual(pipeline._INCIDENT_WINDOWS, {})
        saved = json.loads((Path(self.tmp.name) / "alert-1000.json").read_text(encoding="utf-8"))
        self.assertIn("Zabbix 上下文取不到", saved["analysis_error"])


class TestPrefetchItemFilter(unittest.TestCase):
    """数值型监控项只预取状态类 + 告警那个接口的。

 起因是拿 104 条设备记录量出来的：整段 Zabbix 上下文里「相关监控项最近历史」占 90%，
 而体积前七名全是每个接口的 `net.if.in/out`、`discards`、`errors`——每次采集都在变，
 连「窗口内没变过」那条折叠都折不进去。过滤器拿 198 条记录的证据引文反向验过：
 **59 条进过结论的数值行全部保留，0 误伤。**
    """

    def test_状态类一律留(self):
        for key in ("net.if.status[ifOperStatus.2]", "netops.bgpPeerState[10.0.0.1]",
                    "netops.ospfNbrState[1]", "icmpping", "zabbix[host,snmp,available]",
                    "vm.memory.util[vm.memory.util.1]", "system.hw.uptime[1]"):
            with self.subTest(key=key):
                self.assertTrue(pipeline._is_diagnostic_item({"key_": key, "name": ""}, []))

    def test_流量和错包计数类砍掉(self):
        for key in ("net.if.in[ifHCInOctets.3]", "net.if.out[ifHCOutOctets.3]",
                    "net.if.in.errors[ifInErrors.3]", "net.if.out.discards[ifOutDiscards.3]"):
            with self.subTest(key=key):
                self.assertFalse(pipeline._is_diagnostic_item({"key_": key, "name": ""}, []))

    def test_告警那个接口的计数类要留(self):
        """别的口的流量不看，出事那个口的要看——丢包/错包正是链路质量那一类的判据。"""
        item = {"key_": "net.if.in.errors[ifInErrors.3]", "name": "Interface Et0/1(): Inbound errors"}
        self.assertTrue(pipeline._is_diagnostic_item(item, ["Et0/1"]))
        self.assertFalse(pipeline._is_diagnostic_item(item, ["Et1/2"]))


class TestFetchZabbixContext(unittest.TestCase):
    """回归测试：`problem.get` 在这版 Zabbix 上不支持 `selectHosts`，真实跑
 一次告警时撞到过，
 host 信息改从 `trigger.get` 拿。这组测试确保不会被改回去。
    """

    @mock.patch("netops_ai.api.pipeline.ZabbixClient")
    def test_problem_get不传selectHosts(self, mock_zbx_cls):
        mock_zbx = mock.MagicMock()
        mock_zbx_cls.return_value.__enter__.return_value = mock_zbx
        mock_zbx._call.side_effect = [
            [
                {
                    "eventid": "1",
                    "name": "x",
                    "severity": "3",
                    "clock": "100",
                    "objectid": "5",
                    "opdata": "",
                    "tags": [{"tag": "interface", "value": "Gi0/1"}],
                }
            ],
            [{"description": "x", "expression": "y", "hosts": [{"hostid": "10", "host": "V1"}]}],
            [{"interfaceid": "31", "hostid": "10", "type": "2", "useip": "1", "ip": "10.0.0.1", "dns": "", "port": "161", "available": "1"}],
            [],  # 抖动检测那次 event.get（没有历史事件）
        ]
        mock_zbx.get_active_problems.return_value = []
        mock_zbx.list_items.return_value = []

        text, hostid, clock, err, meta = pipeline.fetch_zabbix_context("1")

        self.assertEqual(err, "")
        self.assertEqual(hostid, "10")
        self.assertEqual(meta["triggerid"], "5")
        self.assertEqual(meta["interfaces"], ["Gi0/1"])
        first_call_params = mock_zbx._call.call_args_list[0].args[1]
        self.assertNotIn("selectHosts", first_call_params)
        second_call_method = mock_zbx._call.call_args_list[1].args[0]
        second_call_params = mock_zbx._call.call_args_list[1].args[1]
        self.assertEqual(second_call_method, "trigger.get")
        self.assertIn("selectHosts", second_call_params)

    @mock.patch("netops_ai.api.pipeline.ZabbixClient")
    def test_日志型监控项一条一行按时间正序(self, mock_zbx_cls):
        # 出处：一条真实告警记录 的 zabbix_context_text，V2 真实 syslog
        lines = [
            (1789903213, "Sep 20 11:20:13 192.0.2.51 82: *Sep 20 11:19:25.825: %BGP-3-NOTIFICATION: sent to neighbor 10.0.0.1"),
            (1789903214, "Sep 20 11:20:14 192.0.2.51 83: *Sep 20 11:19:25.825: %BGP-5-NBR_RESET: Neighbor 10.0.0.1 reset (BGP Notification sent)"),
            (1789903214, "Sep 20 11:20:14 192.0.2.51 84: *Sep 20 11:19:25.826: %BGP-5-ADJCHANGE: neighbor 10.0.0.1 Down BGP Notification sent"),
        ]
        mock_zbx = mock.MagicMock()
        mock_zbx_cls.return_value.__enter__.return_value = mock_zbx
        mock_zbx._call.side_effect = [
            [{"eventid": "1", "name": "x", "severity": "3", "clock": "1789903214", "objectid": "5", "opdata": "", "tags": []}],
            [{"description": "x", "expression": "y", "hosts": [{"hostid": "10", "host": "V2"}],
              # 预取改按需之后，日志只有在它是**触发项**时才进上下文
              "items": [{"itemid": "1", "name": "Syslog from 192.0.2.51",
                         "key_": "logrt[/var/log/network/192.0.2.51.log,,,,skip]", "value_type": "2"}]}],
            [],
            [],  # 抖动检测那次 event.get（没有历史事件）
        ]
        mock_zbx.get_active_problems.return_value = []
        mock_zbx.list_items.return_value = []
        # get_history 是 DESC
        mock_zbx.get_history.return_value = [{"clock": str(c), "value": v} for c, v in reversed(lines)]

        text, *_ = pipeline.fetch_zabbix_context("1")

        self.assertEqual(mock_zbx.get_history.call_args.kwargs["limit"], pipeline.LOG_HISTORY_LIMIT)
        body = [l.strip() for l in text.splitlines() if "%BGP" in l]
        self.assertEqual(body, [v for _, v in lines])

    @mock.patch("netops_ai.api.pipeline.ZabbixClient")
    def test_已自愈的告警改用event_get_并写明恢复时间(self, mock_zbx_cls):
        # #27：93801 等到窗口关闭时已恢复，problem.get 查不到，以前整条跳过
        mock_zbx = mock.MagicMock()
        mock_zbx_cls.return_value.__enter__.return_value = mock_zbx
        mock_zbx._call.side_effect = [
            [],  # problem.get
            [{"eventid": "93801", "name": "BGP down", "severity": "3", "clock": "1000", "objectid": "5",
              "opdata": "", "r_eventid": "93805", "tags": []}],  # event.get
            [{"clock": "1042"}],  # 恢复事件
            [{"description": "x", "expression": "y", "hosts": [{"hostid": "10", "host": "V1"}]}],
            [],
            [],  # 抖动检测那次 event.get（没有历史事件）
        ]
        mock_zbx.get_active_problems.return_value = []
        mock_zbx.list_items.return_value = []

        text, hostid, clock, err, meta = pipeline.fetch_zabbix_context("93801")

        self.assertEqual(err, "")
        self.assertEqual(clock, 1000)
        self.assertIn("已自动恢复", text)
        self.assertIn("约 42 秒", text)
        self.assertEqual(mock_zbx._call.call_args_list[1].args[0], "event.get")

    @mock.patch("netops_ai.api.pipeline.ZabbixClient")
    def test_预取不再带无关监控项的历史(self, mock_zbx_cls):
        """维护者：「拉 Zabbix 原文 <<< 然后这个给我按需改了」。

 原来 15 分钟窗口内所有监控项的历史全拉进来（设备侧平均 6276 字符，九成是它）。
 现在只给「这条告警本身 + 触发它的那个监控项」，别的让 agent 自己用工具取。
        """
        mock_zbx = mock.MagicMock()
        mock_zbx_cls.return_value.__enter__.return_value = mock_zbx
        trigger_item = {"itemid": "2", "name": "Gi0/1 Operational status",
                        "key_": "net.if.status[ifOperStatus.2]", "value_type": "3"}
        mock_zbx._call.side_effect = [
            [{"eventid": "1", "name": "x", "severity": "3", "clock": "1000", "objectid": "5", "opdata": "", "tags": []}],
            [{"description": "x", "expression": "{2}=2", "hosts": [{"hostid": "10", "host": "V1"}],
              "items": [trigger_item]}],
            [],
            [],  # 抖动检测那次 event.get（没有历史事件）
        ]
        mock_zbx.get_active_problems.return_value = []
        mock_zbx.list_items.return_value = [
            {"itemid": "1", "name": "Gi0/1 Admin status", "key_": "ifAdminStatus.2", "value_type": "3"},
            trigger_item,
        ]
        mock_zbx.get_history.return_value = [{"clock": "990", "value": "2"}, {"clock": "960", "value": "1"}]

        text, *_ = pipeline.fetch_zabbix_context("1")

        # 触发项带着它的窗口取值进来
        self.assertIn("Gi0/1 Operational status", text)
        self.assertIn("故障窗口内取值：990=2, 960=1", text)
        # 非触发项一个字都没有，连 list_items 都不该被调
        self.assertNotIn("Admin status", text)
        mock_zbx.list_items.assert_not_called()
        # 但必须写明「这里没给什么」，否则它会以为这就是全部
        self.assertIn("这里只有这条告警本身，别的要自己查", text)
        self.assertIn("zbx_syslog", text)

    @mock.patch("netops_ai.api.pipeline.ZabbixClient")
    def test_查不到告警时返回明确错误(self, mock_zbx_cls):
        mock_zbx = mock.MagicMock()
        mock_zbx_cls.return_value.__enter__.return_value = mock_zbx
        mock_zbx._call.return_value = []

        text, hostid, clock, err, meta = pipeline.fetch_zabbix_context("999")
        self.assertEqual(text, "")
        self.assertIn("999", err)
        self.assertEqual(meta, {})


class TestDiagnosticContext(unittest.TestCase):
    def test_pipeline_record_has_incident_fields(self):
        record = pipeline.PipelineRecord(eventid="1", webhook_payload={}, started_at="now")
        self.assertEqual(record.diagnostic_trace, [])
        self.assertEqual(record.diagnostic_mode, "")
        self.assertEqual(record.incident_id, "")
        self.assertEqual(record.incident_grouped_with, [])

    @mock.patch("netops_ai.api.pipeline.run_agent_loop")
    @mock.patch("netops_ai.api.pipeline._env")
    def test_诊断上下文统一起agent循环_不预塞syslog(self, mock_env, mock_loop):
        mock_env.return_value = {
            "DEVICE_HOST": "10.0.0.1",
            "DEVICE_VENDOR": "cisco",
            "DEVICE_USERNAME": "u",
            "DEVICE_PASSWORD": "p",
        }
        tool_call = mock.Mock(
            tool="device_show",
            args={"command": "show version"},
            ok=True,
            error="",
            elapsed_ms=120,
            result={"command": "show version", "allowed": True, "ok": True, "output": "ok"},
        )
        fake_trace = mock.Mock(tool_calls=[tool_call], final_structured=None, transcript="轨迹原文")
        mock_loop.return_value = mock.Mock(
            answer="接口被人为关闭",
            incomplete=False,
            termination_reason="",
            trace=fake_trace,
        )

        result = pipeline.fetch_diagnostic_context(
            "cisco",
            zabbix_text="## 标签\n[]\n\n## 告警原文\n{\"name\":\"unknown\"}",
            alert_name="unknown",
            alert_time_epoch=1700000000,
            device_host="10.0.0.2",
        )

        self.assertEqual(result.mode, "ai")
        self.assertIn("show version", result.text)
        # A14：syslog 走 Zabbix 监控项，agent 自己查；管道不再预先 SSH 去拼一份
        self.assertNotIn("故障窗口 syslog", result.text)
        self.assertNotIn("syslog", [t.get("source") for t in result.trace])
        mock_loop.assert_called_once()
        self.assertNotIn("故障窗口 syslog", mock_loop.call_args.kwargs["question"])
        # **反过来了**：结论就在这条轨迹末尾出，不再单独走 analyze。
        # 维护者：「研判你也砍了吧，取证那步就应该直接出结论。」
        kwargs = mock_loop.call_args.kwargs
        self.assertIsNotNone(kwargs.get("final_schema"))
        # 收尾那次必须带研判的硬规则，不能用 agent_loop 里那句通用的
        self.assertIn("逐字复制", kwargs["final_system_prompt"])
        # 时间基准也要带上，否则它会拿现在的状态反推故障时刻
        self.assertIn("时间基准", kwargs["final_context"])
        self.assertIn("目标", kwargs["system_prompt"])
        # 停止条件改成粗粒度、模型自己能判断的收尾条件
        self.assertIn("什么时候收尾", kwargs["system_prompt"])
        self.assertIn("下一步查什么", kwargs["system_prompt"])
        # 「收尾会被问什么」整段是分两次调用时代的补丁，下掉了；方向表态/反证要求只留在结构化收尾那次
        self.assertNotIn("收尾会被问什么", kwargs["system_prompt"])
        self.assertNotIn("排除必须有反证", kwargs["system_prompt"])
        self.assertNotIn("local_action", kwargs["system_prompt"])
        # 诚实红线还在
        self.assertIn("逐字", kwargs["system_prompt"])
        self.assertIn("不要编造", kwargs["system_prompt"])
        self.assertLessEqual(len(kwargs["system_prompt"].splitlines()), 35)
        # A16 撤闸实测出现失控（64~85 次调用），维护者拍板恢复默认闸。
        budget = kwargs["budget"]
        self.assertEqual(budget.max_iterations, 18)
        self.assertEqual(budget.max_tool_calls, 12)
        self.assertEqual(budget.no_progress_threshold, 3)
        # A16：知识库工具（doc_search）在告警这条线默认打开，不吃 DOC_SEARCH 环境变量
        self.assertTrue(mock_loop.call_args.args[0].keywords.get("include_doc_search"))

        from netops_ai.graph.agent_loop import DEFAULT_SYSTEM_PROMPT

        self.assertNotIn("真实告警的根因分析", DEFAULT_SYSTEM_PROMPT)

    @mock.patch("netops_ai.api.pipeline.run_agent_loop")
    @mock.patch("netops_ai.playbooks.lookup.sop_lookup")
    def test_命中sop时作为计划骨架传给agent(self, mock_sop, mock_loop):
        mock_sop.return_value = {
            "matched": True,
            "playbook": "interface-link-down",
            "scope": "接口 Link down",
            "applicability": "适用于接口 down 告警。",
            "limits": {"max_main_steps": 5, "max_tokens": 1500},
            "steps": [
                {
                    "id": "check_admin_status",
                    "why": "看 admin",
                    "expect": "看到 admin 状态",
                    "main": True,
                    "action": {"tool": "zbx_items", "key": "net.if.admin.status"},
                },
                {
                    "id": "check_running_config",
                    "why": "看配置",
                    "expect": "看到 running-config",
                    "main": True,
                    "action": {"tool": "device_show", "command": "more system:running-config"},
                },
                {
                    "id": "branch_detail",
                    "why": "分支细节",
                    "main": False,
                    "action": {"tool": "device_show", "command": "show interfaces status"},
                },
            ],
        }
        mock_loop.return_value = mock.Mock(
            incomplete=False,
            termination_reason="",
            trace=mock.Mock(tool_calls=[], final_structured=None, transcript="轨迹原文"),
        )

        pipeline._run_ai_exploration(
            zabbix_text=(
                '## 告警原文\n{"name":"Cisco IOS: Interface Gi0/1(): Link down"}'
                '\n\n## 标签\n[{"tag":"component","value":"network"}]'
            ),
            plan=None,
            host_filter="",
            alert_clock=1700000000,
        )

        plan = mock_loop.call_args.kwargs["plan"]
        self.assertIn("SOP：interface-link-down", plan)
        self.assertIn("本次以这份 SOP 为骨架取证", plan)
        self.assertIn("适用性：适用于接口 down 告警。", plan)
        self.assertIn("id=check_admin_status", plan)
        self.assertIn("expect=看到 admin 状态", plan)
        # action 按原样摆，参数名就是工具的真实参数名（原来 key_contains 被渲染成 `command=`）
        self.assertIn("tool=zbx_items; key=net.if.admin.status", plan)
        self.assertIn("id=check_running_config", plan)
        self.assertIn("tool=device_show; command=more system:running-config", plan)
        # 辅助步骤不进初始计划（曾整段列入）；分支指向它时由 [SOP 下一步] 带出完整命令和去向，
        # 模型仍然看得到它的命令——见 tests/test_loop_speed.py::test_走到辅助步骤时SOP下一步里有完整命令和去向
        self.assertNotIn("id=branch_detail", plan)
        self.assertNotIn("show interfaces status", plan)
        self.assertIn("[SOP 下一步] 给出它的命令和分支", plan)
        from netops_ai.graph import agent_loop

        branch_hint = agent_loop.SopRuntimeState({
            **mock_sop.return_value,
            "steps": [
                {**mock_sop.return_value["steps"][1], "branches": [{"when": "default", "goto": "branch_detail"}]},
                mock_sop.return_value["steps"][2],
            ],
        }).hint_for(
            agent_loop.ToolCallRecord(tool="device_show", args={"command": "more system:running-config"}, ok=True,
                                      result={"allowed": True, "ok": True, "output": "x"}),
            tool_call_index=1,
        )
        self.assertIn("tool=device_show; command=show interfaces status", branch_hint)
        # error / default 的语义写明：空结果不算 error（验证 外部 agent 反馈第 3 条）
        self.assertIn("返回为空（empty=true 或输出空白）不算 error，按 default 走", plan)
        self.assertNotIn("空输出时，按它的 error 分支走", plan)
        self.assertNotIn("可能误判", plan)
        self.assertNotIn("不是必须执行", plan)
        self.assertEqual(mock_loop.call_args.kwargs["sop_data"], mock_sop.return_value)
        mock_sop.assert_called_once_with(
            "Cisco IOS: Interface Gi0/1(): Link down",
            ["component=network"],
            alert_clock=1700000000,
            interface_hint="GigabitEthernet0/1",  # 整批汇总出的接口；告警名自带接口时 sop_lookup 以告警名为准
        )

    @mock.patch("netops_ai.api.pipeline.run_agent_loop")
    @mock.patch("netops_ai.playbooks.lookup.sop_lookup", return_value={"matched": False, "steps": []})
    def test_未命中sop时plan为空(self, _mock_sop, mock_loop):
        mock_loop.return_value = mock.Mock(
            incomplete=False,
            termination_reason="",
            trace=mock.Mock(tool_calls=[], final_structured=None, transcript="轨迹原文"),
        )

        pipeline._run_ai_exploration(
            zabbix_text='## 告警原文\n{"name":"unknown"}\n\n## 标签\n[]',
            plan=None,
            host_filter="",
            alert_clock=1700000000,
        )

        self.assertIsNone(mock_loop.call_args.kwargs["plan"])
        self.assertEqual(mock_loop.call_args.kwargs["sop_data"]["matched"], False)

    def test_sop_plan_respects_max_tokens_truncation(self):
        data = {
            "matched": True,
            "playbook": "p",
            "limits": {"max_tokens": 20},
            "steps": [
                {
                    "id": "s1",
                    "why": "x" * 200,
                    "expect": "y",
                    "main": True,
                    "action": {"tool": "device_show", "command": "show interfaces GigabitEthernet0/1"},
                }
            ],
        }
        plan = pipeline._render_sop_plan(data)

        self.assertIn("已按 SOP limits.max_tokens 截断", plan)
        self.assertLessEqual(len(plan), 80)


class TestRelatedAlertEvidence(unittest.TestCase):
    """A9：对端告警的 eventid 要进研判原文，否则 related_alerts 核不到出处。"""

    @mock.patch("netops_ai.api.pipeline.run_agent_loop")
    @mock.patch("netops_ai.api.pipeline._env", return_value={"DEVICE_HOST": "192.0.2.52"})
    def test_查到的对端告警进原文(self, _env, mock_loop):
        call = mock.Mock(
            tool="zbx_problems", args={"host": "A1"}, ok=True, error="", elapsed_ms=80,
            result={"problems": [{"eventid": "93557", "name": "Interface Et0/1(): Link down"}], "count": 1},
        )
        mock_loop.return_value = mock.Mock(answer="对端 A1 也有告警 93557", incomplete=False,
                                           termination_reason="",
                                           trace=mock.Mock(tool_calls=[call], final_structured=None,
                                                           transcript="轨迹原文"))

        result = pipeline.fetch_diagnostic_context("cisco", zabbix_text="x", alert_name="x", device_host="192.0.2.52")

        self.assertIn('"eventid": "93557"', result.text)
        self.assertIn("topology_neighbors", mock_loop.call_args.kwargs["question"])


class TestDeviceTransportSelection(unittest.TestCase):
    def test_默认使用ssh适配器(self):
        env = {
            "DEVICE_HOST": "10.0.0.99",
            "DEVICE_PORT": "22",
            "DEVICE_USERNAME": "ai-readonly",
            "DEVICE_PASSWORD": "x",
        }
        adapter = pipeline._device_adapter_from_env(env, "cisco")
        self.assertEqual(adapter.__class__.__name__, "SSHDeviceAdapter")
        adapter.close()

    def test_显式telnet才使用telnet适配器(self):
        env = {
            "DEVICE_TRANSPORT": "telnet",
            "DEVICE_HOST": "10.0.0.99",
            "DEVICE_TELNET_PORT": "23",
            "DEVICE_USERNAME": "ai-readonly",
            "DEVICE_PASSWORD": "x",
        }
        adapter = pipeline._device_adapter_from_env(env, "cisco")
        self.assertEqual(adapter.__class__.__name__, "TelnetDeviceAdapter")


class TestWindowCalibration(unittest.TestCase):
    """N1 那轮真实测量：OSPF dead 40s / BGP hold 180s / Zabbix 轮询 30s。
    默认窗口要盖住最长那条链（BGP：180+30=210s），不是随手填的数字。
    """

    def test_默认窗口盖住bgp最长等待(self):
        window, window_max = pipeline._incident_window_seconds({})
        self.assertGreaterEqual(window, 210)
        self.assertLessEqual(window, window_max)

    def test_env可以覆盖窗口(self):
        window, window_max = pipeline._incident_window_seconds(
            {"ALERT_WINDOW_SECONDS": "50", "ALERT_WINDOW_MAX_SECONDS": "60"}
        )
        self.assertEqual(window, 50)
        self.assertEqual(window_max, 60)


if __name__ == "__main__":
    unittest.main()


class TestRedactionRespectsWordBoundaries(unittest.TestCase):
    """凭据脱敏只换**独立出现**的值。

 开发机在真实 syslog 里撞到：`SYSLOG_SSH_PASSWORD=eve`，裸 `replace`
 把 `severity` 腰斩成 `s<SYSLOG_SSH_PASSWORD>rity`——送进模型的证据被打了马赛克。
    """

    ENV = {"SYSLOG_SSH_PASSWORD": "eve", "SYSLOG_SSH_USER": "root", "DEVICE_PASSWORD": "p@ss!"}

    def _r(self, text):
        from netops_ai.api.pipeline import _redact_env_values
        return _redact_env_values(text, self.ENV)

    def test_嵌在单词里的不动(self):
        for text in ("按 severity 筛了一道", "whenever however believe seven", "chroot /rootfs"):
            self.assertEqual(self._r(text), text)

    def test_独立出现的照样换掉(self):
        self.assertEqual(self._r("login root password eve"),
                         "login <SYSLOG_SSH_USER> password <SYSLOG_SSH_PASSWORD>")
        self.assertEqual(self._r("password=eve;"), "password=<SYSLOG_SSH_PASSWORD>;")

    def test_带标点的密码也换得掉(self):
        # 用 `\\b` 做边界的话这条会漏：`!` 和后面的空格都是非单词字符，中间不算边界。
        self.assertEqual(self._r("secret is p@ss! done"), "secret is <DEVICE_PASSWORD> done")

    def test_Zabbix用户名是常用词时不替换(self):
        """Zabbix 默认用户 `Admin` 同时是设备输出里的词（`Idle (Admin)`），替换会让证据引文对不上。"""
        from netops_ai.api.pipeline import _redact_env_values
        env = {"ZABBIX_USER": "Admin", "DEVICE_USERNAME": "ai-readonly"}
        self.assertEqual(_redact_env_values("State Idle (Admin)", env), "State Idle (Admin)")
        self.assertEqual(_redact_env_values("user: ai-readonly", env), "user: <DEVICE_USERNAME>")

    def test_没提高最短长度(self):
        # 提高最短长度等于让短密码原样漏进证据和 prompt，拿安全换好看。
        self.assertNotIn("eve", self._r("pw eve"))


class TestWindowAutoExtension(unittest.TestCase):
    """静默早停 + 相关就延长 + 硬上限封顶（Moogsoft Cookbook 那套 cook-for auto-extension）。

    只测排期算出来的 delay，不真的等定时器——`_rearm_window_timer` 会立刻 start 一个
    `threading.Timer`，测试里拿到之后马上取消。
    """

    def _delay_for(self, window, *, quiet, window_max):
        captured = {}

        class _FakeTimer:
            def __init__(self, delay, fn, kwargs=None):
                captured["delay"] = delay
                self.daemon = False

            def start(self):
                pass

            def cancel(self):
                pass

        with mock.patch.object(pipeline.threading, "Timer", _FakeTimer):
            pipeline._rearm_window_timer("h", window, quiet=quiet, window_max=window_max)
        return captured["delay"]

    def test_孤立告警静默就关窗不用陪BGP等满(self):
        window = pipeline._IncidentWindow(
            first_alert=_pending("1"), opened_at=time.monotonic(), min_wait=0
        )
        self.assertAlmostEqual(self._delay_for(window, quiet=40, window_max=300), 40, delta=1)

    def test_批里有BGP就算静默也要等满最短等待(self):
        # 开窗已经过去 10 秒，BGP 的最短等待是 210，还得再等 200，不能 40 秒就关
        window = pipeline._IncidentWindow(
            first_alert=_pending("1"), opened_at=time.monotonic() - 10, min_wait=210
        )
        self.assertAlmostEqual(self._delay_for(window, quiet=40, window_max=300), 200, delta=1)

    def test_不管怎么延都不超过硬上限(self):
        # 开窗已经过去 290 秒，硬上限 300，只剩 10 秒，静默 40 也得让步
        window = pipeline._IncidentWindow(
            first_alert=_pending("1"), opened_at=time.monotonic() - 290, min_wait=0
        )
        self.assertAlmostEqual(self._delay_for(window, quiet=40, window_max=300), 10, delta=1)

    def test_超过硬上限立刻关窗不给负数(self):
        window = pipeline._IncidentWindow(
            first_alert=_pending("1"), opened_at=time.monotonic() - 400, min_wait=0
        )
        self.assertEqual(self._delay_for(window, quiet=40, window_max=300), 0.0)

    def test_按告警类型算最短等待(self):
        self.assertEqual(pipeline._min_wait_for(_pending("1", name="BGP neighbor down")), 210)
        self.assertEqual(pipeline._min_wait_for(_pending("1", name="OSPF neighbor down")), 60)
        self.assertEqual(pipeline._min_wait_for(_pending("1", name="Interface Gi0/1 down")), 0)


class Test取证提示里对端要查故障窗口(unittest.TestCase):
    """**这段话里有几句是用真实误判换来的，改坏了没有任何地方会报错。**

 V2-vios Gi0/3 的 OSPF 邻居和三条 BGP 邻居在同一时刻一起 down、
 0 秒自愈。原来的提示写的是「用 zbx_problems 按对端主机名查它此刻有没有告警」——
 告警已经恢复时对端那边同样早就恢复，这条必然查空，「对端没问题」是假的。
 那次最后判成「监控采集自身的问题」，而两个独立协议同时掉指向的是链路层。
    """

    def test_对端要查故障窗口的日志(self):
        q = pipeline.FORENSICS_QUESTION
        # 之后改成一个工具。原来是让它「用 zbx_history 按告警时刻取
        # 对端的日志型监控项」——四步推理，gpt-5.5 都没走通，四步包进了 zbx_syslog。
        self.assertIn("zbx_syslog", q)
        self.assertIn("对端在故障窗口里的日志", q)
        # 别再退回让模型自己拼 itemid 那条路
        self.assertNotIn("zbx_history", q)

    def test_明说不许拿当前告警当对端没问题的依据(self):
        q = pipeline.FORENSICS_QUESTION
        head, _, tail = q.partition("zbx_problems 查对端")
        self.assertTrue(tail, "没找到那句警告，zbx_problems 可能又被写成对端的主要查法了")
        self.assertIn("必然是空的", tail)

    def test_日志两个来源都说清_一个空不等于另一个空(self):
        """Zabbix 没收到 A1 的 syslog，工具说明又劝「不要登设备跑 show logging」，
 两个 agent 都错过了 buffer 里的 `%SYS-5-CONFIG_I`。
        """
        q = pipeline.FORENSICS_QUESTION
        self.assertIn("两个来源", q)
        self.assertIn("show logging", q)
        self.assertIn("一个来源没有，不等于另一个也没有", q)

    def test_对端什么时候查说清楚_不跟SOP分支打架(self):
        """外部 agent 反馈第 4 条：「跟链路有关就查对端」vs SOP admin down 分支「两条证据即可收尾」。"""
        q = pipeline.FORENSICS_QUESTION
        self.assertIn("对端可能是原因时", q)
        self.assertIn("影响面", q)

    def test_对端告警写不写eventid只有一种说法(self):
        """验证 外部 agent 反馈第 5 条：一处「对端按需取」，另一处「对端此刻真有告警的写 eventid」，两句读不出同一个答案。"""
        q = pipeline.FORENSICS_QUESTION
        self.assertIn("对端不是必查项", q)
        # 「小结」→「结论」，取证最后那段只写几句（ALERT_CLOSING_NOTE），这句落到结构化收尾那一步
        self.assertIn("只要查了对端、对端此刻有未恢复的告警，就在结论里写明它的 eventid", q)
        self.assertNotIn("按需取", q)
        self.assertEqual(q.count("eventid"), 1)

    def test_时间约定写明各来源格式(self):
        """验证 外部 agent 反馈第 6 条：clock 是 Unix 秒、trap 行首是 UTC、zbx_problems 表是本地时区、设备时钟没对时。"""
        q = pipeline.FORENSICS_QUESTION
        self.assertIn("时间约定", q)
        for fact in ("Unix 秒", "与时区无关", "表头写了 UTC 偏移", "YYYYMMDD.HHMMSS", "没有启用 NTP", "show clock"):
            self.assertIn(fact, q)

    def test_模板能正常渲染(self):
        out = pipeline.FORENSICS_QUESTION.format(zabbix_text="【这里是 Zabbix 原文】")
        self.assertIn("【这里是 Zabbix 原文】", out)
        self.assertNotIn("{zabbix_text}", out)


class TestTriggerItemsAreFirstSource(unittest.TestCase):
    """**触发这条告警的监控项就是第一信源**，必须显式取回来。

 维护者问「Zabbix webhook 是什么场景下？SNMP trap？假如是 trap，
 zabbix API 应该获取 trap 的内容，因为这是第一信源」。查下来：我们一个 trap
 都没有（全是 SNMP 轮询 + syslog），但他指出的问题更严重——`trigger.get`
 当时只传 `selectHosts`，从来没取过触发它的那个监控项。表达式里的
 `{38369}=2` 原样发给模型，那串数字它根本对不上任何东西。
    """

    def _ctx(self, mock_zbx_cls, *, items, trigger_items, expression):
        mock_zbx = mock.MagicMock()
        mock_zbx_cls.return_value.__enter__.return_value = mock_zbx
        mock_zbx._call.side_effect = [
            [{"eventid": "1", "name": "x", "severity": "3", "clock": "100", "objectid": "5",
              "opdata": "", "tags": [{"tag": "interface", "value": "Et0/1"}]}],
            [{"description": "Interface Et0/1(): Link down", "expression": expression,
              "hosts": [{"hostid": "10", "host": "A1"}], "items": trigger_items}],
            [],
            [],  # 抖动检测那次 event.get（没有历史事件）
        ]
        mock_zbx.get_active_problems.return_value = []
        mock_zbx.list_items.return_value = items
        mock_zbx.get_history.return_value = [{"clock": "100", "value": "2"}, {"clock": "40", "value": "1"}]
        return mock_zbx

    @mock.patch("netops_ai.api.pipeline.ZabbixClient")
    def test_触发项即使被数值过滤器砍掉也要取(self, mock_zbx_cls):
        """错包阈值类的触发器就会撞上这条：`net.if.in.errors` 不是状态类，
        过滤器本来会砍掉它——而它正是把告警顶起来的那个。"""
        errors_item = {"itemid": "77", "name": "Interface Et9/9(): Inbound errors",
                       "key_": "net.if.in.errors[ifInErrors.9]", "value_type": "3"}
        zbx = self._ctx(
            mock_zbx_cls,
            items=[errors_item,
                   {"itemid": "88", "name": "Interface Et9/9(): Bits received",
                    "key_": "net.if.in[ifHCInOctets.9]", "value_type": "3"}],
            trigger_items=[errors_item],
            expression="{77}>100",
        )
        # 先确认过滤器本来确实会砍掉它，否则这个测试是空的
        self.assertFalse(pipeline._is_diagnostic_item(errors_item, ["Et0/1"]))

        text, *_ = pipeline.fetch_zabbix_context("1")

        self.assertIn("Inbound errors", text)          # 触发项进来了
        self.assertNotIn("Bits received", text)        # 同类的非触发项照样被砍
        self.assertEqual(zbx.get_history.call_count, 1)

    @mock.patch("netops_ai.api.pipeline.ZabbixClient")
    def test_表达式里的itemid换成监控项名字(self, mock_zbx_cls):
        """`{38369}=2 and ({38370}<>{38371})` 原样发给模型等于没发。"""
        self._ctx(
            mock_zbx_cls,
            items=[],
            trigger_items=[{"itemid": "38369", "name": "Interface Gi0/1(): Operational status",
                            "key_": "net.if.status[ifOperStatus.2]", "value_type": "3"}],
            expression='{$IFCONTROL:"Gi0/1"}=1 and {38369}=2',
        )
        text, *_ = pipeline.fetch_zabbix_context("1")

        self.assertIn("[Interface Gi0/1(): Operational status]=2", text)
        self.assertNotIn("{38369}", text)

    @mock.patch("netops_ai.api.pipeline.ZabbixClient")
    def test_第一信源单独成段(self, mock_zbx_cls):
        """一台设备窗口内几十个监控项，混在一起模型分不出哪个是顶起告警的那个。"""
        self._ctx(
            mock_zbx_cls, items=[],
            trigger_items=[{"itemid": "38369", "name": "Interface Gi0/1(): Operational status",
                            "key_": "net.if.status[ifOperStatus.2]", "value_type": "3"}],
            expression="{38369}=2",
        )
        text, *_ = pipeline.fetch_zabbix_context("1")

        self.assertIn("## 触发这条告警的监控项（第一信源）", text)
        self.assertIn("itemid=38369", text)

    @mock.patch("netops_ai.api.pipeline.ZabbixClient")
    def test_trigger没返回监控项时如实写出来(self, mock_zbx_cls):
        """别让它以为「没这一段」就是「没有第一信源」。"""
        self._ctx(mock_zbx_cls, items=[], trigger_items=[], expression="{38369}=2")
        text, *_ = pipeline.fetch_zabbix_context("1")

        self.assertIn("（trigger.get 没返回监控项）", text)


class TestFlapDetection(unittest.TestCase):
    """抖动检测：业界降噪四个确定性控制里我们唯一没有的那条（flap control）。

 加。**只检测不抑制**——这个 lab 里抖动本身就是要判的故障形态
 （fault-catalog #4 接口抖动、#27 几十秒自愈），抑制了就永远查不出来。
    """

    def _run(self, mock_zbx_cls, flap_events):
        mock_zbx = mock.MagicMock()
        mock_zbx_cls.return_value.__enter__.return_value = mock_zbx
        mock_zbx._call.side_effect = [
            [{"eventid": "1", "name": "x", "severity": "3", "clock": "1000",
              "objectid": "5", "opdata": "", "tags": []}],
            [{"description": "x", "expression": "y", "hosts": [{"hostid": "10", "host": "A1"}], "items": []}],
            [],
            flap_events,
        ]
        return pipeline.fetch_zabbix_context("1")[0]

    @mock.patch("netops_ai.api.pipeline.ZabbixClient")
    def test_报够次数就写进上下文(self, mock_zbx_cls):
        text = self._run(mock_zbx_cls, [{"eventid": str(i), "clock": str(900 + i)} for i in range(5)])

        self.assertIn("## 抖动", text)
        self.assertIn("15 分钟内报了 5 次", text)
        # 别让它只判最后一次——抖动的形状才是线索
        self.assertIn("别只判最后这一次", text)

    @mock.patch("netops_ai.api.pipeline.ZabbixClient")
    def test_没到阈值就一个字都不加(self, mock_zbx_cls):
        text = self._run(mock_zbx_cls, [{"eventid": "1", "clock": "999"}])

        self.assertNotIn("## 抖动", text)

    @mock.patch("netops_ai.api.pipeline.ZabbixClient")
    def test_查不了也不许让整条告警挂掉(self, mock_zbx_cls):
        """抖动是锦上添花，Zabbix 这个调用失败不该拖垮整次分析。"""
        mock_zbx = mock.MagicMock()
        mock_zbx_cls.return_value.__enter__.return_value = mock_zbx
        mock_zbx._call.side_effect = [
            [{"eventid": "1", "name": "x", "severity": "3", "clock": "1000",
              "objectid": "5", "opdata": "", "tags": []}],
            [{"description": "x", "expression": "y", "hosts": [{"hostid": "10", "host": "A1"}], "items": []}],
            [],
            ZabbixAPIError("event.get 挂了"),
        ]

        text, hostid, _, err, _ = pipeline.fetch_zabbix_context("1")

        self.assertEqual(err, "")
        self.assertEqual(hostid, "10")
        self.assertNotIn("## 抖动", text)


class TestTrapItemRendering(unittest.TestCase):
    """SNMP trap 监控项是文本型（value_type=4），不能按数值项渲染。

 维护者：「SNMP trap 得先配，而不是轮询，没人这样监控网络的」。
 把 trap 改成主通路之后，触发告警的第一信源就是一条 trap 正文——
 按数值项渲染成 `clock=value` 会把整条 trap 挤进一行，时间线只认第一个时间戳。
    """

    @mock.patch("netops_ai.api.pipeline.ZabbixClient")
    def test_trap正文一条一行按时间正序(self, mock_zbx_cls):
        trap_item = {"itemid": "9", "name": "SNMP trap (linkDown)",
                     "key_": "snmptrap[linkDown]", "value_type": "4"}
        mock_zbx = mock.MagicMock()
        mock_zbx_cls.return_value.__enter__.return_value = mock_zbx
        mock_zbx._call.side_effect = [
            [{"eventid": "1", "name": "x", "severity": "3", "clock": "1000",
              "objectid": "5", "opdata": "", "tags": []}],
            [{"description": "Interface down (trap)", "expression": "{9}=1",
              "hosts": [{"hostid": "10", "host": "A1"}], "items": [trap_item]}],
            [],
            [],
        ]
        # get_history 是 DESC，渲染要翻回正序
        mock_zbx.get_history.return_value = [
            {"clock": "1002", "value": "IF-MIB::linkDown ifIndex.2 ifOperStatus.2 = down(2)"},
            {"clock": "1001", "value": "IF-MIB::linkDown ifIndex.1 ifOperStatus.1 = down(2)"},
        ]

        text, *_ = pipeline.fetch_zabbix_context("1")

        lines = [l.strip() for l in text.splitlines() if "linkDown" in l and "ifIndex" in l]
        self.assertEqual(lines, [
            "IF-MIB::linkDown ifIndex.1 ifOperStatus.1 = down(2)",
            "IF-MIB::linkDown ifIndex.2 ifOperStatus.2 = down(2)",
        ])
        # 别按数值项渲染
        self.assertNotIn("故障窗口内取值：", text)
        # 文本型要按日志的条数上限取，不是数值项那 10 个点
        self.assertEqual(mock_zbx.get_history.call_args.kwargs["limit"], pipeline.LOG_HISTORY_LIMIT)


class TestSelfContaminationWarning(unittest.TestCase):
    """第一信源里出现我们自己的只读账号时，必须当场标出来。

 真实出过一张垃圾卡（A3，一条真实告警）：agent 14:21:30 SSH 登进
 A3、1 秒后登出，14:21:33 设备发 configChange trap，Zabbix 报警，agent 去分析
 这条告警——**查到了自己刚才的登录，还把它当证据引进了结论**。
 这条规矩原来只写在工具描述里，而第一信源是预取直接塞的，工具描述管不着。
    """

    @mock.patch("netops_ai.api.pipeline._env", return_value={"DEVICE_USERNAME": "ai-readonly"})
    @mock.patch("netops_ai.api.pipeline.ZabbixClient")
    def test_只读账号出现在第一信源里要标出来(self, mock_zbx_cls, _env):
        mock_zbx = mock.MagicMock()
        mock_zbx_cls.return_value.__enter__.return_value = mock_zbx
        trap_item = {"itemid": "9", "name": "SNMP trap (configChange)",
                     "key_": "snmptrap[configChange]", "value_type": "4"}
        mock_zbx._call.side_effect = [
            [{"eventid": "1", "name": "x", "severity": "2", "clock": "1000",
              "objectid": "5", "opdata": "", "tags": []}],
            [{"description": "configChange", "expression": "{9}=1",
              "hosts": [{"hostid": "10", "host": "A3"}], "items": [trap_item]}],
            [], [],
        ]
        mock_zbx.get_history.return_value = [
            {"clock": "999", "value": "%SYS-6-LOGOUT: User ai-readonly has exited tty session 2"},
        ]

        text, *_ = pipeline.fetch_zabbix_context("1")

        self.assertIn("那是本系统自己取证时留下的登录记录", text)
        # 不许把它归到「有人动过配置」，也不许说成分不出
        self.assertIn("监控采集自身的问题", text)

    @mock.patch("netops_ai.api.pipeline._env", return_value={"DEVICE_USERNAME": "ai-readonly"})
    @mock.patch("netops_ai.api.pipeline.ZabbixClient")
    def test_没出现就不要加这段噪声(self, mock_zbx_cls, _env):
        mock_zbx = mock.MagicMock()
        mock_zbx_cls.return_value.__enter__.return_value = mock_zbx
        item = {"itemid": "9", "name": "Oper status", "key_": "net.if.status[2]", "value_type": "3"}
        mock_zbx._call.side_effect = [
            [{"eventid": "1", "name": "x", "severity": "2", "clock": "1000",
              "objectid": "5", "opdata": "", "tags": []}],
            [{"description": "Link down", "expression": "{9}=2",
              "hosts": [{"hostid": "10", "host": "A3"}], "items": [item]}],
            [], [],
        ]
        mock_zbx.get_history.return_value = [{"clock": "999", "value": "2"}]

        text, *_ = pipeline.fetch_zabbix_context("1")

        self.assertNotIn("本系统自己取证", text)


class TestCrossDeviceWindowMerge(unittest.TestCase):
    """同一条链路/邻接两端的告警并进同一个事件窗口（按拓扑直连判断）；不相邻的设备各开各的。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(pipeline, "RECORDS_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self._reset()
        self.addCleanup(self._reset)

    @staticmethod
    def _reset():
        with pipeline._INCIDENT_WINDOWS_LOCK:
            for window in pipeline._INCIDENT_WINDOWS.values():
                if window.timer is not None:
                    window.timer.cancel()
            pipeline._INCIDENT_WINDOWS.clear()
            pipeline._ACTIVE_RUN_HOSTS.clear()
            pipeline._ACTIVE_RUN_DEVICES.clear()
            pipeline._FOLLOWUP_QUEUE.clear()

    def _alert(self, eventid, hostid, zbx_host, clock=1700000000):
        text = f"## 主机：{zbx_host}（管理地址 192.0.2.1）\n\n## 告警原文\n{{\"eventid\": \"{eventid}\"}}"
        with mock.patch("netops_ai.api.pipeline.fetch_zabbix_context",
                        return_value=(text, hostid, clock, "", {"triggerid": eventid, "tags": [], "interfaces": []})):
            return pipeline.process_alert({"eventid": eventid, "name": "x"})

    def test_直连邻居的告警并进同一个窗口(self):
        self._alert("5001", "10", "V1-vios")
        result = self._alert("5002", "11", "V2-vios")  # V1 和 V2 直连
        self.assertEqual(result["status"], "buffered_for_window")
        self.assertEqual(list(pipeline._INCIDENT_WINDOWS), ["10"])
        window = pipeline._INCIDENT_WINDOWS["10"]
        self.assertEqual([a.eventid for a in window.pending], ["5002"])
        self.assertEqual(window.devices, {"V1", "V2"})

    def test_不相邻的设备各开各的窗口(self):
        self._alert("5003", "20", "A2-viosl2")
        self._alert("5004", "10", "V1-vios")  # A2 的邻居是 D1，不是 V1
        self.assertEqual(sorted(pipeline._INCIDENT_WINDOWS), ["10", "20"])

    def test_后到的告警把两个相关窗口并成一个(self):
        self._alert("5005", "30", "D1-vios")   # D1、A3 互不直连
        self._alert("5006", "31", "A3-viosl2")
        self.assertEqual(sorted(pipeline._INCIDENT_WINDOWS), ["30", "31"])
        self._alert("5007", "32", "D2-vios")   # D2 同时是 D1 和 A3 的邻居
        self.assertEqual(list(pipeline._INCIDENT_WINDOWS), ["30"])
        window = pipeline._INCIDENT_WINDOWS["30"]
        self.assertEqual({a.eventid for a in [window.first_alert, *window.pending]}, {"5005", "5006", "5007"})
        self.assertEqual(window.devices, {"D1", "A3", "D2"})

    def test_邻居的分析正在跑时排进它的队列(self):
        with pipeline._INCIDENT_WINDOWS_LOCK:
            pipeline._ACTIVE_RUN_HOSTS.add("10")
            pipeline._ACTIVE_RUN_DEVICES["10"] = {"V1"}
        result = self._alert("5008", "11", "V2-vios")
        self.assertEqual(result["status"], "queued_followup")
        self.assertEqual(result["host_key"], "10")
        self.assertEqual(pipeline._FOLLOWUP_QUEUE["10"].devices, {"V2"})

    def test_拓扑里查不到的主机照旧按主机攒批(self):
        self._alert("5009", "40", "unknown-host")
        self._alert("5010", "41", "V1-vios")
        self.assertEqual(sorted(pipeline._INCIDENT_WINDOWS), ["40", "41"])

    def test_其它设备的告警原文会接到提示里(self):
        a1 = pipeline._PendingAlert(eventid="1", payload={}, zabbix_text="T1", hostid="10", clock=0, triggerid="", tags=[],
                                    interfaces=[], name="n", zbx_err="", started_at="", device_name="V1")
        a2 = pipeline._PendingAlert(eventid="2", payload={}, zabbix_text="T2-peer", hostid="11", clock=0, triggerid="", tags=[],
                                    interfaces=[], name="n", zbx_err="", started_at="", device_name="V2")
        a3 = pipeline._PendingAlert(eventid="3", payload={}, zabbix_text="T3-same", hostid="10", clock=0, triggerid="", tags=[],
                                    interfaces=[], name="n", zbx_err="", started_at="", device_name="V1")
        text = pipeline._other_host_alert_text([a1, a2, a3])
        self.assertIn("T2-peer", text)
        self.assertNotIn("T3-same", text)
        self.assertEqual(pipeline._other_host_alert_text([a1, a3]), "")

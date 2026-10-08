"""巡检计划：对话制定（LangGraph 图 + fake 模型）——入口分流（新建 / 调整现有）、文档库查命令（命中 / 未命中 / 索引缺失降级 /
白名单拒绝的候选被标注）、写命令拦截与自动修正、模型不可用降级、三项确认中断 / 恢复 / 退回；保存前再校验、路径安全、
到点调度（注入时钟）、重入保护。不登真实设备、不调真实模型、不连网。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from netops_ai.graph.plan_graph import PlanConversation, build_graph
from netops_ai.inspection import checklist as CL
from netops_ai.inspection import command_lookup as CMD
from netops_ai.inspection import plan_chat as PC
from netops_ai.inspection import plans as P
from netops_ai.llm.client import LLMError
from netops_ai.topology import TopologyDevice, TopologyLink


def _topo() -> dict:
    def dev(name, host, role, links):
        return TopologyDevice(name=name, host=host, role=role,
                              links=tuple(TopologyLink(a, peer, b) for a, peer, b in links))
    return {
        "V1": dev("V1", "192.0.2.50", "core", [("GigabitEthernet0/1", "V2", "GigabitEthernet0/1"), ("GigabitEthernet0/2", "D1", "GigabitEthernet0/1")]),
        "V2": dev("V2", "192.0.2.51", "core", [("GigabitEthernet0/1", "V1", "GigabitEthernet0/1")]),
        "D1": dev("D1", "192.0.2.52", "aggregation", [("GigabitEthernet0/1", "V1", "GigabitEthernet0/2"), ("GigabitEthernet0/4", "A1", "GigabitEthernet0/1")]),
        "A1": dev("A1", "192.0.2.54", "access", [("GigabitEthernet0/1", "D1", "GigabitEthernet0/4")]),
    }


ALL = {"commands": True, "devices": True, "schedule": True}


def _out(**kw) -> dict:
    """模型一轮的结构化输出（strict schema 的全部字段）。"""
    base = {"reply": "好的。", "plan_name": "core-ospf", "description": "", "devices": ["V1", "V2", "D1"],
            "checks": [{"template": "ospf_neighbors", "id": "", "title": "", "command": "", "devices": [], "expect": [], "extract": []},
                       {"template": "interface_errors", "id": "", "title": "", "command": "", "devices": [], "expect": [], "extract": []}],
            "schedule": {"every_minutes": 60, "daily_at": ""}, "enabled": True, "suggestions": [], "ready": False,
            "quick_replies": ["每小时"]}
    base.update(kw)
    return base


def _custom(cmd: str) -> dict:
    return {"template": "", "id": "raw", "title": "raw", "command": cmd, "devices": [], "expect": [], "extract": []}


class FakeLLM:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    def complete(self, messages, **kw):
        self.calls.append(json.loads(json.dumps(messages)))
        o = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        if isinstance(o, Exception):
            raise o
        return SimpleNamespace(parsed=o, content=json.dumps(o, ensure_ascii=False))


class FakeAdapter:
    OUT = {
        "show ip ospf neighbor": "Neighbor ID     Pri   State           Dead Time   Address         Interface\n"
                                 "192.0.2.51        1   FULL/DR         00:00:35    10.0.0.2        GigabitEthernet0/1\n",
        "show processes cpu | include CPU utilization": "CPU utilization for five seconds: 3%/0%; one minute: 2%; five minutes: 1%\n",
    }

    def __init__(self):
        self.sent = []

    def run(self, command):
        from netops_ai.devices.whitelist import check

        v = check("cisco", command)
        if not v.allowed:
            return SimpleNamespace(allowed=False, denial_reason=v.reason, error="", output="")
        self.sent.append(command)
        return SimpleNamespace(allowed=True, denial_reason="", error="", output=self.OUT.get(command, ""))

    def close(self):
        pass


class Tmp(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # Windows 上 SQLite 句柄偶尔晚一步释放
        root = Path(self.td.name)
        for target, value in ((P, ("PLANS_DIR", root / "plans")), (P, ("RUNS_DIR", root / "runs"))):
            p = mock.patch.object(target, *value)
            p.start()
            self.addCleanup(p.stop)
        from netops_ai.api import schedule

        p = mock.patch.object(schedule, "STATE_PATH", root / "state.json")
        p.start()
        self.addCleanup(p.stop)
        P._LAST_ATTEMPT.clear()
        self.addCleanup(self.td.cleanup)
        self.topo = _topo()


# --------------------------------------------------------------------------- 对话


class TestChat(Tmp):
    def chat(self, msgs, draft=None, llm=None, lang="zh", session="s1"):
        self.conv = getattr(self, "conv", None) or PlanConversation(llm_factory=lambda: self.llm, topology_loader=lambda: self.topo)
        self.llm = llm if llm is not None else getattr(self, "llm", None)
        return self.conv.chat(session, msgs, draft, lang)

    def test_开场白不调模型_带快捷回复(self):
        llm = FakeLLM([LLMError("should not be called")])
        r = self.chat([], None, llm=llm)
        self.assertEqual(r["source"], "opening")
        self.assertEqual(len(r["quick_replies"]), 3)
        self.assertEqual(llm.calls, [])
        self.assertIn("4", r["reply"])  # 拓扑设备数
        self.assertTrue(any(s["id"] == "start-core-agg" for s in r["suggestions"]))

    def test_正常多轮_草案补地址_模型看不到地址(self):
        llm = FakeLLM([_out(), _out(ready=True)])
        msgs = [{"role": "user", "content": "核心和汇聚，担心 OSPF 邻居掉线和接口错误，每小时一次"}]
        r1 = self.chat(msgs, None, llm=llm)
        self.assertEqual(r1["source"], "llm")
        self.assertTrue(r1["validation"]["ok"], r1["validation"])
        self.assertFalse(r1["ready"])
        self.assertEqual({d["name"]: d["host"] for d in r1["draft"]["devices"]}["V1"], "192.0.2.50")
        self.assertEqual(r1["draft"]["schedule"], {"every_minutes": 60})
        ids = [c["id"] for c in r1["draft"]["checks"]]
        self.assertEqual(ids, ["ospf_neighbors", "interface_errors"])
        # 模型拿到的上下文里没有任何设备地址
        self.assertFalse(any("192.0.2.5" in m["content"] for m in llm.calls[0]))
        # 建议：核心缺 BGP，规则建议会补上并带 patch
        self.assertTrue(any(s["id"] == "bgp-core" and s["patch"] for s in r1["suggestions"]))
        msgs += [{"role": "assistant", "content": r1["reply"]}, {"role": "user", "content": "可以"}]
        r2 = self.chat(msgs, r1["draft"], llm=llm)
        self.assertTrue(r2["ready"])
        self.assertIn("core-ospf", json.dumps(llm.calls[1][0]["content"]))  # 当前草案带给了模型

    def test_写命令被拦_触发修正(self):
        bad = _out(checks=[_custom("configure terminal")])
        llm = FakeLLM([bad, _out()])
        r = self.chat([{"role": "user", "content": "x"}], None, llm=llm)
        self.assertEqual(r["fix_rounds"], 1)
        self.assertEqual(len(llm.calls), 2)
        self.assertIn("read-only whitelist", llm.calls[1][-1]["content"])  # 问题原文带回给模型
        self.assertTrue(r["validation"]["ok"])
        self.assertNotIn("configure", json.dumps(r["draft"]))

    def test_两次修正仍失败_不放行且写命令不进草案(self):
        llm = FakeLLM([_out(checks=[_custom("clear counters")], ready=True)])
        r = self.chat([{"role": "user", "content": "x"}], None, llm=llm)
        self.assertEqual(r["fix_rounds"], 2)
        self.assertEqual(len(llm.calls), 3)
        self.assertFalse(r["ready"])
        self.assertFalse(r["validation"]["ok"])
        self.assertTrue(any("whitelist" in p for p in r["validation"]["problems"]))
        self.assertNotIn("clear counters", json.dumps(r["draft"]))
        self.assertIn("clear counters", r["reply"])  # 问题原样告诉用户
        # 这份草案也存不进去
        with self.assertRaises(P.PlanError):
            P.save_plan(r["draft"])

    def test_未知设备和模板也会触发修正(self):
        llm = FakeLLM([_out(devices=["X9"], checks=[{**_custom(""), "template": "nope"}]), _out()])
        r = self.chat([{"role": "user", "content": "x"}], None, llm=llm)
        self.assertEqual(r["fix_rounds"], 1)
        self.assertIn("X9", llm.calls[1][-1]["content"])

    def test_模型不可用_规则降级(self):
        llm = FakeLLM([LLMError("LLM_BASE_URL/LLM_API_KEY/LLM_MODEL 没配全")])
        r = self.chat([{"role": "user", "content": "核心设备，担心 OSPF 邻居，每 15 分钟"}], None, llm=llm, lang="zh")
        self.assertEqual(r["source"], "rules")
        self.assertIn("模型不可用，以下是规则推荐", r["reply"])
        self.assertEqual([d["name"] for d in r["draft"]["devices"]], ["V1", "V2"])
        self.assertIn("ospf_neighbors", [c["id"] for c in r["draft"]["checks"]])
        self.assertEqual(r["draft"]["schedule"], {"every_minutes": 15})
        self.assertTrue(r["ready"])
        en = self.chat([{"role": "user", "content": "hourly"}], None, llm=llm, lang="en")
        self.assertIn("model is unavailable", en["reply"])
        self.assertEqual(en["draft"]["schedule"], {"every_minutes": 60})

    def test_模型返回非JSON也降级(self):
        llm = mock.Mock()
        llm.complete.return_value = SimpleNamespace(parsed=None, content="sorry")
        r = self.chat([{"role": "user", "content": "x"}], None, llm=llm)
        self.assertEqual(r["source"], "rules")

    def test_采纳建议并入草案后建议消失(self):
        d = P.recommended_draft(self.topo, ["core", "aggregation", "access"], templates=["interfaces"])
        sugg = {s["id"]: s for s in P.rule_suggestions(d, self.topo)}
        self.assertIn("access-uplink", sugg)
        d2 = P.apply_patch(d, sugg["access-uplink"]["patch"], self.topo)
        up = [c for c in d2["checks"] if c.get("template") == "uplink_errors"]
        self.assertEqual(up[0]["command"], "show interfaces GigabitEthernet0/1 | include line protocol|input errors|output errors")
        self.assertEqual(up[0]["devices"], ["A1"])
        self.assertNotIn("access-uplink", {s["id"] for s in P.rule_suggestions(d2, self.topo)})
        self.assertEqual(CL.validate_checklist(d2), [])

    def test_计数类太频繁有建议(self):
        d = P.recommended_draft(self.topo, ["core"], templates=["interface_errors"], schedule={"every_minutes": 1})
        ids = {s["id"] for s in P.rule_suggestions(d, self.topo)}
        self.assertIn("counter-interval", ids)
        self.assertIn("trend-runs", ids)


# --------------------------------------------------------------------------- 图结构与各条路径


class TestGraph(Tmp):
    def _conv(self, outputs, docs=None):
        self.llm = FakeLLM(outputs) if outputs is not None else None
        return PlanConversation(llm_factory=lambda: self.llm, topology_loader=lambda: self.topo,
                                docs_db=lambda: docs or str(Path(self.td.name) / "no-index.db"))

    def _path(self, conv, session, payload):
        """跑一次图，按顺序记下经过的节点。"""
        cfg = {"configurable": {"thread_id": session}}
        return [next(iter(u)) for u in conv.graph.stream(payload, cfg, stream_mode="updates") if not next(iter(u)).startswith("__")]

    def test_图的节点和边(self):
        g = build_graph(llm_factory=lambda: None, topology_loader=lambda: self.topo).compile().get_graph()
        self.assertEqual(set(g.nodes) - {"__start__", "__end__"},
                         {"route_intent", "opening", "list_plans", "load_plan", "gather_context", "lookup_commands", "propose",
                          "validate", "repair", "respond", "respond_blocked", "rules_fallback", "confirm_plan", "save"})
        edges = {(e.source, e.target, e.conditional) for e in g.edges}
        for src, dst in [("__start__", "route_intent"), ("opening", "__end__"), ("list_plans", "__end__"), ("load_plan", "__end__"),
                         ("lookup_commands", "propose"), ("respond_blocked", "__end__")]:
            self.assertIn((src, dst, False), edges, (src, dst))
        for src, dst in [("route_intent", "opening"), ("route_intent", "list_plans"), ("route_intent", "load_plan"),
                         ("route_intent", "gather_context"), ("gather_context", "lookup_commands"), ("gather_context", "propose"),
                         ("propose", "validate"), ("propose", "rules_fallback"), ("validate", "respond"), ("validate", "repair"),
                         ("validate", "respond_blocked"), ("repair", "validate"), ("repair", "rules_fallback"),
                         ("respond", "confirm_plan"), ("respond", "__end__"), ("rules_fallback", "confirm_plan"),
                         ("rules_fallback", "__end__"), ("confirm_plan", "save"), ("confirm_plan", "propose"),
                         ("confirm_plan", "gather_context"), ("save", "__end__"), ("save", "confirm_plan")]:
            self.assertIn((src, dst, True), edges, (src, dst))
        m = g.draw_mermaid()
        for n in ("route_intent", "lookup_commands", "confirm_plan"):
            self.assertIn(n, m)

    def test_修正环_一次修好(self):
        conv = self._conv([_out(checks=[_custom("configure terminal")]), _out()])
        path = self._path(conv, "a", {"messages": [{"role": "user", "content": "x"}], "draft": None, "lang": "zh"})
        self.assertEqual(path, ["route_intent", "gather_context", "propose", "validate", "repair", "validate", "respond"])

    def test_修正用尽_放弃不放行(self):
        conv = self._conv([_out(checks=[_custom("reload")], ready=True)])
        path = self._path(conv, "b", {"messages": [{"role": "user", "content": "x"}], "draft": None, "lang": "zh"})
        self.assertEqual(path, ["route_intent", "gather_context", "propose", "validate", "repair", "validate", "repair", "validate",
                                "respond_blocked"])
        self.assertFalse(conv.awaiting_confirm("b"))
        with self.assertRaises(P.PlanError):
            conv.confirm("b")

    def test_降级_模型不可用(self):
        conv = self._conv(None)  # llm_factory 返回 None = 没配模型
        path = self._path(conv, "c", {"messages": [{"role": "user", "content": "核心，每小时"}], "draft": None, "lang": "zh"})
        self.assertEqual(path, ["route_intent", "gather_context", "propose", "rules_fallback"])
        # 规则草案也能手工确认：图停在 confirm_plan 的中断上
        self.assertEqual(conv.graph.get_state({"configurable": {"thread_id": "c"}}).next, ("confirm_plan",))
        self.assertTrue(conv.view("c")["validation"]["ok"])

    def test_修正时模型挂了也降级(self):
        conv = self._conv([_out(checks=[_custom("reload")]), LLMError("boom")])
        path = self._path(conv, "d", {"messages": [{"role": "user", "content": "x"}], "draft": None, "lang": "zh"})
        self.assertEqual(path[:6], ["route_intent", "gather_context", "propose", "validate", "repair", "rules_fallback"])

    def test_开场白(self):
        conv = self._conv([LLMError("should not be called")])
        self.assertEqual(self._path(conv, "e", {"messages": [], "draft": None, "lang": "zh"}), ["route_intent", "opening"])
        self.assertEqual(self.llm.calls, [])

    def test_确认中断_恢复后保存并登记调度(self):
        conv = self._conv([_out(ready=True)])
        r = conv.chat("f", [{"role": "user", "content": "x"}], None, "zh")
        self.assertTrue(r["ready"])
        self.assertTrue(r["awaiting_confirm"])
        self.assertFalse((Path(P.PLANS_DIR) / "core-ospf.yaml").exists())  # 没点确认就不写
        draft = {**r["draft"], "schedule": {"every_minutes": 15}}  # 页面上改了周期
        self.assertEqual(r["confirm_card"]["schedule"], {"every_minutes": 60})
        self.assertEqual([c["device"] for c in r["confirm_card"]["commands"]], ["V1", "V2", "D1"])
        self.assertIn("show ip ospf neighbor", [x["command"] for x in r["confirm_card"]["commands"][0]["commands"]])
        done = conv.confirm("f", draft, ALL)
        self.assertFalse(done["awaiting_confirm"])
        self.assertEqual(done["saved"]["name"], "core-ospf")
        self.assertIsNotNone(done["saved"]["next_run_at"])
        self.assertEqual(P.load_plan("core-ospf")["schedule"], {"every_minutes": 15})
        self.assertEqual([p["name"] for p in P.list_plans() if p["enabled"]], ["core-ospf"])

    def test_确认时被篡改成写命令_保存节点再校验拒绝(self):
        conv = self._conv([_out(ready=True)])
        r = conv.chat("g", [{"role": "user", "content": "x"}], None, "zh")
        bad = {**r["draft"], "checks": r["draft"]["checks"] + [{"id": "w", "command": "write memory"}]}
        done = conv.confirm("g", bad, ALL)
        self.assertIsNotNone(done["save_error"])
        self.assertTrue(any("whitelist" in p for p in done["save_error"]["problems"]))
        self.assertEqual(P.list_plans(), [])

    def test_重名可改名再确认(self):
        conv = self._conv([_out(ready=True)])
        r = conv.chat("h", [{"role": "user", "content": "x"}], None, "zh")
        P.save_plan(r["draft"])  # 同名计划已存在
        done = conv.confirm("h", r["draft"], ALL)
        self.assertEqual(done["save_error"]["status"], 409)
        self.assertTrue(done["awaiting_confirm"])  # 回到确认环节
        done = conv.confirm("h", {**r["draft"], "name": "core-ospf-2"}, ALL)
        self.assertEqual(done["saved"]["name"], "core-ospf-2")

    def test_停在确认环节时继续聊_回到提案(self):
        conv = self._conv([_out(ready=True), _out(ready=False, reply="再加 BGP？")])
        msgs = [{"role": "user", "content": "x"}]
        r = conv.chat("i", msgs, None, "zh")
        self.assertTrue(r["awaiting_confirm"])
        msgs += [{"role": "assistant", "content": r["reply"]}, {"role": "user", "content": "等等，再想想"}]
        r2 = conv.chat("i", msgs, r["draft"], "zh")
        self.assertEqual(r2["reply"], "再加 BGP？")
        self.assertFalse(r2["awaiting_confirm"])
        self.assertEqual(len(self.llm.calls), 2)
        self.assertEqual(self.llm.calls[1][-1]["content"], "等等，再想想")


# --------------------------------------------------------------------------- 入口分流 / 文档库查命令 / 三项确认


def _docs_index(root: Path) -> Path:
    """临时文档库索引：一段讲 OSPF 邻居的说明（含一条能用的命令和一条被白名单拒绝的命令）。"""
    from netops_ai.docs_kb.ingest import ingest

    corpus = root / "corpus"
    corpus.mkdir()
    (corpus / "ospf-guide.md").write_text(
        "# OSPF troubleshooting\n\n## Verify neighbors\n\n"
        "To verify that OSPF neighbors are up, run show ip ospf neighbor detail and look at the state column. "
        "Some guides save the output with show ip ospf neighbor | tee flash:ospf.txt before a change.\n",
        encoding="utf-8")
    db = root / "kb.db"
    ingest(corpus, db)
    return db


class TestIntentLookupConfirm(TestGraph):
    def _saved_plan(self, name="core-health"):
        d = P.recommended_draft(self.topo, devices=["V1", "V2"], templates=["ospf_neighbors", "cpu"], schedule={"every_minutes": 60})
        d["name"] = name
        P.save_plan(d)
        return d

    # ---- route_intent 两条路径
    def test_调整路径_先列计划_再加载成草案_改完覆盖原计划(self):
        self._saved_plan()
        conv = self._conv([_out(plan_name="whatever", devices=["V1", "V2"], schedule={"every_minutes": 15, "daily_at": ""}, ready=True,
                                checks=[{"template": "ospf_neighbors", "id": "", "title": "", "command": "", "devices": [], "expect": [], "extract": []}])])
        path = self._path(conv, "e1", {"messages": [], "draft": None, "lang": "zh", "mode": "edit", "plan": ""})
        self.assertEqual(path, ["route_intent", "list_plans"])
        v = conv.view("e1")
        self.assertIn("core-health", v["reply"])
        self.assertIn("core-health", v["quick_replies"])
        self.assertEqual(v["plans"][0]["checks"], ["ospf_neighbors", "cpu"])
        # 用户点了计划名
        msgs = [{"role": "assistant", "content": v["reply"]}, {"role": "user", "content": "core-health"}]
        r = conv.chat("e1", msgs, None, "zh", mode="edit")
        self.assertEqual(r["editing"], "core-health")
        self.assertEqual(r["draft"]["name"], "core-health")
        self.assertIn("想改什么", r["reply"])
        self.assertEqual(self.llm.calls, [])  # 加载计划不调模型
        msgs += [{"role": "assistant", "content": r["reply"]}, {"role": "user", "content": "去掉 CPU，改成每 15 分钟"}]
        r = conv.chat("e1", msgs, r["draft"], "zh", mode="edit")
        self.assertIn('"editing_plan": "core-health"', self.llm.calls[0][0]["content"])
        self.assertEqual(r["draft"]["name"], "core-health")  # 模型想改名也不行，调整就是存回原计划
        self.assertTrue(r["awaiting_confirm"])
        done = conv.confirm("e1", r["draft"], ALL)
        self.assertIsNone(done["save_error"])
        plan = P.load_plan("core-health")
        self.assertEqual(plan["schedule"], {"every_minutes": 15})
        self.assertEqual([c["id"] for c in plan["checks"]], ["ospf_neighbors"])

    def test_调整路径_网页带计划名直接加载(self):
        self._saved_plan()
        conv = self._conv([LLMError("not called")])
        path = self._path(conv, "e2", {"messages": [], "draft": None, "lang": "en", "mode": "edit", "plan": "core-health"})
        self.assertEqual(path, ["route_intent", "load_plan"])
        self.assertIn("Loaded plan core-health", conv.view("e2")["reply"])

    def test_新建面板里说调整某计划_转到调整路径(self):
        self._saved_plan()
        conv = self._conv([LLMError("not called")])
        path = self._path(conv, "e3", {"messages": [{"role": "user", "content": "调整一下 core-health"}], "draft": None, "lang": "zh"})
        self.assertEqual(path, ["route_intent", "load_plan"])

    def test_调整路径_没找到计划名_再列一次(self):
        self._saved_plan()
        conv = self._conv([LLMError("not called")])
        r = conv.chat("e4", [{"role": "user", "content": "nope"}], None, "zh", mode="edit")
        self.assertIn("没找到", r["reply"])
        self.assertEqual(r["source"], "plans")

    # ---- lookup_commands
    def test_查命令_文档库命中_候选带出处_被拒的标注(self):
        db = _docs_index(Path(self.td.name))
        conv = self._conv([_out(reply="候选命令列在下面。")], docs=str(db))
        path = self._path(conv, "l1", {"messages": [{"role": "user", "content": "不记得命令了，想看 OSPF 邻居有没有掉"}], "draft": None, "lang": "zh"})
        self.assertEqual(path[:4], ["route_intent", "gather_context", "lookup_commands", "propose"])
        v = conv.view("l1")
        self.assertEqual(v["docs_status"], "ok")
        cands = {c["command"]: c for c in v["candidates"]}
        self.assertIn("show ip ospf neighbor detail", cands)
        self.assertEqual(cands["show ip ospf neighbor detail"]["origin"], "docs")
        self.assertIn("ospf-guide.md", cands["show ip ospf neighbor detail"]["source"])
        self.assertIn("Verify neighbors", cands["show ip ospf neighbor detail"]["source"])
        refused = cands["show ip ospf neighbor | tee flash:ospf.txt"]
        self.assertFalse(refused["allowed"])
        self.assertIn("只读白名单不允许", refused["reason"])
        self.assertIn("show ip ospf neighbor", cands)  # 内置命令目录的也在
        # 候选交给模型时标了能不能用，且明确不让模型自己加进草案
        self.assertIn("command_candidates", self.llm.calls[0][0]["content"])
        self.assertNotIn("show ip ospf neighbor detail", json.dumps(v["draft"]))  # 用户选之前不进草案

    def test_查命令_文档库没命中_只用内置目录并说明(self):
        db = _docs_index(Path(self.td.name))
        r = CMD.lookup("CPU 有没有冲高", db_path=db)
        self.assertEqual(r["docs_status"], "ok")
        self.assertIn("文档库里没有检索到", r["note"])
        r = CMD.lookup("看看风扇转速 fan speed", db_path=db)
        self.assertEqual(r["candidates"], [])
        self.assertIn("没有找到合适的候选命令", r["note"])

    def test_查命令_索引缺失或模块缺失_优雅降级(self):
        r = CMD.lookup("看 OSPF 邻居有没有掉", db_path=Path(self.td.name) / "missing.db")
        self.assertEqual(r["docs_status"], "missing")
        self.assertIn("文档库里没有收录", r["note"])
        self.assertIn("show ip ospf neighbor", [c["command"] for c in r["candidates"]])
        with mock.patch.dict("sys.modules", {"netops_ai.docs_kb.search": None}):
            r = CMD.lookup("看 OSPF 邻居有没有掉", db_path=_docs_index(Path(self.td.name)))
        self.assertEqual(r["docs_status"], "missing")
        self.assertTrue(r["candidates"])
        empty = Path(self.td.name) / "empty.db"
        import sqlite3

        sqlite3.connect(empty).close()
        self.assertEqual(CMD.docs_status(empty), "empty")

    def test_什么时候查命令(self):
        self.assertTrue(CMD.wants_lookup("看 OSPF 邻居有没有掉"))
        self.assertTrue(CMD.wants_lookup("接口错误有没有在涨"))
        self.assertTrue(CMD.wants_lookup("I don't remember the command for BGP"))
        self.assertFalse(CMD.wants_lookup("核心和汇聚设备，主要担心 OSPF 邻居掉线，每小时一次"))
        self.assertFalse(CMD.wants_lookup("加一条 show ip ospf neighbor 看看"))

    def test_选用候选命令才并入草案(self):
        d = P.recommended_draft(self.topo, devices=["V1"], templates=["cpu"])
        d2 = P.apply_patch(d, {"add_custom_checks": [{"command": "show ip ospf neighbor detail", "devices": ["V1"]}]}, self.topo)
        self.assertIn("show ip ospf neighbor detail", [c["command"] for c in d2["checks"]])
        self.assertEqual(CL.validate_checklist(d2), [])
        d3 = P.apply_patch(d2, {"add_custom_checks": [{"command": "show ip ospf neighbor | tee flash:x"}]}, self.topo)
        clean, removed = P.sanitize(d3)
        self.assertTrue(removed)
        self.assertNotIn("tee", json.dumps(clean))
        d4 = P.apply_patch(d2, {"remove_devices": ["V1"], "remove_checks": ["cpu"], "enabled": False}, self.topo)
        self.assertEqual((d4["devices"], d4["checks"], d4["enabled"]), ([], [], False))

    def test_用户没让删_模型丢掉的检查项补回来(self):
        # 真实走查里撞到的：用户刚采纳了 CPU 检查，下一轮模型重写草案时把它丢了
        conv = self._conv([_out(), _out()])
        r = conv.chat("k1", [{"role": "user", "content": "x"}], None, "zh")
        d = P.apply_patch(r["draft"], {"add_checks": [{"template": "cpu", "devices": None}]}, self.topo)
        r = conv.chat("k1", [{"role": "user", "content": "x"}, {"role": "user", "content": "周期就每小时，不用改"}], d, "zh")
        self.assertIn("cpu", [c["id"] for c in r["draft"]["checks"]])
        conv = self._conv([_out(), _out()])
        r = conv.chat("k2", [{"role": "user", "content": "x"}], None, "zh")
        d = P.apply_patch(r["draft"], {"add_checks": [{"template": "cpu", "devices": None}]}, self.topo)
        r = conv.chat("k2", [{"role": "user", "content": "x"}, {"role": "user", "content": "去掉 CPU"}], d, "zh")
        self.assertNotIn("cpu", [c["id"] for c in r["draft"]["checks"]])

    # ---- confirm_plan
    def test_三项缺一不可_退回propose(self):
        conv = self._conv([_out(ready=True), _out(ready=False, reply="周期想改成多少？")])
        r = conv.chat("c1", [{"role": "user", "content": "x"}], None, "zh")
        self.assertTrue(r["awaiting_confirm"])
        cfg = {"configurable": {"thread_id": "c1"}}
        from langgraph.types import Command

        updates = [next(iter(u)) for u in conv.graph.stream(
            Command(resume={"action": "confirm", "draft": r["draft"], "confirmed": {"commands": True, "devices": True}}), cfg,
            stream_mode="updates")]
        self.assertEqual(updates[:3], ["confirm_plan", "propose", "validate"])
        self.assertNotIn("save", updates)
        self.assertEqual(P.list_plans(), [])
        self.assertIn("我还没确认：实施周期", self.llm.calls[1][-1]["content"])
        self.assertEqual(conv.view("c1")["reply"], "周期想改成多少？")

    def test_三项任一没确认都不保存(self):
        for missing in ("commands", "devices", "schedule"):
            with self.subTest(missing=missing):
                conv = self._conv([_out(ready=True), _out(ready=False)])
                r = conv.chat("c-" + missing, [{"role": "user", "content": "x"}], None, "zh")
                done = conv.confirm("c-" + missing, r["draft"], {**ALL, missing: False})
                self.assertIsNone(done["saved"])
                self.assertEqual(P.list_plans(), [])
                self.assertEqual(len(self.llm.calls), 2)  # 回到 propose 又问了一次模型

    def test_确认环节直接继续聊_回到gather_context(self):
        conv = self._conv([_out(ready=True), _out()])
        r = conv.chat("c2", [{"role": "user", "content": "x"}], None, "zh")
        cfg = {"configurable": {"thread_id": "c2"}}
        from langgraph.types import Command

        updates = [next(iter(u)) for u in conv.graph.stream(
            Command(resume={"action": "chat", "messages": [{"role": "user", "content": "再加点东西"}], "draft": r["draft"]}), cfg,
            stream_mode="updates")]
        self.assertEqual(updates[:3], ["confirm_plan", "gather_context", "propose"])


# --------------------------------------------------------------------------- 存储 / 路径 / 调度 / 重入


class TestStoreAndSchedule(Tmp):
    def _plan(self, name="p1", every=15, now=None):
        d = P.recommended_draft(self.topo, devices=["V1"], templates=["ospf_neighbors", "cpu"], schedule={"every_minutes": every})
        d["name"] = name
        return P.save_plan(d, now=now)

    def test_保存前再校验_写命令拒绝且不落盘(self):
        d = P.recommended_draft(self.topo, devices=["V1"], templates=["cpu"])
        d["name"] = "bad"
        d["checks"].append({"id": "w", "command": "write memory"})
        with self.assertRaises(P.PlanError) as cm:
            P.save_plan(d)
        self.assertTrue(any("whitelist" in p for p in cm.exception.problems))
        self.assertFalse((Path(P.PLANS_DIR) / "bad.yaml").exists())

    def test_保存_重名409_YAML可被CLI读(self):
        self._plan()
        with self.assertRaises(P.PlanError) as cm:
            self._plan()
        self.assertEqual(cm.exception.status, 409)
        data = CL.load_checklist(Path(P.PLANS_DIR) / "p1.yaml")
        self.assertEqual(CL.validate_checklist(data), [])
        self.assertEqual(data["schedule"], {"every_minutes": 15})
        self.assertTrue(data["enabled"])

    def test_路径安全(self):
        for bad in ("../x", "a/b", "..", ".hidden", "a b", "", "x" * 65, "c:\\x"):
            with self.assertRaises(P.PlanError):
                P.plan_path(bad)
        self.assertEqual(P.plan_path("ok_1.a-b").name, "ok_1.a-b.yaml")

    def test_到点触发_注入时钟(self):
        t0 = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
        self._plan(every=15, now=t0)
        self.assertEqual(P.due_plans(t0 + timedelta(minutes=10)), [])
        self.assertEqual(P.due_plans(t0 + timedelta(minutes=15)), ["p1"])
        ran = []
        started = self._tick(t0 + timedelta(minutes=16), ran)
        self.assertEqual(started, ["p1"])
        self.assertEqual(ran, ["p1"])
        # 跑过之后锚点变成这次的尝试时刻
        self.assertEqual(P.due_plans(t0 + timedelta(minutes=20)), [])
        self.assertEqual(P.due_plans(t0 + timedelta(minutes=31)), ["p1"])
        # 暂停就不再到点
        P.update_plan("p1", enabled=False, now=t0)
        self.assertEqual(P.due_plans(t0 + timedelta(hours=5)), [])

    def _tick(self, now, ran):
        from netops_ai.api import schedule

        def runner(name):
            # 真跑会按墙上时钟记 started_at；这里只验到点逻辑，用注入的时刻记一次「尝试」
            ran.append(name)
            P._LAST_ATTEMPT[name] = now
        return schedule.plan_tick(now, spawn=lambda fn: fn(), runner=runner)

    def test_每天定时(self):
        t0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        plan = {"enabled": True, "schedule": {"daily_at": "03:30"}, "enabled_at": t0.isoformat()}
        nxt = P.next_run(plan, None, t0)
        local = nxt.astimezone()
        self.assertEqual((local.hour, local.minute), (3, 30))
        self.assertTrue(timedelta(0) < nxt - t0 <= timedelta(days=1))

    def test_运行存结果_状态写进schedule_state且保留原格式(self):
        from netops_ai.api import schedule

        self._plan()
        schedule._write_state({"at": "x", "jobs": {"inspection": {"ok": True, "note": "巡检完成"}}})
        entry = schedule.run_plan_and_record("p1", lambda dev: FakeAdapter())
        self.assertTrue(entry["ok"], entry)
        st = schedule.load_state()
        self.assertEqual(st["jobs"]["inspection"]["note"], "巡检完成")
        self.assertIn("p1", st["plans"])
        hist = P.history("p1")
        self.assertEqual(len(hist), 1)
        checks = {c["id"]: c for c in hist[0]["devices"][0]["checks"]}
        self.assertEqual(checks["ospf_neighbors"]["metrics"], {"ospf_full": 1})
        self.assertEqual(checks["cpu"]["metrics"]["cpu_5m"], 1)
        n, ok, note = schedule.plans_status()
        self.assertEqual((n, ok), (1, True))
        self.assertIn("巡检计划 1 个启用", note)

    def test_重入保护(self):
        from netops_ai.api import schedule

        self._plan()
        lock = P._lock("p1")
        lock.acquire()
        try:
            with self.assertRaises(P.PlanBusy):
                P.run_plan("p1", lambda dev: FakeAdapter())
            far = datetime.now(timezone.utc) + timedelta(days=2)
            self.assertEqual(P.due_plans(far), [])  # 正在跑的不算到点
            self.assertEqual(schedule.plan_tick(far, spawn=lambda fn: fn(), runner=lambda n: None), [])
        finally:
            lock.release()

    def test_并发两次只有一次真的跑(self):
        self._plan()
        gate, entered = threading.Event(), threading.Event()

        class Slow(FakeAdapter):
            def run(self, command):
                entered.set()
                gate.wait(5)
                return super().run(command)

        t = threading.Thread(target=lambda: P.run_plan("p1", lambda dev: Slow()))
        t.start()
        entered.wait(5)
        with self.assertRaises(P.PlanBusy):
            P.run_plan("p1", lambda dev: FakeAdapter())
        gate.set()
        t.join(5)
        self.assertEqual(len(P.history("p1")), 1)


# --------------------------------------------------------------------------- 接口


class TestApi(Tmp):
    def setUp(self):
        super().setUp()
        from netops_ai.api import plans as api_plans
        from netops_ai.api.app import app

        p = mock.patch.object(api_plans, "_topology", return_value=self.topo)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(app)

    def test_对话接口_模型不可用也能出草案(self):
        with mock.patch("netops_ai.llm.client.LLMClient.complete", side_effect=LLMError("not configured")):
            r = self.client.post("/api/inspection/plans/chat", json={"messages": [{"role": "user", "content": "核心，每小时"}], "lang": "zh"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(set(body) >= {"reply", "draft", "validation", "ready", "suggestions"}, True)
        self.assertEqual(body["source"], "rules")
        self.assertTrue(body["validation"]["ok"])
        self.assertTrue(body["awaiting_confirm"])
        # 确认：从图的中断处恢复 → 保存
        r = self.client.post("/api/inspection/plans/confirm", json={"session": body["session"], "draft": body["draft"], "confirmed": ALL})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["saved"]["name"], body["draft"]["name"])
        self.assertTrue((Path(P.PLANS_DIR) / f"{body['draft']['name']}.yaml").exists())
        # 再点一次：图已经不在确认环节
        self.assertEqual(self.client.post("/api/inspection/plans/confirm", json={"session": body["session"]}).status_code, 409)

    def test_保存列表启停运行历史(self):
        d = P.recommended_draft(self.topo, devices=["V1"], templates=["cpu"], schedule={"every_minutes": 60})
        d["name"] = "api1"
        r = self.client.post("/api/inspection/plans", json={"draft": d})
        self.assertEqual(r.status_code, 200, r.text)
        bad = {**d, "name": "api2", "checks": d["checks"] + [{"id": "w", "command": "reload"}]}
        r = self.client.post("/api/inspection/plans", json={"draft": bad})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.json()["problems"])
        rows = self.client.get("/api/inspection/plans").json()
        self.assertEqual([x["name"] for x in rows], ["api1"])
        self.assertIsNotNone(rows[0]["next_run_at"])
        r = self.client.patch("/api/inspection/plans/api1", json={"enabled": False, "schedule": {"every_minutes": 15}})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["enabled"])
        self.assertIsNone(r.json()["next_run_at"])
        r = self.client.patch("/api/inspection/plans/api1", json={"schedule": {"every_minutes": 0, "daily_at": "25:00"}})
        self.assertEqual(r.status_code, 400)
        with mock.patch("netops_ai.topology._default_adapter_factory", lambda dev: FakeAdapter()):
            from netops_ai.api import schedule

            schedule.run_plan_and_record("api1")
        runs = self.client.get("/api/inspection/plans/api1/runs").json()["runs"]
        self.assertEqual(len(runs), 1)
        with mock.patch("netops_ai.llm.client.LLMClient.complete", side_effect=LLMError("not configured")):
            t = self.client.post("/api/inspection/plans/api1/trend", json={}).json()
        self.assertFalse(t["llm"])
        self.assertIn("cpu_5m", t["table"])
        self.assertEqual(t["messages"][0]["role"], "system")

    def test_立即运行重入409(self):
        d = P.recommended_draft(self.topo, devices=["V1"], templates=["cpu"])
        d["name"] = "busy"
        P.save_plan(d)
        lock = P._lock("busy")
        lock.acquire()
        try:
            self.assertEqual(self.client.post("/api/inspection/plans/busy/run").status_code, 409)
        finally:
            lock.release()

    def test_接口路径安全(self):
        from netops_ai.api import plans as api_plans

        # 带 %2F 的路径在路由层就匹配不上（4xx），根本到不了处理函数；处理函数自己也拒绝穿越的名字
        self.assertTrue(400 <= self.client.patch("/api/inspection/plans/..%2F..%2Fetc", json={"enabled": True}).status_code < 500)
        self.assertEqual(api_plans.api_patch_plan("../../etc", api_plans.PlanPatchRequest(enabled=True)).status_code, 400)
        self.assertEqual(api_plans.api_plan_runs("..\\x").status_code, 400)
        self.assertEqual(self.client.post("/api/inspection/plans/a%20b/run").status_code, 400)
        self.assertEqual(self.client.get("/api/inspection/plans/nope/runs").status_code, 404)

    def test_草案接口_采纳建议(self):
        d = P.recommended_draft(self.topo, ["core"], templates=["interfaces"])
        r = self.client.post("/api/inspection/plans/draft", json={"draft": d, "patch": {"add_checks": [{"template": "bgp_sessions", "devices": ["V1"]}]}})
        self.assertEqual(r.status_code, 200)
        self.assertIn("bgp_sessions", [c["id"] for c in r.json()["draft"]["checks"]])
        # 页面上还挂着两条效果相同的 CPU 建议：采纳其中一条后，另一条报告为已无效果
        same = [{"id": "m-cpu", "patch": {"add_checks": [{"template": "cpu", "devices": ["V1", "V2"]}]}},
                {"id": "cpu-all", "patch": {"add_checks": [{"template": "cpu", "devices": None}]}},
                {"id": "bgp", "patch": {"add_checks": [{"template": "bgp_sessions", "devices": ["V2"]}]}}]
        r = self.client.post("/api/inspection/plans/draft", json={"draft": d, "patch": same[0]["patch"], "suggestions": same}).json()
        self.assertEqual(r["live_suggestions"], ["bgp"])
        # 草案里混进写命令：接口直接拿掉并报出来
        d["checks"].append({"id": "w", "command": "configure terminal"})
        r = self.client.post("/api/inspection/plans/draft", json={"draft": d}).json()
        self.assertFalse(r["validation"]["ok"])
        self.assertNotIn("configure", json.dumps(r["draft"]))


# --------------------------------------------------------------------------- 清单格式扩展


class TestChecklistExtensions(unittest.TestCase):
    def test_agg_与设备范围(self):
        out = "1 input errors\n2 input errors\n FULL/DR \n FULL/BDR \n"
        self.assertEqual(CL._extract({"regex": r"(\d+) input errors", "agg": "sum", "cast": "int"}, out), 3)
        self.assertEqual(CL._extract({"regex": r"(\d+) input errors", "agg": "max", "cast": "int"}, out), 2)
        self.assertEqual(CL._extract({"regex": r"(FULL)/", "agg": "count"}, out), 2)
        self.assertEqual(CL._extract({"regex": r"(\d+) CRC", "agg": "count"}, out), 0)
        cl = {"name": "s", "devices": [{"name": "D1", "host": "192.0.2.10"}, {"name": "D2", "host": "192.0.2.11"}],
              "checks": [{"id": "a", "command": "show clock", "devices": ["D2"]}, {"id": "b", "command": "show clock"}]}
        self.assertEqual(CL.validate_checklist(cl), [])
        res = CL.run_checklist(cl, lambda dev: FakeAdapter())
        self.assertEqual([c["id"] for c in res["devices"][0]["checks"]], ["b"])
        self.assertEqual([c["id"] for c in res["devices"][1]["checks"]], ["a", "b"])
        cl["checks"][0]["devices"] = ["D9"]
        self.assertTrue(any("D9" in p for p in CL.validate_checklist(cl)))

    def test_历史按名字精确筛(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("core", "core-health"):
                cl = {"name": name, "devices": [{"name": "D1", "host": "192.0.2.10"}], "checks": [{"id": "a", "command": "show clock"}]}
                CL.save_result(CL.run_checklist(cl, lambda dev: FakeAdapter()), tmp)
            self.assertEqual([r["checklist"] for r in CL.load_history(tmp, "core")], ["core"])


if __name__ == "__main__":
    unittest.main()

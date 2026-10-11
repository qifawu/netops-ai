"""巡检助手自己写判定脚本：模板表达不了的需求 → 采真实样本 → 写 → 沙箱自测 → 审核气泡 → 批准后才能保存。
全程 fake LLM + fake 采样，不连网、不连设备。"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from netops_ai.inspection import check_script as CS
from netops_ai.inspection import plan_widgets as W
from netops_ai.inspection import plans as P
from tests.test_check_script import DIFF_SCRIPT, SAMPLE
from tests.test_inspection_plans import TestGraph as _Base
from tests.test_inspection_plans import _out

LAZY = "def evaluate(ctx):\n    return {'status': 'pass', 'summary': 'ok'}"


class ScriptLLM:
    """一个对象两种用途：plan 回合（结构化 plan 输出）和写脚本（返回 {source, explanation}）。按调用顺序吐。"""

    def __init__(self, plan_outputs, sources):
        self.plan_outputs, self.sources, self.calls = list(plan_outputs), list(sources), []

    def complete(self, messages, **kw):
        self.calls.append(messages)
        sys_prompt = messages[0]["content"]
        if "write ONE small Python function" in sys_prompt:
            src = self.sources.pop(0) if len(self.sources) > 1 else self.sources[0]
            from tests.script_fixtures import TESTS

            return SimpleNamespace(parsed=None, content=json.dumps({"source": src, "explanation": "对比两次输出的条目", "tests": TESTS},
                                                                   ensure_ascii=False))
        o = self.plan_outputs.pop(0) if len(self.plan_outputs) > 1 else self.plan_outputs[0]
        return SimpleNamespace(parsed=o, content=json.dumps(o))


def _req(**kw):
    base = {"check_id": "route-diff", "title": "路由表明细对比", "command": "show ip route | begin Gateway",
            "goal": "路由表跟上一次逐条比，少了哪条路由要报警；路由年龄不算变化"}
    base.update(kw)
    return base


class TestScriptFlow(_Base):
    def setUp(self):
        super().setUp()
        from netops_ai.inspection import plan_chat as PC

        self._old = PC.SCRIPT_AUTHORING
        PC.SCRIPT_AUTHORING = True  # 总开关默认关；这里验证打开后的整条链路
        self.addCleanup(setattr, PC, "SCRIPT_AUTHORING", self._old)

    def _conv(self, outputs, docs=None, sources=(DIFF_SCRIPT,), sample=SAMPLE):
        from netops_ai.graph.plan_graph import PlanConversation

        self.sampled = []

        def sample_runner(command, host, name):
            self.sampled.append((command, host, name))
            if isinstance(sample, Exception):
                raise sample
            return sample

        self.llm = ScriptLLM(outputs, list(sources))
        return PlanConversation(llm_factory=lambda: self.llm, topology_loader=lambda: self.topo,
                                docs_db=lambda: None, sample_runner=sample_runner)

    # 只借基类的 setUp（拓扑 / 临时计划目录），不重跑它那一批用例
    test_图的节点和边 = None
    test_降级_模型不可用 = None

    def _turn(self, conv, sid="s1", text="我要路由表逐条跟上一次比，少了哪条要报警"):
        out = _out(script_requests=[_req()], checks=[], devices=["V1"], schedule={"every_minutes": 60, "daily_at": ""})
        return conv.chat(sid, [{"role": "user", "content": text}], None, "zh")

    def test_需求模板接不住_助手采样本写脚本_出审核气泡_草案里是未批准的脚本检查项(self):
        conv = self._conv([_out(script_requests=[_req()], checks=[], devices=["V1"], schedule={"every_minutes": 60, "daily_at": ""})])
        r = self._turn(conv)
        self.assertEqual(self.sampled[0][0], "show ip route | begin Gateway")
        chk = next(c for c in r["draft"]["checks"] if c["id"] == "route-diff")
        e = chk["expect"][0]
        self.assertEqual((e["type"], e["approved"], e["sha256"]), ("script", False, CS.sha256(DIFF_SCRIPT)))
        self.assertEqual(r["widget"]["type"], "script_review")
        item = r["widget"]["items"][0]
        self.assertTrue(item["ok"])
        self.assertIn("203.0.113.0/25", item["sample_head"])
        self.assertIn("一条路由消失", [c["name"] for c in item["cases"]])
        self.assertFalse(r["ready"])  # 没批准，进不了确认环节

    def test_批准_才能进入确认_批准时重算sha(self):
        conv = self._conv([_out(script_requests=[_req()], checks=[], devices=["V1"], schedule={"every_minutes": 60, "daily_at": ""})])
        r = self._turn(conv)
        w = r["widget"]
        r2 = conv.chat("s1", [{"role": "user", "content": "x"}], r["draft"], "zh",
                       selection={"widget_id": w["id"], "type": "script_review", "value": {"check_id": "route-diff", "action": "approve"}})
        e = next(c for c in r2["draft"]["checks"] if c["id"] == "route-diff")["expect"][0]
        self.assertTrue(e["approved"])
        from netops_ai.inspection import checklist as CL

        self.assertEqual([p for p in CL.validate_checklist({"name": "p", "vendor": "cisco", "devices": r2["draft"]["devices"],
                                                            "checks": r2["draft"]["checks"]})], [])

    def test_拒绝_脚本检查项拿掉_以后不再补回(self):
        conv = self._conv([_out(script_requests=[_req()], checks=[], devices=["V1"], schedule={"every_minutes": 60, "daily_at": ""})])
        r = self._turn(conv)
        r2 = conv.chat("s1", [{"role": "user", "content": "x"}], r["draft"], "zh",
                       selection={"widget_id": r["widget"]["id"], "type": "script_review",
                                  "value": {"check_id": "route-diff", "action": "reject"}})
        self.assertFalse(any(c["id"] == "route-diff" for c in r2["draft"]["checks"]))

    def test_自测没过的脚本不能批准(self):
        conv = self._conv([_out(script_requests=[_req()], checks=[], devices=["V1"], schedule={"every_minutes": 60, "daily_at": ""})],
                          sources=[LAZY])
        r = self._turn(conv)
        self.assertFalse(r["widget"]["items"][0]["ok"])
        r2 = conv.chat("s1", [{"role": "user", "content": "x"}], r["draft"], "zh",
                       selection={"widget_id": r["widget"]["id"], "type": "script_review",
                                  "value": {"check_id": "route-diff", "action": "approve"}})
        e = next(c for c in r2["draft"]["checks"] if c["id"] == "route-diff")["expect"][0]
        self.assertFalse(e["approved"])

    def test_采不到样本_如实说_不编脚本(self):
        conv = self._conv([_out(script_requests=[_req()], checks=[], devices=["V1"], schedule={"every_minutes": 60, "daily_at": ""})],
                          sample=RuntimeError("connection timed out"))
        r = self._turn(conv)
        self.assertFalse(any(c["id"] == "route-diff" for c in r["draft"]["checks"]))
        self.assertIn("采样本失败", r["reply"])

    def test_命令是写命令_被白名单拒绝_不去设备上跑(self):
        conv = self._conv([_out(script_requests=[_req(command="clear ip route *")], checks=[], devices=["V1"],
                                schedule={"every_minutes": 60, "daily_at": ""})])
        r = self._turn(conv)
        self.assertEqual(self.sampled, [])
        self.assertIn("白名单", r["reply"])

    def test_下一轮模型整份重写草案_脚本检查项原样保留(self):
        conv = self._conv([_out(script_requests=[_req()], checks=[], devices=["V1"], schedule={"every_minutes": 60, "daily_at": ""}),
                           _out(checks=[], devices=["V1"], schedule={"every_minutes": 60, "daily_at": ""})])
        r = self._turn(conv)
        r2 = conv.chat("s1", [{"role": "user", "content": "x"}, {"role": "user", "content": "每小时就行"}], r["draft"], "zh")
        self.assertTrue(any(c["id"] == "route-diff" for c in r2["draft"]["checks"]))


if __name__ == "__main__":
    unittest.main()


class TestSwitchOff(_Base):
    test_图的节点和边 = None
    test_降级_模型不可用 = None

    def test_开关关闭_提示词没有脚本规则_模型给了script_requests也不进脚本流程(self):
        from netops_ai.graph.plan_graph import PlanConversation
        from netops_ai.inspection import plan_chat as PC

        self.assertFalse(PC.SCRIPT_AUTHORING)
        msgs = PC.build_messages([{"role": "user", "content": "x"}], None, self.topo, "zh")
        self.assertNotIn("script_requests", msgs[0]["content"])
        llm = ScriptLLM([_out(script_requests=[_req()], checks=[], devices=["V1"],
                              schedule={"every_minutes": 60, "daily_at": ""})], [DIFF_SCRIPT])
        conv = PlanConversation(llm_factory=lambda: llm, topology_loader=lambda: self.topo, docs_db=lambda: None,
                                sample_runner=lambda c, h, n: self.fail("must not sample"))
        r = conv.chat("s1", [{"role": "user", "content": "x"}], None, "zh")
        self.assertFalse(any(w.get("type") == "script_review" for w in [r.get("widget") or {}]))

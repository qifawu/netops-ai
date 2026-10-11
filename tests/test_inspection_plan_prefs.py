"""维护者的真实抱怨回放：想要「路由表明细对比 + 路由条目总数」，助手一直塞 BGP 会话 / OSPF 邻居，而且删不掉。

覆盖：用户表明关注点后不再补回角色默认检查、× 删掉的不会长回来、「不需要」的建议不再出现且有数量上限、
检查项气泡里取消勾选等于删除、保存的计划文件里没有对话偏好。全部用 fake LLM，不连网、不连设备。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from netops_ai.inspection import plan_prefs as PF
from netops_ai.inspection import plans as P
from netops_ai.inspection.plan_widgets import apply_selection

from tests.test_inspection_plans import TestGraph, _out


def _chk(tid):
    return {"template": tid, "id": "", "title": "", "command": "", "devices": [], "expect": [], "extract": []}


class TestRoutesComplaint(TestGraph):
    def _draft_with_defaults(self):
        d = P.recommended_draft(self.topo, ["core", "aggregation"], schedule={"every_minutes": 60})
        return PF.put(d, PF.empty())

    def test_用户说要路由明细对比_模型换成路由检查_默认的BGP_OSPF不被补回(self):
        d = self._draft_with_defaults()
        self.assertTrue({"ospf_neighbors", "bgp_sessions"} & {c.get("template") for c in d["checks"]})
        conv = self._conv([_out(checks=[_chk("route_table_diff"), _chk("route_count")], devices=["V1", "V2", "D1"])])
        text = "我需要的是路由表具体明细对比和路由条目总数"
        r = conv.chat("m1", [{"role": "user", "content": text}], d, "zh")
        ids = {c.get("template") for c in r["draft"]["checks"]}
        self.assertEqual(ids, {"route_table_diff", "route_count"}, (r.get("source"), r.get("reply"), r.get("validation"), r.get("removed")))

    def test_点叉删掉的检查项_后面任何一轮都不长回来(self):
        d = self._draft_with_defaults()
        gone = [c["id"] for c in d["checks"] if c.get("template") == "ospf_neighbors"]
        self.assertTrue(gone)
        d = PF.apply_patch_tracked(d, {"remove_checks": gone}, self.topo)
        self.assertFalse(any(c.get("template") == "ospf_neighbors" for c in d["checks"]))
        # 模型下一轮把 OSPF 又写回来（它不记得用户删过）
        conv = self._conv([_out(checks=[_chk("ospf_neighbors"), _chk("interface_errors")], devices=["V1", "V2", "D1"]),
                           _out(checks=[_chk("ospf_neighbors"), _chk("interface_errors")], devices=["V1", "V2", "D1"])])
        r = conv.chat("m2", [{"role": "user", "content": "每小时就行"}], d, "zh")
        self.assertFalse(any(c.get("template") == "ospf_neighbors" for c in r["draft"]["checks"]), r["draft"]["checks"])
        r = conv.chat("m2", [{"role": "user", "content": "每小时就行"}, {"role": "user", "content": "再加一台 A1"}], r["draft"], "zh")
        self.assertFalse(any(c.get("template") == "ospf_neighbors" for c in r["draft"]["checks"]))

    def test_换设备范围的气泡也不会把删掉的检查项带回来(self):
        d = self._draft_with_defaults()
        gone = [c["id"] for c in d["checks"] if c.get("template") == "bgp_sessions"]
        d = PF.apply_patch_tracked(d, {"remove_checks": gone}, self.topo)
        new, problems, _ = apply_selection(d, {"type": "device_picker", "value": {"devices": ["V1", "V2", "D1"]}}, self.topo)
        self.assertEqual(problems, [])
        self.assertFalse(any(c.get("template") == "bgp_sessions" for c in new["checks"]))

    def test_检查项气泡取消勾选等于删除(self):
        d = self._draft_with_defaults()
        keep = ["route_table_diff"]
        new, problems, _ = apply_selection(d, {"type": "check_picker", "value": {"templates": keep}}, self.topo)
        self.assertEqual(problems, [])
        self.assertEqual({c.get("template") for c in new["checks"]}, set(keep))
        prefs = PF.get(new)
        self.assertTrue(any(r.get("template") == "ospf_neighbors" for r in prefs["removed"]))
        # 之后再 enforce / 再套一次推荐，OSPF 也不回来
        again, _ = PF.enforce(P.apply_patch(new, {"add_checks": [{"template": "ospf_neighbors"}]}, self.topo), prefs)
        self.assertFalse(any(c.get("template") == "ospf_neighbors" for c in again["checks"]))

    def test_用户明确又要回来_放行(self):
        d = self._draft_with_defaults()
        gone = [c["id"] for c in d["checks"] if c.get("template") == "ospf_neighbors"]
        d = PF.apply_patch_tracked(d, {"remove_checks": gone}, self.topo)
        d = PF.apply_patch_tracked(d, {"add_checks": [{"template": "ospf_neighbors"}]}, self.topo)  # 点了「采纳」
        self.assertTrue(any(c.get("template") == "ospf_neighbors" for c in d["checks"]))

    def test_不需要的建议_不再出现_关注点之后最多两条(self):
        suggestions = [{"id": f"s{i}", "text": f"建议 {i}", "reason": "r", "patch": {"add_checks": [{"template": t}]}}
                       for i, t in enumerate(["ospf_neighbors", "bgp_sessions", "cpu", "interface_errors", "route_count"])]
        prefs = PF.empty()
        prefs["focus"] = ["route_count"]
        shown = PF.filter_suggestions(suggestions, prefs, 5)
        self.assertEqual([s["patch"]["add_checks"][0]["template"] for s in shown], ["route_count"])  # 跟关注点无关的不推
        prefs = PF.dismiss(PF.empty(), suggestions[0])
        self.assertFalse(PF.allow_suggestion(suggestions[0], prefs))
        equiv = {"id": "other", "text": "给核心加 OSPF 邻居", "reason": "x", "patch": {"add_checks": [{"template": "ospf_neighbors"}]}}
        self.assertFalse(PF.allow_suggestion(equiv, prefs))  # 写法不同、效果相同也不再出现
        many = PF.filter_suggestions([s for s in suggestions], {**PF.empty(), "focus_topics": ["protocol", "resource"]}, 5)
        self.assertLessEqual(len(many), PF.MAX_FOCUSED_SUGGESTIONS)

    def test_保存的计划文件里没有对话偏好(self):
        d = self._draft_with_defaults()
        d["name"] = "prefs-not-saved"
        d = PF.apply_patch_tracked(d, {"remove_checks": [d["checks"][0]["id"]]}, self.topo)
        self.assertIn("prefs", d)
        saved = P.save_plan(d)
        self.assertNotIn("prefs", saved)
        self.assertNotIn("prefs", P.load_plan("prefs-not-saved"))


if __name__ == "__main__":
    unittest.main()

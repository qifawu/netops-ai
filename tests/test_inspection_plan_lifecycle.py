"""巡检计划完整生命周期：对话创建 → 改周期 → 对话里改命令（写命令被拦）→ 运行 → 导出 md/html → 删除（只删计划 / 连历史 /
不存在 / 正在运行 / 名称穿越）→ 同名再创建。整套循环跑 3 遍，互不串状态。不登真实设备、不调真实模型。"""

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

from netops_ai.inspection import plans as P
from netops_ai.topology import TopologyDevice, TopologyLink

ALL = {"commands": True, "devices": True, "schedule": True}
OSPF_OK = ("Neighbor ID     Pri   State           Dead Time   Address         Interface\n"
           "192.0.2.51        1   FULL/DR         00:00:35    10.0.0.2        GigabitEthernet0/1\n")
OSPF_BAD = ("Neighbor ID     Pri   State           Dead Time   Address         Interface\n"
            "192.0.2.51        1   INIT/DROTHER    00:00:31    10.0.0.2        GigabitEthernet0/1\n")
CPU = "CPU utilization for five seconds: 3%/0%; one minute: 2%; five minutes: 1%\n"


def _topo() -> dict:
    def dev(name, host, role, links):
        return TopologyDevice(name=name, host=host, role=role, links=tuple(TopologyLink(a, b, c) for a, b, c in links))
    return {"V1": dev("V1", "192.0.2.50", "core", [("GigabitEthernet0/1", "V2", "GigabitEthernet0/1")]),
            "V2": dev("V2", "192.0.2.51", "core", [("GigabitEthernet0/1", "V1", "GigabitEthernet0/1")])}


class Adapter:
    """V2 的 OSPF 邻居卡在 INIT（造一个失败项）；其它都正常。白名单照真实适配器先查。"""

    def __init__(self, host, gate=None):
        self.host, self.gate = host, gate

    def run(self, command):
        from netops_ai.devices.whitelist import check

        v = check("cisco", command)
        if not v.allowed:
            return SimpleNamespace(allowed=False, denial_reason=v.reason, error="", output="")
        if self.gate is not None:
            self.gate.wait(5)
        out = {"show ip ospf neighbor": OSPF_BAD if self.host.endswith(".51") else OSPF_OK,
               "show processes cpu | include CPU utilization": CPU, "show clock": "*10:00:00.000 UTC Thu Oct 8 2026\n"}
        return SimpleNamespace(allowed=True, denial_reason="", error="", output=out.get(command, ""))

    def close(self):
        pass


def _model(**kw) -> dict:
    base = {"reply": "好的。", "plan_name": "x", "description": "核心 OSPF 巡检", "devices": ["V1", "V2"],
            "checks": [{"template": t, "id": "", "title": "", "command": "", "devices": [], "expect": [], "extract": []}
                       for t in ("ospf_neighbors", "cpu")],
            "schedule": {"every_minutes": 60, "daily_at": ""}, "enabled": True, "suggestions": [], "ready": True, "quick_replies": []}
    base.update(kw)
    return base


def _custom(cmd: str, cid: str = "raw") -> dict:
    return {"template": "", "id": cid, "title": cid, "command": cmd, "devices": [], "expect": [], "extract": []}


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.td.name)
        from netops_ai.api import plans as api_plans
        from netops_ai.api import schedule
        from netops_ai.api.app import app

        for target, attr, value in ((P, "PLANS_DIR", root / "plans"), (P, "RUNS_DIR", root / "runs"),
                                    (P, "TRENDS_DIR", root / "trends"), (schedule, "STATE_PATH", root / "state.json"),
                                    (api_plans, "_topology", lambda: _topo()), (api_plans, "_CONVERSATION", None)):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.gate = None
        p = mock.patch("netops_ai.topology._default_adapter_factory", lambda dev: Adapter(dev.host, self.gate))
        p.start()
        self.addCleanup(p.stop)
        self.outputs: list = []
        self.calls: list = []

        def complete(messages, **kw):
            self.calls.append(messages)
            o = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
            if isinstance(o, Exception):
                raise o
            if isinstance(o, str):  # 趋势分析：纯文本
                return SimpleNamespace(parsed=None, content=o)
            return SimpleNamespace(parsed=o, content=json.dumps(o, ensure_ascii=False))

        p = mock.patch("netops_ai.llm.client.LLMClient.complete", side_effect=complete)
        p.start()
        self.addCleanup(p.stop)
        self.c = TestClient(app)
        self.addCleanup(self.td.cleanup)

    # ------------------------------------------------------------------ 工具
    def chat(self, session, messages, draft=None, **kw):
        r = self.c.post("/api/inspection/plans/chat", json={"session": session, "messages": messages, "draft": draft, "lang": "zh", **kw})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def confirm(self, session, draft, confirmed=ALL):
        return self.c.post("/api/inspection/plans/confirm", json={"session": session, "draft": draft, "confirmed": confirmed})

    def run_now(self, name):
        from netops_ai.api import schedule

        return schedule.run_plan_and_record(name)

    # ------------------------------------------------------------------ 一整套循环
    def cycle(self, i: int) -> None:
        name = f"plan-test-{i}"
        u = lambda t: {"role": "user", "content": t}  # noqa: E731
        # 1) 对话创建 + 三项确认
        self.outputs = [_model(plan_name=name)]
        r = self.chat(f"new-{i}", [u("核心设备 OSPF 邻居和 CPU，每小时")])
        self.assertTrue(r["awaiting_confirm"])
        self.assertEqual(r["draft"]["name"], name)
        self.assertEqual(self.confirm(f"new-{i}", r["draft"], {**ALL, "schedule": False}).json()["saved"], None)  # 缺一项不保存
        self.assertFalse((Path(P.PLANS_DIR) / f"{name}.yaml").exists())
        r = self.chat(f"new-{i}", [u("核心设备 OSPF 邻居和 CPU，每小时"), u("确认")], r["draft"])
        saved = self.confirm(f"new-{i}", r["draft"]).json()
        self.assertEqual(saved["saved"]["name"], name, saved)
        # 2) 网页上改周期：下一次从现在起按新周期算
        before = datetime.now(timezone.utc)
        row = self.c.patch(f"/api/inspection/plans/{name}", json={"schedule": {"every_minutes": 15}}).json()
        nxt = datetime.fromisoformat(row["next_run_at"])
        self.assertTrue(timedelta(minutes=14) < nxt - before < timedelta(minutes=16), row["next_run_at"])
        # 3) 对话里调整：写命令被拦（两次修正仍不过，不放行），再加一条正常命令
        r = self.chat(f"edit-{i}", [], None, mode="edit", plan=name)
        self.assertEqual(r["editing"], name)
        self.outputs = [_model(plan_name=name, checks=_model()["checks"] + [_custom("clear counters", "wipe")])]
        r = self.chat(f"edit-{i}", [u("加一条清计数的 clear counters")], r["draft"], mode="edit", plan=name)
        self.assertFalse(r["awaiting_confirm"])
        self.assertTrue(any("whitelist" in p for p in r["validation"]["problems"]))
        self.assertNotIn("clear counters", json.dumps(r["draft"]))
        self.assertNotIn("clear", (Path(P.PLANS_DIR) / f"{name}.yaml").read_text(encoding="utf-8"))
        self.outputs = [_model(plan_name=name, checks=_model()["checks"] + [_custom("show clock", "clock")],
                               schedule={"every_minutes": 15, "daily_at": ""})]
        r = self.chat(f"edit-{i}", [u("那加一条 show clock")], r["draft"], mode="edit", plan=name)
        self.assertTrue(r["awaiting_confirm"])
        self.assertEqual(self.confirm(f"edit-{i}", r["draft"]).status_code, 200)
        plan = P.load_plan(name)
        self.assertEqual([c["id"] for c in plan["checks"]], ["ospf_neighbors", "cpu", "clock"])
        self.assertEqual(plan["schedule"], {"every_minutes": 15})
        # 4) 运行（V2 的 OSPF 失败）+ 趋势分析存档
        entry = self.run_now(name)
        self.assertTrue(entry["ok"], entry)
        runs = self.c.get(f"/api/inspection/plans/{name}/runs").json()["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["fail"], 1)
        self.outputs = [f"{name}: V2 ospf_neighbors 卡在 INIT，需要尽快处理（run {runs[0]['run_id']}）"]
        t = self.c.post(f"/api/inspection/plans/{name}/trend", json={"last": 12}).json()
        self.assertTrue(t["llm"])
        # 5) 导出
        md = self.c.get(f"/api/inspection/plans/{name}/report?format=md&runs=5")
        self.assertEqual(md.status_code, 200)
        self.assertIn(f'plan-{name}-report.md', md.headers["content-disposition"])
        text = md.text
        for must in (f"巡检计划报告：{name}", "V1", "V2", "192.0.2.51", "show ip ospf neighbor", "INIT/DROTHER", "FULL/DR",
                     "ospf_full", "cpu_5m", "5. 失败项清单", "contains FULL", "卡在 INIT", "show clock"):
            self.assertIn(must, text, must)
        self.assertNotIn("password", text.lower())
        html = self.c.get(f"/api/inspection/plans/{name}/report?format=html").text
        self.assertTrue(html.startswith("<!doctype html>"))
        for must in (name, "INIT/DROTHER", "ospf_full", 'class="bad"'):
            self.assertIn(must, html)
        self.assertEqual(self.c.get(f"/api/inspection/plans/{name}/report?format=pdf").status_code, 400)
        # 6) 删除：先看预览，只删计划、历史保留
        pv = self.c.get(f"/api/inspection/plans/{name}/delete-preview").json()
        self.assertTrue(pv["plan_file"].endswith(f"{name}.yaml"))
        self.assertEqual(pv["history_count"], 1)
        d = self.c.delete(f"/api/inspection/plans/{name}").json()
        self.assertEqual(d["history_deleted"], 0)
        self.assertNotIn(name, [x["name"] for x in self.c.get("/api/inspection/plans").json()])
        self.assertEqual(len(P.history_files(name)), 1)
        self.assertEqual(P.due_plans(datetime.now(timezone.utc) + timedelta(days=2)), [])  # 调度不再触发它
        self.assertEqual(self.c.delete(f"/api/inspection/plans/{name}").status_code, 404)
        # 7) 同名再创建，再连历史一起删
        self.outputs = [_model(plan_name=name)]
        r = self.chat(f"again-{i}", [u("再建一次")])
        self.assertEqual(self.confirm(f"again-{i}", r["draft"]).json()["saved"]["name"], name)
        self.assertEqual(self.c.get(f"/api/inspection/plans/{name}/delete-preview").json()["history_count"], 1)
        d = self.c.delete(f"/api/inspection/plans/{name}?with_history=true").json()
        self.assertEqual(d["history_deleted"], 1)
        self.assertEqual(P.history_files(name), [])
        self.assertIsNone(P.load_trend(name))

    def test_整套循环跑三遍_互不串状态(self):
        for i in (1, 2, 3):
            with self.subTest(cycle=i):
                self.cycle(i)
                self.assertEqual(P.list_plans(), [])  # 每遍结束都删干净
                for j in range(1, i + 1):  # 每遍最后连历史一起删了：前面几遍不会给后面留下任何东西
                    self.assertEqual(P.history_files(f"plan-test-{j}"), [])
                self.assertEqual(list(Path(P.RUNS_DIR).glob("*.json")), [])

    # ------------------------------------------------------------------ 删除 / 改名的边界
    def _plain(self, name="keep-me", every=60):
        d = P.recommended_draft(_topo(), devices=["V1"], templates=["cpu"], schedule={"every_minutes": every})
        d["name"] = name
        return P.save_plan(d)

    def test_删除时正在运行_拒绝并提示稍后(self):
        self._plain("busy")
        self.gate = threading.Event()
        t = threading.Thread(target=self.run_now, args=("busy",))
        t.start()
        for _ in range(100):
            if P.is_running("busy"):
                break
            threading.Event().wait(0.02)
        r = self.c.delete("/api/inspection/plans/busy")
        self.assertEqual(r.status_code, 409)
        self.assertIn("running", r.json()["message"])
        self.assertTrue(self.c.get("/api/inspection/plans/busy/delete-preview").json()["running"])
        # 运行中改周期：改得了，进行中的那次不被打断
        self.assertEqual(self.c.patch("/api/inspection/plans/busy", json={"schedule": {"every_minutes": 30}}).status_code, 200)
        self.assertEqual(self.c.patch("/api/inspection/plans/busy", json={"name": "busy2"}).status_code, 409)  # 跑着不改名
        self.gate.set()
        t.join(5)
        self.assertEqual(len(P.history("busy")), 1)
        self.assertEqual(P.load_plan("busy")["schedule"], {"every_minutes": 30})
        self.assertEqual(self.c.delete("/api/inspection/plans/busy").status_code, 200)

    def test_删除名称穿越与只删自己的历史(self):
        self._plain("core")
        self._plain("core-health")
        self.run_now("core")
        self.run_now("core-health")
        from netops_ai.api import plans as api_plans

        self.assertTrue(400 <= self.c.delete("/api/inspection/plans/..%2F..%2Fetc").status_code < 500)
        for bad in ("../core", "..", "a/b", ".core", "c:\\x"):
            self.assertIn(api_plans.api_delete_plan(bad).status_code, (400, 404), bad)
        r = self.c.delete("/api/inspection/plans/core?with_history=true").json()
        self.assertEqual(r["history_deleted"], 1)
        self.assertEqual(len(P.history_files("core-health")), 1)  # 名字是前缀的另一个计划不受影响
        self.assertTrue((Path(P.PLANS_DIR) / "core-health.yaml").exists())

    def test_报告_时间到秒_回显去掉首尾空行但正文不改(self):
        from netops_ai.inspection import plan_report

        plan = {"name": "r1", "enabled": True, "schedule": {"every_minutes": 15},
                "devices": [{"name": "V1", "host": "192.0.2.50"}], "checks": [{"id": "c", "command": "show clock"}]}
        run = {"run_id": "abcd1234", "started_at": "2026-01-01T08:00:00.123456+00:00", "summary": {"pass": 1},
               "devices": [{"name": "V1", "checks": [{"id": "c", "command": "show clock", "status": "pass", "metrics": {},
                                                      "output": "\n\n\n  *10:00:00.000 UTC\n  second line  \n\n"}]}]}
        md = plan_report.render_markdown(plan, [run], None)
        self.assertNotIn(".123456", md)
        self.assertIn("2026-01-01T08:00:00+00:00", md)
        self.assertIn("```\n  *10:00:00.000 UTC\n  second line  \n```", md)
        self.assertIn("还没有做过趋势分析", md)

    def test_改名(self):
        self._plain("old-name")
        self._plain("taken")
        self.assertEqual(self.c.patch("/api/inspection/plans/old-name", json={"name": "taken"}).status_code, 409)
        self.assertEqual(self.c.patch("/api/inspection/plans/old-name", json={"name": "../evil"}).status_code, 400)
        r = self.c.patch("/api/inspection/plans/old-name", json={"name": "new-name"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "new-name")
        self.assertEqual(sorted(p["name"] for p in P.list_plans()), ["new-name", "taken"])

    def test_对话里调整时改名_旧文件摘掉(self):
        self._plain("before")
        self.outputs = [_model(plan_name="before")]
        r = self.chat("rn", [], None, mode="edit", plan="before")
        r = self.chat("rn", [{"role": "user", "content": "把 OSPF 加上"}], r["draft"], mode="edit", plan="before")
        draft = {**r["draft"], "name": "after"}  # 用户在草案里改了名
        out = self.confirm("rn", draft).json()
        self.assertEqual(out["saved"]["name"], "after")
        self.assertEqual([p["name"] for p in P.list_plans()], ["after"])


if __name__ == "__main__":
    unittest.main()

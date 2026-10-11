"""（跟 test_checklist_metric_rules.py / test_inspection_dry_run.py 重复的用例已合并过去，这里只留它们没覆盖到的：
固定基线的来源、报告和趋势提示词、同一计划不交错时对照的是上一次的 run id、输出截断行数、设备子集 + 整体超时标记。）
有状态的指标规则（首次基线 / 没有基线 / 不可达不覆盖基线 / 复位 / 百分比阈值 / 固定阈值 / 固定基线 / 并发）
和命令采集试运行（白名单拒绝不发出 / 超时 / 取消 / 草案改了就过期 / 不写历史）。不登真实设备、不调模型。"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from netops_ai.inspection import checklist as CL
from netops_ai.inspection import dry_run as DR
from netops_ai.inspection import plan_report
from netops_ai.inspection import plans as P


class Adapter:
    """按设备给 `show ip route | count ...` 一个可调的路由数；`fail` 里的设备连不上。命令照样先过白名单。"""

    def __init__(self, host, routes, *, delay=0.0, error=""):
        self.host, self.routes, self.delay, self.error, self.sent = host, routes, delay, error, []

    def run(self, command):
        from netops_ai.devices.whitelist import check

        v = check("cisco", command)
        if not v.allowed:
            return SimpleNamespace(allowed=False, denial_reason=v.reason, error="", output="")
        self.sent.append(command)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            return SimpleNamespace(allowed=True, denial_reason="", error=self.error, output="")
        n = self.routes.get(self.host)
        out = "" if n is None else f"Number of lines which match regexp = {n}\n"
        if command == "show clock":
            out = "*10:00:00.000 UTC Thu Oct 8 2026\nline two\n"
        return SimpleNamespace(allowed=True, denial_reason="", error="", output=out)

    def close(self):
        pass


ROUTE = P.TEMPLATES["route_count"]["check"]


def plan(name="routes", rule=None, extra_checks=()):
    expect = [rule] if rule else ROUTE["expect"]
    return {"name": name, "vendor": "cisco", "devices": [{"name": "V1", "host": "192.0.2.50"}, {"name": "V2", "host": "192.0.2.51"}],
            "checks": [{"id": "routes", "command": ROUTE["command"], "extract": ROUTE["extract"], "expect": expect}, *extra_checks],
            "schedule": {"every_minutes": 60}}


class Runs:
    """在一个临时目录里按顺序跑、存，history 每次现从目录里读（跟 run_plan 一样不靠内存）。"""

    def __init__(self, cl):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.cl = cl

    def run(self, routes, unreachable=()):
        def factory(dev):
            if dev.host in unreachable:
                raise OSError("connection timed out")
            return Adapter(dev.host, routes)
        r = CL.run_checklist(self.cl, factory, history=CL.load_history(self.td.name, self.cl["name"], 500))
        CL.save_result(r, self.td.name)
        time.sleep(0.002)  # started_at 精确到微秒，保证顺序
        return r

    @staticmethod
    def rule(r, dev="V1"):
        d = next(x for x in r["devices"] if x["name"] == dev)
        c = d["checks"][0]
        return c["status"], next(e for e in c["evaluations"] if e["type"] in CL.METRIC_EXPECT_TYPES)


class TestStatefulRules(unittest.TestCase):
    def test_复位_百分比阈值_固定阈值_固定基线(self):
        rs = Runs(plan(rule={"type": "metric_vs_previous", "metric": "route_count", "direction": "increase", "reset_ok": True, "severity": "warning"}))
        rs.run({"192.0.2.50": 100, "192.0.2.51": 1})
        st, e = rs.rule(rs.run({"192.0.2.50": 3, "192.0.2.51": 1}))
        self.assertEqual((st, e["state"]), ("pass", "reset"))
        st, e = rs.rule(rs.run({"192.0.2.50": 9, "192.0.2.51": 1}))
        self.assertEqual((st, e["state"]), ("fail", "fail"))  # 3 → 9 是增长

        rs = Runs(plan(rule={"type": "metric_vs_previous", "metric": "route_count", "direction": "decrease", "tolerance_pct": 10}))
        rs.run({"192.0.2.50": 100, "192.0.2.51": 1})
        self.assertEqual(rs.rule(rs.run({"192.0.2.50": 92, "192.0.2.51": 1}))[1]["state"], "pass")
        self.assertEqual(rs.rule(rs.run({"192.0.2.50": 80, "192.0.2.51": 1}))[1]["state"], "fail")  # 92 → 80 超过 10%

        rs = Runs(plan(rule={"type": "metric_threshold", "metric": "route_count", "op": ">=", "value": 5}))
        self.assertEqual(rs.rule(rs.run({"192.0.2.50": 4, "192.0.2.51": 9}))[1]["state"], "fail")

        rs = Runs(plan(rule={"type": "metric_vs_baseline", "metric": "route_count", "direction": "change", "baseline": 20, "tolerance_abs": 2}))
        st, e = rs.rule(rs.run({"192.0.2.50": 21, "192.0.2.51": 9}))
        self.assertEqual((st, e["reference"], e["reference_run_id"]), ("pass", 20, None))
        rs = Runs(plan(rule={"type": "metric_vs_baseline", "metric": "route_count", "direction": "change", "tolerance_abs": 0}))
        r1 = rs.run({"192.0.2.50": 6, "192.0.2.51": 9})
        rs.run({"192.0.2.50": 6, "192.0.2.51": 9})
        st, e = rs.rule(rs.run({"192.0.2.50": 7, "192.0.2.51": 9}))
        self.assertEqual((st, e["reference_run_id"]), ("fail", r1["run_id"]))  # 基线 = 首次采到的值

    def test_报告和趋势提示词带上规则(self):
        rs = Runs(plan())
        r1 = rs.run({"192.0.2.50": 12, "192.0.2.51": 12})
        r2 = rs.run({"192.0.2.50": 9, "192.0.2.51": 12})
        md = plan_report.render_markdown(plan(), [r1, r2], None)
        self.assertIn("本次 9 / 对照 12，对照运行 " + r1["run_id"], md)
        md1 = plan_report.render_markdown(plan(), [r1], None)
        self.assertIn("首次运行，已记录基线", md1)
        table = CL.trend_table([r1, r2])
        self.assertIn("V1/routes rule route_count decrease vs previous run", table)
        self.assertIn("baseline | fail(12→9)", table)

    def test_计划运行从存档读历史_并发不交错(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            with mock.patch.object(P, "PLANS_DIR", Path(td) / "plans"), mock.patch.object(P, "RUNS_DIR", Path(td) / "runs"), \
                    mock.patch.object(P, "_refresh_hosts", lambda plan: plan):  # 不去读真实拓扑（会把地址换成真实设备的）
                P.save_plan(plan())
                routes = {"192.0.2.50": 12, "192.0.2.51": 12}
                r1, _ = P.run_plan("routes", lambda dev: Adapter(dev.host, routes))
                gate = threading.Event()

                class Slow(Adapter):
                    def run(self, command):
                        gate.wait(5)
                        return super().run(command)

                out = {}
                t = threading.Thread(target=lambda: out.update(r=P.run_plan("routes", lambda dev: Slow(dev.host, routes))[0]))
                t.start()
                time.sleep(0.2)
                with self.assertRaises(P.PlanBusy):  # 同一个计划不能交错跑（否则「上一次」会读乱）
                    P.run_plan("routes", lambda dev: Adapter(dev.host, routes))
                gate.set()
                t.join(5)
                self.assertEqual(Runs.rule(out["r"])[1]["reference_run_id"], r1["run_id"])


class TestDryRun(unittest.TestCase):
    def draft(self, extra=()):
        d = plan(extra_checks=extra)
        return d

    def test_白名单拒绝不发出_指标规则首次基线_输出前N行(self):
        adapters = {}

        def factory(dev):
            adapters[dev.name] = Adapter(dev.host, {"192.0.2.50": 12, "192.0.2.51": 3})
            return adapters[dev.name]
        d = self.draft(extra=[{"id": "wipe", "command": "clear counters"}, {"id": "clock", "command": "show clock"}])
        r = DR.dry_run(d, factory, head_lines=1)
        v1 = {c["id"]: c for c in r["devices"][0]["checks"]}
        self.assertEqual(v1["wipe"]["status"], "denied")
        self.assertNotIn("clear counters", adapters["V1"].sent)
        self.assertEqual(v1["routes"]["status"], "ok")
        self.assertEqual(v1["routes"]["metrics"], {"route_count": 12})
        self.assertEqual(v1["routes"]["evaluations"][0]["state"], "baseline")
        self.assertEqual((v1["clock"]["lines"], v1["clock"]["output_head"], v1["clock"]["truncated_lines"]), (2, "*10:00:00.000 UTC Thu Oct 8 2026", 1))
        self.assertGreater(v1["clock"]["bytes"], 0)
        self.assertEqual(r["summary"]["denied"], 2)

    def test_设备子集_超时_单条命令超时(self):
        r = DR.dry_run(plan(), lambda dev: Adapter(dev.host, {}, error="ssh command timed out after 10.0 seconds"), devices=["V2"])
        self.assertEqual([d["name"] for d in r["devices"]], ["V2"])
        self.assertEqual(r["devices"][0]["checks"][0]["status"], "timeout")
        r = DR.dry_run(self.draft(extra=[{"id": "clock", "command": "show clock"}]),
                       lambda dev: Adapter(dev.host, {"192.0.2.50": 1, "192.0.2.51": 1}, delay=0.4), total_timeout=0.2, max_workers=2)
        self.assertTrue(r["timed_out"])
        self.assertIn("timeout", [c["status"] for d in r["devices"] for c in d["checks"]])
        self.assertEqual(len([c for d in r["devices"] for c in d["checks"]]), 4)  # 没跑到的也列出来

if __name__ == "__main__":
    unittest.main()

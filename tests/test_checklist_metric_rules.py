"""有状态的指标规则：首次 = 建基线不判；之前没采到 = no_baseline 不判；不可达 / 被拒的运行不覆盖基线；
计数复位（reset_ok）；百分比 / 绝对容差；阈值；固定基线；校验；路由条目模板；计划连跑两次（存档 → 对照）。"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from netops_ai.inspection import checklist as CL
from netops_ai.inspection import plans as P

COUNT = "show ip route | count ^[A-Z][A-Z0-9* ]+[0-9]+[.]"


def routes(n: int) -> str:
    return f"Number of lines which match regexp = {n}\n"


class Adapter:
    """按设备给不同的回显；`fail` 里的设备直接连不上（工厂抛异常），`deny` 让命令被拒。"""

    def __init__(self, out: str | None, deny: bool = False):
        self.out, self.deny = out, deny

    def run(self, command):
        if self.deny:
            return SimpleNamespace(allowed=False, denial_reason="refused", error="", output="")
        return SimpleNamespace(allowed=True, denial_reason="", error="", output=self.out or "")

    def close(self):
        pass


def checklist(rule: dict) -> dict:
    return {"name": "rt", "vendor": "cisco", "devices": [{"name": "V1", "host": "192.0.2.50"}],
            "checks": [{"id": "routes", "command": COUNT, "extract": [{"name": "route_count", "regex": r"=\s*(\d+)", "cast": "int"}],
                        "expect": [rule]}]}


PREV = {"type": "metric_vs_previous", "metric": "route_count", "direction": "decrease", "tolerance_pct": 0, "severity": "critical"}


class TestRules(unittest.TestCase):
    def run_seq(self, rule, outputs):
        """依次跑几次，每次把前面的结果当历史。outputs 里：数字=回显；None=连不上；"deny"=命令被拒；"junk"=采不到。"""
        hist, evals = [], []
        for o in outputs:
            if o is None:
                factory = mock.Mock(side_effect=OSError("timed out"))
            elif o == "deny":
                factory = lambda dev: Adapter("", deny=True)  # noqa: E731
            elif o == "junk":
                factory = lambda dev: Adapter("% Invalid input")  # noqa: E731
            else:
                factory = lambda dev, o=o: Adapter(routes(o))  # noqa: E731
            r = CL.run_checklist(checklist(rule), factory, history=hist)
            r["started_at"] = f"2026-01-01T00:{len(hist):02d}:00+00:00"
            hist.append(r)
            d = r["devices"][0]
            c = d["checks"][0] if d["checks"] else None
            evals.append(((c["evaluations"] or [None])[0] if c else None, c["status"] if c else "unreachable", r["run_id"]))
        return evals

    def test_首次建基线_持平通过_减少失败(self):
        ev = self.run_seq(PREV, [10, 10, 9])
        (e1, s1, _), (e2, s2, r2), (e3, s3, _) = ev
        self.assertEqual((e1["state"], e1["passed"], s1), ("baseline", None, "pass"))
        self.assertEqual((e2["state"], e2["passed"], e2["reference"], e2["current"]), ("pass", True, 10, 10))
        self.assertEqual((e3["state"], e3["passed"], s3, e3["reference"], e3["current"], e3["reference_run_id"]), ("fail", False, "fail", 10, 9, r2))

    def test_不可达和被拒不覆盖基线_跟最近一次成功采集比(self):
        ev = self.run_seq(PREV, [10, None, "deny", 10])
        self.assertEqual(ev[1][1], "unreachable")
        self.assertEqual(ev[2][1], "denied")
        last = ev[3][0]
        self.assertEqual((last["state"], last["reference"], last["reference_run_id"]), ("pass", 10, ev[0][2]))

    def test_之前没采到是no_baseline_这次没采到是no_value(self):
        ev = self.run_seq(PREV, ["junk", 10, "junk"])
        self.assertEqual((ev[0][0]["state"], ev[0][1]), ("no_value", "pass"))  # 采不到不判，检查项本身不算失败
        self.assertEqual((ev[1][0]["state"], ev[1][0]["passed"]), ("no_baseline", None))
        self.assertEqual(ev[2][0]["state"], "no_value")

    def test_百分比和绝对容差(self):
        rule = {**PREV, "tolerance_pct": 10}
        ev = self.run_seq(rule, [100, 91, 80])
        self.assertTrue(ev[1][0]["passed"])  # 100 → 91：在 10% 以内
        self.assertFalse(ev[2][0]["passed"])  # 91 → 80：超过 9.1
        ev = self.run_seq({**PREV, "tolerance_abs": 2}, [10, 8, 5])
        self.assertEqual([e[0]["passed"] for e in ev], [None, True, False])

    def test_计数复位(self):
        rule = {"type": "metric_vs_previous", "metric": "route_count", "direction": "increase", "tolerance_abs": 5, "reset_ok": True}
        ev = self.run_seq(rule, [100, 102, 3, 20])
        self.assertEqual([e[0]["state"] for e in ev], ["baseline", "pass", "reset", "fail"])
        ev = self.run_seq({**rule, "reset_ok": False, "direction": "change"}, [100, 3])
        self.assertEqual(ev[1][0]["state"], "fail")

    def test_阈值和基线(self):
        ev = self.run_seq({"type": "metric_threshold", "metric": "route_count", "op": ">=", "value": 5}, [7, 4])
        self.assertEqual([e[0]["passed"] for e in ev], [True, False])
        ev = self.run_seq({"type": "metric_vs_baseline", "metric": "route_count", "direction": "change", "tolerance_pct": 20}, [10, 11, 13])
        self.assertEqual([e[0]["state"] for e in ev], ["baseline", "pass", "fail"])
        self.assertEqual(ev[2][0]["reference"], 10)  # 基线是第一次采到的值，不是上一次
        ev = self.run_seq({"type": "metric_vs_baseline", "metric": "route_count", "direction": "decrease", "baseline": 50}, [49])
        self.assertEqual((ev[0][0]["state"], ev[0][0]["reference"]), ("fail", 50))

    def test_校验(self):
        ok = checklist(PREV)
        self.assertEqual(CL.validate_checklist(ok), [])
        for bad in ({**PREV, "metric": "nope"}, {**PREV, "direction": "down"}, {**PREV, "tolerance_pct": -1},
                    {"type": "metric_threshold", "metric": "route_count", "op": "!=", "value": 1},
                    {"type": "metric_threshold", "metric": "route_count", "op": ">", "value": "1"},
                    {**PREV, "reset_ok": "yes"}):
            self.assertTrue(CL.validate_checklist(checklist(bad)), bad)
        # 老格式的文本规则照旧
        self.assertEqual(CL.validate_checklist({**ok, "checks": [{"id": "a", "command": "show clock", "expect": [{"type": "contains", "value": "UTC"}]}]}), [])

    def test_趋势表带规则状态(self):
        hist = []
        for n in (10, 9):
            r = CL.run_checklist(checklist(PREV), lambda dev, n=n: Adapter(routes(n)), history=hist)
            r["started_at"] = f"2026-01-01T00:0{len(hist)}:00+00:00"
            hist.append(r)
        table = CL.trend_table(hist)
        self.assertIn("rule route_count decrease vs previous run", table)
        self.assertIn("baseline | fail(10→9)", table)
        self.assertIn("baseline", CL.build_trend_messages(hist)[0]["content"])


class TestPlanRuns(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.td.name)
        for attr, value in (("PLANS_DIR", root / "plans"), ("RUNS_DIR", root / "runs")):
            p = mock.patch.object(P, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.td.cleanup)

    def test_路由条目模板_计划连跑_基线来自存档(self):
        from tests.test_inspection_plans import _topo

        d = P.recommended_draft(_topo(), devices=["V1"], templates=["route_count"], schedule={"every_minutes": 60})
        d["name"] = "routes"
        P.save_plan(d)
        n = {"v": 12}
        factory = lambda dev: Adapter(routes(n["v"]))  # noqa: E731
        r1, _ = P.run_plan("routes", factory)
        r2, _ = P.run_plan("routes", factory)
        n["v"] = 11
        r3, _ = P.run_plan("routes", factory)
        states = [r["devices"][0]["checks"][0]["evaluations"][0]["state"] for r in (r1, r2, r3)]
        self.assertEqual(states, ["baseline", "pass", "fail"])
        self.assertEqual(r3["devices"][0]["checks"][0]["evaluations"][0]["reference_run_id"], r2["run_id"])
        from netops_ai.inspection import plan_report

        md = plan_report.render_markdown(P.load_plan("routes"), P.history("routes"), None)
        self.assertIn("首次运行，已记录基线", md)
        self.assertIn(f"本次 11 / 对照 12，对照运行 {r2['run_id']}", md)

if __name__ == "__main__":
    unittest.main()

"""试运行：走同一个白名单和适配器（被拒的不发）、超时 / 出错 / 设备连不上分得开、能取消、整体超时、
草案改了指纹就变（页面标「已过期」）、不写任何运行记录；有状态规则显示「首次 / 与上次比」。"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from netops_ai.inspection import dry_run as DR
from netops_ai.inspection import plans as P
from netops_ai.inspection.checklist import run_checklist, save_result


class Adapter:
    """真实适配器的样子：先过白名单（被拒的不发、记下来）。`delay` 模拟慢命令，`error` 模拟设备侧报错。"""

    def __init__(self, outputs=None, delay=0.0, error=""):
        self.outputs, self.delay, self.error, self.sent = outputs or {}, delay, error, []

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
        return SimpleNamespace(allowed=True, denial_reason="", error="", output=self.outputs.get(command, ""))

    def close(self):
        pass


ROUTES = "show ip route | count ^[A-Z][A-Z0-9* ]+[0-9]+[.]"


def draft(*checks, devices=("V1", "V2")):
    return {"name": "dr", "vendor": "cisco", "devices": [{"name": n, "host": f"192.0.2.{i + 10}"} for i, n in enumerate(devices)],
            "checks": list(checks), "schedule": {"every_minutes": 60}}


CLOCK = {"id": "clock", "command": "show clock", "expect": [{"type": "contains", "value": "UTC", "severity": "warning"}]}
ROUTE_CHECK = {"id": "routes", "command": ROUTES, "extract": [{"name": "route_count", "regex": r"=\s*(\d+)", "cast": "int"}],
               "expect": [{"type": "metric_vs_previous", "metric": "route_count", "direction": "decrease", "severity": "critical"}]}


class TestDryRun(unittest.TestCase):
    def test_结果表_耗时行数字节指标和规则预判(self):
        out = {"show clock": "*10:00:00.000 UTC Sun Oct 11 2026\nline2\n", ROUTES: "Number of lines which match regexp = 7\n"}
        r = DR.dry_run(draft(CLOCK, ROUTE_CHECK), lambda dev: Adapter(out))
        self.assertEqual(r["state"], "done")
        c = {x["id"]: x for x in r["devices"][0]["checks"]}
        self.assertEqual((c["clock"]["status"], c["clock"]["lines"], c["clock"]["bytes"]), ("ok", 2, len(out["show clock"].encode())))
        self.assertTrue(c["clock"]["evaluations"][0]["passed"])
        self.assertEqual(c["routes"]["metrics"], {"route_count": 7})
        self.assertEqual(c["routes"]["evaluations"][0]["state"], "baseline")  # 新计划：首次
        self.assertGreaterEqual(c["routes"]["elapsed_ms"], 0)
        self.assertEqual(c["clock"]["output_head"], "*10:00:00.000 UTC Sun Oct 11 2026\nline2")
        self.assertEqual(r["summary"]["ok"], 4)

    def test_有存档时规则跟上一次比(self):
        hist = [run_checklist(draft(ROUTE_CHECK, devices=("V1",)), lambda dev: Adapter({ROUTES: "= 9\n"}))]
        r = DR.dry_run(draft(ROUTE_CHECK, devices=("V1",)), lambda dev: Adapter({ROUTES: "= 8\n"}), history=hist)
        rule = r["devices"][0]["checks"][0]["evaluations"][0]
        self.assertEqual((rule["state"], rule["reference"], rule["current"], rule["passed"]), ("fail", 9, 8, False))
        self.assertEqual(r["summary"]["would_fail"], 1)

    def test_写命令被白名单拒绝不发出(self):
        bad = {"id": "w", "command": "show running-config | redirect flash:x"}
        adapters = []

        def factory(dev):
            adapters.append(Adapter())
            return adapters[-1]

        r = DR.dry_run(draft(CLOCK, bad, devices=("V1",)), factory)
        c = {x["id"]: x for x in r["devices"][0]["checks"]}
        self.assertEqual(c["w"]["status"], "denied")
        self.assertNotIn("show running-config | redirect flash:x", adapters[0].sent)
        r = DR.dry_run(draft({"id": "x", "command": "configure terminal"}, devices=("V1",)), factory)
        self.assertEqual(r["devices"][0]["checks"][0]["status"], "denied")
        self.assertNotIn("configure terminal", adapters[-1].sent)
        with self.assertRaises(ValueError):  # 别的格式错误照样拒绝
            DR.dry_run(draft({"id": "bad id!", "command": "show clock"}), factory)

    def test_超时出错连不上分得开(self):
        def factory(dev):
            if dev.name == "V2":
                raise OSError("connection timed out")
            return Adapter(error="ssh 命令 timed out after 10.0 seconds")

        r = DR.dry_run(draft(CLOCK), factory)
        self.assertEqual(r["devices"][0]["checks"][0]["status"], "timeout")
        self.assertEqual(r["devices"][1]["checks"][0]["status"], "timeout")
        self.assertIn("connection timed out", r["devices"][1]["error"])
        r = DR.dry_run(draft(CLOCK, devices=("V1",)), lambda dev: Adapter(error="% Invalid input"))
        self.assertEqual(r["devices"][0]["checks"][0]["status"], "error")

    def test_取消和整体超时(self):
        cancel = threading.Event()
        seen = []

        def progress(done, total):
            seen.append((done, total))
            cancel.set()  # 第一项做完就取消

        r = DR.dry_run(draft(CLOCK, ROUTE_CHECK, devices=("V1",)), lambda dev: Adapter(delay=0.05), cancel=cancel, progress=progress, max_workers=1)
        self.assertEqual(r["state"], "cancelled")
        self.assertTrue(r["cancelled"])
        self.assertEqual([c["status"] for c in r["devices"][0]["checks"]], ["ok", "cancelled"])
        self.assertEqual(seen[0], (1, 2))
        r = DR.dry_run(draft(CLOCK, devices=("V1",)), lambda dev: Adapter(delay=1.0), total_timeout=0.2)
        self.assertEqual(r["state"], "timeout")
        self.assertEqual(r["devices"][0]["checks"][0]["status"], "timeout")

    def test_草案指纹_改了就过期(self):
        a = draft(CLOCK)
        k = DR.draft_key(a)
        self.assertEqual(DR.draft_key({**a, "schedule": {"every_minutes": 15}}), k)  # 周期不影响试运行结果
        self.assertNotEqual(DR.draft_key({**a, "devices": a["devices"][:1]}), k)
        self.assertNotEqual(DR.draft_key({**a, "checks": [{**CLOCK, "command": "show version"}]}), k)


class TestDryRunApi(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.td.name)
        for attr, value in (("PLANS_DIR", root / "plans"), ("RUNS_DIR", root / "runs")):
            p = mock.patch.object(P, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.td.cleanup)
        from netops_ai.api.app import app

        self.c = TestClient(app)

    def wait(self, job_id, timeout=10):
        t0 = time.time()
        while time.time() - t0 < timeout:
            v = self.c.get(f"/api/inspection/plans/dry-run/{job_id}").json()
            if v["state"] != "running":
                return v
            time.sleep(0.05)
        raise TimeoutError

    def test_接口_后台跑_不写任何运行记录(self):
        with mock.patch("netops_ai.topology._default_adapter_factory", lambda dev: Adapter({"show clock": "10:00 UTC"})):
            r = self.c.post("/api/inspection/plans/dry-run", json={"draft": draft(CLOCK)}).json()
            self.assertEqual(r["total"], 2)
            v = self.wait(r["job_id"])
        self.assertEqual(v["state"], "done")
        self.assertEqual(v["result"]["summary"]["ok"], 2)
        self.assertEqual(v["result"]["draft_key"], r["draft_key"])
        self.assertFalse(Path(P.RUNS_DIR).exists() and any(Path(P.RUNS_DIR).iterdir()))  # 没有写运行记录
        self.assertEqual(P.list_plans(), [])

    def test_接口_设备子集_取消_参数校验(self):
        self.assertEqual(self.c.post("/api/inspection/plans/dry-run", json={"draft": {"devices": [], "checks": []}}).status_code, 400)
        self.assertEqual(self.c.post("/api/inspection/plans/dry-run", json={"draft": draft(CLOCK), "devices": ["X9"]}).status_code, 400)
        with mock.patch("netops_ai.topology._default_adapter_factory", lambda dev: Adapter(delay=0.3)):
            r = self.c.post("/api/inspection/plans/dry-run", json={"draft": draft(CLOCK, ROUTE_CHECK), "devices": ["V2"]}).json()
            self.assertEqual(r["total"], 2)
            self.c.delete(f"/api/inspection/plans/dry-run/{r['job_id']}")
            v = self.wait(r["job_id"])
        self.assertEqual(v["state"], "cancelled")
        self.assertEqual([d["name"] for d in v["result"]["devices"]], ["V2"])
        self.assertEqual(self.c.get("/api/inspection/plans/dry-run/nope").status_code, 404)

    def test_存过的计划_规则对照它的存档(self):
        d = draft(ROUTE_CHECK, devices=("V1",))
        P.save_plan(d)
        save_result(run_checklist(d, lambda dev: Adapter({ROUTES: "= 9\n"})), P.RUNS_DIR)
        with mock.patch("netops_ai.topology._default_adapter_factory", lambda dev: Adapter({ROUTES: "= 9\n"})):
            r = self.c.post("/api/inspection/plans/dry-run", json={"draft": d}).json()
            v = self.wait(r["job_id"])
        rule = v["result"]["devices"][0]["checks"][0]["evaluations"][0]
        self.assertEqual((rule["state"], rule["reference"]), ("pass", 9))
        self.assertEqual(len(P.history("dr")), 1)  # 试运行没加记录


if __name__ == "__main__":
    unittest.main()

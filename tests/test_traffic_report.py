"""链路流量报告：周期、取数配对、汇总（利用率 / 环比 / Top / 无数据）、PDF 生成、接口。Zabbix 用假客户端，输出写临时目录。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from netops_ai.inspection import traffic_report as TR

TZ = timezone(timedelta(hours=8))
HOUR = 3600


class FakeZbx:
    """两台主机：R1（Gi0/1 跑得高、有速率；Gi0/2 没数据）、R2（Gi0/0 速率未知）。"""

    def __init__(self, scale_prev: float = 1.0):
        self.scale_prev = scale_prev
        self.calls = []

    def list_hosts(self):
        return [{"hostid": "1", "host": "R1"}, {"hostid": "2", "host": "R2"}]

    def list_items(self, host_id):
        def it(i, name, last=""):
            return {"itemid": str(i), "name": name, "status": "0", "lastvalue": last}
        if host_id == "1":
            return [it(11, "Interface Gi0/1(): Bits received"), it(12, "Interface Gi0/1(): Bits sent"), it(13, "Interface Gi0/1(): Speed", "1000000000"),
                    it(14, "Interface Gi0/2(): Bits received"), it(15, "Interface Gi0/2(): Bits sent"),
                    it(16, "Interface Gi0/1(): Operational status"), {"itemid": "17", "name": "Interface Gi0/9(): Bits sent", "status": "1"}]
        return [it(21, "Interface Gi0/0(): Bits received"), it(22, "Interface Gi0/0(): Bits sent"), it(23, "Interface Gi0/0(): Speed", "0")]

    def get_trends(self, item_id, *, time_from=None, time_till=None, limit=100):
        self.calls.append((item_id, time_from, time_till))
        if item_id in ("14", "15"):
            return []
        base = {"11": 400e6, "12": 900e6, "21": 1e3, "22": 2e3}[item_id]
        n = max(1, int((time_till - time_from) // HOUR))
        # 上一个周期（更早的那个窗口）流量减半 → 本周期是它的 2 倍
        scale = 1.0 if getattr(self, "_cur_from", None) is None or time_from >= self._cur_from else 0.5
        return [{"clock": str(time_from + i * HOUR), "value_min": str(base * 0.5 * scale), "value_avg": str(base * scale),
                 "value_max": str(base * 1.1 * scale if i == 3 else base * scale)} for i in range(min(n, 24))]


class TestPeriods(unittest.TestCase):
    def test_日_周_月(self):
        now = datetime(2026, 10, 17, 9, 30, tzinfo=TZ)  # 周六
        self.assertEqual(TR.period_range("daily", now), (datetime(2026, 10, 16, tzinfo=TZ), datetime(2026, 10, 17, tzinfo=TZ)))
        s, e = TR.period_range("weekly", now)
        self.assertEqual((s.date().isoformat(), e.date().isoformat(), s.weekday()), ("2026-10-05", "2026-10-12", 0))
        s, e = TR.period_range("monthly", now)
        self.assertEqual((s.date().isoformat(), e.date().isoformat()), ("2026-09-01", "2026-10-01"))
        with self.assertRaises(ValueError):
            TR.period_range("yearly", now)

    def test_跨年的月报(self):
        s, e = TR.period_range("monthly", datetime(2027, 1, 5, tzinfo=TZ))
        self.assertEqual((s.date().isoformat(), e.date().isoformat()), ("2026-12-01", "2027-01-01"))

    def test_上一周期(self):
        s, e = TR.period_range("monthly", datetime(2026, 10, 17, tzinfo=TZ))
        ps, pe = TR.previous_range("monthly", s, e)
        self.assertEqual((ps.date().isoformat(), pe.date().isoformat()), ("2026-08-01", "2026-09-01"))
        s, e = TR.period_range("weekly", datetime(2026, 10, 17, tzinfo=TZ))
        ps, pe = TR.previous_range("weekly", s, e)
        self.assertEqual((pe, e - ps), (s, timedelta(days=14)))


class TestCollectSummarize(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 16, tzinfo=TZ)
        self.end = self.start + timedelta(hours=24)

    def collect(self, **kw):
        return TR.collect(FakeZbx(), self.start, self.end, **kw)

    def test_按接口配对_停用的监控项和没有成对数据的不算(self):
        data = self.collect()
        self.assertEqual(sorted(data["R1"]), ["Gi0/1", "Gi0/2"])  # Gi0/9 被停用；Operational status 不是流量项
        self.assertEqual(data["R1"]["Gi0/1"]["speed"], 1_000_000_000)
        self.assertIsNone(data["R2"]["Gi0/0"]["speed"])  # 速率 0 = 未知

    def test_只要指定的主机和接口(self):
        self.assertEqual(list(self.collect(hosts=["R2"])), ["R2"])
        self.assertEqual(list(self.collect(interfaces={"R1": ["Gi0/1"]})["R1"]), ["Gi0/1"])

    def test_汇总_利用率_Top_高利用率_没有数据(self):
        s = TR.summarize(self.collect())
        by = {(r["host"], r["iface"]): r for r in s["interfaces"]}
        r = by[("R1", "Gi0/1")]
        self.assertAlmostEqual(r["peak_bps"], 990e6, delta=1)
        self.assertAlmostEqual(r["peak_util_pct"], 99.0)
        self.assertEqual(s["top"][0]["iface"], "Gi0/1")
        self.assertEqual([x["iface"] for x in s["high_util"]], ["Gi0/1"])
        self.assertTrue(by[("R1", "Gi0/2")]["no_data"])
        self.assertEqual(len(s["no_data"]), 1)
        self.assertIsNone(by[("R2", "Gi0/0")]["peak_util_pct"])  # 速率未知不算利用率

    def test_环比(self):
        cur = self.collect()
        prev = {h: {i: {**d, "in": [(t, lo / 2, av / 2, hi / 2) for t, lo, av, hi in d["in"]],
                         "out": [(t, lo / 2, av / 2, hi / 2) for t, lo, av, hi in d["out"]]} for i, d in ifs.items()} for h, ifs in cur.items()}
        s = TR.summarize(cur, prev)
        r = next(x for x in s["interfaces"] if x["iface"] == "Gi0/1")
        self.assertEqual((r["in_change_pct"], r["out_change_pct"]), (100.0, 100.0))
        self.assertTrue(s["movers"])

    def test_没有上一周期_环比留空(self):
        s = TR.summarize(self.collect(), None)
        self.assertTrue(all(r.get("in_change_pct") is None for r in s["interfaces"] if not r.get("no_data")))

    def test_单位和事实清单(self):
        self.assertEqual(TR.fmt_bps(1_500_000_000), "1.50 Gbps")
        self.assertEqual(TR.fmt_bps(2_500), "2.50 Kbps")
        self.assertEqual(TR.fmt_bps(None), "-")
        facts = TR.facts_text(TR.summarize(self.collect()), "测试")
        self.assertIn("R1 Gi0/1", facts)
        self.assertIn("高利用率", facts)


class TestPdf(unittest.TestCase):
    def test_出PDF_含中文_narrate失败不挡住(self):
        with tempfile.TemporaryDirectory() as d:
            now = datetime(2026, 10, 17, 9, 0, tzinfo=TZ)

            def boom(_):
                raise RuntimeError("llm down")
            res = TR.generate("daily", FakeZbx(), Path(d), now=now, narrate=boom)
            data = Path(res["path"]).read_bytes()
            self.assertTrue(data.startswith(b"%PDF"))
            self.assertGreater(len(data), 3000)
            self.assertEqual(res["narrative"], "")
            res2 = TR.generate("weekly", FakeZbx(), Path(d), now=now, narrate=lambda f: "本周整体平稳。")
            self.assertEqual(res2["narrative"], "本周整体平稳。")
            self.assertTrue(Path(res2["path"]).read_bytes().startswith(b"%PDF"))
            self.assertTrue(Path(res["path"]).with_suffix(".png").exists())

    def test_全部没数据也能出报告(self):
        class Empty(FakeZbx):
            def get_trends(self, *a, **k):
                return []
        with tempfile.TemporaryDirectory() as d:
            res = TR.generate("monthly", Empty(), Path(d), now=datetime(2026, 10, 17, tzinfo=TZ))
            self.assertTrue(Path(res["path"]).read_bytes().startswith(b"%PDF"))
            self.assertEqual(res["summary"]["count"], 0)


class TestApi(unittest.TestCase):
    def test_接口_坏kind_400_取不到数据_502(self):
        from fastapi.testclient import TestClient

        from netops_ai.api import traffic_reports as API
        from netops_ai.api.app import app

        c = TestClient(app)
        self.assertEqual(c.get("/api/reports/traffic/yearly").status_code, 400)
        old = API._zabbix

        def broken():
            raise RuntimeError("no zabbix")
        API._zabbix = broken
        try:
            r = c.get("/api/reports/traffic/daily")
            self.assertEqual(r.status_code, 502)
            self.assertIn("could not build", r.json()["message"])
        finally:
            API._zabbix = old

    def test_接口_成功返回pdf(self):
        from fastapi.testclient import TestClient

        from netops_ai.api import traffic_reports as API
        from netops_ai.api.app import app

        old_z, old_d = API._zabbix, API.report_dir
        with tempfile.TemporaryDirectory() as d:
            class Ctx(FakeZbx):
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            API._zabbix, API.report_dir = (lambda: Ctx()), (lambda: Path(d))
            try:
                r = TestClient(app).get("/api/reports/traffic/daily", params={"host": ["R1"]})
                self.assertEqual((r.status_code, r.headers["content-type"]), (200, "application/pdf"))
                self.assertTrue(r.content.startswith(b"%PDF"))
            finally:
                API._zabbix, API.report_dir = old_z, old_d


class TestHtmlReportsApi(unittest.TestCase):
    """CVE / 拓扑发现按需生成的 HTML 报告接口：取不到数据 502，成功返回 HTML。设备和外部数据源都用假的。"""

    def test_cve_成功返回html_失败502(self):
        from types import SimpleNamespace
        from unittest import mock

        from fastapi.testclient import TestClient

        from netops_ai.api import traffic_reports as API
        from netops_ai.api.app import app
        from netops_ai.inspection import cve_report as CR

        class Ad:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def run(self, cmd):
                return SimpleNamespace(allowed=True, denial_reason="", error="", output="Cisco IOS Software, IOSv Software, Version 15.9(3)M9, RELEASE SOFTWARE")

        def net(url, headers):
            return (200, b'{"vulnerabilities": []}') if "cisa" in url else (200, b'{"resultsPerPage":0,"totalResults":0,"vulnerabilities":[]}')
        import tempfile
        from pathlib import Path

        real = CR.Sources
        with tempfile.TemporaryDirectory() as d, mock.patch.object(API, "_topology_and_factory", lambda: ({"R1": 1}, lambda dev: Ad())),                 mock.patch.object(CR, "Sources", lambda: real(root=Path(d), fetch=net, api_key="")):
            r = TestClient(app).get("/api/reports/cve")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("text/html", r.headers["content-type"])
        self.assertIn("R1", r.text)
        with mock.patch.object(API, "_topology_and_factory", lambda: (_ for _ in ()).throw(RuntimeError("no topology"))):
            r = TestClient(app).get("/api/reports/cve")
        self.assertEqual(r.status_code, 502)

    def test_拓扑发现_失败502(self):
        from unittest import mock

        from fastapi.testclient import TestClient

        from netops_ai.api import traffic_reports as API
        from netops_ai.api.app import app

        with mock.patch.object(API, "_topology_and_factory", lambda: (_ for _ in ()).throw(RuntimeError("no topology"))):
            r = TestClient(app).get("/api/reports/topology")
        self.assertEqual(r.status_code, 502)
        self.assertIn("could not build", r.json()["message"])


if __name__ == "__main__":
    unittest.main()

"""历史告警列表（筛选 / 分页 / 同类折叠）和设备排查历史（去重 / 统计）。

**全部用临时目录里自建的合成数据**，不读 tests/fixtures/records 或真实 records/：
那些目录导出公开版时会被排除，而且真实目录一直在长，断言会随数据漂。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from netops_ai.api import incident_history as ih
from netops_ai.api.app import app
from netops_ai.topology import TopologyDevice

NOW = 1_800_000_000
DAY = 86400

TOPO = {
    "V1": TopologyDevice(name="V1", host="192.0.2.11", links=(), aliases=("V1-vios",), role="core"),
    "D1": TopologyDevice(name="D1", host="192.0.2.12", links=(), aliases=("D1-vios",), role="aggregation"),
}


def _inc(iid: str, fp: str, host: str, clock: int, *, name: str = "Interface Gi0/1 link down", conf: str = "high",
         supported: tuple[str, ...] = ("local_action",), root: str = "接口被人为关闭", alerts: int = 1,
         elapsed: float = 60.0) -> dict:
    """形状跟 `dashboard.build_incidents` 的输出一致（只放这里用得到的字段）。"""
    checklist = {k: {"status": "有证据支持" if k in supported else "已排除", "reason": ""}
                 for k in ("local_action", "remote_or_upstream", "link_or_path_quality")}
    return {
        "incident_id": iid, "fingerprint": fp, "host": host, "clock": clock, "headline": "", "root_cause": root,
        "confidence": conf, "repeat_count": 0, "alert_count": alerts,
        "alerts": [{"eventid": iid, "name": name}], "checklist": checklist,
        "elapsed_total": elapsed, "incomplete": False,
    }


INCIDENTS = [
    _inc("a-1", "fpA", "V1-vios", NOW - 1 * 3600),
    _inc("a-2", "fpA", "V1-vios", NOW - 3 * DAY),
    _inc("a-3", "fpA", "V1-vios", NOW - 20 * DAY, supported=("remote_or_upstream",), conf="medium"),
    _inc("b-1", "fpB", "D1-vios", NOW - 2 * 3600, name="BGP peer 10.0.0.2 down", conf="low", root=""),
    _inc("c-1", "fpC", "V1-vios", NOW - 40 * DAY, name="OSPF neighbor down", elapsed=120.0),
    _inc("d-1", "fpD", "D1-vios / V1-vios", NOW - 5 * DAY, name="Device restarted (coldStart)", alerts=3),
]


def q(**kw):
    return ih.query_incidents(INCIDENTS, now=NOW, topology=TOPO, **kw)


class FaultTypeTests(unittest.TestCase):
    def test_类型规则跟前端一致_接口优先于路由协议(self):
        self.assertEqual(ih.fault_type_of({"alerts": [{"name": "Interface Gi0/1 down"}, {"name": "OSPF neighbor down"}]}), "interfaceDown")
        self.assertEqual(ih.fault_type_of({"alerts": [{"name": "BGP peer down"}]}), "bgp")
        self.assertEqual(ih.fault_type_of({"alerts": [{"name": "x"}], "root_cause": "设备重启了"}), "restart")
        self.assertEqual(ih.fault_type_of({"alerts": []}), "other")

    def test_需人工只看有没有下定论(self):
        self.assertTrue(ih.needs_human({"confidence": "low", "root_cause": "x"}))
        self.assertTrue(ih.needs_human({"confidence": "high", "root_cause": ""}))
        self.assertFalse(ih.needs_human({"confidence": "high", "root_cause": "x", "incomplete": True}))


class QueryTests(unittest.TestCase):
    def test_同一指纹折叠成一行并标出次数(self):
        r = q()
        self.assertEqual(r["total_incidents"], 6)
        self.assertEqual(r["total_rows"], 4)
        row_a = next(x for x in r["rows"] if x["fingerprint"] == "fpA")
        self.assertEqual(row_a["count"], 3)
        self.assertEqual([o["incident_id"] for o in row_a["occurrences"]], ["a-1", "a-2", "a-3"])  # 新的在前
        self.assertEqual(row_a["latest"]["incident_id"], "a-1")

    def test_结论一致性按排查方向比_少数派标不一致(self):
        row_a = next(x for x in q()["rows"] if x["fingerprint"] == "fpA")
        self.assertEqual([o["consistent"] for o in row_a["occurrences"]], [True, True, False])
        single = next(x for x in q()["rows"] if x["fingerprint"] == "fpB")
        self.assertIsNone(single["occurrences"][0]["consistent"])  # 只有一次的不标

    def test_行按最近一次出现倒序(self):
        self.assertEqual([x["fingerprint"] for x in q()["rows"]], ["fpA", "fpB", "fpD", "fpC"])

    def test_时间范围(self):
        self.assertEqual(q(range_="24h")["total_incidents"], 2)
        self.assertEqual(q(range_="7d")["total_incidents"], 4)
        self.assertEqual(q(range_="30d")["total_incidents"], 5)
        self.assertEqual(q(range_="all")["total_incidents"], 6)
        self.assertEqual(q(range_="bogus")["range"], "all")  # 不认识的值当「全部」，不 500

    def test_设备筛选认拓扑名也认监控主机名_合并多台的故障两边都算(self):
        by_name = q(device="V1")
        by_alias = q(device="V1-vios")
        self.assertEqual(by_name["total_incidents"], 5)  # a×3 + c + d（d 是 D1/V1 合并的那次）
        self.assertEqual(by_alias["total_incidents"], 5)
        self.assertEqual(q(device="D1")["total_incidents"], 2)

    def test_类型置信度需人工筛选(self):
        self.assertEqual(q(fault_type="bgp")["total_incidents"], 1)
        self.assertEqual(q(fault_type="restart")["rows"][0]["fingerprint"], "fpD")
        self.assertEqual(q(confidence="medium")["total_incidents"], 1)
        self.assertEqual(q(unresolved=True)["total_incidents"], 1)
        # 筛选是按次过滤之后再折叠：中置信只剩 a-3，那一行就只算 1 次
        self.assertEqual(q(confidence="medium")["rows"][0]["count"], 1)

    def test_筛选候选值不受其它筛选影响(self):
        r = q(device="D1", range_="30d")
        self.assertEqual({d["value"] for d in r["facets"]["devices"]}, {"V1", "D1"})
        self.assertEqual(r["facets"]["in_range"], 5)

    def test_分页按折叠后的行(self):
        p1, p2, p3 = q(page_size=3), q(page_size=3, page=2), q(page_size=3, page=3)
        self.assertEqual(len(p1["rows"]), 3)
        self.assertTrue(p1["has_more"])
        self.assertEqual(len(p2["rows"]), 1)
        self.assertFalse(p2["has_more"])
        self.assertEqual(p3["rows"], [])
        self.assertEqual(q(page_size=10_000)["page_size"], ih.PAGE_SIZE_MAX)
        self.assertEqual(q(page=0)["page"], 1)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _status(at: str, v1_bad: bool) -> dict:
    checks = [{"check": "reachable", "status": "bad" if v1_bad else "ok", "summary": "设备无法登录" if v1_bad else "ok"}]
    return {"checked_at": at, "devices": [
        {"name": "V1", "checks": checks},
        {"name": "D1", "checks": [{"check": "ospf", "status": "warn", "summary": "OSPF 邻居少一个"}]},
    ]}


class DeviceHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        hist = root / "inspection-history"
        # 基础巡检：V1 连着 3 份连不上 → 好了一份 → 又连不上 1 份。应该是两条，不是四条。
        for i, (stamp, bad) in enumerate([("20270115T000000000000Z", True), ("20270115T010000000000Z", True),
                                          ("20270115T020000000000Z", True), ("20270115T030000000000Z", False),
                                          ("20270115T040000000000Z", True)]):
            _write(hist / f"status-{stamp}.json", _status(f"2027-01-15 0{i}:00:00 UTC", bad))
        # 趋势：监控主机名是别名 V1-vios，也要算到 V1 头上；同一条连续两份 → 一条
        for i, stamp in enumerate(["20270115T000000000000Z", "20270115T010000000000Z"]):
            _write(hist / f"trend-{stamp}.json", {"scanned_at": f"2027-01-15 0{i}:00:00 UTC", "findings": [
                {"kind": "periodic_spike", "host_name": "V1-vios", "item_name": "CPU utilization", "item_key": "cpu"},
                {"kind": "trend", "host_name": "Zabbix server", "item_name": "x", "item_key": "x"},
            ]})
        # 自定义巡检：一次失败
        _write(root / "checklist-runs" / "20270115T000000Z-core-abcd.json", {
            "checklist": "core", "started_at": "2027-01-15T00:00:00+00:00",
            "devices": [{"name": "V1", "checks": [{"id": "bgp-up", "status": "fail", "detail": "0 个 BGP 会话"},
                                                  {"id": "cpu", "status": "pass"}]}],
        })
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def hist(self, name="V1"):
        return ih.build_device_history(name, INCIDENTS, now=NOW, topology=TOPO, records_dir=self.root)

    def test_事件按设备归属_含多台合并的那次(self):
        ids = {e["incident_id"] for e in self.hist()["entries"] if e["kind"] == "incident"}
        self.assertEqual(ids, {"a-1", "a-2", "a-3", "c-1", "d-1"})

    def test_巡检连续出现的同一条只算一条_断开再出现另起一条(self):
        basic = [e for e in self.hist()["entries"] if e["kind"] == "basic"]
        self.assertEqual(len(basic), 2)
        first = min(basic, key=lambda e: e["at"])
        self.assertEqual(first["occurrences"], 3)
        self.assertLess(first["at"], first["last_at"])
        self.assertTrue(all(e["same_type"] for e in basic))  # 同一条发现出现过两段，都标同类

    def test_趋势按别名归到设备_其它主机不混进来(self):
        trend = [e for e in self.hist()["entries"] if e["kind"] == "trend"]
        self.assertEqual(len(trend), 1)
        self.assertEqual(trend[0]["occurrences"], 2)
        self.assertEqual(trend[0]["title"], "CPU utilization")

    def test_自定义巡检只收失败项(self):
        plan = [e for e in self.hist()["entries"] if e["kind"] == "plan"]
        self.assertEqual([(e["plan"], e["type"], e["status"]) for e in plan], [("core", "bgp-up", "fail")])

    def test_别的设备的巡检发现不混进来(self):
        d1 = self.hist("D1")
        kinds = {(e["kind"], e["type"]) for e in d1["entries"] if e["kind"] != "incident"}
        self.assertEqual(kinds, {("basic", "ospf")})
        self.assertEqual(d1["entries"][[e["kind"] for e in d1["entries"]].index("basic")]["occurrences"], 5)

    def test_倒序(self):
        lasts = [e["last_at"] for e in self.hist()["entries"]]
        self.assertEqual(lasts, sorted(lasts, reverse=True))

    def test_同类标记(self):
        by_id = {e["incident_id"]: e for e in self.hist()["entries"] if e["kind"] == "incident"}
        self.assertTrue(by_id["a-1"]["same_type"])
        self.assertEqual(by_id["a-1"]["same_type_count"], 3)
        self.assertFalse(by_id["c-1"]["same_type"])

    def test_统计只算近30天的事件(self):
        s = self.hist()["stats"]
        self.assertEqual(s["incidents_30d"], 4)  # c-1 是 40 天前的
        self.assertEqual(s["top_type"], "interfaceDown")
        self.assertEqual(s["top_type_count"], 3)
        self.assertEqual(s["avg_elapsed"], 60.0)
        self.assertEqual(s["needs_human_30d"], 0)
        self.assertEqual(s["incidents_total"], 5)
        self.assertEqual(self.hist("D1")["stats"]["needs_human_30d"], 1)

    def test_拓扑里没有的名字也不报错(self):
        h = ih.build_device_history("nope", INCIDENTS, now=NOW, topology=TOPO, records_dir=self.root)
        self.assertFalse(h["known"])
        self.assertEqual(h["entries"], [])


class RouteTests(unittest.TestCase):
    """新接口挂上了、旧 /api/incidents 的形状没变（还是数组）。不碰真实 records：数据源整个替换掉。"""

    def setUp(self):
        self.client = TestClient(app)
        self.tmp = tempfile.TemporaryDirectory()
        patches = [
            mock.patch.object(ih, "cached_incidents", return_value=INCIDENTS),
            mock.patch.object(ih, "_topology", return_value=TOPO),
            mock.patch("netops_ai.api.dashboard.RECORDS_DIR", Path(self.tmp.name)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_列表接口带筛选和分页(self):
        r = self.client.get("/api/incidents/list", params={"device": "V1", "page_size": 1, "range": "all"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["device"], "V1")
        self.assertEqual(len(body["rows"]), 1)
        self.assertTrue(body["has_more"])

    def test_详情接口按ID取_不存在给404(self):
        r = self.client.get("/api/incidents/a-2")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["fault_type"], "interfaceDown")
        self.assertEqual(r.json()["devices"], ["V1"])
        self.assertEqual(self.client.get("/api/incidents/nope").status_code, 404)

    def test_设备历史接口(self):
        r = self.client.get("/api/devices/V1-vios/history")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["device"], "V1")

    def test_旧接口还是数组(self):
        with mock.patch("netops_ai.api.dashboard.build_incidents", return_value=INCIDENTS):
            r = self.client.get("/api/incidents")
        self.assertIsInstance(r.json(), list)


if __name__ == "__main__":
    unittest.main()

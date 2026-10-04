"""巡检配置（inspection.yaml）+ 每条发现的 rule/series 字段。

**所有读写都指向临时目录**（环境变量 `NETOPS_INSPECTION_CONFIG`），不碰仓库根的真实 inspection.yaml，
也不碰 records/。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from netops_ai.api import dashboard
from netops_ai.api.app import app
from netops_ai.inspection import config as icfg
from netops_ai.inspection import detectors
from netops_ai.inspection.explain import MAX_SERIES_POINTS, downsample
from netops_ai.inspection.scan import scan_all_hosts, scan_host

REAL_CONFIG = icfg.CONFIG_PATH


def _hist(pairs):
    return [{"clock": str(c), "value": str(v)} for c, v in pairs]


def _flap_series():
    series, t = [], 0
    for _ in range(3):
        series += [(t, 2), (t + 300, 1)]
        t += 900
    return series


def _fake_zbx():
    """一台主机三个监控项，各命中一类检测器；外加一个什么都不命中的。"""
    series_by_item = {
        "1": [(i * 7200, 10.0 + i * 2) for i in range(12)],  # 趋势
        "2": [(d * 86400 + h * 3600, 100.0 if h == 3 else 10.0) for d in range(4) for h in range(24)],  # 周期冲高
        "3": _flap_series(),  # 自愈抖动
        "4": [(i * 7200, 5.0) for i in range(12)],  # 平的
    }
    zbx = mock.MagicMock()
    zbx.list_hosts.return_value = [{"hostid": "10", "host": "V1"}, {"hostid": "11", "host": "LAB-X"}]
    zbx.list_items.return_value = [
        {"itemid": "1", "name": "CPU", "key_": "system.cpu.util", "value_type": 0},
        {"itemid": "2", "name": "Traffic", "key_": "net.if.in[Gi0/1]", "value_type": 3},
        {"itemid": "3", "name": "Gi0/1 status", "key_": "net.if.status[ifOperStatus.2]", "value_type": 0},
        {"itemid": "4", "name": "Flat", "key_": "flat.metric", "value_type": 0},
    ]
    zbx.get_history.side_effect = lambda itemid, **kw: _hist(series_by_item[itemid])
    return zbx


def _flat(report):
    return [(f.host_name, f.item_name, f.item_key, asdict(f.finding)) for f in report.findings]


class TestDefaultsComeFromDetectors(unittest.TestCase):
    def test_默认值就是检测器函数签名里的默认值(self):
        d = icfg.default_detector_params()
        self.assertEqual(d["trend"]["min_relative_change"], 0.2)
        self.assertEqual(d["trend"]["monotonic_fraction_threshold"], 0.7)
        self.assertEqual(d["trend"]["min_points"], 6)
        self.assertEqual(d["periodic_spike"]["spike_ratio_threshold"], 1.5)
        self.assertEqual(d["self_healing_flap"], {"down_value": 2, "up_value": 1, "min_flaps": 2, "max_recovery_seconds": 3600})

    def test_每个检测器的每个参数都有元信息(self):
        for name, params in icfg.default_detector_params().items():
            self.assertEqual(set(params), set(icfg.PARAM_META[name]))


class TestNoFileEqualsToday(unittest.TestCase):
    """对照实验：同一份输入，只有"有没有配置文件"这一个变量，发现结果必须逐条相同。"""

    def test_有无默认配置文件发现结果逐条相同(self):
        baseline = scan_all_hosts(_fake_zbx())  # 不传 config = 改动前的行为
        self.assertGreaterEqual(len(baseline.findings), 3)
        self.assertEqual({f.kind for f in baseline.findings}, {"trend", "periodic_spike", "self_healing_flap"})

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "inspection.yaml"
            no_file = scan_all_hosts(_fake_zbx(), config=icfg.load_config(missing))

            explicit = Path(tmp) / "explicit.yaml"
            icfg.save_config(icfg.default_detector_params(), icfg.default_scope(), path=explicit)
            self.assertTrue(explicit.exists())
            with_file = scan_all_hosts(_fake_zbx(), config=icfg.load_config(explicit))

        for other in (no_file, with_file):
            self.assertEqual(_flat(baseline), _flat(other))
            self.assertEqual([f.rule for f in baseline.findings], [f.rule for f in other.findings])
            self.assertEqual([f.series for f in baseline.findings], [f.series for f in other.findings])
            self.assertEqual(
                (baseline.scanned_hosts, baseline.scanned_items, baseline.items_with_data),
                (other.scanned_hosts, other.scanned_items, other.items_with_data),
            )

    def test_scan结果跟直接调检测器不传参数一致(self):
        """scan 把配置里的阈值以关键字传进去，默认配置下必须等于"不传"。"""
        report = scan_host(_fake_zbx(), "10", "V1")
        by_key = {f.item_key: f.finding for f in report.findings}
        self.assertEqual(by_key["system.cpu.util"], detectors.detect_trend(
            [(i * 7200, 10.0 + i * 2) for i in range(12)], higher_is_worse=None))
        self.assertEqual(by_key["net.if.status[ifOperStatus.2]"], detectors.detect_self_healing_flap(_flap_series()))

    def test_改一个阈值才会变(self):
        """另一半对照：把趋势的最小变化调到 100 倍，趋势发现消失，其余两类不变。"""
        base = scan_host(_fake_zbx(), "10", "V1")
        cfg = icfg.InspectionConfig()
        cfg.detectors["trend"]["min_relative_change"] = 100.0
        changed = scan_host(_fake_zbx(), "10", "V1", config=cfg)
        self.assertEqual({f.kind for f in base.findings} - {f.kind for f in changed.findings}, {"trend"})
        self.assertEqual([_t for _t in _flat(base) if _t[3]["kind"] != "trend"], _flat(changed))


class TestScope(unittest.TestCase):
    def test_排除主机和监控项(self):
        cfg = icfg.InspectionConfig()
        cfg.scope["exclude_hosts"] = ["lab-*"]
        rep = scan_all_hosts(_fake_zbx(), config=cfg)
        self.assertEqual(rep.scanned_hosts, 1)

        cfg = icfg.InspectionConfig()
        cfg.scope["include_item_keys"] = ["net.if.*"]
        rep = scan_host(_fake_zbx(), "10", "V1", config=cfg)
        self.assertEqual(rep.scanned_items, 2)
        self.assertNotIn("trend", {f.kind for f in rep.findings})


class TestRuleAndSeries(unittest.TestCase):
    def test_每条发现带rule和不超过60点的series(self):
        for f in scan_host(_fake_zbx(), "10", "V1").findings:
            self.assertIsNotNone(f.rule, f.kind)
            self.assertEqual(f.rule["kind"], f.kind)
            self.assertTrue(f.rule["checks"])
            self.assertTrue(f.rule["summary"])
            self.assertTrue(f.rule["params"])
            self.assertLessEqual(len(f.series), MAX_SERIES_POINTS)
            self.assertTrue(all(c["passed"] for c in f.rule["checks"]), f.rule)

    def test_趋势规则文案是阈值对实际值(self):
        f = next(x for x in scan_host(_fake_zbx(), "10", "V1").findings if x.kind == "trend")
        texts = [c["text"] for c in f.rule["checks"]]
        self.assertTrue(any("前后段均值变化" in t and "≥ 20%" in t for t in texts), texts)
        self.assertTrue(any("相邻点同向" in t and "≥ 70%" in t for t in texts), texts)

    def test_降采样保留首尾和极值(self):
        series = [(i, 1.0) for i in range(1000)]
        series[500] = (500, 99.0)
        out = downsample(series)
        self.assertLessEqual(len(out), MAX_SERIES_POINTS)
        self.assertIn(99.0, [v for _, v in out])
        self.assertEqual(out[0][0] // 34, 0)
        self.assertEqual([c for c, _ in out], sorted(c for c, _ in out))

    def test_短序列原样返回(self):
        self.assertEqual(downsample([(1, 2.0), (2, 3.0)]), [[1, 2.0], [2, 3.0]])


class TestValidation(unittest.TestCase):
    def test_类型和范围被拒绝(self):
        bad = [
            {"trend": {"min_points": "6"}},
            {"trend": {"min_points": True}},
            {"trend": {"min_points": 1}},
            {"trend": {"min_points": 6.5}},
            {"trend": {"monotonic_fraction_threshold": 1.5}},
            {"trend": {"min_relative_change": float("nan")}},
            {"periodic_spike": {"spike_ratio_threshold": 0.5}},
            {"self_healing_flap": {"down_value": 1, "up_value": 1}},
            {"self_healing_flap": {"max_recovery_seconds": -1}},
            {"trend": {"no_such_param": 1}},
            {"no_such_detector": {}},
            {"trend": [1]},
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(icfg.ConfigError):
                icfg.validate_detectors(raw)

    def test_整数样的浮点被接受(self):
        out = icfg.validate_detectors({"trend": {"min_points": 8.0}})
        self.assertEqual(out["trend"]["min_points"], 8)
        self.assertIsInstance(out["trend"]["min_points"], int)

    def test_范围列表和lookback(self):
        icfg.validate_scope({"include_hosts": ["V*"], "lookback_days": 3})
        for raw in ({"include_hosts": "V1"}, {"include_hosts": [""]}, {"lookback_days": 0}, {"lookback_days": 91}, {"x": 1},
                    {"exclude_item_keys": ["a\nb"]}):
            with self.subTest(raw=raw), self.assertRaises(icfg.ConfigError):
                icfg.validate_scope(raw)

    def test_忽略项校验(self):
        icfg.normalize_ignore_entry({"host": "V1", "item_key": "k", "reason": "r"})
        for raw in ({"host": "", "item_key": "k"}, {"host": "V1"}, {"host": "V1", "item_key": "k", "reason": "x" * 201},
                    {"host": "V1", "item_key": "k", "path": "/etc"}):
            with self.subTest(raw=raw), self.assertRaises(icfg.ConfigError):
                icfg.normalize_ignore_entry(raw)

    def test_手改坏的文件不悄悄退回默认(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "inspection.yaml"
            p.write_text("detectors:\n  trend:\n    min_points: 0\n", encoding="utf-8")
            with self.assertRaises(icfg.ConfigError):
                icfg.load_config(p)
            p.write_text("detectors: [\n", encoding="utf-8")
            with self.assertRaises(icfg.ConfigError):
                icfg.load_config(p)


class TestFileRoundTrip(unittest.TestCase):
    def test_保存保留人手写的注释和忽略清单(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "inspection.yaml"
            icfg.save_config({"trend": {"min_points": 9}}, {"include_hosts": ["V*"]}, path=p)
            icfg.add_ignore("V1", "icmpping", "测试", path=p)
            text = p.read_text(encoding="utf-8")
            p.write_text(text.replace("min_points: 9", "min_points: 9  # 手写的备注"), encoding="utf-8")
            icfg.save_config({"trend": {"min_points": 10}}, {}, path=p)
            after = p.read_text(encoding="utf-8")
            self.assertIn("# 手写的备注", after.replace("min_points: 10  # 手写的备注", "min_points: 10  # 手写的备注"))
            cfg = icfg.load_config(p)
            self.assertEqual(cfg.detectors["trend"]["min_points"], 10)
            self.assertEqual(cfg.detectors["trend"]["min_relative_change"], 0.2)
            self.assertEqual(cfg.scope["include_hosts"], [])  # 没写 = 默认
            self.assertEqual([e["item_key"] for e in cfg.ignore], ["icmpping"])

    def test_忽略清单增删(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "inspection.yaml"
            cfg = icfg.add_ignore("V1", "k1", "", path=p)
            self.assertEqual(len(cfg.ignore), 1)
            self.assertTrue(cfg.ignore[0]["reason"])
            self.assertTrue(cfg.ignore[0]["at"].endswith("UTC"))
            icfg.add_ignore("V1", "k1", "改了原因", path=p)  # 同一条覆盖不重复
            self.assertEqual([e["reason"] for e in icfg.load_config(p).ignore], ["改了原因"])
            _, removed = icfg.remove_ignore("V1", "nope", path=p)
            self.assertFalse(removed)
            cfg, removed = icfg.remove_ignore("V1", "k1", path=p)
            self.assertTrue(removed)
            self.assertEqual(cfg.ignore, [])


class _TempConfigMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "inspection.yaml"
        self._env = mock.patch.dict(os.environ, {"NETOPS_INSPECTION_CONFIG": str(self.path)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


class TestApi(_TempConfigMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)
        self._real_existed = REAL_CONFIG.exists()

    def tearDown(self):
        # 测试没有写仓库根的真实文件
        self.assertEqual(REAL_CONFIG.exists(), self._real_existed)
        super().tearDown()

    def test_get没有文件时给默认值(self):
        r = self.client.get("/api/inspection/config")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["file_exists"])
        self.assertEqual(body["values"]["detectors"], body["defaults"]["detectors"])
        self.assertEqual({s["name"] for s in body["schema"]}, {"trend", "periodic_spike", "self_healing_flap"})
        self.assertFalse(self.path.exists())

    def test_put写入并回读(self):
        r = self.client.put("/api/inspection/config", json={"detectors": {"trend": {"min_relative_change": 0.5}}, "scope": {"exclude_hosts": ["lab-*"]}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.path.exists())
        self.assertEqual(r.json()["values"]["detectors"]["trend"]["min_relative_change"], 0.5)
        self.assertEqual(icfg.load_config().scope["exclude_hosts"], ["lab-*"])

    def test_put非法值400且不落盘(self):
        for body in ({"detectors": {"trend": {"min_points": 0}}}, {"detectors": {"trend": {"min_points": "x"}}},
                     {"scope": {"lookback_days": 1000}}):
            with self.subTest(body=body):
                r = self.client.put("/api/inspection/config", json=body)
                self.assertEqual(r.status_code, 400)
                self.assertIn("message", r.json())
        self.assertFalse(self.path.exists())

    def test_put不接受路径或其它多余字段(self):
        for body in ({"path": "../../x.yaml"}, {"detectors": {}, "file": "x"}, {"ignore": []}):
            with self.subTest(body=body):
                self.assertEqual(self.client.put("/api/inspection/config", json=body).status_code, 422)
        self.assertEqual(self.client.put("/api/inspection/config?path=/tmp/x", json={}).status_code, 200)
        self.assertFalse(Path("/tmp/x").exists())

    def test_忽略清单增删与put不冲掉它(self):
        r = self.client.post("/api/inspection/ignore", json={"host": "V1", "item_key": "icmpping", "reason": "已知"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["ignore_list"][0]["reason"], "已知")
        self.client.put("/api/inspection/config", json={"detectors": {}, "scope": {}})
        self.assertEqual(len(icfg.load_config().ignore), 1)
        self.assertEqual(self.client.delete("/api/inspection/ignore", params={"host": "V1", "item_key": "nope"}).status_code, 404)
        r = self.client.delete("/api/inspection/ignore", params={"host": "V1", "item_key": "icmpping"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["ignore_list"], [])

    def test_忽略POST非法体422(self):
        self.assertEqual(self.client.post("/api/inspection/ignore", json={"host": "", "item_key": "k"}).status_code, 422)
        self.assertEqual(self.client.post("/api/inspection/ignore", json={"host": "a", "item_key": "k", "x": 1}).status_code, 422)

    def test_处置建议不看被忽略的发现(self):
        latest = {"findings": [{"host_name": "V1", "item_key": "k1"}, {"host_name": "V1", "item_key": "k2"}]}
        icfg.add_ignore("V1", "k1", "r")
        with (
            mock.patch.object(dashboard, "load_latest_inspection", return_value=latest),
            mock.patch("netops_ai.inspection.advise.advise", return_value={}) as advise,
            mock.patch("netops_ai.inspection.advise.save") as save,
        ):
            r = self.client.post("/api/inspection/advise")
        self.assertEqual(r.status_code, 200)
        advise.assert_called_once()
        self.assertEqual([f["item_key"] for f in advise.call_args[0][0]["findings"]], ["k2"])
        save.assert_called_once()

    def test_手改坏文件时get带错误不500(self):
        self.path.write_text("detectors:\n  trend:\n    min_points: 0\n", encoding="utf-8")
        r = self.client.get("/api/inspection/config")
        self.assertEqual(r.status_code, 200)
        self.assertIn("min_points", r.json()["error"])


class TestViewHasNewFields(_TempConfigMixin, unittest.TestCase):
    """`/api/inspection` 和 export-fixtures 共用 build_inspection_view，这里守住新字段两个出口都有。"""

    def _raw(self):
        rep = scan_host(_fake_zbx(), "10", "V1")
        return {
            "scanned_hosts": 1, "scanned_items": rep.scanned_items, "items_with_data": rep.items_with_data, "errors": [],
            "findings": [{"kind": f.kind, "host_name": f.host_name, "item_name": f.item_name, "item_key": f.item_key,
                          "finding": asdict(f.finding), "rule": f.rule, "series": f.series} for f in rep.findings],
        }

    def test_旧字段还在新字段加上(self):
        with mock.patch("netops_ai.inspection.advise.load", return_value=None):
            view = dashboard.build_inspection_view(self._raw())
        for key in ("scanned_hosts", "scanned_items", "items_with_data", "errors", "finding_count", "groups",
                    "report_markdown", "advice", "config", "ignored", "ignore_list"):
            self.assertIn(key, view)
        rows = [r for g in view["groups"] for r in g["rows"]]
        self.assertEqual(len(rows), 3)
        for r in rows:
            for key in ("host", "item", "key", "detail", "reason", "rule", "series", "host_raw", "key_raw"):
                self.assertIn(key, r)
            self.assertEqual(r["rule"]["kind"] in ("trend", "periodic_spike", "self_healing_flap"), True)  # 没被 humanize 改写

    def test_老记录没有rule字段也能出视图(self):
        raw = self._raw()
        for row in raw["findings"]:
            row.pop("rule")
            row.pop("series")
        with mock.patch("netops_ai.inspection.advise.load", return_value=None):
            view = dashboard.build_inspection_view(raw)
        self.assertTrue(all(r["rule"] is None and r["series"] is None for g in view["groups"] for r in g["rows"]))

    def test_被忽略的发现不进分组和计数但可见可撤销(self):
        raw = self._raw()
        icfg.add_ignore("V1", "system.cpu.util", "测试")
        with mock.patch("netops_ai.inspection.advise.load", return_value=None):
            view = dashboard.build_inspection_view(raw)
        self.assertEqual(view["finding_count"], 2)
        self.assertEqual([i["key"] for i in view["ignored"]], ["system.cpu.util"])
        self.assertEqual(view["ignored"][0]["reason"], "测试")
        self.assertNotIn("system.cpu.util", json.dumps(view["groups"], ensure_ascii=False))
        icfg.remove_ignore("V1", "system.cpu.util")
        with mock.patch("netops_ai.inspection.advise.load", return_value=None):
            self.assertEqual(dashboard.build_inspection_view(raw)["finding_count"], 3)

    def test_叫zabbix的主机名ignore按钮仍能原值回指(self):
        raw = self._raw()
        raw["findings"][0]["host_name"] = "zabbix"
        with mock.patch("netops_ai.inspection.advise.load", return_value=None):
            view = dashboard.build_inspection_view(raw)
        self.assertIn("zabbix", [r["host_raw"] for g in view["groups"] for r in g["rows"]])

    def test_配置文件写坏时视图仍出且带错误(self):
        self.path.write_text("scope: 1\n", encoding="utf-8")
        with mock.patch("netops_ai.inspection.advise.load", return_value=None):
            view = dashboard.build_inspection_view(self._raw())
        self.assertTrue(view["config"]["error"])
        self.assertEqual(view["finding_count"], 3)


class TestRunPersistUsesConfig(_TempConfigMixin, unittest.TestCase):
    def test_重跑把配置和rule写进落盘结果(self):
        icfg.save_config({"trend": {"min_relative_change": 100.0}}, {"exclude_hosts": ["lab-*"]})
        out_dir = Path(self._tmp.name) / "records"
        with (
            mock.patch.object(dashboard, "RECORDS_DIR", out_dir),
            mock.patch.object(dashboard, "INSPECTION_LATEST_PATH", out_dir / "inspection-latest.json"),
            mock.patch("netops_ai.zabbix.client.ZabbixClient") as zc,
        ):
            zc.return_value.__enter__.return_value = _fake_zbx()
            payload = dashboard.run_inspection_and_persist()
        self.assertEqual(payload["scanned_hosts"], 1)
        self.assertEqual({f["kind"] for f in payload["findings"]}, {"periodic_spike", "self_healing_flap"})
        self.assertTrue(all(f["rule"] and f["series"] for f in payload["findings"]))
        self.assertTrue((out_dir / "inspection-latest.json").exists())


if __name__ == "__main__":
    unittest.main()

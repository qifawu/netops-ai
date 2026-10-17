"""设备版本 × CVE 报告：版本识别、CPE 拼写、NVD/KEV 取数（缓存 / 限速 / 失败退回 / 分页）、排序和 KEV 标记、报告块。网络一律用假的。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from netops_ai.inspection import cve_report as CR
from netops_ai.inspection.export import blocks_to_html, blocks_to_markdown

NL = chr(10)
IOS = ("Cisco IOS Software, IOSv Software (VIOS-ADVENTERPRISEK9-M), Version 15.9(3)M9, RELEASE SOFTWARE (fc1)" + NL + "Technical Support: x")
XE = "Cisco IOS XE Software, Version 17.03.04a" + NL + "Cisco IOS Software [Amsterdam], Virtual XE Software, Version 17.3.4a, RELEASE SOFTWARE"


def cve(cid, score, sev, pub="2025-01-01", desc="d"):
    return {"id": cid, "published": pub + "T00:00:00.000", "descriptions": [{"lang": "en", "value": desc}],
            "metrics": {"cvssMetricV31": [{"cvssData": {"version": "3.1", "baseScore": score, "baseSeverity": sev}}]} if score is not None else {}}


class Net:
    """假网络：按 URL 关键字返回；记录调用。"""

    def __init__(self, nvd_pages=None, nvd_status=200, kev_status=200, kev=None):
        self.calls, self.nvd_pages, self.nvd_status, self.kev_status = [], list(nvd_pages or []), nvd_status, kev_status
        self.kev = kev if kev is not None else [{"cveID": "CVE-2025-2", "dueDate": "2025-10-20", "vulnerabilityName": "x"}]

    def __call__(self, url, headers):
        self.calls.append(url)
        if "cisa.gov" in url:
            return (self.kev_status, json.dumps({"vulnerabilities": self.kev}).encode()) if self.kev_status == 200 else (self.kev_status, b"")
        if self.nvd_status != 200:
            return self.nvd_status, b""
        page = self.nvd_pages.pop(0) if self.nvd_pages else {"resultsPerPage": 0, "totalResults": 0, "vulnerabilities": []}
        return 200, json.dumps(page).encode()


def page(cves, total=None, start=0):
    return {"resultsPerPage": len(cves), "totalResults": total if total is not None else len(cves), "startIndex": start,
            "vulnerabilities": [{"cve": c} for c in cves]}


class Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.root = Path(self._d.name)
        self.t = [__import__("time").time()]
        self.sleeps = []

    def src(self, net, **kw):
        return CR.Sources(root=self.root, fetch=net, now=lambda: self.t[0], sleep=lambda s: (self.sleeps.append(s), self.t.__setitem__(0, self.t[0] + s)), api_key="", **kw)


class TestVersion(unittest.TestCase):
    def test_识别IOS和IOSXE(self):
        self.assertEqual(CR.parse_version(IOS), {"product": "ios", "version": "15.9(3)m9", "raw": "15.9(3)M9"})
        v = CR.parse_version(XE)
        self.assertEqual((v["product"], v["version"]), ("ios_xe", "17.3.4a"))
        self.assertIsNone(CR.parse_version("Huawei VRP"))
        self.assertIsNone(CR.parse_version(""))

    def test_CPE拼写_括号转义(self):
        self.assertEqual(CR.cpe_for("ios", "15.9(3)m9"), r"cpe:2.3:o:cisco:ios:15.9\(3\)m9:*:*:*:*:*:*:*")
        self.assertEqual(CR.cpe_for("ios_xe", "17.3.4a"), "cpe:2.3:o:cisco:ios_xe:17.3.4a:*:*:*:*:*:*:*")


class TestSources(Base):
    def test_解析CVE_取最新的CVSS_缺失的留空(self):
        c = CR.parse_cve(cve("CVE-1", 9.8, "CRITICAL", desc="a  b" + NL + "c"))
        self.assertEqual((c["score"], c["severity"], c["summary"]), (9.8, "CRITICAL", "a b c"))
        self.assertIsNone(CR.parse_cve(cve("CVE-2", None, ""))["score"])

    def test_分页取全(self):
        net = Net([page([cve("A", 5.0, "MEDIUM")], total=2), page([cve("B", 7.0, "HIGH")], total=2, start=1)])
        got = self.src(net).cves("cpe:x")
        self.assertEqual([c["id"] for c in got], ["A", "B"])
        self.assertEqual(len(net.calls), 2)

    def test_缓存_24小时内不重复请求_过期重新取(self):
        net = Net([page([cve("A", 5.0, "MEDIUM")]), page([cve("A2", 5.0, "MEDIUM")])])
        s = self.src(net)
        s.cves("cpe:x")
        s.cves("cpe:x")
        self.assertEqual(len(net.calls), 1)
        self.t[0] += CR.CACHE_TTL + 10
        self.assertEqual([c["id"] for c in self.src(net).cves("cpe:x")], ["A2"])

    def test_限速_两次NVD请求之间留间隔(self):
        net = Net([page([cve("A", 5.0, "MEDIUM")]), page([cve("B", 5.0, "MEDIUM")])])
        s = self.src(net)
        s.cves("cpe:a")
        s.cves("cpe:b")
        self.assertTrue(self.sleeps and self.sleeps[0] >= 6.0)

    def test_NVD字典里没有这个版本_404_返回None不当成没漏洞(self):
        s = self.src(Net(nvd_status=404))
        self.assertIsNone(s.cves("cpe:x"))

    def test_失败时退回旧缓存_没有缓存返回None并记说明(self):
        ok = Net([page([cve("A", 5.0, "MEDIUM")])])
        self.src(ok).cves("cpe:x")
        self.t[0] += CR.CACHE_TTL + 10
        s = self.src(Net(nvd_status=503))
        self.assertEqual([c["id"] for c in s.cves("cpe:x")], ["A"])
        self.assertTrue(any("older cached" in n for n in s.notes))
        s2 = self.src(Net(nvd_status=503))
        self.assertIsNone(s2.cves("cpe:never"))
        self.assertTrue(any("failed" in n for n in s2.notes))

    def test_KEV_取不到_说明缺失(self):
        s = self.src(Net(kev_status=500))
        self.assertEqual(s.kev(), {})
        self.assertTrue(any("KEV" in n for n in s.notes))


class FakeAdapter:
    def __init__(self, out=None, allowed=True, err=""):
        self.out, self.allowed, self.err = out, allowed, err

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cmd):
        return SimpleNamespace(allowed=self.allowed, denial_reason="denied", error=self.err, output=self.out)


class TestReport(Base):
    def test_取版本_失败不影响其它(self):
        devs = {"R1": 1, "R2": 2, "R3": 3, "R4": 4}
        mk = {1: FakeAdapter(IOS), 2: FakeAdapter("garbage"), 3: FakeAdapter(allowed=False), 4: None}

        def factory(d):
            if mk[d] is None:
                raise OSError("unreachable")
            return mk[d]
        v = CR.collect_versions(devs, factory)
        self.assertEqual(v["R1"]["version"], "15.9(3)m9")
        self.assertIn("recognise", v["R2"]["error"])
        self.assertIn("denied", v["R3"]["error"])
        self.assertIn("unreachable", v["R4"]["error"])

    def test_排序_KEV在前_再按CVSS_同版本只查一次_查不到的标unknown(self):
        net = Net([page([cve("CVE-2025-1", 9.8, "CRITICAL"), cve("CVE-2025-2", 7.0, "HIGH"), cve("CVE-2025-3", 8.0, "HIGH")])])
        versions = {"R1": {"product": "ios", "version": "15.9(3)m9", "raw": "15.9(3)M9"},
                    "R2": {"product": "ios", "version": "15.9(3)m9", "raw": "15.9(3)M9"},
                    "R3": {"product": "ios", "version": "99.9", "raw": "99.9"}, "R4": {"error": "x"}}
        rep = CR.build_report(versions, self.src(net))
        nvd_calls = [c for c in net.calls if "nvd" in c]
        self.assertEqual(len(nvd_calls), 2)  # 15.9(3)m9 一次 + 99.9 一次（第二次是空页）
        r1 = rep["devices"][0]
        self.assertEqual([c["id"] for c in r1["cves"]], ["CVE-2025-2", "CVE-2025-1", "CVE-2025-3"])  # KEV 的 2 号排最前
        self.assertTrue(r1["cves"][0]["kev"])
        self.assertEqual(rep["kev_count"], 2)  # R1、R2 各一条
        self.assertEqual(rep["devices"][1]["cves"][0]["id"], "CVE-2025-2")
        self.assertEqual(rep["devices"][3]["error"], "x")

    def test_查不到_报告里明说不等于没漏洞(self):
        rep = CR.build_report({"R1": {"product": "ios", "version": "1.1", "raw": "1.1"}}, self.src(Net(nvd_status=404)))
        self.assertTrue(rep["devices"][0]["unknown"])
        md = blocks_to_markdown(CR.to_blocks(rep))
        self.assertIn("这不等于没有漏洞", md)
        self.assertIn("未查到", md)

    def test_报告块_Markdown和HTML都能渲染_含KEV期限(self):
        net = Net([page([cve("CVE-2025-2", 7.0, "HIGH", desc="snmp dos")])])
        rep = CR.build_report({"R1": {"product": "ios", "version": "15.9(3)m9", "raw": "15.9(3)M9"}}, self.src(net))
        blocks = CR.to_blocks(rep)
        md, html = blocks_to_markdown(blocks), blocks_to_html(blocks, "t", classify=None)
        for text in (md, html):
            self.assertIn("CVE-2025-2", text)
            self.assertIn("2025-10-20", text)
            self.assertIn("snmp dos", text)
        self.assertIn("不下这个结论", md)


if __name__ == "__main__":
    unittest.main()

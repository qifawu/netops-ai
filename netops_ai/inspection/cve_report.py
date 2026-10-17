"""设备软件版本 × 已公开漏洞（CVE）报告。

流程：只读命令 `show version` 取每台设备的系统版本 → 拼成 CPE → 向 NVD（美国国家漏洞库，公开 API）查适用于这个版本的 CVE →
和 CISA KEV（已知被利用漏洞目录，公开 JSON）对照标出「已被在野利用」的 → 出报告（Markdown / HTML）。

只读、只访问两个官方公开数据源：NVD 的 `services.nvd.nist.gov` 和 CISA 的 `www.cisa.gov`；响应缓存在本地（默认 24 小时），
离线或被限流时用缓存，没有缓存就在报告里明说「没查到」，不当成「没有漏洞」。
匹配精度取决于 NVD 对这个版本的适用性描述：命中只说明「NVD 认为该版本在受影响范围内」，是否真的可被利用还要看功能是否启用，
报告里不下这个结论。

NVD 免费接口不带 key 时限速（每 30 秒 5 次），所以逐条查询之间留间隔；有 `NVD_API_KEY` 就放宽。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
CACHE_TTL = 24 * 3600
SEV_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "NONE": 4, "": 5}

Fetch = Callable[[str, dict[str, str]], tuple[int, bytes]]


# ------------------------------------------------------------------ 版本识别

def parse_version(output: str) -> dict[str, str] | None:
    """从 `show version` 输出认出 {product, version, raw}。认得 Cisco IOS 和 IOS XE；认不出返回 None。"""
    text = output or ""
    m = re.search(r"Cisco IOS XE Software, Version (\S+)", text)
    if m:
        v = re.sub(r"\b0+(\d)", r"\1", m.group(1))  # 17.03.04a → 17.3.4a（NVD 的写法）
        return {"product": "ios_xe", "version": v.lower(), "raw": m.group(1)}
    m = re.search(r"Cisco IOS Software.*?Version (\d[^,\s]*)", text, re.S)
    if m:
        return {"product": "ios", "version": m.group(1).lower(), "raw": m.group(1)}
    return None


def cpe_for(product: str, version: str) -> str:
    esc = re.sub(r"([()\[\]!])", r"\\\1", version)
    return f"cpe:2.3:o:cisco:{product}:{esc}:*:*:*:*:*:*:*"


# ------------------------------------------------------------------ 数据源（带缓存）

def _http(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "netops-ai-cve-report", **headers})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def cache_dir() -> Path:
    return Path(os.environ.get("NETOPS_CVE_CACHE_DIR") or Path(__file__).resolve().parents[2] / "records" / "cve-cache")


def _cached(key: str, root: Path, ttl: int, now: float) -> bytes | None:
    p = root / key
    try:
        if now - p.stat().st_mtime <= ttl:
            return p.read_bytes()
    except OSError:
        pass
    return None


def _stale(key: str, root: Path) -> bytes | None:
    try:
        return (root / key).read_bytes()
    except OSError:
        return None


def _store(key: str, root: Path, data: bytes) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / key).write_bytes(data)


def _key(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:150] + ".json"


class Sources:
    """NVD + KEV。`fetch` 可注入（测试用）；`sleep` 控制限速间隔。"""

    def __init__(self, *, root: Path | None = None, fetch: Fetch = _http, api_key: str | None = None,
                 now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self.root, self.fetch, self.now, self.sleep = root or cache_dir(), fetch, now, sleep
        self.api_key = api_key if api_key is not None else os.environ.get("NVD_API_KEY", "")
        self._last_nvd = 0.0
        self.notes: list[str] = []

    def kev(self) -> dict[str, dict[str, str]]:
        raw = _cached("kev.json", self.root, CACHE_TTL, self.now())
        if raw is None:
            status, body = self.fetch(KEV_URL, {})
            if status == 200 and body:
                _store("kev.json", self.root, body)
                raw = body
            else:
                raw = _stale("kev.json", self.root)
                self.notes.append("CISA KEV list unavailable" + ("; used an older cached copy" if raw else "; known-exploited flags are missing"))
        try:
            items = json.loads(raw or b"{}").get("vulnerabilities", [])
        except ValueError:
            items = []
        return {i["cveID"]: {"due": i.get("dueDate", ""), "name": i.get("vulnerabilityName", "")} for i in items if i.get("cveID")}

    def cves(self, cpe: str) -> list[dict[str, Any]] | None:
        """适用于这个 CPE 的 CVE 列表；查不到（网络 / 限流且没缓存）返回 None，和「查到 0 条」区分开。"""
        key = _key("nvd_" + cpe)
        raw = _cached(key, self.root, CACHE_TTL, self.now())
        if raw is None:
            pages, start = [], 0
            while True:
                wait = (0.7 if self.api_key else 6.5) - (self.now() - self._last_nvd)
                if wait > 0 and self._last_nvd:
                    self.sleep(wait)
                self._last_nvd = self.now()
                url = f"{NVD_URL}?{urllib.parse.urlencode({'cpeName': cpe, 'resultsPerPage': 200, 'startIndex': start})}"
                status, body = self.fetch(url, {"apiKey": self.api_key} if self.api_key else {})
                if status == 404:  # NVD 的 CPE 字典里没有这个版本：不是「没漏洞」，是「没法按这个 CPE 查」
                    pages = None
                    break
                if status != 200 or not body:
                    pages = None
                    self.notes.append(f"NVD query failed (HTTP {status or 'error'}) for {cpe}")
                    break
                data = json.loads(body)
                pages.append(data)
                start += int(data.get("resultsPerPage") or 0)
                if start >= int(data.get("totalResults") or 0) or not data.get("resultsPerPage"):
                    break
            if pages is None:
                raw = _stale(key, self.root)
                if raw is None:
                    return None
                self.notes.append(f"used an older cached NVD answer for {cpe}")
            else:
                raw = json.dumps({"pages": pages}).encode()
                _store(key, self.root, raw)
        out = []
        for page in json.loads(raw).get("pages", []):
            out += [parse_cve(v["cve"]) for v in page.get("vulnerabilities", []) if v.get("cve")]
        return out


def parse_cve(c: dict[str, Any]) -> dict[str, Any]:
    score, sev, ver = None, "", ""
    for k in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        ms = (c.get("metrics") or {}).get(k)
        if ms:
            d = ms[0].get("cvssData") or {}
            score, ver = d.get("baseScore"), d.get("version", "")
            sev = str(d.get("baseSeverity") or ms[0].get("baseSeverity") or "").upper()
            break
    desc = next((d["value"] for d in c.get("descriptions") or [] if d.get("lang") == "en"), "")
    return {"id": c.get("id", ""), "published": str(c.get("published", ""))[:10], "score": score, "severity": sev, "cvss": ver,
            "summary": " ".join(desc.split())[:300]}


# ------------------------------------------------------------------ 取版本 / 汇总

def collect_versions(devices: dict[str, Any], adapter_factory: Callable[[Any], Any], command: str = "show version") -> dict[str, dict[str, Any]]:
    """{设备名: {product, version} 或 {error}}。走只读适配器（白名单照常把关）。"""
    out: dict[str, dict[str, Any]] = {}
    for name, dev in sorted(devices.items()):
        try:
            with adapter_factory(dev) as ad:
                r = ad.run(command)
            if not r.allowed or r.error:
                out[name] = {"error": str(r.denial_reason or r.error)[:160]}
                continue
            v = parse_version(r.output or "")
            out[name] = v or {"error": "could not recognise the software version in the output"}
        except Exception as exc:  # noqa: BLE001 - 一台失败不影响其它
            out[name] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    return out


def build_report(versions: dict[str, dict[str, Any]], src: Sources, *, now: datetime | None = None) -> dict[str, Any]:
    """逐设备查 CVE，标 KEV，排序（已被利用的在前，再按 CVSS 从高到低）。同一个版本只查一次。"""
    kev = src.kev()
    by_cpe: dict[str, list[dict[str, Any]] | None] = {}
    rows = []
    for name, v in sorted(versions.items()):
        if "error" in v:
            rows.append({"device": name, "error": v["error"]})
            continue
        cpe = cpe_for(v["product"], v["version"])
        if cpe not in by_cpe:
            by_cpe[cpe] = src.cves(cpe)
        found = by_cpe[cpe]
        if found is None:
            rows.append({"device": name, "product": v["product"], "version": v["raw"], "cpe": cpe, "unknown": True, "cves": []})
            continue
        cves = [{**c, "kev": c["id"] in kev, "kev_due": kev.get(c["id"], {}).get("due", "")} for c in found]
        cves.sort(key=lambda c: (not c["kev"], SEV_ORDER.get(c["severity"], 5), -(c["score"] or 0), c["id"]))
        rows.append({"device": name, "product": v["product"], "version": v["raw"], "cpe": cpe, "cves": cves})
    total = [c for r in rows for c in r.get("cves", [])]
    return {"generated_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"), "devices": rows, "notes": src.notes,
            "kev_count": sum(1 for c in total if c["kev"]),
            "by_severity": {s: sum(1 for c in total if c["severity"] == s) for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW")}}


def to_blocks(report: dict[str, Any], *, top: int = 15) -> list[tuple]:
    """中性块（和巡检报告共用 `export.blocks_to_markdown / blocks_to_html`）。"""
    sev = report["by_severity"]
    blocks: list[tuple] = [("h1", "设备软件版本漏洞报告"), ("p", f"生成时间：{report['generated_at']}；数据来源：NVD、CISA KEV；"
                                                           f"命中 CVE：严重 {sev['CRITICAL']} / 高危 {sev['HIGH']} / 中危 {sev['MEDIUM']} / 低危 {sev['LOW']}；"
                                                           f"其中已被在野利用（KEV）{report['kev_count']} 条。"),
                           ("p", "命中表示 NVD 认为该版本在受影响范围内；漏洞是否可被利用还取决于相关功能是否启用，本报告不下这个结论。")]
    blocks.append(("table", ["设备", "系统", "版本", "CVE 数", "KEV", "最高 CVSS"],
                   [[r["device"], r.get("product", "-"), r.get("version", "-"),
                     "未查到" if r.get("unknown") else ("-" if "error" in r else len(r["cves"])),
                     "-" if "error" in r or r.get("unknown") else sum(1 for c in r["cves"] if c["kev"]),
                     "-" if not r.get("cves") else max((c["score"] or 0) for c in r["cves"])] for r in report["devices"]]))
    for r in report["devices"]:
        if "error" in r:
            blocks += [("h3", r["device"]), ("p", f"没取到版本：{r['error']}")]
        elif r.get("unknown"):
            blocks += [("h3", f"{r['device']}（{r['version']}）"), ("p", "没能从 NVD 查到这个版本的漏洞信息（网络不通、被限流且没有缓存，或 NVD 字典里没有该版本）。这不等于没有漏洞。")]
        elif r["cves"]:
            blocks += [("h3", f"{r['device']}（{r['product']} {r['version']}，共 {len(r['cves'])} 条，下列前 {min(top, len(r['cves']))} 条）"),
                       ("table", ["CVE", "CVSS", "严重度", "已被利用", "发布", "说明"],
                        [[c["id"], c["score"] if c["score"] is not None else "-", c["severity"] or "-",
                          ("是（要求修复期限 " + c["kev_due"] + "）") if c["kev"] and c["kev_due"] else ("是" if c["kev"] else "-"),
                          c["published"], c["summary"]] for c in r["cves"][:top]])]
    if report["notes"]:
        blocks += [("h3", "数据说明"), ("ul", report["notes"])]
    return blocks


def main() -> None:
    """`python -m netops_ai.inspection.cve_report [--out 目录]`：取拓扑里每台设备的版本，查 CVE，写 Markdown + HTML。"""
    import argparse

    from netops_ai import topology as topo
    from netops_ai.inspection.export import blocks_to_html, blocks_to_markdown

    ap = argparse.ArgumentParser(description="设备软件版本 × CVE 报告")
    ap.add_argument("--out", default="reports")
    ap.add_argument("--policy", default="version-policy.yaml", help="目标版本清单；文件存在才出「软件版本合规」一节")
    a = ap.parse_args()
    versions = collect_versions(topo.load_topology(), topo._default_adapter_factory)
    report = build_report(versions, Sources())
    blocks = to_blocks(report)
    policy_path = Path(a.policy)
    if policy_path.exists():
        from netops_ai.inspection import version_policy as VP

        rows = VP.evaluate(versions, VP.load_policy(policy_path))
        counts = {r["device"]: len(r["cves"]) for r in report["devices"] if r.get("cves") is not None and not r.get("unknown") and "error" not in r}
        blocks = VP.to_blocks(rows, counts) + blocks
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    (out / f"cve-{stamp}.md").write_text(blocks_to_markdown(blocks), encoding="utf-8")
    (out / f"cve-{stamp}.html").write_text(blocks_to_html(blocks, "设备软件版本漏洞报告", classify=None), encoding="utf-8")
    print(out / f"cve-{stamp}.html")


if __name__ == "__main__":
    main()

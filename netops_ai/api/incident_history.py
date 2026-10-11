"""历史告警 + 排查历史：「告警与结论」页左侧列表和「排查历史」页签的数据层。

**不新增存储**：全部从现有的 `records/alert-*.json`（经 `dashboard.build_incidents`）、
基础巡检历史快照（`records/inspection-history/`）和自定义巡检运行结果（`records/checklist-runs/`）现算。

两件事：

1. `query_incidents`：按时间范围 / 设备 / 故障类型 / 置信度 / 是否需人工筛选，**同一指纹折叠成一行**
   （指纹 = 主机 + 触发器 + 接口，见 `netops_ai/incident.compute_fingerprint`，就是流水线里
   `incident_repeat_count` 用的那个，不另造一套「同类」的定义），再按行分页。
2. `build_device_history`：一台设备过去所有的事件和巡检发现，按时间倒序。巡检每小时一份快照，
   同一条发现连着几份都在只算一条（记首次 / 最近一次 / 连续出现次数），不然一台连不上的设备能刷出几十行一样的。

`/api/incidents` 原样不动（总览、旧调用方、测试都在用）；这里的结果走新接口
`/api/incidents/list`、`/api/incidents/{id}`、`/api/devices/{name}/history`。
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from netops_ai.analysis.schema import STATUS_SUPPORTED, normalize_enum
from netops_ai.api import dashboard
from netops_ai.labels import label_for

#: 时间范围（秒）。`all` = 不限。
RANGES: dict[str, int | None] = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "all": None}
PAGE_SIZE_MAX = 200
#: 排查历史统计窗口
STATS_WINDOW = 30 * 86400

#: 故障类型：跟前端 `web/src/lib/incident.ts::faultType` 同一套规则、同一个顺序（先看告警名，再看结论）。
#: 接口 down 会连带引发 OSPF / BGP 邻居掉线，所以有接口告警时先归接口。后端要按类型筛选，所以这里也得算一份。
FAULT_TYPES: list[tuple[str, re.Pattern[str]]] = [
    ("restart", re.compile(r"restarted|coldStart|warmStart|重启", re.I)),
    ("interfaceDown", re.compile(r"link down|linkDown|line protocol|lower speed|Interface", re.I)),
    ("bgp", re.compile(r"BGP", re.I)),
    ("ospf", re.compile(r"OSPF", re.I)),
    ("unreachable", re.compile(r"ICMP|unreachable|不可达", re.I)),
    ("monitoringDown", re.compile(r"No SNMP|SNMP", re.I)),
    ("configChange", re.compile(r"configChange|config", re.I)),
]


def fault_type_of(inc: dict) -> str:
    names = " | ".join(str(a.get("name") or "") for a in inc.get("alerts") or [])
    for key, rx in FAULT_TYPES:
        if rx.search(names):
            return key
    rc = inc.get("root_cause") or ""
    for key, rx in FAULT_TYPES:
        if rx.search(rc):
            return key
    return "other"


def needs_human(inc: dict) -> bool:
    """「需人工」：没下定论的——低置信 / 没评级、或者干脆没有结论（分析失败）。

    不看 `incomplete`：取证轨迹里有一步没查完很常见（实测 76 次里 60 多次带），结论照样是高置信，
    算进来这个筛选就等于没筛。
    records 里没有「已恢复」状态，界面上的「仅看需人工」筛的是这个，不是「告警还没恢复」，别把不知道的事说死。"""
    return (inc.get("confidence") or "") not in ("high", "medium") or not (inc.get("root_cause") or "").strip()


def verdict_of(inc: dict) -> list[str]:
    """这次结论落在哪几个排查方向上：假设清单里判「有证据支持」的方向（结构化枚举，不是自由文本）。
    同类复现的「结论是否一致」就比这个——根因原文每次措辞都不一样，比原文没意义。"""
    out = []
    for cat, item in (inc.get("checklist") or {}).items():
        if isinstance(item, dict) and normalize_enum(str(item.get("status") or "")) == STATUS_SUPPORTED:
            out.append(cat)
    return sorted(out)


# ---------------------------------------------------------------------------- 拓扑 / 设备名

def _topology() -> dict:
    """拿不到拓扑（NetBox 挂了、yaml 写坏）就当没有：设备名退回监控主机名原样，不让列表整页 500。"""
    try:
        from netops_ai import topology as topo

        return topo.load_topology()
    except Exception:  # noqa: BLE001
        return {}


def _resolve(topology: dict, host: str) -> str:
    """监控主机名（`V1-vios`）→ 拓扑设备名（`V1`）；对不上就原样返回。"""
    from netops_ai import topology as topo

    if topology:
        dev = topo.resolve_device(topology, host)
        if dev is not None:
            return dev.name
    return host


def devices_of(host: str, topology: dict) -> list[str]:
    """一次故障可能合并了多台设备（host 写成 `D1-vios / D2-vios`），拆开逐个归一到拓扑设备名。"""
    parts = [p.strip() for p in (host or "").split(" / ") if p.strip()]
    out: list[str] = []
    for p in parts:
        name = _resolve(topology, p)
        if name not in out:
            out.append(name)
    return out


# ---------------------------------------------------------------------------- 事件缓存 / 摘要

_INC_CACHE: dict = {"key": None, "rows": []}


def cached_incidents() -> list[dict]:
    """`build_incidents` 要读全部记录 + token 账本，列表翻页 / 点详情每次都重算太浪费。
    签名 = 记录目录签名（`load_alerts` 自己维护）+ 账本大小和修改时间；有新记录就自动失效。"""
    from netops_ai.llm.meter import ledger_path

    alerts = dashboard.load_alerts()
    sig = dashboard._ALERTS_CACHE.get("sig")
    try:
        st = ledger_path().stat()
        ledger_sig = (st.st_size, st.st_mtime_ns)
    except OSError:
        ledger_sig = None
    key = (sig, ledger_sig) if sig is not None else None
    if key is not None and _INC_CACHE["key"] == key:
        return _INC_CACHE["rows"]
    rows = dashboard.build_incidents(alerts)
    if key is not None:
        _INC_CACHE.update(key=key, rows=rows)
    return rows


def incident_brief(inc: dict, topology: dict) -> dict:
    """列表一行要的字段。完整证据 / 调查步骤不在这里，点进去再按 ID 拉（全量 /api/incidents 有几 MB）。"""
    return {
        "incident_id": inc.get("incident_id", ""),
        "fingerprint": inc.get("fingerprint") or inc.get("incident_id", ""),
        "host": inc.get("host", ""),
        "devices": devices_of(inc.get("host", ""), topology),
        "clock": int(inc.get("clock") or 0),
        "headline": inc.get("headline", ""),
        "root_cause": (inc.get("root_cause") or "")[:400],
        "confidence": inc.get("confidence", ""),
        "fault_type": fault_type_of(inc),
        "alert_count": inc.get("alert_count", 0),
        "repeat_count": inc.get("repeat_count"),
        "elapsed_total": inc.get("elapsed_total"),
        "needs_human": needs_human(inc),
        "verdict": verdict_of(inc),
    }


def incident_detail(incident_id: str, incidents: list[dict], topology: dict | None = None) -> dict | None:
    """单条完整详情 = `/api/incidents` 里那条原样 + 列表同款的几个派生字段。"""
    topo = _topology() if topology is None else topology
    for inc in incidents:
        if inc.get("incident_id") == incident_id:
            return {**inc, "fault_type": fault_type_of(inc), "needs_human": needs_human(inc),
                    "devices": devices_of(inc.get("host", ""), topo), "verdict": verdict_of(inc)}
    return None


# ---------------------------------------------------------------------------- 历史告警列表

def _mark_consistency(occ: list[dict]) -> None:
    """同一组里，跟出现最多的那种结论（排查方向组合）一样的算「一致」。只有一次的不标。"""
    if len(occ) < 2:
        for o in occ:
            o["consistent"] = None
        return
    mode, _ = Counter(tuple(o["verdict"]) for o in occ).most_common(1)[0]
    for o in occ:
        o["consistent"] = tuple(o["verdict"]) == mode


def query_incidents(
    incidents: list[dict],
    *,
    range_: str = "all",
    device: str = "",
    fault_type: str = "",
    confidence: str = "",
    unresolved: bool = False,
    page: int = 1,
    page_size: int = 30,
    now: float | None = None,
    topology: dict | None = None,
) -> dict:
    topo = _topology() if topology is None else topology
    now = time.time() if now is None else now
    range_ = range_ if range_ in RANGES else "all"
    span = RANGES[range_]
    briefs = [incident_brief(i, topo) for i in incidents]
    in_range = [b for b in briefs if span is None or b["clock"] >= now - span]

    # 筛选项的候选值按「时间范围内」算，不按已经筛过的算：不然选了设备之后其它设备从下拉框里消失，切不回去。
    dev_count: Counter = Counter(d for b in in_range for d in b["devices"])
    facets = {
        "devices": [{"value": k, "count": n} for k, n in sorted(dev_count.items())],
        "types": [{"value": k, "count": n} for k, n in Counter(b["fault_type"] for b in in_range).most_common()],
        "confidence": [{"value": k or "none", "count": n} for k, n in Counter(b["confidence"] for b in in_range).most_common()],
        "needs_human": sum(1 for b in in_range if b["needs_human"]),
        "in_range": len(in_range),
    }

    want_dev = _resolve(topo, device.strip()) if device.strip() else ""
    rows = in_range
    if want_dev:
        rows = [b for b in rows if want_dev in b["devices"] or device.strip() == b["host"]]
    if fault_type:
        rows = [b for b in rows if b["fault_type"] == fault_type]
    if confidence:
        rows = [b for b in rows if (b["confidence"] or "none") == confidence]
    if unresolved:
        rows = [b for b in rows if b["needs_human"]]

    groups: dict[str, list[dict]] = {}
    for b in sorted(rows, key=lambda r: r["clock"], reverse=True):
        groups.setdefault(b["fingerprint"], []).append(b)
    out_rows = []
    for fp, occ in groups.items():
        _mark_consistency(occ)
        latest = occ[0]
        out_rows.append({
            "fingerprint": fp,
            "host": latest["host"],
            "devices": latest["devices"],
            "fault_type": latest["fault_type"],
            "count": len(occ),
            "latest": latest,
            "occurrences": occ,
        })
    out_rows.sort(key=lambda r: r["latest"]["clock"], reverse=True)

    page_size = max(1, min(int(page_size or 30), PAGE_SIZE_MAX))
    page = max(1, int(page or 1))
    start = (page - 1) * page_size
    return {
        "range": range_,
        "device": want_dev,
        "total_incidents": len(rows),
        "total_rows": len(out_rows),
        "page": page,
        "page_size": page_size,
        "has_more": start + page_size < len(out_rows),
        "rows": out_rows[start:start + page_size],
        "facets": facets,
    }


# ---------------------------------------------------------------------------- 设备排查历史

def _epoch(value: object) -> int:
    """巡检快照里的时间有两种写法：`10:13:34 UTC`（基础巡检）和 ISO（自定义巡检）。"""
    s = str(value or "").strip()
    if not s:
        return 0
    try:
        if s.endswith(" UTC"):
            dt = datetime.strptime(s[:-4], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return 0


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class _Streaks:
    """同一条发现连续几次巡检都在 → 合成一条（首次、最近一次、次数）；中间断过一次再出现 → 新的一条。"""

    def __init__(self) -> None:
        self.open: dict[tuple, dict] = {}
        self.entries: list[dict] = []

    def feed(self, at: int, found: dict[tuple, dict]) -> None:
        for key in list(self.open):
            if key not in found:
                self.open.pop(key)
        for key, entry in found.items():
            cur = self.open.get(key)
            if cur is not None:
                cur["last_at"] = at
                cur["occurrences"] += 1
                cur["title"] = entry["title"]  # 说明文字取最近一次的
                continue
            new = {**entry, "key": "|".join(str(k) for k in key), "at": at, "last_at": at, "occurrences": 1}
            self.open[key] = new
            self.entries.append(new)


def _basic_findings(device: str, labels: set[str], history_dir: Path) -> list[dict]:
    streaks = _Streaks()
    snaps = []
    for path in sorted(history_dir.glob("status-*.json")) if history_dir.is_dir() else []:
        data = _read_json(path)
        if data is not None:
            snaps.append((_epoch(data.get("checked_at")), data))
    snaps.sort(key=lambda x: x[0])
    for at, data in snaps:
        found: dict[tuple, dict] = {}
        for dev in data.get("devices") or []:
            if str(dev.get("name") or "").lower() not in labels:
                continue
            for c in dev.get("checks") or []:
                status = c.get("status")
                if status not in ("bad", "warn"):
                    continue
                found[("basic", c.get("check", ""), status)] = {
                    "kind": "basic", "type": c.get("check", ""), "status": status, "title": c.get("summary", ""),
                }
        streaks.feed(at, found)
    return streaks.entries


def _trend_findings(labels: set[str], topology: dict, history_dir: Path) -> list[dict]:
    streaks = _Streaks()
    snaps = []
    for path in sorted(history_dir.glob("trend-*.json")) if history_dir.is_dir() else []:
        data = _read_json(path)
        if data is not None:
            snaps.append((_epoch(data.get("scanned_at")), data))
    snaps.sort(key=lambda x: x[0])
    for at, data in snaps:
        found: dict[tuple, dict] = {}
        for f in data.get("findings") or []:
            host = str(f.get("host_name") or "")
            if host.lower() not in labels and _resolve(topology, host).lower() not in labels:
                continue
            found[("trend", f.get("kind", ""), f.get("item_key", ""))] = {
                "kind": "trend", "type": f.get("kind", ""), "type_label": label_for(f.get("kind", "")),
                "status": "warn", "title": f.get("item_name", "") or f.get("item_key", ""),
            }
        streaks.feed(at, found)
    return streaks.entries


def _plan_findings(labels: set[str], runs_dir: Path) -> list[dict]:
    runs = []
    for path in sorted(runs_dir.glob("*.json")) if runs_dir.is_dir() else []:
        data = _read_json(path)
        if data is not None and data.get("checklist"):
            runs.append((_epoch(data.get("started_at")), data))
    runs.sort(key=lambda x: x[0])
    per_plan: dict[str, _Streaks] = {}
    for at, run in runs:
        plan = str(run.get("checklist"))
        found: dict[tuple, dict] = {}
        for dev in run.get("devices") or []:
            if str(dev.get("name") or "").lower() not in labels:
                continue
            if dev.get("error"):
                found[("plan", plan, "__unreachable__")] = {
                    "kind": "plan", "plan": plan, "type": "unreachable", "status": "error", "title": str(dev["error"]),
                }
            for c in dev.get("checks") or []:
                status = c.get("status")
                if status not in ("fail", "error", "denied"):
                    continue
                found[("plan", plan, c.get("id", ""), status)] = {
                    "kind": "plan", "plan": plan, "type": c.get("id", ""), "status": status,
                    "title": c.get("detail") or c.get("command") or c.get("id", ""),
                }
        per_plan.setdefault(plan, _Streaks()).feed(at, found)
    return [e for s in per_plan.values() for e in s.entries]


def build_device_history(
    name: str,
    incidents: list[dict],
    *,
    now: float | None = None,
    topology: dict | None = None,
    records_dir: Path | None = None,
    limit: int = 300,
) -> dict:
    topo = _topology() if topology is None else topology
    now = time.time() if now is None else now
    root = Path(records_dir or dashboard.RECORDS_DIR)
    from netops_ai import topology as _topo_mod

    dev = _topo_mod.resolve_device(topo, name) if topo else None
    canonical = dev.name if dev is not None else name
    aliases = list(dev.aliases) if dev is not None else []
    labels = {x.lower() for x in (canonical, name, *aliases) if x}

    entries: list[dict] = []
    mine = [b for b in (incident_brief(i, topo) for i in incidents)
            if canonical in b["devices"] or any(p.strip().lower() in labels for p in b["host"].split(" / "))]
    fp_count = Counter(b["fingerprint"] for b in mine)
    for b in mine:
        entries.append({
            "kind": "incident", "key": b["fingerprint"], "at": b["clock"], "last_at": b["clock"], "occurrences": 1,
            "type": b["fault_type"], "title": b["headline"] or b["root_cause"], "headline": b["headline"],
            "root_cause": b["root_cause"], "confidence": b["confidence"], "incident_id": b["incident_id"],
            "host": b["host"], "alert_count": b["alert_count"], "elapsed_total": b["elapsed_total"],
            "needs_human": b["needs_human"], "same_type": fp_count[b["fingerprint"]] > 1,
            "same_type_count": fp_count[b["fingerprint"]],
        })

    history_dir = root / "inspection-history"
    findings = _basic_findings(canonical, labels, history_dir) + _trend_findings(labels, topo, history_dir) \
        + _plan_findings(labels, root / "checklist-runs")
    key_count = Counter(f["key"] for f in findings)
    for f in findings:
        f["same_type"] = f["occurrences"] > 1 or key_count[f["key"]] > 1
        f["same_type_count"] = key_count[f["key"]]
    entries.extend(findings)
    entries.sort(key=lambda e: (e["last_at"], e["at"]), reverse=True)

    recent = [e for e in entries if e["kind"] == "incident" and e["at"] >= now - STATS_WINDOW]
    types = Counter(e["type"] for e in recent)
    elapsed = [float(e["elapsed_total"]) for e in recent if isinstance(e.get("elapsed_total"), (int, float)) and e["elapsed_total"] > 0]
    stats = {
        "incidents_30d": len(recent),
        "top_type": types.most_common(1)[0][0] if types else "",
        "top_type_count": types.most_common(1)[0][1] if types else 0,
        "avg_elapsed": round(sum(elapsed) / len(elapsed), 1) if elapsed else None,
        "needs_human_30d": sum(1 for e in recent if e["needs_human"]),
        "findings_30d": sum(1 for e in entries if e["kind"] != "incident" and e["last_at"] >= now - STATS_WINDOW),
        "incidents_total": len(mine),
    }
    return {
        "device": canonical,
        "known": dev is not None,
        "aliases": aliases,
        "stats": stats,
        "total": len(entries),
        "entries": entries[:limit],
    }

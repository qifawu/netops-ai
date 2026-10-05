"""M4 看板的数据层——纯函数/薄封装，`app.py` 只负责把这些函数接成路由，
逻辑本身在这里，方便不起 HTTP 服务也能单测。

**不做登录/用户系统**（NEXT.md 明确说了，这是 lab，不是要上生产的产品）。
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from netops_ai.analysis.schema import ROLE_ROOT, WHO_AGENT_CAN_RETRY, normalize_enum
from netops_ai.labels import humanize_text, humanize_value, label_for

REPO_ROOT = Path(__file__).resolve().parents[2]
RECORDS_DIR = REPO_ROOT / "records"
CHAT_TRACE_DIR = RECORDS_DIR / "chat-traces"
INSPECTION_LATEST_PATH = RECORDS_DIR / "inspection-latest.json"
#: 状态巡检（登设备只读，见 `inspection/status.py`）的最近一份快照；历史在 `inspection-history/`。
INSPECTION_STATUS_PATH = RECORDS_DIR / "inspection-status-latest.json"
#: 剧本目录。`NETOPS_PLAYBOOKS_DIR` 可覆盖（网页编辑的端到端测试用临时目录，不碰真实剧本）。
PLAYBOOKS_DIR = Path(os.environ.get("NETOPS_PLAYBOOKS_DIR") or (REPO_ROOT / "playbooks"))

#: 角色枚举 起就是中文了；这张表只为读旧记录留着。
CN_ROLE = {"root": "根因", "consequence": "连带", "independent": "无关"}
CN_ROLE_LAYER = {"core": "核心", "aggregation": "汇聚", "access": "接入", "unmanaged": "未纳管"}


def _load_dotenv(path: Path) -> dict:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _env() -> dict:
    import os

    return {**_load_dotenv(REPO_ROOT / ".env"), **os.environ}


def list_records() -> list[dict]:
    """按 `finished_at` 倒序列出已处理的告警摘要（不是全文——看板列表用，
    点进去才拿全文，避免列表页把所有证据链全量传一遍）。
    """
    if not RECORDS_DIR.exists():
        return []
    summaries = []
    for path in RECORDS_DIR.glob("alert-*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        analysis = record.get("analysis_parsed") or {}
        summaries.append(
            {
                "eventid": record.get("eventid", ""),
                "root_cause": humanize_text(analysis.get("root_cause", "")),
                "confidence": analysis.get("confidence", ""),
                "has_undistinguishable": bool(analysis.get("undistinguishable_candidates")),
                "analysis_error": record.get("analysis_error", ""),
                "finished_at": record.get("finished_at", ""),
                "device_fetch_error": record.get("device_fetch_error", ""),
            }
        )
    summaries.sort(key=lambda s: s["finished_at"], reverse=True)
    return summaries


def load_record(eventid: str) -> dict | None:
    """看板的"推理过程展开"用这个拿全文——`hypothesis_checklist` 每一类的
    表态、每条证据的逐字出处和来源标记、`counter_evidence` 的
    `contradiction` 类型，**全部原样返回，不做任何折叠/摘要**，折叠成
    一句"AI 认为…"就完了是这个项目最不该做的事（NEXT.md 原话）。
    """
    path = RECORDS_DIR / f"alert-{eventid}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


#: trace_id 格式固定是 `agent_loop.py` 生成的 `{毫秒时间戳}-{4位十六进制}`（见 `run_agent_loop`），
#: 校验一下再拼文件名——不是信不过调用方，是不想把"路径参数直接拼文件名"这个口子开在新代码里。
_TRACE_ID_RE = re.compile(r"^[0-9a-f-]+$")


def load_chat_trace(trace_id: str) -> dict | None:
    """聊天页"展开这一步"按需拉完整取证明细（含原始 result）用——SSE 直播那条不带 result，
    见 `agent_loop.py::record_tool_call` 的注释：设备回显几千行不该塞进推流。
    """
    if not _TRACE_ID_RE.match(trace_id):
        return None
    path = CHAT_TRACE_DIR / f"{trace_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def run_inspection_and_persist(*, lookback_seconds: int | None = None) -> dict:
    """真跑一次 M5 巡检（只读，见 `netops_ai/inspection/scan.py`），
    结果落盘到固定位置，看板读的是这份缓存，不是每次刷新页面都重新扫
    2000+ 个监控项。
    """
    from netops_ai.inspection.config import load_config
    from netops_ai.inspection.scan import scan_all_hosts
    from netops_ai.zabbix.client import ZabbixClient

    cfg = load_config()  # 没有 inspection.yaml = 默认配置；文件写坏了这里会抛，不悄悄退回默认
    if lookback_seconds is None:
        lookback_seconds = cfg.lookback_seconds
    env = _env()
    with ZabbixClient(
        url=env.get("ZABBIX_URL", ""), user=env.get("ZABBIX_USER", ""), password=env.get("ZABBIX_PASSWORD", "")
    ) as zbx:
        report = scan_all_hosts(zbx, lookback_seconds=lookback_seconds, config=cfg)

    payload = {
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "scanned_hosts": report.scanned_hosts,
        "scanned_items": report.scanned_items,
        "items_with_data": report.items_with_data,
        "errors": report.errors,
        "findings": [
            {
                "kind": f.kind,
                "host_name": f.host_name,
                "item_name": f.item_name,
                "item_key": f.item_key,
                "finding": asdict(f.finding),
                "rule": f.rule,
                "series": f.series,
            }
            for f in report.findings
        ],
    }
    RECORDS_DIR.mkdir(exist_ok=True)
    # 先写临时文件再原子替换：多个对话/定时任务同时触发重扫时，读的人不会读到写了一半的 json。
    tmp = INSPECTION_LATEST_PATH.with_name(f"{INSPECTION_LATEST_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, INSPECTION_LATEST_PATH)
    _save_history("trend", payload)
    return payload


def _history_dir() -> Path:
    return RECORDS_DIR / "inspection-history"


def _save_history(kind: str, payload: dict) -> None:
    from netops_ai.inspection import history

    try:
        history.save(kind, payload, _history_dir())
    except OSError:
        pass  # 历史是附加功能，写不进去不能让巡检本身失败


def _write_json_atomic(path: Path, payload: dict) -> None:
    RECORDS_DIR.mkdir(exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_latest_status() -> dict | None:
    try:
        return json.loads(INSPECTION_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_status_and_persist() -> dict:
    """状态巡检：登拓扑里的每台设备只读检查，落盘最近一份 + 一份历史。上一份快照当错误计数的基线。"""
    from netops_ai.inspection.status import run_status_inspection

    snapshot = run_status_inspection(baseline=load_latest_status())
    _write_json_atomic(INSPECTION_STATUS_PATH, snapshot)
    _save_history("status", snapshot)
    return snapshot


def run_full_inspection() -> dict:
    """页面「立即巡检」/ 定时任务用：趋势（读 Zabbix）和状态（登设备）并行跑，互不拖累。
    一边失败不影响另一边，失败原因带回去。"""
    from concurrent.futures import ThreadPoolExecutor

    def _guard(fn):
        try:
            fn()
            return ""
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        trend_f, status_f = pool.submit(_guard, run_inspection_and_persist), pool.submit(_guard, run_status_and_persist)
        errors = {"trend": trend_f.result(), "status": status_f.result()}
    return {k: v for k, v in errors.items() if v}


def load_latest_inspection() -> dict | None:
    if not INSPECTION_LATEST_PATH.exists():
        return None
    return json.loads(INSPECTION_LATEST_PATH.read_text(encoding="utf-8"))


def split_ignored(raw: dict, cfg) -> tuple[list[dict], list[dict]]:
    """把巡检发现按忽略清单分成（保留的, 被忽略的）；被忽略的每条多一个 `ignore` 字段（原因、时间）。"""
    kept, ignored = [], []
    for row in raw.get("findings") or []:
        entry = cfg.is_ignored(row.get("host_name", ""), row.get("item_key", ""))
        (ignored if entry else kept).append({**row, "ignore": entry} if entry else row)
    return kept, ignored


def build_inspection_view(raw: dict) -> dict:
    """巡检页要的形状：分好组、带报告、带处置建议。不端原始 payload：少了 `groups` 巡检页会白屏。

    `config` / `ignored` 是后加的字段：当前生效的巡检配置（含默认值、范围说明，给页面顶部的表单用），
    和被人标成"不用管"的发现。被忽略的发现不进 `groups` / 报告 / 计数，但留在 `ignored` 里，页面能撤销。
    **`/api/inspection` 和 `export-fixtures.py` 共用这个函数**，改它两个出口一起变。
    """
    from netops_ai.inspection import advise as _advise
    from netops_ai.inspection.config import ConfigError, InspectionConfig, describe, load_config
    from netops_ai.inspection.report import group_findings, render_inspection_markdown

    config_error = ""
    try:
        cfg = load_config()
    except ConfigError as exc:
        cfg, config_error = InspectionConfig(), str(exc)

    kept, ignored = split_ignored(raw, cfg)
    visible = {**raw, "findings": kept}

    raw_groups = group_findings(visible)
    groups = humanize_value(raw_groups)
    # humanize 会把整串恰好等于登记词的 host 名（比如叫 zabbix 的主机）换成中文，
    # 也会改写 rule 里的 kind 码——"不用管"按钮要用原值回指，rule/series 是结构化数据，都原样放回
    for g_out, g_raw in zip(groups, raw_groups):
        for r_out, r_raw in zip(g_out["rows"], g_raw["rows"]):
            for k in ("host_raw", "key_raw", "rule", "series"):
                r_out[k] = r_raw[k]

    from netops_ai.inspection import history as _history

    status = load_latest_status()
    trend_recent = _history.load_recent("trend", _history_dir(), 2)
    status_recent = _history.load_recent("status", _history_dir(), 2)
    # 对比的是「最近一份历史 vs 它的上一份」，跟页面当前显示的是同一轮（历史在落盘时同步写入）
    diff = {
        "trend": _history.diff_trend(trend_recent[1], trend_recent[0]) if len(trend_recent) == 2 else None,
        "status": _history.diff_status(status_recent[1], status_recent[0]) if len(status_recent) == 2 else None,
    }
    return {
        **{k: raw.get(k) for k in ("scanned_hosts", "scanned_items", "items_with_data", "errors")},
        "status": status,
        "diff": diff,
        "history": _history.list_runs(_history_dir(), 20),
        "scanned_at": raw.get("scanned_at"),
        "finding_count": len(kept),
        "groups": groups,
        "report_markdown": humanize_text(render_inspection_markdown(visible)),
        "advice": humanize_value(_advise.load()),
        "config": describe(cfg, error=config_error),
        "ignore_list": cfg.ignore,
        "ignored": [
            {
                "host": r.get("host_name", ""), "item": r.get("item_name", ""), "key": r.get("item_key", ""),
                "kind": r.get("kind", ""), "reason": r["ignore"].get("reason", ""), "at": r["ignore"].get("at", ""),
            }
            for r in ignored
        ],
    }


def render_markdown_report(record: dict) -> str:
    """把一条告警记录导出成 md——报告导出功能，纯字符串拼接，不依赖模板引擎。"""
    analysis = record.get("analysis_parsed") or {}
    lines = [
        f"# 告警分析报告 · eventid {record.get('eventid', '')}",
        "",
        f"- 处理时间：{record.get('started_at', '')} ~ {record.get('finished_at', '')}",
        f"- 根因：{humanize_text(analysis.get('root_cause', '（无结论）'))}",
        f"- 置信度：{label_for(analysis.get('confidence', '（无）'))}",
        *(f"- 同一件事：#{r.get('eventid', '')} {r.get('host', '')}，{r.get('link', '')}" for r in analysis.get("related_alerts") or []),
        "",
        "## 证据链",
        "",
    ]
    for e in analysis.get("evidence") or []:
        lines.append(f"- [{label_for(e.get('source_from', ''))}] {humanize_text(e.get('claim', ''))}")
        lines.append(f"  > {humanize_text(e.get('source', ''))}")

    checklist = analysis.get("hypothesis_checklist") or {}
    if checklist:
        lines += ["", "## 根因方向表态", ""]
        for cat, item in checklist.items():
            lines.append(f"### {label_for(cat)}：{label_for(item.get('status', ''))}")
            lines.append(humanize_text(item.get("reason", "")))
            for ce in item.get("counter_evidence") or []:
                lines.append(
                    f"- 反证（{label_for(ce.get('contradiction', ''))}，来自 {label_for(ce.get('source_from', ''))}）："
                    f"{humanize_text(ce.get('claim', ''))}"
                )
                lines.append(f"  > {humanize_text(ce.get('source', ''))}")
            lines.append("")

    candidates = analysis.get("undistinguishable_candidates") or []
    if candidates:
        lines += ["## 判不出的候选方向", ""]
        for c in candidates:
            lines.append(f"- **{ '、'.join(humanize_text(x) for x in (c.get('candidates') or [])) }**："
                         f"{humanize_text(c.get('why_indistinguishable', ''))}")
            lines.append(f"  需要的数据：{humanize_text(c.get('what_data_would_help', ''))}")

    if record.get("device_fetch_error"):
        lines += ["", "## 设备取数错误", "", humanize_text(record["device_fetch_error"])]
    if record.get("zabbix_fetch_error"):
        lines += ["", "## Zabbix 取数错误", "", humanize_text(record["zabbix_fetch_error"])]

    return "\n".join(lines) + "\n"


# ---- 只读查询层：前端、HTTP 接口、fixture 导出三处共用 ----
# incident 列表扫 `records/*.json` 现算，原文才是审计真源；上千条之后再加索引。

_ALERTS_CACHE: dict = {"sig": None, "rows": []}


def load_alerts() -> list[dict]:
    """读 records/alert-*.json。看板每个页面每次都要读全量（几百个文件），按「文件数 + 最新修改时间」做签名缓存，
    没有新记录时直接复用上一次解析的结果。调用方只读、不改返回的列表。"""
    paths = sorted(RECORDS_DIR.glob("alert-*.json"))
    try:
        sig = (str(RECORDS_DIR), len(paths), max((p.stat().st_mtime_ns for p in paths), default=0))
    except OSError:
        sig = None
    if sig is not None and _ALERTS_CACHE["sig"] == sig:
        return _ALERTS_CACHE["rows"]
    rows = []
    for p in paths:
        try:
            rows.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    if sig is not None:
        _ALERTS_CACHE.update(sig=sig, rows=rows)
    return rows

def build_incidents(alerts: list[dict]) -> list[dict]:
    from netops_ai.llm.meter import ledger_path

    token_by_eventid = _token_by_event(_read_jsonl(ledger_path()))
    by_incident: dict[str, list[dict]] = defaultdict(list)
    loose = []
    for a in alerts:
        iid = a.get("incident_id") or ""
        (by_incident[iid] if iid else loose).append(a)

    out = []
    for iid, group in by_incident.items():
        group.sort(key=lambda a: int(a.get("alert_clock") or 0))
        lead = next((a for a in group if normalize_enum(a.get("incident_role")) == ROLE_ROOT), group[0])
        ap = lead.get("analysis_parsed") or {}
        roles = {r.get("eventid"): r for r in (ap.get("alert_roles") or [])}
        ver = (lead.get("evidence_verification") or {}).get("summary") or {}
        elapsed = lead.get("elapsed_seconds") or {}
        trace = [t for t in (lead.get("diagnostic_trace") or []) if t.get("source") == "ai" and t.get("tool")]
        incident_tokens = _token_bucket()
        for alert in group:
            item = token_by_eventid.get(str(alert.get("eventid") or ""), _token_bucket())
            for key in incident_tokens:
                incident_tokens[key] += item[key]
        candidates = []
        for candidate in ap.get("undistinguishable_candidates") or []:
            candidates.append({
                **candidate,
                "candidate_codes": list(candidate.get("candidates") or []),
                "candidates": [label_for(code) for code in (candidate.get("candidates") or [])],
                "why_indistinguishable": humanize_text(candidate.get("why_indistinguishable", "")),
                "what_data_would_help": humanize_text(candidate.get("what_data_would_help", "")),
                "how_to_get_it": humanize_text(candidate.get("how_to_get_it", "")),
            })
        out.append({
            "incident_id": iid,
            "fingerprint": iid.split("-")[0],
            "host": (lead.get("webhook_payload") or {}).get("host", ""),
            "clock": int(lead.get("alert_clock") or 0),
            "started_at": lead.get("started_at"),
            "root_cause": humanize_text(ap.get("root_cause", "")),
            # 模型给的一句话定性（schema 要求 ≤20 字，卡片标题用）。旧记录没有，前端回落到根因第一句。
            "headline": humanize_text(ap.get("headline", "")),
            "confidence": ap.get("confidence", ""),
            "repeat_count": lead.get("incident_repeat_count"),
            "alert_count": len(group),
            "alerts": [{
                "eventid": a.get("eventid"),
                "name": (a.get("webhook_payload") or {}).get("name", ""),
                "severity": (a.get("webhook_payload") or {}).get("severity", ""),
                "clock": int(a.get("alert_clock") or 0),
                "role": a.get("incident_role") or "",
                "role_cn": CN_ROLE.get(a.get("incident_role") or "", ""),
                "caused_by": (roles.get(a.get("eventid")) or {}).get("caused_by_eventid", ""),
                "reason": humanize_text((roles.get(a.get("eventid")) or {}).get("reason", "")),
            } for a in group],
            "evidence": [
                {
                    **e,
                    "claim": humanize_text(e.get("claim", "")),
                    "source": humanize_text(e.get("source", "")),
                    "source_label": label_for(e.get("source_from", "")),
                }
                for e in (ap.get("evidence") or [])
            ],
            "checklist": humanize_value(ap.get("hypothesis_checklist") or {}),
            "undistinguishable": candidates,
            "related": humanize_value(ap.get("related_alerts") or []),
            # 「需人工介入」不能只是个标签，得告诉人下一步干什么。
            # **这句不让模型再生成一遍**——`what_data_would_help` 是它下结论那次
            # 就一起写的，跟结论同源；再问一次是多花一次钱换一句可能对不上的话。
            # 用 `how_to_get_it`（怎么拿）而不是 `what_data_would_help`（要什么）——
            # 「缺 eth0 的内核日志」看完还得自己想从哪下手，
            # 「登 A1 跑 show logging | begin ...」看完就能动手。
            # 旧记录没有这个字段，回落到 what_data_would_help。
            "next_step": "；".join(
                humanize_text(x.get("how_to_get_it") or x.get("what_data_would_help") or "").strip()
                for x in (ap.get("undistinguishable_candidates") or [])
                if (x.get("how_to_get_it") or x.get("what_data_would_help") or "").strip()
            ),
            # 它自己能再查一轮的，跟要人上手的，处理方式不一样，前端要分开显示。
            "agent_can_retry": any(
                normalize_enum(x.get("who")) == WHO_AGENT_CAN_RETRY
                for x in (ap.get("undistinguishable_candidates") or [])
            ),
            "verification": {"total": ver.get("total", 0), "verified": ver.get("verified", 0), "unverified": ver.get("unverified", 0)},
            "trace": [
                {
                    "tool": t.get("tool"),
                    "tool_label": label_for(t.get("tool", "")),
                    "args": t.get("args"),
                    "ok": t.get("ok"),
                    "error": humanize_text(t.get("error", "")),
                }
                for t in trace
            ],
            "device_commands": lead.get("device_commands_run") or [],
            "elapsed": elapsed,
            "elapsed_total": _elapsed_total(lead),
            "token_cost": {
                "forensics": incident_tokens["forensics"],
                "analysis": incident_tokens["analysis"],
                "other": incident_tokens["other"],
                "total": incident_tokens["total"],
            },
            "feishu": lead.get("feishu_report") or {},
            "incomplete": any(t.get("incomplete") for t in (lead.get("diagnostic_trace") or [])),
            # 系统自己标出的矛盾要端上界面：藏起来，用户看到的就是一条理直气壮的错结论。
            "rule_violations": sorted({humanize_text(v) for a in group for v in (a.get("business_rule_violations") or [])}),
        })
    out.sort(key=lambda r: r["clock"], reverse=True)
    return out

def build_topology_view(incidents: list[dict], *, window_hours: int = 24) -> dict:
    """拓扑 + 台账 + 告警叠加。叠告警是自己做这页的理由：NetBox 的拓扑插件不知道 Zabbix 的事。

    写「出过故障」不写「正在告警」：records 里没有恢复状态，别把不知道的事说死。
    """
    import time as _time

    from netops_ai import topology as topo

    devices = topo.load_topology()
    cutoff = _time.time() - window_hours * 3600
    hits: dict[str, list[dict]] = defaultdict(list)
    for inc in incidents:
        if inc["clock"] < cutoff:
            continue
        dev = topo.resolve_device(devices, inc["host"])
        if dev is not None:
            hits[dev.name].append(inc)

    seen, links = set(), []
    unmanaged: set[str] = set()
    for name, dev in devices.items():
        for link in dev.links:
            key = tuple(sorted([(name, link.local_interface), (link.peer, link.peer_interface)]))
            if key in seen:
                continue
            seen.add(key)
            peer_managed = link.peer in devices
            if not peer_managed:
                unmanaged.add(link.peer)
            links.append({
                "a": name,
                "a_if": link.local_interface,
                "b": link.peer,
                "b_if": link.peer_interface,
                "managed": peer_managed,
            })

    order = {"core": 0, "aggregation": 1, "access": 2}
    rows = []
    for name, dev in sorted(devices.items()):
        mine = order.get(dev.role, 99)
        up = sorted({l.peer for l in dev.links if order.get(getattr(devices.get(l.peer), "role", ""), 99) < mine})
        mine_hits = hits.get(name, [])
        interfaces = dev.interfaces or tuple(
            topo.TopologyInterface(link.local_interface, link.peer, link.peer_interface)
            for link in dev.links
        )
        rows.append({
            "name": name,
            "managed": True,
            "role": dev.role,
            "role_cn": CN_ROLE_LAYER.get(dev.role, dev.role or "未标"),
            "mgmt_ip": dev.host,
            "zabbix_host": dev.aliases[0] if dev.aliases else "",
            "inventory_source": "NetBox" if topo.LAST_SOURCE == "netbox" else "本地文件，不是 NetBox",
            "model": dev.model,
            "serial": dev.serial,
            "site": dev.site,
            "rack": dev.rack,
            "platform": dev.platform,
            "software_version": dev.software_version,
            "interfaces": [
                {
                    "name": item.name,
                    "peer": item.peer,
                    "peer_interface": item.peer_interface,
                    "description": item.description,
                }
                for item in interfaces
            ],
            "uplinks": up,
            "single_homed": dev.role != "core" and len(up) == 1,
            "incident_count": len(mine_hits),
            "latest_incident": max((i["clock"] for i in mine_hits), default=0),
            "latest_root_cause": max(mine_hits, key=lambda i: i["clock"])["root_cause"] if mine_hits else "",
        })
    for name in sorted(unmanaged):
        rows.append({
            "name": name,
            "managed": False,
            "role": "unmanaged",
            "role_cn": "未纳管",
            "mgmt_ip": "",
            "zabbix_host": "",
            "inventory_source": "未纳管：不在 NetBox 或本地拓扑设备清单",
            "model": "",
            "serial": "",
            "site": "",
            "rack": "",
            "platform": "",
            "software_version": "",
            "interfaces": [],
            "uplinks": [],
            "single_homed": False,
            "incident_count": 0,
            "latest_incident": 0,
            "latest_root_cause": "",
        })
    return {
        "source": topo.LAST_SOURCE,
        "netbox_error": topo.LAST_NETBOX_ERROR,
        "cached_seconds": topo.cache_age(),
        "window_hours": window_hours,
        "devices": rows,
        "links": links,
    }


def _elapsed_total(alert: dict) -> float:
    """一次分析从窗口关闭起算的总耗时（秒）。

 取证和结论合成一条轨迹（起，`analysis_trace.from` 以 `agent_loop` 开头）之后，pipeline 里
 `analysis` 的计时起点和 `device_fetch` 是同一个（`analysis` 已经包含取证），直接求和会把这段时间算两遍——
 看板上「11 分 48 秒」其实只有约一半。旧记录两段是分开计时的，仍然求和。
    """
    elapsed = {k: v for k, v in (alert.get("elapsed_seconds") or {}).items() if isinstance(v, (int, float))}
    merged = str((alert.get("analysis_trace") or {}).get("from") or "").startswith("agent_loop")
    if merged:
        elapsed.pop("device_fetch", None)
    return round(sum(elapsed.values()), 2)


def build_overview(alerts: list[dict], incidents: list[dict]) -> dict:
    low = [i for i in incidents if i["confidence"] == "low"]
    merged = [i for i in incidents if i["alert_count"] > 1]
    ver_total = sum(i["verification"]["total"] for i in incidents)
    ver_ok = sum(i["verification"]["verified"] for i in incidents)
    denials = [c for a in alerts for c in (a.get("device_commands_run") or []) if c.get("allowed") is False]
    durations = [i["elapsed_total"] for i in incidents if i["elapsed_total"]]
    flagged = [i for i in incidents if i["rule_violations"]]

    # `ratio` 只给**真有分母**的卡片。界面上画环要靠它。
    # **没有比例的卡片不画环**——为了六张卡长得一样去编一个分母，
    # 就是拿视觉一致性换数字可信，这个项目不干这事。
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cards": [
            {"key": "incidents", "label": "已自动分析的故障", "value": len(incidents), "sub": f"{len(alerts)} 条告警"},
            {"key": "merged", "label": "合并发出的卡片", "value": len(merged), "sub": f"最多一次合并 {max([i['alert_count'] for i in incidents] or [0])} 条",
             "ratio": round(len(merged) / max(len(incidents), 1), 3)},
            # **措辞是「需人工介入」，不是「判不出」。** 同一件事，前者是流程状态、
            # 后者听起来像无能。但 `sub` 必须说清它是"判不出时如实转人工"——
            # 这个项目最值钱的就是不硬给结论，措辞可以柔和，事实不能含糊。
            {"key": "undetermined", "label": "需人工介入", "value": len(low), "sub": f"占 {round(len(low) / max(len(incidents), 1) * 100)}%，判不出时如实转人工，不硬给结论",
             "ratio": round(len(low) / max(len(incidents), 1), 3)},
            # 证据核对不在线上跑后没有分母，这张卡不显示，不拿 0/0 冒充核过。
            *([{"key": "evidence", "label": "证据逐字核过", "value": f"{ver_ok}/{ver_total}", "sub": "核不过不许发",
               "ratio": round(ver_ok / ver_total, 3)}] if ver_total else []),
            {"key": "denied", "label": "被白名单拦下的命令", "value": len(denials), "sub": "一条都没发给设备"},
            {"key": "flagged", "label": "它自己标出的矛盾", "value": len(flagged), "sub": f"占 {round(len(flagged) / max(len(incidents), 1) * 100)}%，重试到上限仍违反",
             "ratio": round(len(flagged) / max(len(incidents), 1), 3)},
            {"key": "speed", "label": "平均出结论", "value": f"{round(sum(durations) / max(len(durations), 1), 1)} 秒", "sub": "窗口关闭之后算"},
        ],
        "recent": incidents[:8],
    }


def _schedule_status(cfg: dict) -> dict:
    """定时任务那个点：没开就说没开；开了看上一轮每一项成没成。"""
    from . import schedule

    minutes = schedule.interval_minutes(cfg)
    if not minutes:
        return {"key": "schedule", "label": "定时任务未开", "ok": False, "note": "配 SCHEDULE_INTERVAL_MINUTES 打开"}
    st = schedule.load_state()
    if not st:
        return {"key": "schedule", "label": f"定时任务 每 {minutes} 分钟", "ok": True, "note": "还没跑过第一轮"}
    jobs = st.get("jobs") or {}
    return {
        "key": "schedule",
        "label": f"定时任务 每 {minutes} 分钟",
        "ok": all(j.get("ok") for j in jobs.values()),
        "note": f"上一轮 {st.get('at', '')}：" + "；".join(j.get("note", "") for j in jobs.values()),
    }


def build_status() -> dict:
    """头区状态点，每个都对应一次真实判断。常亮的绿点第一次出事时还是绿的，比没有更坏。"""
    from netops_ai import topology as topo

    cfg = _env()
    llm_ok = all(cfg.get(k) for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"))
    topo.load_topology()
    return {
        "items": [
            {"key": "backend", "label": "后端在跑", "ok": True},
            {
                "key": "topology",
                "label": "拓扑源：NetBox" if topo.LAST_SOURCE == "netbox" else "拓扑源：本地 yaml",
                "ok": topo.LAST_SOURCE == "netbox",
                "note": topo.LAST_NETBOX_ERROR,
            },
            {"key": "llm", "label": f"模型 {cfg.get('LLM_MODEL', '')}" if llm_ok else "模型没配",
             "ok": llm_ok},
            _schedule_status(cfg),
        ],
        "readonly": True,
    }

def build_playbooks() -> dict:
    def summarize(p: Path) -> dict:
        text = p.read_text(encoding="utf-8", errors="ignore")
        name = (re.search(r"^name:\s*(.+)$", text, re.M) or [None, p.stem])[1].strip()
        desc = (re.search(r"^description:\s*>?\s*\n?\s*(.+)$", text, re.M) or [None, ""])[1].strip()
        vendor = (re.search(r"^\s*vendor:\s*(.+)$", text, re.M) or [None, ""])[1].strip()
        steps = re.findall(r"^\s*-\s*id:\s*(\S+)", text, re.M)
        cmds = re.findall(r"command:\s*\"?([^\"\n}]+)", text)
        return {"file": p.name, "name": name, "description": desc, "vendor": vendor,
                "steps": steps, "commands": [c.strip() for c in cmds]}

    from netops_ai.playbooks import review as _review

    def summarize_proposed(p: Path) -> dict:
        row = summarize(p)
        # 已审核标记（通过 / 建议修改 / 有风险）；没审过是 None。新增字段，不动原有字段。
        row.update(_review.review_badge(p.name))
        return row

    base = PLAYBOOKS_DIR
    return {
        "active": [summarize(p) for p in sorted(base.glob("*.yaml"))],
        # 新增字段：跨厂商意图版正式剧本（playbooks/intent/）
        "intent": [summarize(p) for p in sorted((base / "intent").glob("*.yaml"))],
        "proposed": [summarize_proposed(p) for p in sorted((base / "_proposed").glob("*.yaml"))],
        "rejected": [summarize(p) for p in sorted((base / "_rejected").glob("*.yaml"))],
        "approved_archive": [p.name for p in sorted((base / "_approved_archive").glob("*.yaml"))],
    }


def build_sop_usage() -> dict:
    """SOP 使用聚合，只读返回；复核队列追加由 CLI 负责，避免 GET 接口写 records。"""
    from tools.sop_usage_report import aggregate_sop_usage

    return aggregate_sop_usage(load_alerts())


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 进程被杀留下的半行，跳过
    except OSError:
        pass
    return rows


#: 账本里的 tag 翻成人话。没登记的原样显示。
_LEDGER_TAG = {"langchain": "① 取证 / 对话", "llm_client": "② 研判 / 巡检建议"}

#: 每日花费按「谁在花」拆开（告警 / AI 问答 / SOP审核+巡检建议），不是按「① 取证 / 对话」
#: 这种"哪个客户端调的"拆法——那种拆法会把告警取证和对话循环揉在一起（两条都走
#: `run_agent_loop`，tag 都是 langchain）。
#:
#: 区分依据：`CURRENT_EVENTID`（`pipeline.py::_process_incident_batch` 设置）只在告警分析
#: 整段执行期间非空，取证循环和它的结构化收尾都在这段里，所以 eventid 非空 = 告警。
#: eventid 为空时再看 tag：走 `run_agent_loop`（tag=langchain）但没有 eventid 的只能是
#: 对话（chat_agent 不设 CURRENT_EVENTID）；tag=llm_client 且没有 eventid 的是 SOP 审核
#: 或巡检建议（两者都直接用 LLMClient，不经过循环也不设 eventid）。
_SOURCE_LABEL = {"alert": "告警分析", "chat": "AI 问答", "other": "SOP 审核 / 巡检建议"}


def _ledger_source(row: dict) -> str:
    if str(row.get("eventid") or "").strip():
        return "alert"
    return "chat" if row.get("tag") == "langchain" else "other"


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return default


def _host_of(alert: dict) -> str:
    return str((alert.get("webhook_payload") or {}).get("host") or alert.get("host") or "")


def _command_device(alert: dict, command_row: dict) -> str:
    return str(command_row.get("device") or command_row.get("host") or _host_of(alert))


def _command_allowed(command_row: dict) -> bool:
    return command_row.get("allowed") is not False


def _is_show_command(command: str) -> bool:
    text = command.strip().lower()
    return text.startswith("show ") or text == "show"


def _iter_alert_device_ops(alerts: list[dict]) -> list[dict]:
    rows = []
    for alert in alerts:
        eventid = str(alert.get("eventid") or "")
        clock = _as_int(alert.get("alert_clock"))
        trace_commands = {
            str((t.get("args") or {}).get("command") or "").strip()
            for t in (alert.get("diagnostic_trace") or [])
            if isinstance(t, dict) and t.get("tool") in {"device_show", "device_show_many"}
        }
        for index, command_row in enumerate(alert.get("device_commands_run") or []):
            if not isinstance(command_row, dict):
                continue
            command = str(command_row.get("command") or "").strip()
            if not command:
                continue
            rows.append(
                {
                    "clock": clock,
                    "device": _command_device(alert, command_row),
                    "command": command,
                    "allowed": _command_allowed(command_row),
                    "ok": bool(command_row.get("ok")),
                    "denial_reason": str(command_row.get("denial_reason") or ""),
                    "source_type": "alert",
                    "source_label": f"告警 {eventid}",
                    "eventid": eventid,
                    "trace_id": "",
                    "order": index,
                    "seen_in_trace": command in trace_commands,
                }
            )
    return rows


#: 跟 `_ALERTS_CACHE` 一个套路：按「目录 + 文件数 + 最新修改时间」签名。
#: 之前这里完全没缓存——审计页每次打开都要重新读+解析全部对话 trace 文件，
#: 实测 517 个文件、21M（单个大的能到 240K），每次都全量重读，
#: 是审计页加载慢的主因（`load_alerts()` 那份早就缓存了，这份漏了）。
_CHAT_TRACES_CACHE: dict = {"sig": None, "rows": []}


def _load_chat_traces(trace_dir: Path | None = None) -> list[dict]:
    base = trace_dir or CHAT_TRACE_DIR
    if not base.exists():
        return []
    paths = sorted(base.glob("*.json"))
    try:
        sig = (str(base), len(paths), max((p.stat().st_mtime_ns for p in paths), default=0))
    except OSError:
        sig = None
    if sig is not None and _CHAT_TRACES_CACHE["sig"] == sig:
        return _CHAT_TRACES_CACHE["rows"]
    rows = []
    for path in paths:
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    if sig is not None:
        _CHAT_TRACES_CACHE.update(sig=sig, rows=rows)
    return rows


def _iter_chat_device_ops(traces: list[dict]) -> list[dict]:
    rows = []
    for trace in traces:
        trace_id = str(trace.get("trace_id") or "")
        clock = _as_int(trace.get("finished_at") or trace.get("started_at"))
        trace_host = str(trace.get("host") or trace.get("default_device") or "")
        for index, call in enumerate(trace.get("tool_calls") or []):
            if not isinstance(call, dict):
                continue
            if call.get("tool") not in {"device_show", "device_show_many"}:
                continue
            args = call.get("args") or {}
            commands = args.get("commands") if isinstance(args, dict) else None
            if isinstance(commands, list):
                command_list = [str(c).strip() for c in commands if str(c).strip()]
            else:
                command_list = [str((args or {}).get("command") or "").strip()]
            for command in [c for c in command_list if c]:
                rows.append(
                    {
                        "clock": clock,
                        "device": str((args or {}).get("device") or (args or {}).get("host") or trace_host),
                        "command": command,
                        "allowed": True,
                        "ok": bool(call.get("ok")),
                        "denial_reason": str(call.get("error") or ""),
                        "source_type": "chat",
                        "source_label": f"对话 {trace_id[:8]}",
                        "eventid": "",
                        "trace_id": trace_id,
                        "order": index,
                        "seen_in_trace": True,
                    }
                )
    return rows


def _token_bucket() -> dict:
    return {"forensics": 0, "analysis": 0, "other": 0, "total": 0, "calls": 0}


def _token_phase(tag: str) -> str:
    if tag == "langchain":
        return "forensics"
    if tag == "llm_client":
        return "analysis"
    return "other"


def _token_by_event(ledger: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(_token_bucket)
    for row in ledger:
        eventid = str(row.get("eventid") or "")
        if not eventid:
            continue
        tokens = _as_int(row.get("total_tokens"))
        calls = _as_int(row.get("calls"))
        phase = _token_phase(str(row.get("tag") or ""))
        bucket = out[eventid]
        bucket[phase] += tokens
        bucket["total"] += tokens
        bucket["calls"] += calls
    return out


def _incident_token_summary(alerts: list[dict], token_by_event: dict[str, dict]) -> dict[str, dict]:
    out = {}
    for alert in alerts:
        eventid = str(alert.get("eventid") or "")
        b = token_by_event.get(eventid, _token_bucket())
        out[eventid] = {
            "eventid": eventid,
            "forensics": b["forensics"],
            "analysis": b["analysis"],
            "other": b["other"],
            "total": b["total"],
            "calls": b["calls"],
        }
    return out


def _why_expensive(alert: dict, token_row: dict, tool_rounds: int) -> str:
    if token_row["forensics"] > token_row["analysis"] and tool_rounds >= 6:
        return "取证轮数多，上下文反复带回模型。"
    if token_row["analysis"] > token_row["forensics"]:
        return "研判阶段占比高，证据和合并告警一起进了结论模型。"
    if token_row["total"] == 0:
        return "旧账本没有 eventid，无法对回这条告警。"
    return "取证和研判都有消耗。"


def _not_worth_model(alert: dict, token_row: dict, tool_rounds: int) -> str:
    """保守标记“不值得上模型”：只抓低价值且高成本的模式，宁可少报。

    规则来源是审计目标：自动化应该先省下重复、秒级自愈、低置信且花费高的工单。
    因此只有在 token 已明显偏高、且结论低置信或告警名呈现 flap/recovery/秒级抖动时才标。
    """
    name = str((alert.get("webhook_payload") or {}).get("name") or "").lower()
    confidence = str((alert.get("analysis_parsed") or {}).get("confidence") or "").lower()
    elapsed = _elapsed_total(alert)
    if token_row["total"] < 20000:
        return ""
    if confidence == "low":
        return "token 已高但结论仍是低置信，适合先走规则或人工补数据。"
    if elapsed and elapsed <= 10 and any(k in name for k in ("flap", "recovery", "resolved", "link down")):
        return "像秒级抖动或已恢复告警，适合先用规则过滤。"
    if tool_rounds == 0 and token_row["total"] >= 20000:
        return "没有设备取证却消耗较高，适合先用轻量摘要判断是否需要模型。"
    return ""


def _expensive_alerts(alerts: list[dict], token_by_eventid: dict[str, dict], *, limit: int = 8) -> list[dict]:
    rows = []
    for alert in alerts:
        eventid = str(alert.get("eventid") or "")
        token_row = token_by_eventid.get(eventid, _token_bucket())
        tool_rounds = sum(
            1
            for t in (alert.get("diagnostic_trace") or [])
            if isinstance(t, dict) and t.get("tool")
        )
        rows.append(
            {
                "eventid": eventid,
                "host": _host_of(alert),
                "name": (alert.get("webhook_payload") or {}).get("name", ""),
                "clock": _as_int(alert.get("alert_clock")),
                "tokens": token_row["total"],
                "forensics": token_row["forensics"],
                "analysis": token_row["analysis"],
                "tool_rounds": tool_rounds,
                "why": _why_expensive(alert, token_row, tool_rounds),
                "not_worth_model": _not_worth_model(alert, token_row, tool_rounds),
            }
        )
    return sorted(rows, key=lambda r: (r["tokens"], r["tool_rounds"], r["clock"]), reverse=True)[:limit]


def build_audit_view(alerts: list[dict]) -> dict:
    """命令审计页：回答 AI 查过哪些设备、敲过什么命令、每条告警花了多少 token。"""
    traces = _load_chat_traces()
    alert_ops = _iter_alert_device_ops(alerts)
    chat_ops = _iter_chat_device_ops(traces)
    ops = alert_ops + chat_ops
    from netops_ai.topology import load_topology, resolve_device

    topology = load_topology()
    for op in ops:
        raw_device = str(op["device"] or "").strip()
        resolved = resolve_device(topology, raw_device) if raw_device else None
        op["device"] = resolved.name if resolved is not None else (raw_device or "未指明设备")

    allowed_ops = [op for op in ops if op["allowed"]]
    non_show_ops = [op for op in allowed_ops if not _is_show_command(op["command"])]
    denial_groups: dict[tuple[str, str], dict] = {}
    for op in ops:
        if op["allowed"]:
            continue
        command = op["command"]
        raw_reason = str(op["denial_reason"] or "").strip()
        key = (command, raw_reason)
        group = denial_groups.setdefault(
            key,
            {
                "command": command,
                "reason": (
                    "命令里带了第二个管道符（正则里的 | 也算）"
                    if raw_reason == "只允许一个管道"
                    else humanize_text(raw_reason)
                ),
                "count": 0,
                "devices": set(),
                "eventids": set(),
                "sources": set(),
                "clock": 0,
            },
        )
        group["count"] += 1
        group["devices"].add(op["device"])
        if op["eventid"]:
            group["eventids"].add(op["eventid"])
        group["sources"].add(op["source_label"])
        group["clock"] = max(group["clock"], op["clock"])

    denials = []
    for group in denial_groups.values():
        eventids = sorted(group["eventids"])
        if eventids:
            source = f"告警 { '、'.join(eventids) } 的取证"
        else:
            source = "、".join(sorted(group["sources"]))
        denials.append(
            {
                "command": group["command"],
                "reason": group["reason"],
                "count": group["count"],
                "devices": sorted(group["devices"]),
                "device_count": len(group["devices"]),
                "eventids": eventids,
                "alert_count": len(eventids),
                "clock": group["clock"],
                "source": source,
            }
        )
    denials.sort(key=lambda d: d["clock"], reverse=True)

    command_counts = Counter(op["command"] for op in allowed_ops)
    command_top = [
        {"command": command, "count": count, "non_show": not _is_show_command(command)}
        for command, count in command_counts.most_common(20)
    ]

    device_rows = {}
    for op in allowed_ops:
        device = op["device"]
        row = device_rows.setdefault(device, {"device": device, "commands": 0, "latest_clock": 0})
        row["commands"] += 1
        row["latest_clock"] = max(row["latest_clock"], op["clock"])

    timeline = sorted(
        [
            {
                "clock": op["clock"],
                "device": op["device"] or "未知设备",
                "command": op["command"],
                "source_type": op["source_type"],
                "source": op["source_label"],
                "eventid": op["eventid"],
                "trace_id": op["trace_id"],
                "ok": op["ok"],
            }
            for op in allowed_ops
        ],
        key=lambda op: (op["clock"], op["source"], op["command"]),
        reverse=True,
    )

    return {
        "summary": {
            "command_total": len(allowed_ops),
            "unique_commands": len(command_counts),
            "non_show_commands": len(non_show_ops),
            "denied_commands": sum(d["count"] for d in denials),
            "devices": len(device_rows),
            "chat_traces": len(traces),
            "denied_groups": len(denials),
            "alerts_total": len(alerts),
        },
        "commands_top": command_top,
        "devices": sorted(device_rows.values(), key=lambda r: (r["commands"], r["latest_clock"]), reverse=True),
        "timeline": timeline,
        "denials": denials,
    }

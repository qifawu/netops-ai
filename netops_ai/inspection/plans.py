"""巡检计划：巡检清单（`checklist.py` 的格式）+ 周期 + 启用开关，存成仓库根 `inspection-plans/<name>.yaml`。

网页上用对话制定（`plan_chat.py`），CLI（`tools/checklist_run.py`）读写的也是同一份 YAML。这里只管：

- **检查模板库**：接口状态、OSPF 邻居、BGP 会话、接口错误计数、上联口错误、CPU、内存。模型和规则推荐都从这里挑，
  正则由我们写死，不让模型现编（模型编的正则多半对不上真实回显）。
- **草案**：模板展开、按角色推荐、建议（每条带一个可直接并入草案的 patch）、校验（复用 `validate_checklist`，
  命令照样过只读白名单）。
- **存储与执行**：名字只允许 `[A-Za-z0-9_.-]`；执行走 `run_checklist`，结果存 `records/checklist-runs/`；
  同一个计划不能重入（第二次直接拒绝，不排队）。
- **到点判断**：`every_minutes` 或 `daily_at`（服务器本地时间），锚点是「上次运行」和「启用时刻」里较晚的那个。

这里不 import 任何框架，也不登设备（执行时由调用方给适配器工厂，默认用 topology 里那个）。
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from netops_ai.devices.whitelist import check as whitelist_check
from netops_ai.inspection.checklist import load_history, run_checklist, save_result, validate_checklist

REPO_ROOT = Path(__file__).resolve().parents[2]
#: 模块级变量，测试里 `mock.patch.object(plans, "PLANS_DIR", tmp)` 换成临时目录
PLANS_DIR = REPO_ROOT / "inspection-plans"
RUNS_DIR = REPO_ROOT / "records" / "checklist-runs"
#: 每个计划最近一次趋势分析的结论（导出报告要带上）。单独一个目录：放进 RUNS_DIR 会被 `*-<name>-*.json` 的通配当成运行记录
TRENDS_DIR = REPO_ROOT / "records" / "checklist-trends"

NAME_RX = re.compile(r"[A-Za-z0-9_.-]{1,64}")
MIN_EVERY_MINUTES, MAX_EVERY_MINUTES = 1, 7 * 24 * 60
#: 计数类检查（错误计数）周期再短就只剩噪声：两次之间的增量太小，看不出趋势
COUNTER_MIN_MINUTES = 5
#: 趋势结论至少要这么多次运行
TREND_MIN_RUNS = 3
DEFAULT_EVERY_MINUTES = 60
ROLES = ("core", "aggregation", "access")


class PlanError(ValueError):
    """计划本身的问题（名字不合法、找不到、校验不过）。`problems` 是逐条的原因。"""

    def __init__(self, message: str, problems: list[str] | None = None, status: int = 400) -> None:
        super().__init__(message)
        self.problems = problems or []
        self.status = status


class PlanBusy(RuntimeError):
    """同一个计划正在跑，这次不重入。"""


# --------------------------------------------------------------------------- 检查模板库

_IPV4 = r"\d+\.\d+\.\d+\.\d+"

#: 模板 id → 定义。`check` 是展开后的清单检查项（不含 id，id 就是模板 id）；
#: `kind`：state（看当前状态）/ counter（只增的计数，看增量）/ gauge（会上下浮动的量）。
#: `roles`：按角色推荐时给哪些层级。`per_uplink`：按设备的上联口逐个展开（命令里有 {interface}）。
TEMPLATES: dict[str, dict[str, Any]] = {
    "interfaces": {
        "title": {"zh": "接口状态", "en": "Interface status"},
        "what": {"zh": "配了地址的接口协议是否 down；统计 up/up 的接口数",
                 "en": "Interfaces with an address whose protocol is down; count of up/up interfaces"},
        "kind": "state", "roles": ["core", "aggregation", "access"],
        "check": {
            "command": "show ip interface brief",
            "expect": [{"type": "not_regex", "severity": "critical",
                        "value": rf"^\S+\s+{_IPV4}\s+YES\s+\S+\s+(?:up|down)\s+down\s*$"}],
            "extract": [{"name": "interfaces_up", "regex": r"^\S+\s+\S+\s+\S+\s+\S+\s+(up)\s+up\s*$", "agg": "count", "cast": "int"}],
        },
    },
    "ospf_neighbors": {
        "title": {"zh": "OSPF 邻居", "en": "OSPF neighbors"},
        "what": {"zh": "至少一个 FULL 邻居、没有卡在中间状态的邻居；统计 FULL 邻居数（掉线时数字会降）",
                 "en": "At least one FULL neighbor, none stuck in a transitional state; count of FULL neighbors"},
        "kind": "state", "roles": ["core", "aggregation"],
        "check": {
            "command": "show ip ospf neighbor",
            "expect": [{"type": "contains", "value": "FULL", "severity": "critical"},
                       {"type": "not_regex", "value": r"\b(?:INIT|EXSTART|EXCHANGE|LOADING|ATTEMPT|DOWN)/", "severity": "warning"}],
            "extract": [{"name": "ospf_full", "regex": r"(FULL)/", "agg": "count", "cast": "int"}],
        },
    },
    "bgp_sessions": {
        "title": {"zh": "BGP 会话", "en": "BGP sessions"},
        "what": {"zh": "没有处于 Idle/Active/Connect 等非建立状态的邻居；统计已建立的会话数",
                 "en": "No neighbor in Idle/Active/Connect or another non-established state; count of established sessions"},
        "kind": "state", "roles": ["core"],
        "check": {
            "command": "show ip bgp summary",
            "expect": [{"type": "not_regex", "severity": "critical",
                        "value": rf"^{_IPV4}\s+4\s+\d+\s.*\s(?:Idle|Active|Connect|OpenSent|OpenConfirm)(?:\s*\(Admin\))?\s*$"}],
            "extract": [{"name": "bgp_established", "regex": rf"^{_IPV4}\s+4\s+\d+\s.*\s(\d+)\s*$", "agg": "count", "cast": "int"}],
        },
    },
    "interface_errors": {
        "title": {"zh": "接口错误计数", "en": "Interface error counters"},
        "what": {"zh": "全部接口的输入错误 / CRC / 输出错误合计；看相邻两次的增量，不看绝对值",
                 "en": "Input errors / CRC / output errors summed over all interfaces; judged by the increase between runs"},
        "kind": "counter", "roles": ["core", "aggregation"],
        "check": {
            "command": "show interfaces | include line protocol|input errors|output errors",
            "extract": [{"name": "input_errors_total", "regex": r"(\d+) input errors", "agg": "sum", "cast": "int"},
                        {"name": "crc_total", "regex": r"(\d+) CRC", "agg": "sum", "cast": "int"},
                        {"name": "output_errors_total", "regex": r"(\d+) output errors", "agg": "sum", "cast": "int"}],
        },
    },
    "uplink_errors": {
        "title": {"zh": "上联口错误计数", "en": "Uplink error counters"},
        "what": {"zh": "接入设备每个上联口：协议 up，并记录输入错误 / CRC / 输出错误",
                 "en": "Each uplink of an access device: protocol up, input errors / CRC / output errors recorded"},
        "kind": "counter", "roles": ["access"], "per_uplink": True,
        "check": {
            "command": "show interfaces {interface} | include line protocol|input errors|output errors",
            "expect": [{"type": "contains", "value": "line protocol is up", "severity": "critical"}],
            "extract": [{"name": "input_errors", "regex": r"(\d+) input errors", "cast": "int"},
                        {"name": "crc", "regex": r"(\d+) CRC", "cast": "int"},
                        {"name": "output_errors", "regex": r"(\d+) output errors", "cast": "int"}],
        },
    },
    "cpu": {
        "title": {"zh": "CPU 利用率", "en": "CPU utilization"},
        "what": {"zh": "5 秒 / 1 分钟 / 5 分钟 CPU；5 分钟均值 ≥ 90% 记警告",
                 "en": "CPU over 5 s / 1 min / 5 min; warning when the 5-minute average is 90% or more"},
        "kind": "gauge", "roles": ["core", "aggregation", "access"],
        "check": {
            "command": "show processes cpu | include CPU utilization",
            "expect": [{"type": "not_regex", "value": r"five minutes: (?:9\d|100)%", "severity": "warning"}],
            "extract": [{"name": "cpu_5s", "regex": r"five seconds: (\d+)%", "cast": "int"},
                        {"name": "cpu_1m", "regex": r"one minute: (\d+)%", "cast": "int"},
                        {"name": "cpu_5m", "regex": r"five minutes: (\d+)%", "cast": "int"}],
        },
    },
    "memory": {
        "title": {"zh": "内存", "en": "Memory"},
        "what": {"zh": "处理器内存池的已用 / 空闲字节数", "en": "Processor pool used / free bytes"},
        "kind": "gauge", "roles": [],
        "check": {
            "command": "show processes memory | include Processor Pool",
            "extract": [{"name": "mem_used", "regex": r"Used:\s*(\d+)", "cast": "int"},
                        {"name": "mem_free", "regex": r"Free:\s*(\d+)", "cast": "int"}],
        },
    },
}

#: 每个角色默认推荐的模板（规则推荐 / 模型不可用时用）
ROLE_DEFAULTS: dict[str, list[str]] = {
    "core": ["interfaces", "ospf_neighbors", "bgp_sessions", "interface_errors", "cpu"],
    "aggregation": ["interfaces", "ospf_neighbors", "interface_errors", "cpu"],
    "access": ["interfaces", "uplink_errors", "cpu"],
    "": ["interfaces", "interface_errors", "cpu"],
}


def template_catalog(lang: str = "zh") -> list[dict[str, Any]]:
    """给模型 / 前端看的模板清单（id、名字、命令、看什么、推荐给哪些角色）。"""
    lang = "en" if lang == "en" else "zh"
    return [{"id": tid, "title": t["title"][lang], "command": t["check"]["command"], "what": t["what"][lang],
             "kind": t["kind"], "roles": t["roles"]} for tid, t in TEMPLATES.items()]


def _short_if(name: str) -> str:
    """GigabitEthernet0/1 → Gi0-1，拿来拼检查项 id（id 只允许字母数字和 _.-）。"""
    m = re.match(r"([A-Za-z]{1,2})[A-Za-z-]*\s*([\d/.:]+)$", name.strip())
    raw = (m.group(1) + m.group(2)) if m else name
    return re.sub(r"[^A-Za-z0-9_.-]", "-", raw)


def uplinks(device: Any, topology: dict[str, Any], limit: int = 2) -> list[str]:
    """一台设备的上联口：对端是核心 / 汇聚（或对端不是接入）的链路的本端接口。"""
    out: list[str] = []
    for link in getattr(device, "links", ()) or ():
        peer = topology.get(link.peer)
        if peer is not None and getattr(peer, "role", "") == "access":
            continue
        if link.local_interface and link.local_interface not in out:
            out.append(link.local_interface)
    return out[:limit]


def expand_template(tid: str, scope: list[str] | None, topology: dict[str, Any], lang: str = "zh") -> list[dict[str, Any]]:
    """模板 → 清单检查项。`scope` 为 None 表示对计划里全部设备生效。上联口模板按设备逐口展开。"""
    t = TEMPLATES.get(tid)
    if t is None:
        raise PlanError(f"unknown template {tid!r} (known: {', '.join(TEMPLATES)})")
    base = json.loads(json.dumps(t["check"]))
    title = t["title"]["en" if lang == "en" else "zh"]
    if not t.get("per_uplink"):
        item = {"id": tid, "template": tid, "title": title, **base}
        if scope is not None:
            item["devices"] = list(scope)
        return [item]
    out: list[dict[str, Any]] = []
    for name in scope if scope is not None else [n for n, d in topology.items() if getattr(d, "role", "") == "access"]:
        dev = topology.get(name)
        if dev is None:
            continue
        for ifname in uplinks(dev, topology):
            item = json.loads(json.dumps(base))
            item["command"] = item["command"].replace("{interface}", ifname)
            out.append({"id": f"uplink-{name}-{_short_if(ifname)}", "template": tid, "title": f"{title} {name} {ifname}",
                        **item, "devices": [name]})
    return out


# --------------------------------------------------------------------------- 草案

def empty_draft() -> dict[str, Any]:
    return {"name": "", "vendor": "cisco", "devices": [], "checks": [], "schedule": {}}


def slug(text: str, fallback: str = "") -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(text or "").strip()).strip("-.").lower()[:48]
    return s or fallback


def default_name(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"plan-{now:%m%d-%H%M}"


def topology_devices(topology: dict[str, Any]) -> list[dict[str, Any]]:
    """给模型 / 前端的设备清单：名字、角色、邻居数。**只有这三项**，不带地址，更不带任何凭据。"""
    return [{"name": d.name, "role": getattr(d, "role", "") or "", "neighbors": len(getattr(d, "links", ()) or ())}
            for d in topology.values()]


def add_devices(draft: dict[str, Any], names: list[str], topology: dict[str, Any]) -> list[str]:
    """把拓扑里的设备加进草案（地址从拓扑取）。返回拓扑里找不到的名字。"""
    from netops_ai.topology import resolve_device

    have = {d["name"] for d in draft["devices"]}
    unknown = []
    for raw in names:
        dev = resolve_device(topology, raw)
        if dev is None:
            unknown.append(raw)
            continue
        if dev.name not in have:
            draft["devices"].append({"name": dev.name, "host": dev.host, "role": getattr(dev, "role", "") or ""})
            have.add(dev.name)
    return unknown


def add_template_checks(draft: dict[str, Any], tid: str, scope: list[str] | None, topology: dict[str, Any], lang: str = "zh") -> None:
    """并入一个模板：设备范围里不在草案的设备先补进来；同 id 已有就合并设备范围（不重复加）。"""
    from netops_ai.topology import resolve_device

    if scope:
        add_devices(draft, scope, topology)
        wanted = {(dev.name if (dev := resolve_device(topology, s)) is not None else s) for s in scope}
        scope = [d["name"] for d in draft["devices"] if d["name"] in wanted]
    elif TEMPLATES.get(tid, {}).get("per_uplink"):
        scope = [d["name"] for d in draft["devices"] if d.get("role") == "access"]  # 上联口模板只对草案里的接入设备
    for item in expand_template(tid, scope, topology, lang):
        existing = next((c for c in draft["checks"] if c.get("id") == item["id"]), None)
        if existing is None:
            draft["checks"].append(item)
        elif existing.get("devices") is not None:
            if item.get("devices") is None:
                existing.pop("devices", None)
            else:
                existing["devices"] = sorted(set(existing["devices"]) | set(item["devices"]), key=_device_order(draft))
    _drop_full_scopes(draft)


def _device_order(draft: dict[str, Any]) -> Callable[[str], int]:
    order = {d["name"]: i for i, d in enumerate(draft["devices"])}
    return lambda n: order.get(n, 999)


def _drop_full_scopes(draft: dict[str, Any]) -> None:
    """设备范围等于全部设备的，去掉 `devices`（= 全部），草案更短，以后加设备也自动覆盖。上联口检查除外。"""
    names = {d["name"] for d in draft["devices"]}
    for c in draft["checks"]:
        if c.get("template") == "uplink_errors":
            continue
        if c.get("devices") is not None and set(c["devices"]) >= names and names:
            c.pop("devices")


def apply_patch(draft: dict[str, Any] | None, patch: dict[str, Any] | None, topology: dict[str, Any], lang: str = "zh") -> dict[str, Any]:
    """把一条建议的 patch 并进草案（「采纳」按钮），确定性的，不调模型。

    patch：`{add_devices: [名字], add_checks: [{template, devices|null}], schedule: {every_minutes|daily_at}|null,
    add_custom_checks: [{command, title?, devices?}]（用户从检索到的候选命令里选的）, remove_checks: [id],
    remove_devices: [名字], enabled: bool}`。自定义命令不在这里判白名单——之后的 `sanitize` / `check_draft` 统一判。
    """
    out = json.loads(json.dumps(draft or empty_draft()))
    for k, v in empty_draft().items():
        out.setdefault(k, v)
    patch = patch or {}
    add_devices(out, [str(x) for x in patch.get("add_devices") or []], topology)
    for c in patch.get("add_checks") or []:
        if isinstance(c, dict) and c.get("template"):
            add_template_checks(out, str(c["template"]), c.get("devices") or None, topology, lang)
    for c in patch.get("add_custom_checks") or []:
        if not isinstance(c, dict) or not str(c.get("command") or "").strip():
            continue
        cmd = " ".join(str(c["command"]).split())
        if any(x.get("command") == cmd for x in out["checks"]):
            continue
        cid = slug(c.get("id") or re.sub(r"^show\s+", "", cmd), "cmd")[:40] or "cmd"
        while any(x.get("id") == cid for x in out["checks"]):
            cid += "-2"
        item: dict[str, Any] = {"id": cid, "title": str(c.get("title") or cmd)[:80], "command": cmd}
        if c.get("devices"):
            add_devices(out, [str(x) for x in c["devices"]], topology)
            item["devices"] = [d["name"] for d in out["devices"] if d["name"] in set(c["devices"])]
        out["checks"].append(item)
    gone = {str(x) for x in patch.get("remove_checks") or []}
    if gone:
        out["checks"] = [c for c in out["checks"] if c.get("id") not in gone]
    drop = {str(x) for x in patch.get("remove_devices") or []}
    if drop:
        out["devices"] = [d for d in out["devices"] if d["name"] not in drop]
        kept = []
        for c in out["checks"]:
            if c.get("devices") is not None:
                c["devices"] = [n for n in c["devices"] if n not in drop]
                if not c["devices"]:
                    continue  # 只针对被拿掉的设备的检查项一起拿掉
            kept.append(c)
        out["checks"] = kept
    if isinstance(patch.get("enabled"), bool):
        out["enabled"] = patch["enabled"]
    if isinstance(patch.get("schedule"), dict) and patch["schedule"]:
        out["schedule"] = normalize_schedule(patch["schedule"])
    if not out.get("name"):
        out["name"] = default_name()
    return out


def recommended_draft(topology: dict[str, Any], roles: list[str] | None = None, *, devices: list[str] | None = None,
                      templates: list[str] | None = None, schedule: dict[str, Any] | None = None, lang: str = "zh",
                      base: dict[str, Any] | None = None) -> dict[str, Any]:
    """规则推荐：按角色挑设备，按角色给每台推荐检查模板；`templates` 给了就只用这些模板（仍按角色挑适用的设备）。"""
    draft = json.loads(json.dumps(base)) if base else empty_draft()
    for k, v in empty_draft().items():
        draft.setdefault(k, v)
    names = list(devices or [])
    if roles:
        names += [d.name for d in topology.values() if (getattr(d, "role", "") or "") in roles]
    add_devices(draft, names, topology)
    by_role: dict[str, list[str]] = {}
    for d in draft["devices"]:
        by_role.setdefault(d.get("role", "") if d.get("role", "") in ROLE_DEFAULTS else "", []).append(d["name"])
    wanted: dict[str, list[str]] = {}
    for role, members in by_role.items():
        for tid in ROLE_DEFAULTS[role]:
            if templates is not None and tid not in templates:
                continue
            wanted.setdefault(tid, []).extend(members)
    if templates:
        for tid in templates:  # 用户点名要的检查，角色默认里没有的也要给（对全部设备）
            if tid not in wanted and tid in TEMPLATES and not TEMPLATES[tid].get("per_uplink"):
                wanted[tid] = [d["name"] for d in draft["devices"]]
    for tid in TEMPLATES:  # 按模板库的顺序加，草案读起来稳定
        if tid in wanted:
            add_template_checks(draft, tid, wanted[tid], topology, lang)
    if schedule:
        draft["schedule"] = normalize_schedule(schedule)
    elif not draft.get("schedule"):
        draft["schedule"] = {"every_minutes": DEFAULT_EVERY_MINUTES}
    if not draft.get("name"):
        draft["name"] = default_name()
    return draft


def confirmation_card(draft: dict[str, Any]) -> dict[str, Any]:
    """保存前的三项确认：①每台设备要执行的只读命令 ②实施机器 ③实施周期。"""
    d = draft or {}
    devices = [{"name": x.get("name"), "host": x.get("host"), "role": x.get("role", "")} for x in d.get("devices") or []]
    commands = []
    for dev in devices:
        cmds = [{"id": c.get("id"), "command": c.get("command")} for c in d.get("checks") or []
                if c.get("devices") is None or dev["name"] in c["devices"]]
        commands.append({"device": dev["name"], "host": dev["host"], "commands": cmds})
    return {"name": d.get("name"), "commands": commands, "devices": devices, "schedule": d.get("schedule") or {},
            "enabled": d.get("enabled", True)}


def plan_brief(plan: dict[str, Any]) -> dict[str, Any]:
    """列计划给用户挑（调整现有计划时）：名字、设备、检查项、周期、启用、最近结果。"""
    last = run_summary(last_run(plan["name"]))
    return {"name": plan["name"], "devices": [d.get("name") for d in plan.get("devices") or []],
            "checks": [c.get("id") for c in plan.get("checks") or []], "schedule": plan.get("schedule") or {},
            "enabled": bool(plan.get("enabled")), "last_run": last}


def load_as_draft(name: str) -> dict[str, Any]:
    """把一个已存的计划加载成草案（去掉时间戳这类运行时字段）。"""
    plan = load_plan(name)
    return {k: v for k, v in plan.items() if k not in ("created_at", "updated_at", "enabled_at")}


# --------------------------------------------------------------------------- 周期

def normalize_schedule(s: Any) -> dict[str, Any]:
    """`{every_minutes: N}` 或 `{daily_at: "HH:MM"}`；模型给两个都填时 every_minutes>0 优先。格式错的原样留着交给校验报。"""
    if not isinstance(s, dict):
        return {}
    every = s.get("every_minutes")
    daily = str(s.get("daily_at") or "").strip()
    try:
        every_i = int(every) if every not in (None, "") else 0
    except (TypeError, ValueError):
        return {"every_minutes": every}
    if every_i > 0:
        return {"every_minutes": every_i}
    if daily:
        m = re.fullmatch(r"(\d{1,2})[:：](\d{2})", daily)
        return {"daily_at": f"{int(m.group(1)):02d}:{m.group(2)}" if m else daily}
    return {}


def validate_schedule(s: Any) -> list[str]:
    if not isinstance(s, dict) or not s:
        return ["schedule: required, {every_minutes: N} or {daily_at: \"HH:MM\"}"]
    if set(s) - {"every_minutes", "daily_at"} or len(s) != 1:
        return ["schedule: exactly one of every_minutes / daily_at"]
    if "every_minutes" in s:
        v = s["every_minutes"]
        if not isinstance(v, int) or isinstance(v, bool) or not MIN_EVERY_MINUTES <= v <= MAX_EVERY_MINUTES:
            return [f"schedule.every_minutes: an integer between {MIN_EVERY_MINUTES} and {MAX_EVERY_MINUTES}"]
        return []
    m = re.fullmatch(r"(\d{2}):(\d{2})", str(s["daily_at"]))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return ["schedule.daily_at: HH:MM, 00:00-23:59 (server local time)"]
    return []


def schedule_minutes(s: dict[str, Any]) -> int:
    """周期折成分钟（每天一次 = 1440），拿来判断「计数类别太频繁」「多久能出趋势」。"""
    if isinstance(s, dict) and isinstance(s.get("every_minutes"), int):
        return s["every_minutes"]
    return 24 * 60 if isinstance(s, dict) and s.get("daily_at") else 0


def next_run(plan: dict[str, Any], last_run_at: datetime | None, now: datetime | None = None) -> datetime | None:
    """下一次该跑的时刻（UTC aware）。锚点 = 上次运行和启用时刻里较晚的；都没有就从 now 算。没启用返回 None。"""
    if not plan.get("enabled"):
        return None
    now = now or datetime.now(timezone.utc)
    anchors = [t for t in (last_run_at, _parse_ts(plan.get("enabled_at"))) if t is not None]
    anchor = max(anchors) if anchors else now
    s = plan.get("schedule") or {}
    if isinstance(s.get("every_minutes"), int) and s["every_minutes"] > 0:
        return anchor + timedelta(minutes=s["every_minutes"])
    m = re.fullmatch(r"(\d{2}):(\d{2})", str(s.get("daily_at") or ""))
    if not m:
        return None
    local = anchor.astimezone()  # 服务器本地时区
    cand = local.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    if cand <= local:
        cand += timedelta(days=1)
    return cand.astimezone(timezone.utc)


def _parse_ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- 校验

_MISSING_PREFIXES = ("devices: a non-empty list", "checks: a non-empty list")


def check_draft(draft: dict[str, Any] | None) -> dict[str, Any]:
    """草案校验：`problems` 是硬错误（命令被白名单拒、正则写错、周期格式错……），`missing` 是还没定的部分
    （还没选设备 / 检查项 / 周期）。`ok` 只有两样都空才是 True。修正回合只拿 problems 去让模型改。"""
    d = draft or {}
    problems: list[str] = []
    missing: list[str] = []
    for p in validate_checklist(d):
        (missing if p.startswith(_MISSING_PREFIXES) else problems).append(p)
    if not d.get("schedule"):
        missing.append("schedule: not decided yet")
    else:
        problems.extend(validate_schedule(d["schedule"]))
    if "enabled" in d and not isinstance(d["enabled"], bool):
        problems.append("enabled: true or false")
    return {"ok": not problems and not missing, "problems": problems, "missing": missing}


def refused_checks(draft: dict[str, Any]) -> list[tuple[int, str, str]]:
    """被只读白名单拒绝的检查项：(下标, 命令, 原因)。"""
    vendor = draft.get("vendor", "cisco")
    out = []
    for i, c in enumerate(draft.get("checks") or []):
        cmd = c.get("command") if isinstance(c, dict) else None
        if isinstance(cmd, str) and cmd.strip():
            try:
                v = whitelist_check(vendor, cmd)
            except ValueError as exc:  # 不认识的厂商
                out.append((i, cmd, str(exc)))
                continue
            if not v.allowed:
                out.append((i, cmd, v.reason))
    return out


def sanitize(draft: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """把有硬错误的检查项（尤其是被白名单拒的命令）从草案里拿掉。**写命令永远不留在草案里。**
    返回（干净的草案, 拿掉了什么）。"""
    d = json.loads(json.dumps(draft))
    removed: list[str] = []
    bad_idx = {i for i, _, _ in refused_checks(d)}
    for i, cmd, reason in refused_checks(d):
        removed.append(f"removed checks[{i}] {cmd!r}: refused by the read-only whitelist ({reason})")
    for i, c in enumerate(d.get("checks") or []):
        if i in bad_idx:
            continue
        probe = {"name": "x", "vendor": d.get("vendor", "cisco"), "devices": d.get("devices") or [{"name": "_", "host": "_"}],
                 "checks": [c]}
        own = [p for p in validate_checklist(probe) if p.startswith("checks[0]")]
        if own:
            bad_idx.add(i)
            removed.append(f"removed checks[{i}] {c.get('id') if isinstance(c, dict) else c!r}: " + "; ".join(own))
    d["checks"] = [c for i, c in enumerate(d.get("checks") or []) if i not in bad_idx]
    return d, removed


# --------------------------------------------------------------------------- 建议

def rule_suggestions(draft: dict[str, Any] | None, topology: dict[str, Any], lang: str = "zh") -> list[dict[str, Any]]:
    """按规则给的建议，每条：id、text、reason、patch（None 表示只是提醒，没有可并入的改动）。
    已经满足的不再提——采纳之后这条自然消失。"""
    zh = lang != "en"
    d = draft or empty_draft()
    devs = d.get("devices") or []
    have: dict[str, set[str]] = {}
    for c in d.get("checks") or []:
        tid = c.get("template") or c.get("id")
        scope = c.get("devices")
        names = set(scope) if scope is not None else {x["name"] for x in devs}
        have.setdefault(tid, set()).update(names)
    by_role = lambda *roles: [x["name"] for x in devs if x.get("role") in roles]  # noqa: E731
    out: list[dict[str, Any]] = []

    if not devs:
        names = [x.name for x in topology.values() if getattr(x, "role", "") in ("core", "aggregation")]
        if names:
            out.append({"id": "start-core-agg",
                        "text": "先从核心和汇聚设备开始：" + "、".join(names) if zh else "Start with the core and aggregation devices: " + ", ".join(names),
                        "reason": "故障影响面最大的在这两层，接入层可以之后再加。" if zh else "Faults there affect the most users; access switches can be added later.",
                        "patch": {"add_devices": names}})
    core_agg = by_role("core", "aggregation")
    miss = [n for n in core_agg if n not in have.get("ospf_neighbors", set())]
    if miss:
        out.append({"id": "ospf-core-agg",
                    "text": f"给 {'、'.join(miss)} 加 OSPF 邻居检查" if zh else f"Add an OSPF neighbor check on {', '.join(miss)}",
                    "reason": "核心 / 汇聚跑路由协议，邻居掉线是影响最大的一类故障；FULL 邻居数下降会直接体现在趋势里。" if zh
                    else "Core and aggregation run the routing protocol; a lost neighbor is the most disruptive fault, and a drop in FULL neighbors shows in the trend.",
                    "patch": {"add_checks": [{"template": "ospf_neighbors", "devices": miss}]}})
    core = by_role("core")
    miss = [n for n in core if n not in have.get("bgp_sessions", set())]
    if miss:
        out.append({"id": "bgp-core",
                    "text": f"给核心 {'、'.join(miss)} 加 BGP 会话检查" if zh else f"Add a BGP session check on the core devices {', '.join(miss)}",
                    "reason": "核心一般承载外部 / 跨域 BGP；没跑 BGP 的设备这条只会记 0 个会话，不会误报。" if zh
                    else "The core usually carries external BGP; on a device without BGP the check just records 0 sessions, no false alarm.",
                    "patch": {"add_checks": [{"template": "bgp_sessions", "devices": miss}]}})
    access = by_role("access")
    miss = [n for n in access if n not in have.get("uplink_errors", set()) and uplinks(topology.get(n), topology)] if access else []
    if miss:
        out.append({"id": "access-uplink",
                    "text": f"接入层 {'、'.join(miss)} 盯上联口错误计数" if zh else f"Watch the uplink error counters on the access switches {', '.join(miss)}",
                    "reason": "接入交换机最常见的问题是上联口的 CRC / 输入错误慢慢涨（线缆、光模块），单看一次看不出来。" if zh
                    else "The most common access-switch problem is CRC / input errors slowly growing on the uplink (cable, optics) — invisible in a single run.",
                    "patch": {"add_checks": [{"template": "uplink_errors", "devices": miss}]}})
    minutes = schedule_minutes(d.get("schedule") or {})
    counters = [c for c in d.get("checks") or [] if TEMPLATES.get(c.get("template") or "", {}).get("kind") == "counter"]
    if counters and 0 < minutes < COUNTER_MIN_MINUTES:
        out.append({"id": "counter-interval",
                    "text": "计数类检查的周期不要短于 5 分钟，建议改成每 15 分钟" if zh else "Counter checks should not run more often than every 5 minutes; suggest every 15 minutes",
                    "reason": "两次之间的增量太小，趋势里只剩噪声；还会无谓地增加设备登录次数。" if zh
                    else "The increase between runs is too small to show a trend, and it adds needless logins to the devices.",
                    "patch": {"schedule": {"every_minutes": 15}}})
    if minutes and d.get("checks"):
        total = minutes * TREND_MIN_RUNS
        span = ((f"{total} 分钟" if total < 60 else f"{total / 60:g} 小时") if zh
                else (f"{total} minutes" if total < 60 else f"{total / 60:g} hours"))
        out.append({"id": "trend-runs",
                    "text": (f"趋势结论至少需要 {TREND_MIN_RUNS} 次运行：按这个周期大约 {span}后才有" if zh
                             else f"A trend needs at least {TREND_MIN_RUNS} runs: with this schedule that is about {span}"),
                    "reason": "一两次的数字只能说明当时的状态，分不清是偶发尖峰还是持续变坏。" if zh
                    else "One or two data points only show the state at that moment; they cannot tell a spike from a worsening trend.",
                    "patch": None})
    if devs and "cpu" not in have:
        out.append({"id": "cpu-all",
                    "text": "加一条 CPU 利用率检查（全部设备）" if zh else "Add a CPU utilization check (all devices)",
                    "reason": "成本很低（一条命令），CPU 长时间偏高往往是路由震荡或广播风暴的旁证。" if zh
                    else "Cheap (one command); sustained high CPU is often a side effect of route flapping or a broadcast storm.",
                    "patch": {"add_checks": [{"template": "cpu", "devices": None}]}})
    return out


# --------------------------------------------------------------------------- 存储

def safe_name(name: Any) -> str:
    if not isinstance(name, str) or not NAME_RX.fullmatch(name) or name in (".", "..") or name.startswith("."):
        raise PlanError("plan name: 1-64 characters of letters, digits, '_', '.', '-' (not starting with '.')")
    return name


def plan_path(name: str) -> Path:
    path = (Path(PLANS_DIR) / f"{safe_name(name)}.yaml").resolve()
    if path.parent != Path(PLANS_DIR).resolve():  # 双保险：解析后必须还在计划目录里
        raise PlanError("plan name escapes the plans directory")
    return path


#: 存进 YAML 的字段顺序（人打开文件时先看到名字和周期）
_KEY_ORDER = ("name", "description", "enabled", "schedule", "vendor", "devices", "checks", "created_at", "updated_at", "enabled_at")


def _ordered(plan: dict[str, Any]) -> dict[str, Any]:
    out = {k: plan[k] for k in _KEY_ORDER if k in plan}
    out.update({k: v for k, v in plan.items() if k not in out})
    return out


def load_plan(name: str) -> dict[str, Any]:
    path = plan_path(name)
    if not path.exists():
        raise PlanError(f"no plan named {name!r}", status=404)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PlanError(f"{path.name}: not a mapping", status=500)
    data["name"] = name  # 文件名为准
    return data


def list_plans() -> list[dict[str, Any]]:
    out = []
    root = Path(PLANS_DIR)
    if not root.is_dir():
        return out
    for f in sorted(root.glob("*.yaml")):
        if not NAME_RX.fullmatch(f.stem):
            continue
        try:
            out.append(load_plan(f.stem))
        except (PlanError, yaml.YAMLError, OSError):
            continue
    return out


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def save_plan(draft: dict[str, Any], *, enabled: bool = True, overwrite: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """保存（用户点了「确认并启用」）。**保存前再校验一次**，不过就拒绝，什么都不写。"""
    plan = json.loads(json.dumps(draft or {}))
    plan.pop("ready", None)
    name = safe_name(plan.get("name"))
    plan["enabled"] = bool(enabled)
    plan.setdefault("vendor", "cisco")
    v = check_draft(plan)
    if not v["ok"]:
        raise PlanError("the plan does not pass validation", v["problems"] + v["missing"])
    path = plan_path(name)
    if path.exists() and not overwrite:
        raise PlanError(f"a plan named {name!r} already exists", status=409)
    stamp = _now_iso(now)
    plan.setdefault("created_at", stamp)
    plan["updated_at"] = stamp
    if plan["enabled"]:
        plan["enabled_at"] = stamp
    _write(path, plan)
    return plan


def update_plan(name: str, *, enabled: bool | None = None, schedule: dict[str, Any] | None = None,
                now: datetime | None = None) -> dict[str, Any]:
    """启用 / 暂停、改周期。改周期或重新启用时刷新 `enabled_at`：下一次从现在起算，不会因为上次运行很久以前而立刻补跑。"""
    plan = load_plan(name)
    stamp = _now_iso(now)
    if schedule is not None:
        s = normalize_schedule(schedule)
        problems = validate_schedule(s)
        if problems:
            raise PlanError("invalid schedule", problems)
        plan["schedule"] = s
        plan["enabled_at"] = stamp
    if enabled is not None:
        if enabled and not plan.get("enabled"):
            plan["enabled_at"] = stamp
        plan["enabled"] = bool(enabled)
    v = check_draft(plan)
    if not v["ok"]:
        raise PlanError("the plan does not pass validation", v["problems"] + v["missing"])
    plan["updated_at"] = stamp
    _write(plan_path(name), plan)
    return plan


def rename_plan(old: str, new: str, *, now: datetime | None = None) -> dict[str, Any]:
    """改名：新名字过路径安全校验、不能重名；正在运行的不改（结果文件按名字落盘，跑到一半改名会对不上）。
    历史运行记录不跟着改名——它们是旧名字那次运行的事实，留在原处。"""
    safe_name(old)
    new = safe_name(new)
    if new == old:
        return load_plan(old)
    plan = load_plan(old)
    if is_running(old):
        raise PlanError(f"plan {old!r} is running; rename it after the run finishes", status=409)
    if plan_path(new).exists():
        raise PlanError(f"a plan named {new!r} already exists", status=409)
    plan["name"] = new
    plan["updated_at"] = _now_iso(now)
    _write(plan_path(new), plan)
    plan_path(old).unlink()
    if old in _LAST_ATTEMPT:
        _LAST_ATTEMPT[new] = _LAST_ATTEMPT.pop(old)
    return plan


def history_files(name: str, runs_dir: Path | None = None) -> list[Path]:
    """这个计划自己的历史运行文件：文件名通配 + 记录里的名字精确等于 name（`core` 不会带上 `core-health` 的）。"""
    root = Path(runs_dir or RUNS_DIR)
    if not root.is_dir():
        return []
    out = []
    for f in sorted(root.glob(f"*-{safe_name(name)}-*.json")):
        if f.parent.resolve() != root.resolve():
            continue
        try:
            run = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(run, dict) and run.get("checklist") == name:
            out.append(f)
    return out


def trend_path(name: str) -> Path:
    return Path(TRENDS_DIR) / f"{safe_name(name)}.json"


def save_trend(name: str, result: dict[str, Any]) -> None:
    path = trend_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


def load_trend(name: str) -> dict[str, Any] | None:
    try:
        data = json.loads(trend_path(name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def delete_preview(name: str, runs_dir: Path | None = None) -> dict[str, Any]:
    """删除前给用户看：会删哪个文件、勾选「连历史一起删」时还会删多少个历史文件。"""
    load_plan(name)  # 不存在就 404
    hist = history_files(name, runs_dir)
    return {"name": name, "plan_file": _rel(plan_path(name)), "history_dir": _rel(Path(runs_dir or RUNS_DIR)),
            "history_count": len(hist), "trend_file": _rel(trend_path(name)) if trend_path(name).exists() else "",
            "running": is_running(name)}


def delete_plan(name: str, *, with_history: bool = False, runs_dir: Path | None = None) -> dict[str, Any]:
    """删除一个计划。**正在运行就拒绝（409，请稍后再删）**，不去打断它。
    只删：`inspection-plans/<name>.yaml`；勾了 with_history 再删这个计划自己的历史运行文件和趋势结论。没有任何路径参数。"""
    path = plan_path(name)
    if not path.exists():
        raise PlanError(f"no plan named {name!r}", status=404)
    lock = _lock(name)
    if not lock.acquire(blocking=False):  # 拿住锁再删：删的过程中调度线程也起不了这个计划
        raise PlanError(f"plan {name!r} is running; delete it after the run finishes", status=409)
    try:
        deleted = [_rel(path)]
        hist = history_files(name, runs_dir) if with_history else []
        path.unlink()
        for f in hist:
            f.unlink()
            deleted.append(_rel(f))
        if with_history and trend_path(name).exists():
            deleted.append(_rel(trend_path(name)))
            trend_path(name).unlink()
        _LAST_ATTEMPT.pop(name, None)
        return {"name": name, "deleted": deleted, "history_deleted": len(hist)}
    finally:
        lock.release()


def _write(path: Path, plan: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(_ordered(plan), sort_keys=False, allow_unicode=True, width=200), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- 执行

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
#: 上次「尝试」运行的时刻（含失败、没存下结果的）。到点判断要算上它，不然一个跑不起来的计划会每分钟重试一次。
_LAST_ATTEMPT: dict[str, datetime] = {}


def _lock(name: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(name, threading.Lock())


def is_running(name: str) -> bool:
    return _lock(name).locked()


def last_run(name: str, runs_dir: Path | None = None) -> dict[str, Any] | None:
    """最近一次运行的结果。文件名以开始时间开头，倒序找第一份名字对得上的，不用全读。"""
    root = Path(runs_dir or RUNS_DIR)
    if not root.is_dir():
        return None
    for f in sorted(root.glob(f"*-{name}-*.json"), reverse=True):
        try:
            run = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(run, dict) and run.get("checklist") == name:
            return run
    return None


def history(name: str, last: int = 20, runs_dir: Path | None = None) -> list[dict[str, Any]]:
    return load_history(Path(runs_dir or RUNS_DIR), safe_name(name), last)


def _refresh_hosts(plan: dict[str, Any]) -> dict[str, Any]:
    """执行时按设备名从当前拓扑取地址（设备换了管理地址也能跟上）；拓扑读不到就用计划里存的。"""
    try:
        from netops_ai.topology import load_topology, resolve_device

        topo = load_topology()
    except Exception:  # noqa: BLE001 - 拓扑读不到不该让计划跑不了
        return plan
    out = json.loads(json.dumps(plan))
    for d in out.get("devices") or []:
        dev = resolve_device(topo, d.get("name", ""))
        if dev is not None and dev.host:
            d["host"] = dev.host
    return out


def run_plan(name: str, adapter_factory: Callable[[Any], Any] | None = None, *, runs_dir: Path | None = None,
             now: datetime | None = None) -> tuple[dict[str, Any], Path]:
    """跑一次并存结果。同一个计划正在跑就抛 `PlanBusy`（不排队、不重入）。"""
    lock = _lock(safe_name(name))
    if not lock.acquire(blocking=False):
        raise PlanBusy(f"plan {name!r} is already running")
    try:
        _LAST_ATTEMPT[name] = now or datetime.now(timezone.utc)
        plan = _refresh_hosts(load_plan(name))
        if adapter_factory is None:
            from netops_ai.topology import _default_adapter_factory as adapter_factory  # noqa: N813
        result = run_checklist(plan, adapter_factory)
        path = save_result(result, Path(runs_dir or RUNS_DIR))
        return result, path
    finally:
        lock.release()


def due_plans(now: datetime, runs_dir: Path | None = None) -> list[str]:
    """到点、已启用、当前没在跑的计划名。"""
    out = []
    for plan in list_plans():
        name = plan["name"]
        if not plan.get("enabled") or is_running(name):
            continue
        last = last_run(name, runs_dir)
        anchors = [t for t in (_parse_ts(last.get("started_at")) if last else None, _LAST_ATTEMPT.get(name)) if t is not None]
        nxt = next_run(plan, max(anchors) if anchors else None, now)
        if nxt is not None and nxt <= now:
            out.append(name)
    return out


def run_summary(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """一次运行的摘要（列表 / 状态点用）。"""
    if not result:
        return None
    s = result.get("summary") or {}
    return {"run_id": result.get("run_id"), "started_at": result.get("started_at"), "finished_at": result.get("finished_at"),
            "pass": s.get("pass", 0), "fail": s.get("fail", 0), "denied": s.get("denied", 0), "error": s.get("error", 0),
            "unreachable_devices": s.get("unreachable_devices", 0)}


def plan_view(plan: dict[str, Any], now: datetime | None = None, runs_dir: Path | None = None) -> dict[str, Any]:
    """计划列表的一行：计划本身 + 上次运行摘要 + 下次运行时间 + 是否正在跑。"""
    now = now or datetime.now(timezone.utc)
    last = last_run(plan["name"], runs_dir)
    anchors = [t for t in (_parse_ts(last.get("started_at")) if last else None, _LAST_ATTEMPT.get(plan["name"])) if t is not None]
    nxt = next_run(plan, max(anchors) if anchors else None, now)
    return {**plan, "device_count": len(plan.get("devices") or []), "check_count": len(plan.get("checks") or []),
            "last_run": run_summary(last), "next_run_at": nxt.isoformat(timespec="seconds") if nxt else None,
            "running": is_running(plan["name"])}

"""对话里的交互式「选择气泡」和「提示泡」：都是确定性的，不调模型。

- **选择气泡（widget）**：草案缺什么就给什么——缺设备给 device_picker（按核心 / 汇聚 / 接入分组的复选列表），
  缺周期给 schedule_picker（周期 chips），缺检查项给 check_picker（检查模板复选卡，按设备角色预勾选推荐项）。
  用户操作后把选择作为结构化消息回传，`apply_selection` 校验（设备必须在拓扑里、模板必须在模板库里、周期格式合法）
  后直接写进草案。每个气泡带一个 id，只有最新那个能提交，旧的一律拒收。
- **提示泡（hint）**：来自规则的一两句提醒（趋势至少 3 次运行、核心同时看 OSPF 和 BGP、计数类别短于 5 分钟、
  写命令不会被接受），每条每个对话只出现一次。

这里不 import 任何框架；编排在 `netops_ai/graph/plan_graph.py`。
"""

from __future__ import annotations

import uuid
from typing import Any

from netops_ai.inspection import plans as P

ROLE_ORDER = ("core", "aggregation", "access", "")
ROLE_LABEL = {"zh": {"core": "核心", "aggregation": "汇聚", "access": "接入", "": "其它"},
              "en": {"core": "Core", "aggregation": "Aggregation", "access": "Access", "": "Other"}}
SCHEDULE_OPTIONS = (15, 60, 360)
MAX_HINTS_PER_TURN = 2


def _zh(lang: str) -> bool:
    return lang != "en"


# --------------------------------------------------------------------------- 缺口 → 气泡

def next_gap(draft: dict[str, Any] | None) -> str | None:
    """草案先缺哪一项：devices → schedule → checks；都不缺返回 None。"""
    d = draft or {}
    if not d.get("devices"):
        return "devices"
    if not d.get("schedule"):
        return "schedule"
    if not d.get("checks"):
        return "checks"
    return None


WIDGET_FOR_GAP = {"devices": "device_picker", "schedule": "schedule_picker", "checks": "check_picker"}


def build_widget(kind: str, draft: dict[str, Any] | None, topology: dict[str, Any], lang: str = "zh") -> dict[str, Any]:
    """生成一个选择气泡。`kind`：device_picker / schedule_picker / check_picker。"""
    d = draft or P.empty_draft()
    zh = _zh(lang)
    wid = uuid.uuid4().hex[:12]
    if kind == "device_picker":
        selected = [x["name"] for x in d.get("devices") or []]
        groups = []
        for role in ROLE_ORDER:
            members = [{"name": dev.name, "role": getattr(dev, "role", "") or "", "host": dev.host,
                        "neighbors": len(getattr(dev, "links", ()) or ())}
                       for dev in topology.values() if (getattr(dev, "role", "") or "") == role]
            if members:
                groups.append({"role": role, "label": ROLE_LABEL["zh" if zh else "en"][role], "devices": members})
        return {"id": wid, "type": kind, "groups": groups, "selected": selected,
                "prompt": "先定一下设备范围：" if zh else "First, pick the devices:"}
    if kind == "schedule_picker":
        return {"id": wid, "type": kind, "options": list(SCHEDULE_OPTIONS), "daily": True, "selected": d.get("schedule") or {},
                "hint": schedule_hint(d, lang), "prompt": "多久跑一次？" if zh else "How often should it run?"}
    if kind == "check_picker":
        roles = {x.get("role", "") for x in d.get("devices") or []}
        recommended: list[str] = []
        for r in roles or {""}:
            for tid in P.ROLE_DEFAULTS.get(r if r in P.ROLE_DEFAULTS else "", []):
                if tid not in recommended:
                    recommended.append(tid)
        have = [c.get("template") for c in d.get("checks") or [] if c.get("template")]
        items = []
        for tid, t in P.TEMPLATES.items():
            cmd = t["check"]["command"]
            if t.get("per_uplink"):
                cmd = cmd.replace("{interface}", "<uplink>" if not zh else "<上联口>")
            items.append({"id": tid, "title": t["title"]["zh" if zh else "en"], "what": t["what"]["zh" if zh else "en"],
                          "command": cmd, "kind": t["kind"], "roles": t["roles"]})
        selected = [tid for tid in P.TEMPLATES if tid in have] or [tid for tid in P.TEMPLATES if tid in recommended]
        return {"id": wid, "type": kind, "items": items, "recommended": recommended, "selected": selected,
                "prompt": "要做哪些检查？已按设备角色勾好推荐项：" if zh else "Which checks? Recommended ones for these device roles are ticked:"}
    raise ValueError(f"unknown widget type {kind!r}")


def widget_for_gap(draft: dict[str, Any] | None, topology: dict[str, Any], lang: str = "zh") -> dict[str, Any] | None:
    gap = next_gap(draft)
    return build_widget(WIDGET_FOR_GAP[gap], draft, topology, lang) if gap else None


def schedule_hint(draft: dict[str, Any], lang: str = "zh") -> str:
    """周期 chips 旁边那句理由（沿用规则建议的说法）。"""
    zh = _zh(lang)
    counters = [c for c in draft.get("checks") or [] if P.TEMPLATES.get(c.get("template") or "", {}).get("kind") == "counter"]
    if counters:
        return ("计数类检查（接口错误）周期不要短于 5 分钟，否则两次之间的增量太小，趋势里只剩噪声。" if zh
                else "Counter checks (interface errors) should not run more often than every 5 minutes, or the trend is just noise.")
    return ("周期越短越早发现问题；趋势结论至少需要 3 次运行。" if zh
            else "A shorter schedule finds problems sooner; a trend needs at least 3 runs.")


# --------------------------------------------------------------------------- 回传 → 草案

def apply_selection(draft: dict[str, Any] | None, selection: dict[str, Any], topology: dict[str, Any],
                    lang: str = "zh") -> tuple[dict[str, Any], list[str], str]:
    """把一次选择写进草案。返回（新草案, 问题, 给用户看的摘要）。有问题时草案原样返回、什么都不改。"""
    from netops_ai.topology import resolve_device

    zh = _zh(lang)
    base = P.apply_patch(draft, None, topology, lang)  # 规整一份（补默认字段 / 名字）
    kind = selection.get("type")
    value = selection.get("value") or {}
    if kind == "device_picker":
        names = [str(x) for x in value.get("devices") or []]
        if not names:
            return base, ["devices: pick at least one device"], ""
        bad = [n for n in names if resolve_device(topology, n) is None]
        if bad:
            return base, [f"devices: {', '.join(map(repr, bad))} not in the topology"], ""
        wanted = list(dict.fromkeys(resolve_device(topology, n).name for n in names))
        gone = [x["name"] for x in base["devices"] if x["name"] not in wanted]
        new = P.apply_patch(base, {"remove_devices": gone, "add_devices": wanted}, topology, lang)
        new["devices"] = sorted(new["devices"], key=_device_key(topology))
        _rescope_templates(new)
        return new, [], devices_summary(new, lang)
    if kind == "schedule_picker":
        sched = P.normalize_schedule(value)
        problems = P.validate_schedule(sched)
        if problems:
            return base, problems, ""
        new = dict(base, schedule=sched)
        return new, [], (("已选择周期：" if zh else "Schedule: ") + schedule_text(sched, lang))
    if kind == "check_picker":
        tids = [str(x) for x in value.get("templates") or []]
        if not tids:
            return base, ["checks: pick at least one check"], ""
        bad = [t for t in tids if t not in P.TEMPLATES]
        if bad:
            return base, [f"checks: unknown template {', '.join(map(repr, bad))}"], ""
        new = dict(base, checks=[c for c in base["checks"] if not c.get("template")])  # 自定义命令保留
        for tid in P.TEMPLATES:  # 按模板库顺序加
            if tid not in tids:
                continue
            roles = P.TEMPLATES[tid]["roles"]
            scope = [x["name"] for x in new["devices"] if x.get("role") in roles] if roles else []
            if P.TEMPLATES[tid].get("per_uplink"):
                P.add_template_checks(new, tid, scope or None, topology, lang)
            else:
                P.add_template_checks(new, tid, scope or [x["name"] for x in new["devices"]], topology, lang)
        titles = [P.TEMPLATES[t]["title"]["zh" if zh else "en"] for t in P.TEMPLATES if t in tids]
        return new, [], (("已选择检查项：" if zh else "Checks: ") + ("、" if zh else ", ").join(titles))
    return base, [f"unknown selection type {kind!r}"], ""


def _device_key(topology: dict[str, Any]):
    """核心 → 汇聚 → 接入 → 其它，同层按拓扑里的顺序。"""
    order = {n: i for i, n in enumerate(topology)}
    rank = {r: i for i, r in enumerate(ROLE_ORDER)}
    return lambda x: (rank.get(x.get("role", ""), len(ROLE_ORDER)), order.get(x["name"], 999))


def _rescope_templates(draft: dict[str, Any]) -> None:
    """设备范围变了：模板检查按角色重新划范围（OSPF 只给核心 / 汇聚，新加进来的接入交换机不会被套上 OSPF）。
    没有匹配角色的设备时，保留原范围里还在的设备；一台都不剩就拿掉这条。自定义命令和上联口检查不动。"""
    names = [x["name"] for x in draft.get("devices") or []]
    kept = []
    for c in draft.get("checks") or []:
        t = P.TEMPLATES.get(c.get("template") or "")
        if not t or t.get("per_uplink") or not t["roles"]:
            kept.append(c)
            continue
        scope = [x["name"] for x in draft["devices"] if x.get("role") in t["roles"]]
        if not scope:
            scope = [n for n in (c.get("devices") or names) if n in names]
        if not scope:
            continue
        if set(scope) >= set(names):
            c.pop("devices", None)
        else:
            c["devices"] = scope
        kept.append(c)
    draft["checks"] = kept


def devices_summary(draft: dict[str, Any], lang: str = "zh") -> str:
    zh = _zh(lang)
    groups: dict[str, list[str]] = {}
    for x in draft.get("devices") or []:
        groups.setdefault(x.get("role", "") if x.get("role", "") in ROLE_ORDER else "", []).append(x["name"])
    parts = [f"{ROLE_LABEL['zh' if zh else 'en'][r]} {len(groups[r])}" for r in ROLE_ORDER if r in groups]
    return ("已选择：" if zh else "Selected: ") + ("、" if zh else ", ").join(parts)


def schedule_text(s: dict[str, Any], lang: str = "zh") -> str:
    zh = _zh(lang)
    n = s.get("every_minutes")
    if isinstance(n, int):
        if n == 60:
            return "每小时" if zh else "hourly"
        if n % 60 == 0:
            return f"每 {n // 60} 小时" if zh else f"every {n // 60} h"
        return f"每 {n} 分钟" if zh else f"every {n} min"
    return (f"每天 {s.get('daily_at')}" if zh else f"daily at {s.get('daily_at')}") if s.get("daily_at") else ("未定" if zh else "not set")


def next_prompt(draft: dict[str, Any], lang: str = "zh") -> str:
    zh = _zh(lang)
    gap = next_gap(draft)
    if gap == "devices":
        return "再选一下设备范围。" if zh else "Now pick the devices."
    if gap == "schedule":
        return "接下来选一下运行周期。" if zh else "Next, pick how often it runs."
    if gap == "checks":
        return "再选要做哪些检查。" if zh else "Then pick the checks."
    return "设备、周期、检查项都齐了，请在下方确认卡里逐项确认。" if zh else "Devices, schedule and checks are set — confirm them in the card below."


# --------------------------------------------------------------------------- 提示泡

def hints_for(draft: dict[str, Any] | None, lang: str = "zh", *, shown: list[str] | None = None,
              opening: bool = False) -> list[dict[str, str]]:
    """规则产出的提示，去掉这个对话里已经出现过的，最多 2 条。"""
    zh = _zh(lang)
    d = draft or {}
    shown_set = set(shown or [])
    cands: list[tuple[str, str]] = []
    if opening or d.get("checks"):
        cands.append(("readonly", "写命令不会被接受：所有命令都要过只读白名单，配置 / 清计数 / 重启类命令进不了计划。" if zh
                      else "Write commands are never accepted: every command goes through the read-only whitelist."))
    core = [x["name"] for x in d.get("devices") or [] if x.get("role") == "core"]
    covered = {t: {n for c in d.get("checks") or [] if c.get("template") == t
                   for n in (c.get("devices") if c.get("devices") is not None else [x["name"] for x in d.get("devices") or []])}
               for t in ("ospf_neighbors", "bgp_sessions")}
    if core and not all(n in covered["ospf_neighbors"] and n in covered["bgp_sessions"] for n in core):  # 已经都看了就不提
        cands.append(("core-ospf-bgp", "核心设备建议同时看 OSPF 邻居和 BGP 会话。" if zh
                      else "For core devices, watch both OSPF neighbors and BGP sessions."))
    counters = [c for c in d.get("checks") or [] if P.TEMPLATES.get(c.get("template") or "", {}).get("kind") == "counter"]
    if counters:
        cands.append(("counter-interval", "计数类检查周期不要短于 5 分钟，否则看不出趋势。" if zh
                      else "Counter checks should not run more often than every 5 minutes, or no trend is visible."))
    if d.get("schedule") and d.get("checks"):
        cands.append(("trend-runs", "趋势结论至少需要 3 次运行。" if zh else "A trend needs at least 3 runs."))
    out = [{"id": i, "text": t} for i, t in cands if i not in shown_set]
    return out[:MAX_HINTS_PER_TURN]

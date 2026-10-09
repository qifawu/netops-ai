"""对话式制定巡检计划的各个步骤：每一轮把「拓扑设备 + 命令目录 + 检查模板库 + 当前草案」交给模型，
模型用结构化输出返回（给人看的回复、完整的新草案、建议、是否可确认）。

**流程本身是一张图**，编排在 `netops_ai/graph/plan_graph.py`（LangGraph StateGraph：收集上下文 → 提案 → 校验
→ 修正环 / 放弃 / 降级 → 回复 → 人工确认中断 → 保存）。这里只放图里各节点调用的纯函数，不 import 任何框架。

几条硬规矩（不靠提示词，靠代码）：

- **草案每轮都过 `check_draft`**（格式 + 只读白名单）。不过就带着问题原文让模型改，**最多改 2 次**；
  还不过就把问题原样告诉用户，`ready` 强制为 False。被白名单拒的检查项从草案里拿掉——写命令永远进不了草案。
- **模型只给设备名**，地址由我们从拓扑里补；发给模型的设备信息只有名字、角色、邻居数，没有地址，更没有凭据。
- **模板优先**：模型说「用 ospf_neighbors 模板」，正则由模板库给，不让模型现编。
- **模型不可用就降级**：没配模型 / 调用失败 / 返回的不是合法 JSON → 规则推荐（按角色挑模板 + 默认周期），
  回复里明确说「模型不可用，以下是规则推荐」。开场白不调模型。
"""

from __future__ import annotations

import json
import re
from typing import Any

from netops_ai.inspection import plans as P
from netops_ai.inspection.checklist import EXPECT_TYPES, EXTRACT_AGGS, SEVERITIES

#: 校验不过时最多让模型修正几次
MAX_FIX_ROUNDS = 2
#: 带给模型的历史消息条数上限（草案本身每轮都完整带上，老消息丢了不丢信息）
MAX_HISTORY = 16
MAX_SUGGESTIONS = 5

ROLE_LABEL = {"zh": {"core": "核心", "aggregation": "汇聚", "access": "接入", "": "未标角色"},
              "en": {"core": "core", "aggregation": "aggregation", "access": "access", "": "no role"}}


def _str_list() -> dict:
    return {"type": "array", "items": {"type": "string"}}


def plan_json_schema() -> dict:
    """strict structured output。所有字段都必填（strict 的要求），「不用」就填空串 / 0 / 空数组。"""
    check = {
        "type": "object", "additionalProperties": False,
        "required": ["template", "id", "title", "command", "devices", "expect", "extract"],
        "properties": {
            "template": {"type": "string", "description": "Template id from the template library, or \"\" for a custom check."},
            "id": {"type": "string", "description": "Custom checks only: English identifier, letters/digits/_.-; \"\" when template is set."},
            "title": {"type": "string", "description": "Custom checks only: short human title; \"\" when template is set."},
            "command": {"type": "string", "description": "Custom checks only: one full read-only show command; \"\" when template is set."},
            "devices": {**_str_list(), "description": "Subset of the plan devices this check runs on; [] = all plan devices."},
            "expect": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["type", "value", "severity"],
                "properties": {"type": {"type": "string", "enum": list(EXPECT_TYPES)}, "value": {"type": "string"},
                               "severity": {"type": "string", "enum": list(SEVERITIES)}}}},
            "extract": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["name", "regex", "cast", "agg"],
                "properties": {"name": {"type": "string"}, "regex": {"type": "string", "description": "One capture group."},
                               "cast": {"type": "string", "enum": ["int", "float"]},
                               "agg": {"type": "string", "enum": list(EXTRACT_AGGS)}}}},
        },
    }
    suggestion = {
        "type": "object", "additionalProperties": False,
        "required": ["id", "text", "reason", "add_devices", "add_checks", "every_minutes", "daily_at"],
        "properties": {
            "id": {"type": "string", "description": "Short English id, e.g. bgp-core."},
            "text": {"type": "string", "description": "The suggestion, one sentence, in the user's language."},
            "reason": {"type": "string", "description": "Why, one sentence, in the user's language."},
            "add_devices": _str_list(),
            "add_checks": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["template", "devices"],
                "properties": {"template": {"type": "string"}, "devices": _str_list()}}},
            "every_minutes": {"type": "integer", "description": "0 = no schedule change."},
            "daily_at": {"type": "string", "description": "\"\" = no schedule change."},
        },
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "inspection_plan_turn",
            "strict": True,
            "schema": {
                "type": "object", "additionalProperties": False,
                "required": ["reply", "plan_name", "description", "devices", "checks", "schedule", "enabled", "suggestions", "ready",
                             "quick_replies"],
                "properties": {
                    "reply": {"type": "string"},
                    "plan_name": {"type": "string", "description": "English identifier, letters/digits/_.-, e.g. core-ospf-errors."},
                    "description": {"type": "string", "description": "One sentence: what this plan watches, in the user's language."},
                    "devices": {**_str_list(), "description": "Device names from the topology."},
                    "checks": {"type": "array", "items": check},
                    "schedule": {"type": "object", "additionalProperties": False, "required": ["every_minutes", "daily_at"],
                                 "properties": {"every_minutes": {"type": "integer"}, "daily_at": {"type": "string"}}},
                    "suggestions": {"type": "array", "items": suggestion},
                    "enabled": {"type": "boolean", "description": "false only when the user asked to pause the plan."},
                    "ready": {"type": "boolean"},
                    "quick_replies": _str_list(),
                },
            },
        },
    }


SYSTEM_PROMPT = """You help a network operator define a scheduled, READ-ONLY inspection plan through conversation.

Language: write `reply`, `description`, suggestion `text`/`reason` and `quick_replies` in {language}. Plan names, check ids and template ids are English identifiers.

How to talk:
- Keep `reply` short (at most 4 sentences). Ask at most 1-2 of the most important open questions per turn — e.g. which faults matter most, which devices are the focus, how often to run, how much alert noise is acceptable. Do not ask about things already decided.
- Every turn return the COMPLETE updated draft (devices, checks, schedule), not a diff. Keep everything the user did not ask to change.

Rules for the draft:
- devices: names from `topology_devices` only.
- checks: prefer templates — set `template` to a template id and leave id/title/command/expect/extract empty. Write a custom check (template "") only when no template fits: one full read-only `show ...` command (no abbreviations, no placeholders), preferably from `command_catalog`, expect/extract regexes that match real Cisco IOS output. Never propose configuration, clear, reload, debug, copy or any command that changes the device: a whitelist rejects them and they never reach the device.
- A check's `devices` = [] means every plan device; otherwise list the subset (e.g. BGP only on core).
- schedule: `every_minutes` (integer) or `daily_at` "HH:MM" (server local time); set the unused one to 0 / "". If the user has not said, set both to 0 / "" and ask.
- ready = true only when devices, checks and schedule are all decided and nothing important is still open.
- enabled: true, unless the user asked to pause the plan (then false).
- If `editing_plan` is set, the user is adjusting that existing plan: keep its plan_name, start from `current_draft` and change only what the user asks (add/remove checks or commands, other devices, schedule, pause).
- If `command_candidates` is present, those commands were found in the documentation library / command catalog for what the user described. Do NOT add them to the draft yourself: the user picks them from a list shown under your reply. Say in one sentence that candidates are listed below; never present them as device evidence.

Suggestions (be proactive, 0-3 per turn, never repeat one that is already in the draft): each is one concrete change with a one-sentence reason, expressed as a patch (add_devices, add_checks with template ids, every_minutes / daily_at; unused fields empty / 0). Good advice looks like: core devices → OSPF neighbor and BGP checks; counter checks no more often than every 5 minutes, or the trend is just noise; a trend needs at least 3 runs; access switches → watch uplink error counters.

quick_replies: 0-3 short answers the user is likely to click, in {language}.

Context (JSON):
{context}"""


def _catalog_commands(vendor: str = "cisco") -> list[dict[str, str]]:
    """命令目录（playbooks/catalog）里能直接用在巡检上的只读命令；带占位符的注明要填真实接口名。只给白名单放行的。"""
    from netops_ai.devices.whitelist import check as wl

    try:
        from netops_ai.playbooks.catalog import load_catalog

        intents = load_catalog().get({"cisco": "cisco_ios"}.get(vendor, vendor), {})
    except Exception:  # noqa: BLE001 - 目录读不到就只靠模板库
        intents = {}
    out = []
    for intent, d in intents.items():
        cmd = str(d.get("command") or "")
        if not cmd or "{alert_log_prefix}" in cmd:
            continue
        sample = cmd.replace("{interface}", "GigabitEthernet0/1").replace("{interface_short}", "Gi0/1")
        if not wl(vendor, sample).allowed:
            continue
        item = {"intent": intent, "command": cmd, "note": str(d.get("note") or "")}
        if "{" in cmd:
            item["note"] += " (replace {interface} with a real interface name)"
        out.append(item)
    return out


def list_plans_turn(plans: list[dict[str, Any]], lang: str, *, not_found: str = "") -> dict[str, Any]:
    """「调整现有计划」但还没选是哪个：列出已有计划让用户挑。"""
    zh = lang != "en"
    if not plans:
        reply = "还没有已保存的巡检计划，可以先新建一个。" if zh else "There are no saved inspection plans yet; create one first."
        return {"reply": reply, "draft": P.empty_draft(), "validation": P.check_draft(None), "ready": False, "suggestions": [],
                "quick_replies": ["新建计划" if zh else "New plan"], "source": "plans", "fix_rounds": 0, "plans": []}
    lines = []
    for b in plans:
        last = b.get("last_run")
        res = ((f"上次 通过 {last['pass']} / 未通过 {last['fail']}" if zh else f"last: {last['pass']} pass / {last['fail']} fail")
               if last else ("未运行" if zh else "never run"))
        state = ("启用" if b["enabled"] else "已暂停") if zh else ("enabled" if b["enabled"] else "paused")
        lines.append(f"- {b['name']}：{', '.join(b['devices'])}；{len(b['checks'])} 项检查；{_schedule_text(b['schedule'], zh)}；{state}；{res}"
                     if zh else f"- {b['name']}: {', '.join(b['devices'])}; {len(b['checks'])} checks; {_schedule_text(b['schedule'], zh)}; {state}; {res}")
    head = (f"没找到叫「{not_found}」的计划。" if zh else f"No plan named \"{not_found}\". ") if not_found else ""
    reply = head + ("现有这些巡检计划，要调整哪一个？\n" if zh else "These are the existing plans — which one do you want to adjust?\n") + "\n".join(lines)
    return {"reply": reply, "draft": P.empty_draft(), "validation": P.check_draft(None), "ready": False, "suggestions": [],
            "quick_replies": [b["name"] for b in plans[:4]], "source": "plans", "fix_rounds": 0, "plans": plans}


def loaded_plan_turn(draft: dict[str, Any], topology: dict[str, Any], lang: str) -> dict[str, Any]:
    """已加载一个现有计划：说清楚它现在是什么样，问要改什么。"""
    zh = lang != "en"
    names = ", ".join(d["name"] for d in draft.get("devices") or [])
    state = ("启用中" if draft.get("enabled", True) else "已暂停") if zh else ("enabled" if draft.get("enabled", True) else "paused")
    if zh:
        reply = (f"已加载计划 {draft['name']}（{state}）：设备 {names}；{len(draft.get('checks') or [])} 项检查；"
                 f"{_schedule_text(draft.get('schedule') or {}, True)}。\n想改什么？可以增删检查或命令、换设备、改周期，或者暂停它。")
        quick = ["加一条检查", "换几台设备", "改成每 15 分钟", "暂停这个计划"]
    else:
        reply = (f"Loaded plan {draft['name']} ({state}): devices {names}; {len(draft.get('checks') or [])} checks; "
                 f"{_schedule_text(draft.get('schedule') or {}, False)}.\nWhat would you like to change? Add or remove checks or commands, other devices, the schedule, or pause it.")
        quick = ["Add a check", "Change devices", "Every 15 minutes", "Pause this plan"]
    v = P.check_draft(draft)
    return {"reply": reply, "draft": draft, "validation": v, "ready": False, "quick_replies": quick, "source": "plans",
            "suggestions": merge_suggestions([], draft, topology, lang), "fix_rounds": 0}


def _compact_draft(draft: dict[str, Any] | None) -> dict[str, Any]:
    d = draft or {}
    checks = []
    for c in d.get("checks") or []:
        row = {"id": c.get("id"), "template": c.get("template", "")}
        if not c.get("template"):
            row.update(command=c.get("command"), expect=c.get("expect") or [], extract=c.get("extract") or [])
        if c.get("devices") is not None:
            row["devices"] = c["devices"]
        checks.append(row)
    return {"name": d.get("name", ""), "devices": [x.get("name") for x in d.get("devices") or []], "checks": checks,
            "schedule": d.get("schedule") or {}, "enabled": d.get("enabled", True)}


def build_messages(messages: list[dict[str, str]], draft: dict[str, Any] | None, topology: dict[str, Any], lang: str, *,
                   editing: str = "", candidates: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    context: dict[str, Any] = {
        "topology_devices": P.topology_devices(topology),
        "templates": P.template_catalog(lang),
        "command_catalog": _catalog_commands(),
        "current_draft": _compact_draft(draft),
    }
    if editing:
        context["editing_plan"] = editing
    if candidates:
        context["command_candidates"] = [{"command": c["command"], "purpose": c.get("purpose", ""), "usable": c.get("allowed", False)}
                                         for c in candidates]
    system = SYSTEM_PROMPT.replace("{language}", "English" if lang == "en" else "Simplified Chinese").replace(
        "{context}", json.dumps(context, ensure_ascii=False))
    return [{"role": "system", "content": system}, *messages[-MAX_HISTORY:]]


def _clean_messages(messages: Any) -> list[dict[str, str]]:
    out = []
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"].strip():
            out.append({"role": m["role"], "content": m["content"][:4000]})
    return out


def _parse_json(resp: Any) -> dict[str, Any] | None:
    if isinstance(getattr(resp, "parsed", None), dict):
        return resp.parsed
    text = getattr(resp, "content", "") or ""
    m = re.search(r"\{.*\}", text, re.S)  # 有的模型会包一层 ```json
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- 模型输出 → 草案

def draft_from_model(out: dict[str, Any], prev: dict[str, Any] | None, topology: dict[str, Any], lang: str) -> tuple[dict[str, Any], list[str]]:
    """模型的结构化输出 → 草案（模板展开、补地址）。返回（草案, 转换时就发现的问题）。"""
    from netops_ai.topology import resolve_device

    prev = prev or {}
    draft = P.empty_draft()
    draft["name"] = P.slug(out.get("plan_name"), prev.get("name") or P.default_name())
    if str(out.get("description") or "").strip():
        draft["description"] = str(out["description"]).strip()[:300]
    problems: list[str] = []
    unknown = P.add_devices(draft, [str(x) for x in out.get("devices") or []], topology)
    problems += [f"devices: {x!r} is not a device in the topology" for x in unknown]
    for i, c in enumerate(out.get("checks") or []):
        if not isinstance(c, dict):
            continue
        scope = [str(x) for x in c.get("devices") or []] or None
        if scope:
            bad = [s for s in scope if resolve_device(topology, s) is None]
            if bad:
                problems.append(f"checks[{i}].devices: {', '.join(map(repr, bad))} not in the topology")
                scope = [s for s in scope if s not in bad] or None
        tid = str(c.get("template") or "").strip()
        if tid:
            if tid not in P.TEMPLATES:
                problems.append(f"checks[{i}].template: unknown template {tid!r} (known: {', '.join(P.TEMPLATES)}); "
                                "use one of them or leave template empty and write a custom check")
                continue
            P.add_template_checks(draft, tid, scope, topology, lang)
            continue
        cid = P.slug(c.get("id"), f"custom-{i + 1}")
        while any(x.get("id") == cid for x in draft["checks"]):
            cid += "-2"
        item: dict[str, Any] = {"id": cid, "title": str(c.get("title") or cid)[:80], "command": str(c.get("command") or "").strip()}
        expect = [{"type": e.get("type"), "value": e.get("value"), "severity": e.get("severity", "warning")}
                  for e in c.get("expect") or [] if isinstance(e, dict)]
        extract = [{"name": x.get("name"), "regex": x.get("regex"), "cast": x.get("cast", "float"), "agg": x.get("agg", "first")}
                   for x in c.get("extract") or [] if isinstance(x, dict)]
        if expect:
            item["expect"] = expect
        if extract:
            item["extract"] = extract
        if scope:
            P.add_devices(draft, scope, topology)
            item["devices"] = [d.name for s in scope if (d := resolve_device(topology, s)) is not None]
        draft["checks"].append(item)
    sched = out.get("schedule") or {}
    draft["schedule"] = P.normalize_schedule(sched) or P.normalize_schedule(prev.get("schedule") or {})
    if out.get("enabled") is False or (out.get("enabled") is None and prev.get("enabled") is False):
        draft["enabled"] = False
    elif "enabled" in prev or "enabled" in out:
        draft["enabled"] = True
    return draft, problems


def _patch_from_model(s: dict[str, Any]) -> dict[str, Any] | None:
    patch: dict[str, Any] = {}
    if s.get("add_devices"):
        patch["add_devices"] = [str(x) for x in s["add_devices"]]
    checks = [{"template": str(c.get("template")), "devices": [str(x) for x in c.get("devices") or []] or None}
              for c in s.get("add_checks") or [] if isinstance(c, dict) and c.get("template") in P.TEMPLATES]
    if checks:
        patch["add_checks"] = checks
    sched = P.normalize_schedule({"every_minutes": s.get("every_minutes") or 0, "daily_at": s.get("daily_at") or ""})
    if sched and not P.validate_schedule(sched):
        patch["schedule"] = sched
    return patch or None


def _essence(d: dict[str, Any]) -> str:
    return json.dumps({"devices": [x.get("name") for x in d.get("devices") or []],
                       "checks": [[c.get("id"), c.get("devices")] for c in d.get("checks") or []],
                       "schedule": d.get("schedule") or {}}, sort_keys=True)


def merge_suggestions(model_items: list[dict[str, Any]], draft: dict[str, Any], topology: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    """模型给的建议 + 规则建议，去重；有 patch 但并进去草案不变的（已经采纳过）丢掉。"""
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_effect: set[str] = set()  # 按「并进去之后草案变成什么样」去重：模型和规则常给出写法不同、效果一样的建议
    base = _essence(draft)
    cands = [{"id": P.slug(s.get("id"), f"s{i}"), "text": str(s.get("text") or "").strip(), "reason": str(s.get("reason") or "").strip(),
              "patch": _patch_from_model(s), "source": "llm"} for i, s in enumerate(model_items or []) if isinstance(s, dict)]
    # 规则里没有 patch 的纯提醒（「趋势至少 3 次运行」）改由提示泡承担，建议卡里不再重复
    cands += [{**s, "source": "rules"} for s in P.rule_suggestions(draft, topology, lang) if s.get("patch")]
    for s in cands:
        if not s["text"] or s["id"] in seen_ids:
            continue
        if s["patch"] is not None:
            try:
                effect = _essence(P.apply_patch(draft, s["patch"], topology, lang))
            except P.PlanError:
                continue
            if effect == base or effect in seen_effect:
                continue
            seen_effect.add(effect)
        seen_ids.add(s["id"])
        out.append(s)
    return out[:MAX_SUGGESTIONS]


def live_suggestions(items: list[dict[str, Any]], draft: dict[str, Any], topology: dict[str, Any], lang: str) -> list[str]:
    """页面上还挂着的建议里，哪些并进去仍会改变草案（别的建议采纳后，效果相同的那条就没用了）。"""
    base = _essence(draft)
    out = []
    for s in items or []:
        if not isinstance(s, dict) or not s.get("patch"):
            continue
        try:
            if _essence(P.apply_patch(draft, s["patch"], topology, lang)) != base:
                out.append(str(s.get("id")))
        except P.PlanError:
            continue
    return out


# --------------------------------------------------------------------------- 一轮对话

def opening(topology: dict[str, Any], draft: dict[str, Any] | None, lang: str) -> dict[str, Any]:
    """开场白：不调模型，告诉用户拓扑里有什么、先问最关键的一个问题，附 3 个快捷回复。"""
    zh = lang != "en"
    groups: dict[str, list[str]] = {}
    for d in topology.values():
        groups.setdefault(getattr(d, "role", "") or "", []).append(d.name)
    labels = ROLE_LABEL["zh" if zh else "en"]
    parts = [(f"{labels.get(r, r)} {len(n)} 台（{'、'.join(n)}）" if zh else f"{len(n)} {labels.get(r, r)} ({', '.join(n)})")
             for r, n in sorted(groups.items(), key=lambda kv: ("core", "aggregation", "access", "").index(kv[0]) if kv[0] in ("core", "aggregation", "access", "") else 9)]
    if zh:
        reply = (f"你好，我来帮你制定一个只读巡检计划。拓扑里有 {len(topology)} 台设备：" + "，".join(parts) + "。"
                 "\n可以先在下面勾选设备范围，也可以直接说说最担心哪类问题，我会给出推荐的检查项和周期。")
        quick = ["核心设备的邻居和路由", "接口错误增长", "CPU 和内存"]
    else:
        reply = (f"Hi — let's set up a read-only inspection plan. The topology has {len(topology)} devices: " + ", ".join(parts) + "."
                 "\nPick the devices below, or just tell me what worries you most and I'll propose checks and a schedule.")
        quick = ["Neighbors and routing on the core", "Growing interface errors", "CPU and memory"]
    d = draft if draft and draft.get("devices") else P.empty_draft()
    d.setdefault("name", "")
    if not d["name"]:
        d["name"] = P.default_name()
    v = P.check_draft(d)
    return {"reply": reply, "draft": d, "validation": v, "ready": False, "quick_replies": quick,
            "suggestions": merge_suggestions([], d, topology, lang), "source": "opening", "fix_rounds": 0}


_ROLE_WORDS = {"core": r"核心|core", "aggregation": r"汇聚|aggregation|distribution", "access": r"接入|access"}
_TEMPLATE_WORDS = {
    "ospf_neighbors": r"ospf|邻居|neighbou?r|路由|routing",
    "bgp_sessions": r"bgp|路由|routing",
    "interface_errors": r"错误|error|crc|丢包",
    "uplink_errors": r"上联|uplink",
    "interfaces": r"接口状态|接口\s*up|接口\s*down|interface status|link down|端口",
    "cpu": r"cpu",
    "memory": r"内存|memory",
}


def parse_schedule_text(text: str) -> dict[str, Any] | None:
    """从一句话里认周期：每小时 / 每 N 分钟 / 每 N 小时 / 每天 HH:MM（中英文）。认不出返回 None。"""
    t = text.lower()
    m = re.search(r"每天\s*(\d{1,2})\s*[:：点]\s*(\d{1,2})?|daily at (\d{1,2}):(\d{2})|every day at (\d{1,2}):(\d{2})", t)
    if m:
        g = [x for x in m.groups() if x is not None]
        return {"daily_at": f"{int(g[0]):02d}:{int(g[1]) if len(g) > 1 else 0:02d}"}
    m = re.search(r"每\s*(\d+)\s*分钟|every (\d+) ?min", t)
    if m:
        return {"every_minutes": int(next(x for x in m.groups() if x))}
    m = re.search(r"每\s*(\d+)\s*(?:个)?小时|every (\d+) ?hours?", t)
    if m:
        return {"every_minutes": 60 * int(next(x for x in m.groups() if x))}
    if re.search(r"每小时|每个小时|hourly|every hour", t):
        return {"every_minutes": 60}
    if re.search(r"每天|daily|every day", t):
        return {"daily_at": "08:00"}
    return None


def rule_turn(messages: list[dict[str, str]], draft: dict[str, Any] | None, topology: dict[str, Any], lang: str, reason: str) -> dict[str, Any]:
    """模型不可用时的规则版：从用户最后一句话里认角色 / 设备 / 关心的检查 / 周期，按角色推荐模板。"""
    zh = lang != "en"
    text = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    low = text.lower()
    roles = [r for r, rx in _ROLE_WORDS.items() if re.search(rx, low)]
    names = [d.name for d in topology.values() if re.search(rf"(?<![A-Za-z0-9]){re.escape(d.name.lower())}(?![A-Za-z0-9])", low)]
    templates = [tid for tid, rx in _TEMPLATE_WORDS.items() if re.search(rx, low)]
    sched = parse_schedule_text(text)
    base = draft if draft and draft.get("devices") else None
    if base is None and not roles and not names:
        roles = ["core", "aggregation"]
    new_dev = bool(roles or names)
    d = P.recommended_draft(topology, roles, devices=names, templates=templates or (None if base is None or new_dev else []),
                            schedule=sched, lang=lang, base=base)
    clean, removed = P.sanitize(d)
    v = P.check_draft(clean)
    sched_txt = _schedule_text(clean.get("schedule") or {}, zh)
    if zh:
        reply = (f"模型不可用，以下是规则推荐（{reason}）。\n草案：{len(clean['devices'])} 台设备、{len(clean['checks'])} 项检查，{sched_txt}。"
                 "可以在右侧直接改周期；确认无误后点「确认并启用」。")
    else:
        reply = (f"The model is unavailable; below is a rule-based recommendation ({reason}).\nDraft: {len(clean['devices'])} devices, "
                 f"{len(clean['checks'])} checks, {sched_txt}. Adjust the schedule on the right, then click \"Confirm and enable\".")
    return {"reply": reply, "draft": clean, "validation": v, "ready": v["ok"], "source": "rules", "fix_rounds": 0,
            "suggestions": merge_suggestions([], clean, topology, lang),
            "quick_replies": (["加上接入层", "改成每 15 分钟", "只看核心"] if zh else ["Add the access layer", "Every 15 minutes", "Core only"]),
            "removed": removed}


def _schedule_text(s: dict[str, Any], zh: bool) -> str:
    if isinstance(s.get("every_minutes"), int):
        n = s["every_minutes"]
        if n % 60 == 0:
            return (f"每 {n // 60} 小时一次" if zh else f"every {n // 60} h") if n != 60 else ("每小时一次" if zh else "hourly")
        return f"每 {n} 分钟一次" if zh else f"every {n} min"
    if s.get("daily_at"):
        return f"每天 {s['daily_at']}" if zh else f"daily at {s['daily_at']}"
    return "周期未定" if zh else "no schedule yet"


def _fix_message(problems: list[str]) -> str:
    return ("Your draft did not pass validation. Problems:\n- " + "\n- ".join(problems) +
            "\nReturn the complete JSON again with these fixed. Only read-only `show` commands (full words, no abbreviations); "
            "prefer templates; device names only from topology_devices. Keep everything else unchanged.")


class ModelUnavailable(RuntimeError):
    """模型这一侧的任何失败（没配、连不上、不支持结构化输出、返回的不是 JSON）。图里据此走规则降级。"""


def has_user_message(messages: list[dict[str, str]]) -> bool:
    return any(m.get("role") == "user" for m in messages)


def call_model(client: Any, convo: list[dict[str, str]]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """调一次模型拿结构化输出。返回（解析好的输出, 实际用的消息列表——退到 JSON 模式时系统提示会多一段 schema）。
    任何失败都抛 `ModelUnavailable`，调用方不用认识各家 SDK 的异常。"""
    from netops_ai.llm.client import LLMError

    if client is None:
        raise ModelUnavailable("no LLM client")
    fmt: dict[str, Any] = plan_json_schema()
    convo = list(convo)
    try:
        try:
            resp = client.complete(convo, response_format=fmt, temperature=0.2, max_completion_tokens=4000)
        except LLMError as exc:
            if getattr(exc, "status_code", None) != 400:
                raise
            # 不支持 strict json_schema 的服务退到 JSON 模式，字段约定写进系统提示
            schema_text = json.dumps(fmt["json_schema"]["schema"], ensure_ascii=False)
            convo[0] = {**convo[0], "content": convo[0]["content"] + "\n\nOutput JSON schema:\n" + schema_text}
            resp = client.complete(convo, response_format={"type": "json_object"}, temperature=0.2, max_completion_tokens=4000)
    except Exception as exc:  # noqa: BLE001 - 模型侧失败统一成一种，图里走降级
        raise ModelUnavailable((str(exc).splitlines()[0] if str(exc) else type(exc).__name__)[:160]) from exc
    out = _parse_json(resp)
    if out is None:
        raise ModelUnavailable("the model did not return valid JSON")
    return out, convo


#: 用户这句话有没有要「拿掉 / 换掉」东西。没有的话，模型新给的草案里少了的检查项和设备要补回来——
#: 实测模型重写整份草案时会顺手丢掉用户刚「采纳」或「选用」进来的检查项（它没记住），用户并没让删。
REMOVAL_RX = re.compile(r"去掉|删|移除|不要(?!改|动)|不用(?!改|动)|取消|换掉|换成|换几|只看|只查|只留|只保留|仅|"
                        r"\b(?:remove|drop|delete|without|only|instead|replace|exclude)\b", re.IGNORECASE)


def keep_unrequested_removals(cand: dict[str, Any], prev: dict[str, Any] | None, user_text: str) -> list[str]:
    """用户没要求删，就把模型丢掉的设备 / 检查项补回草案。返回补回了什么（给调用方记录）。"""
    if not prev or REMOVAL_RX.search(user_text or ""):
        return []
    restored: list[str] = []
    have_dev = {d["name"] for d in cand.get("devices") or []}
    for d in prev.get("devices") or []:
        if d.get("name") and d["name"] not in have_dev:
            cand.setdefault("devices", []).append(dict(d))
            have_dev.add(d["name"])
            restored.append(f"device {d['name']}")
    have_chk = {c.get("id") for c in cand.get("checks") or []}
    have_cmd = {c.get("command") for c in cand.get("checks") or []}
    for c in prev.get("checks") or []:
        if c.get("id") not in have_chk and c.get("command") not in have_cmd:
            cand.setdefault("checks", []).append(json.loads(json.dumps(c)))
            restored.append(f"check {c.get('id')}")
    return restored


def evaluate(out: dict[str, Any], prev: dict[str, Any] | None, topology: dict[str, Any], lang: str,
             user_text: str = "") -> tuple[dict[str, Any], list[str]]:
    """模型输出 → 候选草案 + 硬错误（转换时发现的 + `check_draft` 的，含只读白名单）。用户没要求删的东西不许模型删。"""
    cand, problems = draft_from_model(out, prev, topology, lang)
    keep_unrequested_removals(cand, prev, user_text)
    return cand, problems + P.check_draft(cand)["problems"]


def repair_messages(convo: list[dict[str, str]], out: dict[str, Any], problems: list[str]) -> list[dict[str, str]]:
    """修正回合：把模型上一版输出和问题原文接在对话后面，让它整份重给。"""
    return [*convo, {"role": "assistant", "content": json.dumps(out, ensure_ascii=False)},
            {"role": "user", "content": _fix_message(problems)}]


def finish_turn(out: dict[str, Any], candidate: dict[str, Any], problems: list[str], repairs: int,
                topology: dict[str, Any], lang: str) -> dict[str, Any]:
    """收尾：拿掉坏检查项（写命令永远不留在草案里）、合并建议、算 ready。`problems` 非空 = 修正用尽仍不过 → 不放行。"""
    zh = lang != "en"
    clean, removed = P.sanitize(candidate)
    v = P.check_draft(clean)
    validation = {"ok": not problems and v["ok"], "problems": problems + [p for p in v["problems"] if p not in problems],
                  "missing": v["missing"]}
    reply = str(out.get("reply") or "").strip()
    if problems:
        head = (f"草案没有通过校验（已让模型自动修正 {repairs} 次，仍有问题），这一版不能确认：" if zh
                else f"The draft still fails validation after {repairs} automatic correction(s), so it cannot be confirmed:")
        tail = ("被只读白名单拒绝或格式有误的检查项已从草案移除，不会被保存。" if zh
                else "Checks refused by the read-only whitelist or malformed were removed from the draft and will not be saved.")
        reply = (reply + "\n\n" if reply else "") + head + "\n- " + "\n- ".join(problems) + ("\n" + tail if removed else "")
    return {
        "reply": reply,
        "draft": clean,
        "validation": validation,
        "ready": bool(out.get("ready")) and validation["ok"],
        "suggestions": merge_suggestions(out.get("suggestions") or [], clean, topology, lang),
        "quick_replies": [str(x)[:40] for x in out.get("quick_replies") or []][:3],
        "source": "llm",
        "fix_rounds": repairs,
        "removed": removed,
    }

"""制定巡检计划时这一个对话里用户的「偏好记忆」：关注点、明确删掉的检查项、点过「不需要」的建议、刚亲手选进来的东西。

存在草案里（`draft["prefs"]`），跟着草案在前后端之间来回——每一轮、每个按钮（采纳 / × / 改命令 / 不需要 / 气泡）
都带着它，所以换哪条路径（模型、规则降级、选择气泡）都看得到同一份。保存计划时去掉（`strip`），不进 YAML。

    focus         用户点名要看的检查模板（「路由表明细对比和条目总数」→ route_table_diff、route_count）
    focus_topics  用户说到的大类（route / protocol / interface / errors / resource）；「我需要的是 X」「只要 X」会整个替换
    removed       用户明确删掉的检查项（× 按钮、气泡里取消勾选、「不要 X」）：{id, template, command}。
                  之后任何一轮（模型重写、规则降级、气泡、规则建议）都不许再加回来，除非用户又明确要了
    dismissed     点过「不需要」的建议的语义键（建议 id、它要加的模板、改周期）——等价的建议也不再出现
    picked        上一轮之后用户亲手采纳 / 勾选进来的检查项 id（和设备）；模型这一轮无故丢了它们才补回，补完就清空

全是纯函数，不 import 框架。
"""

from __future__ import annotations

import json
import re
from typing import Any

from netops_ai.inspection import plans as P

#: 检查模板按「关注的大类」分组：用户说了关注路由，就只围绕路由给建议、预勾选
TOPICS: dict[str, tuple[str, ...]] = {
    "route": ("route_table_diff", "route_count"),
    "protocol": ("ospf_neighbors", "bgp_sessions"),
    "interface": ("interfaces",),
    "errors": ("interface_errors", "uplink_errors"),
    "resource": ("cpu", "memory"),
}
TOPIC_OF = {tid: topic for topic, tids in TOPICS.items() for tid in tids}
TOPIC_LABEL = {"zh": {"route": "路由相关", "protocol": "路由协议", "interface": "接口", "errors": "错误计数", "resource": "CPU / 内存"},
               "en": {"route": "routing table", "protocol": "routing protocols", "interface": "interfaces", "errors": "error counters",
                      "resource": "CPU / memory"}}

#: 点名某个模板（比「路由」这种大类更具体）。注意「路由」不再指向 OSPF / BGP：问路由表的人要的是路由表。
TEMPLATE_MENTION: dict[str, str] = {
    "route_table_diff": r"明细|对比|比对|差异|逐条|哪些路由|路由.{0,4}(?:变化|增减|变动)|\bdiff\b|which routes|route changes|table changes",
    "route_count": r"条目|条数|总数|路由数|数量|route count|number of routes|route entries",
    "ospf_neighbors": r"ospf",
    "bgp_sessions": r"bgp",
    "interfaces": r"接口状态|端口状态|接口\s*(?:up|down)|interface status|link down",
    "interface_errors": r"接口错误|错误计数|crc|丢包|input errors|error counters?",
    "uplink_errors": r"上联|uplink",
    "cpu": r"\bcpu\b|cpu",
    "memory": r"内存|memory",
}
#: 只说到大类时
TOPIC_MENTION: dict[str, str] = {
    "route": r"路由表|路由条目|路由明细|routing table|route table|\broutes?\b",
    "protocol": r"邻居|neighbou?rs?|路由协议|routing protocol",
    "interface": r"接口状态|端口状态|interface status",
    "errors": r"错误|errors?|crc|丢包",
    "resource": r"\bcpu\b|cpu|内存|memory",
}
#: 「明细对比」「条目」这类词只在说路由表时才指向路由模板
_ROUTE_SCOPED = {"route_table_diff", "route_count"}
_ROUTE_CONTEXT = re.compile(r"路由|routing|routes?\b|route table", re.IGNORECASE)

#: 否定 / 不想要（同一小句里出现在点名之前）；「一直给我 X」「老是出 X」是抱怨，也不算想要
NEGATION = re.compile(r"不要|不需要|不用|别再?|去掉|删掉|删除|删了|移除|拿掉|取消|不想要|一直给我|老是|总是给|又出现|还在推|"
                      r"\b(?:no|not|without|remove|drop|delete|don'?t (?:need|want)|stop)\b", re.IGNORECASE)
#: 意图替换：「我需要的是 X」「只要 X」「只看 X」——没点名的旧东西是被替换掉的，模型丢了它们不补回
INTENT_CHANGE = re.compile(r"需要的是|要的是|想要的是|只要|只看|只查|只关心|只留|只保留|只需要|而不是|不是要|换成|改成看|"
                           r"\b(?:instead|only|rather than|just)\b", re.IGNORECASE)
_CLAUSE = re.compile(r"[，。,;；!?！？\n]|但是|不过|\bbut\b", re.IGNORECASE)

MAX_FOCUSED_SUGGESTIONS = 2


def empty() -> dict[str, Any]:
    return {"focus": [], "focus_topics": [], "removed": [], "dismissed": [], "picked": [], "picked_devices": []}


def get(draft: dict[str, Any] | None) -> dict[str, Any]:
    """草案里的偏好（没有就是空的）；返回副本，改了要用 `put` 写回。"""
    raw = (draft or {}).get("prefs")
    out = empty()
    if isinstance(raw, dict):
        for k in out:
            v = raw.get(k)
            if isinstance(v, list):
                out[k] = json.loads(json.dumps(v))
    return out


def put(draft: dict[str, Any], prefs: dict[str, Any]) -> dict[str, Any]:
    draft["prefs"] = prefs
    return draft


def strip(draft: dict[str, Any]) -> dict[str, Any]:
    """保存前去掉（偏好只属于这次对话，不进计划文件）。"""
    out = dict(draft)
    out.pop("prefs", None)
    return out


def has_focus(prefs: dict[str, Any]) -> bool:
    return bool(prefs.get("focus") or prefs.get("focus_topics"))


def related_templates(prefs: dict[str, Any]) -> list[str]:
    """跟用户关注点相关的模板：点名的 + 关注大类里的。"""
    out = list(prefs.get("focus") or [])
    for topic in prefs.get("focus_topics") or []:
        for tid in TOPICS.get(topic, ()):
            if tid not in out:
                out.append(tid)
    return [t for t in out if t in P.TEMPLATES]


# --------------------------------------------------------------------------- 读用户的话


def read_text(text: str) -> dict[str, Any]:
    """从一句话里读：点名要的模板、点名不要的模板、提到的大类、是不是意图替换。按小句判断否定。"""
    wanted: list[str] = []
    negated: list[str] = []
    topics: list[str] = []
    neg_topics: list[str] = []
    for clause in _CLAUSE.split(text or ""):
        if not clause.strip():
            continue
        low = clause.lower()
        route_ctx = bool(_ROUTE_CONTEXT.search(clause)) or bool(_ROUTE_CONTEXT.search(text or ""))
        for tid, rx in TEMPLATE_MENTION.items():
            m = re.search(rx, low, re.IGNORECASE)
            if not m or (tid in _ROUTE_SCOPED and not route_ctx):
                continue
            neg = NEGATION.search(low[: m.start()])
            (negated if neg else wanted).append(tid)
        for topic, rx in TOPIC_MENTION.items():
            m = re.search(rx, low, re.IGNORECASE)
            if not m:
                continue
            (neg_topics if NEGATION.search(low[: m.start()]) else topics).append(topic)
    wanted = list(dict.fromkeys(t for t in wanted if t not in negated))
    for t in wanted:
        if TOPIC_OF.get(t) and TOPIC_OF[t] not in topics:
            topics.append(TOPIC_OF[t])
    topics = list(dict.fromkeys(t for t in topics if t not in neg_topics or any(TOPIC_OF.get(w) == t for w in wanted)))
    return {"wanted": wanted, "negated": list(dict.fromkeys(negated)), "topics": topics,
            "exclusive": bool(INTENT_CHANGE.search(text or "")) and bool(wanted or topics)}


def update_from_text(prefs: dict[str, Any], text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """用户说了一句话：更新关注点；点名不要的记进 removed，点名要的从 removed 里放出来。返回（新偏好, 读到的东西）。"""
    info = read_text(text)
    p = json.loads(json.dumps(prefs))
    if info["exclusive"]:
        p["focus"] = list(info["wanted"])
        p["focus_topics"] = list(info["topics"])
    else:
        p["focus"] = list(dict.fromkeys([*p["focus"], *info["wanted"]]))
        p["focus_topics"] = list(dict.fromkeys([*p["focus_topics"], *info["topics"]]))
    p["focus"] = [t for t in p["focus"] if t not in info["negated"]]
    if info["wanted"]:
        p["removed"] = [r for r in p["removed"] if r.get("template") not in info["wanted"]]
        p["dismissed"] = [k for k in p["dismissed"] if k not in {f"tpl:{t}" for t in info["wanted"]}]
    for tid in info["negated"]:
        if not any(r.get("template") == tid for r in p["removed"]):
            p["removed"].append({"id": tid, "template": tid, "command": ""})
    return p, info


# --------------------------------------------------------------------------- 删除记忆


def _check_key(c: dict[str, Any]) -> dict[str, str]:
    return {"id": str(c.get("id") or ""), "template": str(c.get("template") or ""), "command": " ".join(str(c.get("command") or "").split())}


def record_removed(prefs: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    p = json.loads(json.dumps(prefs))
    for c in checks:
        k = _check_key(c)
        if not any(r.get("id") == k["id"] and r.get("command") == k["command"] for r in p["removed"]):
            p["removed"].append(k)
        p["picked"] = [x for x in p["picked"] if x != k["id"]]
        p["focus"] = [t for t in p["focus"] if t != k["template"]]
    return p


def lift_removed(prefs: dict[str, Any], *, templates: list[str] = (), ids: list[str] = (), commands: list[str] = ()) -> dict[str, Any]:
    """用户又明确要回来（勾选 / 采纳 / 点名）：从 removed 里放出来。"""
    p = json.loads(json.dumps(prefs))
    cmds = {" ".join(c.split()) for c in commands}
    p["removed"] = [r for r in p["removed"] if not ((r.get("template") and r["template"] in templates) or r.get("id") in ids
                                                     or (r.get("command") and r["command"] in cmds))]
    return p


def is_removed(prefs: dict[str, Any], c: dict[str, Any]) -> bool:
    k = _check_key(c)
    per_uplink = bool(P.TEMPLATES.get(k["template"], {}).get("per_uplink"))
    for r in prefs.get("removed") or []:
        if r.get("id") and r["id"] == k["id"]:
            return True
        if r.get("command") and r["command"] == k["command"]:
            return True
        # 删掉的是一个模板（不是逐口展开的那种）：同一模板换了设备范围 / 换了 id 也算同一项
        if r.get("template") and r["template"] == k["template"] and not per_uplink:
            return True
    return False


def enforce(draft: dict[str, Any], prefs: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[str]]:
    """把用户删掉过的检查项从草案里拿掉（每一轮最后都过一遍）。返回（草案, 拿掉的 id）。"""
    p = prefs if prefs is not None else get(draft)
    out = dict(draft)
    kept, dropped = [], []
    for c in out.get("checks") or []:
        if is_removed(p, c):
            dropped.append(str(c.get("id")))
        else:
            kept.append(c)
    out["checks"] = kept
    return put(out, p), dropped


def record_picked(prefs: dict[str, Any], *, ids: list[str] = (), devices: list[str] = ()) -> dict[str, Any]:
    p = json.loads(json.dumps(prefs))
    p["picked"] = list(dict.fromkeys([*p["picked"], *ids]))
    p["picked_devices"] = list(dict.fromkeys([*p["picked_devices"], *devices]))
    return p


def clear_picked(prefs: dict[str, Any]) -> dict[str, Any]:
    return {**prefs, "picked": [], "picked_devices": []}


def diff_checks(before: dict[str, Any] | None, after: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """（新加的检查项, 拿掉的检查项），按 id 比。"""
    b = {c.get("id"): c for c in (before or {}).get("checks") or []}
    a = {c.get("id"): c for c in after.get("checks") or []}
    return [c for i, c in a.items() if i not in b], [c for i, c in b.items() if i not in a]


# --------------------------------------------------------------------------- 建议 / 快捷回复


def suggestion_keys(s: dict[str, Any]) -> set[str]:
    """一条建议的语义键：建议 id、它要加的每个模板、改周期、加的每台设备。等价的建议（写法不同、效果一样）键相同。"""
    keys = {f"id:{s.get('id')}"} if s.get("id") else set()
    patch = s.get("patch") or {}
    for c in patch.get("add_checks") or []:
        if isinstance(c, dict) and c.get("template"):
            keys.add(f"tpl:{c['template']}")
    for c in patch.get("add_custom_checks") or []:
        if isinstance(c, dict) and c.get("command"):
            keys.add("cmd:" + " ".join(str(c["command"]).split()))
    if patch.get("schedule"):
        keys.add("schedule")
    for n in patch.get("add_devices") or []:
        keys.add(f"dev:{n}")
    return keys


def dismiss(prefs: dict[str, Any], suggestion: dict[str, Any]) -> dict[str, Any]:
    p = json.loads(json.dumps(prefs))
    keys = suggestion_keys(suggestion)
    # 只加了设备的建议，记设备；加模板的建议记模板（不记它顺带的设备，免得以后别的建议都被连坐）
    if any(k.startswith(("tpl:", "cmd:", "schedule")) for k in keys):
        keys = {k for k in keys if not k.startswith("dev:")}
    p["dismissed"] = list(dict.fromkeys([*p["dismissed"], *sorted(keys)]))
    return p


def allow_suggestion(s: dict[str, Any], prefs: dict[str, Any]) -> bool:
    keys = suggestion_keys(s)
    dismissed = set(prefs.get("dismissed") or [])
    if keys & dismissed:
        return False
    tpls = [k[4:] for k in keys if k.startswith("tpl:")]
    if any(any(r.get("template") == t for r in prefs.get("removed") or []) for t in tpls):
        return False
    if has_focus(prefs) and tpls:
        related = set(related_templates(prefs))
        if not all(t in related for t in tpls):
            return False  # 说了关注点之后，不再推跟它无关的「按角色默认」检查
    return True


def filter_suggestions(items: list[dict[str, Any]], prefs: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    out = [s for s in items if allow_suggestion(s, prefs)]
    return out[: (min(limit, MAX_FOCUSED_SUGGESTIONS) if has_focus(prefs) else limit)]


def _mentions(text: str) -> list[str]:
    low = (text or "").lower()
    return [tid for tid, rx in TEMPLATE_MENTION.items() if re.search(rx, low, re.IGNORECASE)
            and (tid not in _ROUTE_SCOPED or _ROUTE_CONTEXT.search(text or ""))]


def filter_quick_replies(replies: list[str], prefs: dict[str, Any]) -> list[str]:
    """模型给的快捷回复里，提到被删 / 被拒 / 跟关注点无关的检查的去掉。"""
    removed = {r.get("template") for r in prefs.get("removed") or [] if r.get("template")}
    dismissed = {k[4:] for k in prefs.get("dismissed") or [] if k.startswith("tpl:")}
    related = set(related_templates(prefs)) if has_focus(prefs) else None
    out = []
    for r in replies:
        tids = _mentions(r)
        if any(t in removed or t in dismissed for t in tids):
            continue
        if related is not None and tids and not all(t in related for t in tids):
            continue
        out.append(r)
    return out


def quick_replies(draft: dict[str, Any], prefs: dict[str, Any], lang: str = "zh") -> list[str]:
    """按关注点和草案现状生成的快捷回复（不写死某条检查）。"""
    zh = lang != "en"
    have = {c.get("template") for c in draft.get("checks") or []}
    blocked = {r.get("template") for r in prefs.get("removed") or []} | {k[4:] for k in prefs.get("dismissed") or [] if k.startswith("tpl:")}
    out: list[str] = []
    for tid in related_templates(prefs):
        if tid not in have and tid not in blocked:
            title = P.TEMPLATES[tid]["title"]["zh" if zh else "en"]
            out.append(f"加上{title}" if zh else f"Add {title.lower()}")
    if not draft.get("schedule"):
        out.append("每小时跑一次" if zh else "Run hourly")
    if draft.get("devices") and draft.get("checks") and draft.get("schedule"):
        out.append("先试运行一下" if zh else "Dry run first")
    if not has_focus(prefs) and not draft.get("checks"):
        out += (["路由表有没有变化", "接口错误增长", "CPU 和内存"] if zh else ["Routing table changes", "Growing interface errors", "CPU and memory"])
    return out[:3]


def picker_defaults(draft: dict[str, Any], prefs: dict[str, Any]) -> tuple[list[str], list[str]]:
    """检查项气泡的（推荐, 预勾选）：说了关注点就只推荐 / 勾关注点相关的；没说才按设备角色默认。删过 / 拒过的不勾。"""
    blocked = {r.get("template") for r in prefs.get("removed") or []} | {k[4:] for k in prefs.get("dismissed") or [] if k.startswith("tpl:")}
    if has_focus(prefs):
        recommended = related_templates(prefs)
    else:
        recommended = []
        roles = {x.get("role", "") for x in draft.get("devices") or []}
        for r in roles or {""}:
            for tid in P.ROLE_DEFAULTS.get(r if r in P.ROLE_DEFAULTS else "", []):
                if tid not in recommended:
                    recommended.append(tid)
    recommended = [t for t in recommended if t not in blocked]
    have = [c.get("template") for c in draft.get("checks") or [] if c.get("template")]
    selected = [t for t in P.TEMPLATES if t in have] or [t for t in P.TEMPLATES if t in recommended]
    return recommended, [t for t in selected if t not in blocked]


# --------------------------------------------------------------------------- 把用户的操作记进偏好（API / 气泡共用）


def apply_patch_tracked(draft: dict[str, Any] | None, patch: dict[str, Any] | None, topology: dict[str, Any],
                        lang: str = "zh", *, dismiss_suggestion: dict[str, Any] | None = None) -> dict[str, Any]:
    """`P.apply_patch` + 把这次操作记进 `draft["prefs"]`：
    - `remove_checks`（× 按钮）→ removed：之后任何一轮都不再补回；
    - `add_checks` / `add_custom_checks`（采纳 / 选用）→ 从 removed 放出来，并记进 picked（模型下一轮无故丢了才补回）；
    - `dismiss_suggestion`（「不需要」）→ dismissed：等价的建议不再出现。
    `P.apply_patch` 会原样保留草案里的 `prefs` 键。"""
    before = json.loads(json.dumps(draft or {}))
    prefs = get(before)
    patch = patch or {}
    gone = {str(x) for x in patch.get("remove_checks") or []}
    if gone:
        prefs = record_removed(prefs, [c for c in before.get("checks") or [] if str(c.get("id")) in gone])
    out = P.apply_patch(before, patch, topology, lang)
    added_templates = [str(c["template"]) for c in patch.get("add_checks") or [] if isinstance(c, dict) and c.get("template")]
    added_cmds = [" ".join(str(c.get("command") or "").split()) for c in patch.get("add_custom_checks") or [] if isinstance(c, dict)]
    if added_templates or added_cmds:
        prefs = lift_removed(prefs, templates=added_templates, commands=added_cmds)
        new, _ = diff_checks(before, out)
        prefs = record_picked(prefs, ids=[str(c.get("id")) for c in new], devices=[str(x) for x in patch.get("add_devices") or []])
    elif patch.get("add_devices"):
        prefs = record_picked(prefs, devices=[str(x) for x in patch.get("add_devices") or []])
    if dismiss_suggestion:
        prefs = dismiss(prefs, dismiss_suggestion)
    out, _ = enforce(out, prefs)
    return put(out, prefs)


def track_check_selection(before: dict[str, Any] | None, after: dict[str, Any], chosen_templates: list[str]) -> dict[str, Any]:
    """检查项气泡提交后：原来在草案里、这次没勾的模板检查 → removed（用户亲手取消勾选）；勾上的 → 放出来 + picked。"""
    prefs = get(before)
    had = [c for c in (before or {}).get("checks") or [] if c.get("template") and c["template"] not in chosen_templates]
    if had:
        prefs = record_removed(prefs, had)
    prefs = lift_removed(prefs, templates=list(chosen_templates))
    new, _ = diff_checks(before, after)
    prefs = record_picked(prefs, ids=[str(c.get("id")) for c in new if c.get("template") in chosen_templates])
    return put(dict(after), prefs)

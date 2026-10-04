"""Read-only SOP lookup tool for the LangChain agent."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import re
import string
import time
from typing import Any

from netops_ai.graph.agent_loop import REPO_ROOT
from netops_ai.graph.tool_names import DEVICE_SHOW_TOOL_NAME, registered_readonly_tool_names, zabbix_tool_name
from netops_ai.llm.factory import env
from netops_ai.playbooks.engine import INTERFACE_PLACEHOLDER, load_playbooks, match_playbooks

PLAYBOOK_DIR = REPO_ROOT / "playbooks"
NO_MATCH_NOTE = (
    "没有匹配到团队 SOP。这不代表不用排查，只代表这类告警还没有人沉淀过经验。"
    "请按你自己的判断进行，并在结论里说明你用了哪些步骤。"
)
#: 分支条件 `error` / `default` 的语义，SOP 计划（pipeline）和 sop_lookup 返回共用这一句。
#: 与 `engine.BRANCH_ERROR_STATUSES` 一致：只有报错和白名单拒绝算 error，空结果走 default。
#: 原来写的是「被拒绝、报错或空输出时按 error 分支走」，空输出同时满足 error 和 default（验证 外部 agent 反馈第 3 条）。
BRANCH_SEMANTICS = (
    "branches 的 error 指工具报错或命令被白名单拒绝；返回为空（empty=true 或输出空白）不算 error，按 default 走；"
    "参数校验没通过、工具没有执行的调用不算走过这一步。报错且没有 error 分支时，这一步跳过、按 default 继续。"
)
MATCHED_NOTE = (
    "以上是团队沉淀的排查骨架。命中后从 start 开始取证，按每步 branches 的条件决定下一步；"
    "偏离要先写明理由。" + BRANCH_SEMANTICS +
    "SOP 与工具回显矛盾时，以回显为准并在结论里写明。"
)


def action_tool_mapping() -> dict[str, str]:
    return {
        "device": DEVICE_SHOW_TOOL_NAME,
        "zabbix_history": zabbix_tool_name("history"),
        "zabbix_reachability": zabbix_tool_name("history"),
    }


def validate_playbook_actions(playbooks: list[dict], *, known_tools: set[str] | None = None) -> None:
    known = known_tools or registered_readonly_tool_names()
    mapping = action_tool_mapping()
    errors: list[str] = []
    for playbook in playbooks:
        playbook_name = playbook.get("name") or playbook.get("_path") or "<unknown>"
        for step in playbook.get("steps") or []:
            action = step.get("action") or {}
            raw_tool = str(action.get("tool") or "")
            mapped_tool = mapping.get(raw_tool, raw_tool)
            if not raw_tool:
                errors.append(f"{playbook_name}:{step.get('id', '<missing-id>')} missing action.tool")
            elif mapped_tool not in known:
                errors.append(
                    f"{playbook_name}:{step.get('id', '<missing-id>')} action.tool={raw_tool!r} "
                    f"maps to {mapped_tool!r}, which is not registered"
                )
    if errors:
        raise ValueError("Invalid SOP playbook action tools: " + "; ".join(errors))


def load_validated_playbooks(playbook_dir: Path = PLAYBOOK_DIR, *, known_tools: set[str] | None = None) -> list[dict]:
    playbooks = load_playbooks(playbook_dir)
    validate_playbook_actions(playbooks, known_tools=known_tools)
    return playbooks


#: 同时命中多条时，最多把几条摆给 agent。
#:
#: 定 3 是有依据的：实测一条 6 步 SOP 返回约 640~760 token，3 条 ~2000，
#: 在 5000 token 的单次输入里占得起。**SOP 检索从来不是 token 瓶颈**——
#: `sop_lookup` 只返回命中的那几条，不管仓库里有 4 个还是 400 个。
MAX_AMBIGUOUS_CANDIDATES = 3

AMBIGUOUS_NOTE = (
    "这条告警同时命中了多条 SOP，已按匹配特异性排序（越具体越靠前）。"
    "**第一条不一定就是对的**——请结合告警的实际内容判断该用哪条，"
    "并在结论里说明你选了哪条、为什么。如果都不贴切，按你自己的判断排查。"
)


def sop_lookup(
    alert_name: str,
    tags: list[str] = [],
    vendor: str = "",
    *,
    alert_clock: int | float | None = None,
    interface_hint: str = "",
) -> dict:  # noqa: B006 - public contract fixes this signature
    """查团队 SOP，**只返回建议，不执行**。多条命中时都摆出来带理由，让 agent 自己挑；厂商是硬过滤。

 `interface_hint`：告警名本身没有接口时用的接口名（告警主线传整批告警汇总出的接口，
 见 `pipeline._interface_from_zabbix_text`）。优先级：告警名 > interface_hint > 占位符。
 对话路径的 `sop_lookup` 工具不传它，工具签名不变。
    """
    playbooks = load_validated_playbooks()
    alert = {
        "name": alert_name,
        "trigger_name": alert_name,
        "tags": _normalize_tags(tags),
        "vendor": vendor or env().get("DEVICE_VENDOR", ""),
    }
    alert_interface = parse_alert_interface(alert_name, alert["tags"])
    if alert_interface is None and str(interface_hint or "").strip():
        alert_interface = _normalize_interface_name(str(interface_hint).strip())
    matches = match_playbooks(playbooks, alert)
    if not matches:
        return {"matched": False, "steps": [], "note": NO_MATCH_NOTE}

    best = matches[0]
    out: dict[str, Any] = {
        "matched": True,
        "playbook": best.name,
        # **这条 SOP 覆盖什么、不覆盖什么，必须让 agent 看见。**
        # 一条 SOP 的匹配关键词总是比它实际覆盖的范围宽（`"OSPF neighbor"`
        # 会吃掉所有含这个词的告警，但 SOP 只讲邻接建不起来这一类）。
        # 不把范围说出来，agent 就会照着不贴切的步骤去查——而且不知道自己错了。
        "scope": _scope_of(best.playbook),
        "applicability": _applicability_of(best.playbook),
        "limits": _limits_of(best.playbook),
        "start": best.playbook.get("start") or "",
        "matched_because": best.reasons,
        "steps": [
            _step_for_agent(step, alert_clock=alert_clock, alert_interface=alert_interface)
            for step in best.playbook.get("steps") or []
        ],
        "note": MATCHED_NOTE,
    }

    others = matches[1:MAX_AMBIGUOUS_CANDIDATES]
    if others:
        # **歧义必须说出来。** 静默挑一条是这套机制最危险的失败模式：
        # agent 会照着可能错的 SOP 去查错的东西，而且不知道自己错了。
        out["ambiguous"] = True
        out["other_candidates"] = [
            {
                "playbook": m.name,
                "scope": _scope_of(m.playbook),
                "applicability": _applicability_of(m.playbook),
                "limits": _limits_of(m.playbook),
                "start": m.playbook.get("start") or "",
                "matched_because": m.reasons,
                "steps": [
                    _step_for_agent(step, alert_clock=alert_clock, alert_interface=alert_interface)
                    for step in m.playbook.get("steps") or []
                ],
            }
            for m in others
        ]
        out["total_matched"] = len(matches)
        out["note"] = AMBIGUOUS_NOTE
    warnings = _render_warnings(out)
    if warnings:
        out["render_warnings"] = warnings
    return out


_INTERFACE_PREFIX_TO_FULL = {
    "gi": "GigabitEthernet",
    "gigabitethernet": "GigabitEthernet",
    "fa": "FastEthernet",
    "fastethernet": "FastEthernet",
    "te": "TenGigabitEthernet",
    "tengigabitethernet": "TenGigabitEthernet",
    "et": "Ethernet",
    "ethernet": "Ethernet",
    "vl": "Vlan",
    "vlan": "Vlan",
    "bv": "BVI",
    "bvi": "BVI",
    "lo": "Loopback",
    "loopback": "Loopback",
    "po": "Port-channel",
    "port-channel": "Port-channel",
    "portchannel": "Port-channel",
}

_INTERFACE_PREFIX_TO_SHORT = {
    "gi": "Gi",
    "gigabitethernet": "Gi",
    "fa": "Fa",
    "fastethernet": "Fa",
    "te": "Te",
    "tengigabitethernet": "Te",
    "et": "Et",
    "ethernet": "Et",
    "vl": "Vl",
    "vlan": "Vl",
    "bv": "BV",
    "bvi": "BV",
    "lo": "Lo",
    "loopback": "Lo",
    "po": "Po",
    "port-channel": "Po",
    "portchannel": "Po",
}

_ALERT_INTERFACE_RE = re.compile(
    r"\bInterface\s+(?P<intf>[A-Za-z][A-Za-z-]*\d[\w/.:+-]*)",
    re.IGNORECASE,
)
_INTERFACE_PARTS_RE = re.compile(r"^(?P<prefix>[A-Za-z][A-Za-z-]*?)(?P<suffix>\d[\w/.:+-]*)$")


def parse_alert_interface(alert_name: str, tags: list[dict[str, str]] | list[str]) -> tuple[str, str] | None:
    """Parse an interface from a Zabbix-style alert name, falling back to an ``interface`` tag.

 Returns ``(full_name, short_name)`` such as
 ``("GigabitEthernet0/1", "Gi0/1")``. Unknown interface words are ignored
 so a generic alert like ``Cisco IOS: Interface Link down`` does not become
 a bogus command target.

 判断类 bug 审查发现：这个函数签名一直接收 `tags`，但函数体从来没读过它——
 告警名不带接口、接口只在 Zabbix tag 里时（比如 `Syslog: BGP neighbor down` 配
 `{"tag": "interface", "value": "Gi0/1"}`），SOP 里 `{alert_interface}` 就渲染成
 `<接口>` 占位符，明明有真实值却被当成缺失（真实记录 100615/100616/101155 等）。
 这里只是把已经传进来的 tag 读出来，**不额外发起任何查询、不改变 agent 能调用
 的工具或参数**——SOP 计划本来就是取证开始前渲染好的方向性文本，这条修复只是让
 它渲染得准，不是新增一次预取。
    """
    text = str(alert_name or "")
    match = _ALERT_INTERFACE_RE.search(text)
    if match:
        parsed = _normalize_interface_name(match.group("intf"))
        if parsed:
            return parsed
    for tag in tags or []:
        if isinstance(tag, dict):
            key, value = str(tag.get("tag") or ""), str(tag.get("value") or "")
        else:
            raw = str(tag)
            if "=" in raw:
                key, value = raw.split("=", 1)
            elif ":" in raw:
                key, value = raw.split(":", 1)
            else:
                continue
        if key.strip().lower() != "interface":
            continue
        parsed = _normalize_interface_name(value.strip())
        if parsed:
            return parsed
    return None


def _normalize_interface_name(raw: str) -> tuple[str, str] | None:
    token = str(raw or "").strip().rstrip(":,.)")
    match = _INTERFACE_PARTS_RE.match(token)
    if not match:
        return None
    prefix = match.group("prefix")
    suffix = match.group("suffix")
    key = prefix.lower()
    full_prefix = _INTERFACE_PREFIX_TO_FULL.get(key)
    short_prefix = _INTERFACE_PREFIX_TO_SHORT.get(key)
    if not full_prefix or not short_prefix:
        return None
    return f"{full_prefix}{suffix}", f"{short_prefix}{suffix}"


def _scope_of(playbook: dict[str, Any]) -> str:
    """SOP 的 `description` 就是它的范围声明，原样给 agent 看：名字和匹配关键词总比实际覆盖的宽。"""
    return " ".join(str(playbook.get("description") or "").split())


def _applicability_of(playbook: dict[str, Any]) -> str:
    value = str(playbook.get("applicability") or "").strip()
    return " ".join(value.split())


def _limits_of(playbook: dict[str, Any]) -> dict[str, int]:
    raw = playbook.get("limits") or {}
    def _positive_int(name: str, default: int) -> int:
        try:
            value = int(raw.get(name, default))
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default
    return {
        "max_main_steps": _positive_int("max_main_steps", 5),
        "max_tokens": _positive_int("max_tokens", 1500),
    }


def _normalize_tags(tags: list[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in tags or []:
        text = str(item)
        if "=" in text:
            tag, value = text.split("=", 1)
        elif ":" in text:
            tag, value = text.split(":", 1)
        else:
            if text.strip().lower() != "network":
                continue
            tag, value = "component", text
        if tag.strip() and value.strip():
            out.append({"tag": tag.strip(), "value": value.strip()})
    return out


def _step_for_agent(
    step: dict[str, Any],
    *,
    alert_clock: int | float | None,
    alert_interface: tuple[str, str] | None = None,
) -> dict[str, Any]:
    item = deepcopy(step)
    action = item.get("action") or {}
    raw_tool = str(action.get("tool") or "")
    if raw_tool:
        action["tool"] = action_tool_mapping().get(raw_tool, raw_tool)
    render_warnings: list[str] = []
    for key, value in list(action.items()):
        if not isinstance(value, str) or "{" not in value:
            continue
        if _template_needs_alert_interface(value) and alert_interface is None:
            item["needs_interface"] = True
            item["why"] = _append_missing_interface_note(str(item.get("why", "")))
        rendered, warning = _render_command(
            value,
            alert_clock=alert_clock,
            alert_interface=alert_interface,
        )
        action[key] = rendered
        if warning:
            render_warnings.append(f"{key}: {warning}")
    if render_warnings:
        action["render_warning"] = "; ".join(render_warnings)
    out = {
        "id": item.get("id", ""),
        "why": item.get("why", ""),
        "expect": item.get("expect", ""),
        "main": item.get("main", True) is not False,
        "action": action,
        "branches": item.get("branches") or [],
        "provenance": item.get("provenance") or {},
    }
    if item.get("needs_interface"):
        out["needs_interface"] = True
    return out


#: SOP 模板里 `{alert_window_from}` / `{alert_window_to}` 的取值：告警时刻前 15 分钟到后 5 分钟，Unix 秒。
#: 前 15 分钟跟 pipeline 预取第一信源的 `CONTEXT_WINDOW_SECONDS` 对齐。
#: **渲染成具体的 Unix 秒**，是为了让 SOP 里写的参数可以原样填进 zbx_syslog——
#: 两个 agent 都因为不知道时间格式白白报错了一次（外部 agent 反馈第 5 条）。
ALERT_WINDOW_BEFORE_SECONDS = 15 * 60
ALERT_WINDOW_AFTER_SECONDS = 5 * 60


def _alert_window(alert_clock: int | float | None) -> tuple[int, int]:
    epoch = int(time.time() if alert_clock is None else float(alert_clock))
    return epoch - ALERT_WINDOW_BEFORE_SECONDS, epoch + ALERT_WINDOW_AFTER_SECONDS


def _alert_log_prefix(alert_clock: int | float | None) -> str:
    """Build the IOS log prefix from the alert time or current UTC."""
    epoch = time.time() if alert_clock is None else float(alert_clock)
    dt = datetime.fromtimestamp(epoch - 300, tz=timezone.utc)
    return f"{dt:%b} {dt.day:2d} {dt:%H}"


def _render_command(
    command: str,
    *,
    alert_clock: int | float | None,
    alert_interface: tuple[str, str] | None = None,
) -> tuple[str, str]:
    """Render known SOP variables and keep unknown variables visible."""
    window_from, window_to = _alert_window(alert_clock)
    values = {
        "alert_log_prefix": _alert_log_prefix(alert_clock),
        "alert_window_from": str(window_from),
        "alert_window_to": str(window_to),
    }
    if alert_interface is not None:
        values["alert_interface"] = alert_interface[0]
        values["alert_interface_short"] = alert_interface[1]
    else:
        values["alert_interface"] = values["alert_interface_short"] = INTERFACE_PLACEHOLDER
    formatter = _SopFormatter()
    try:
        rendered = formatter.format(command, **values)
    except Exception as exc:  # noqa: BLE001 - a template must never break SOP lookup
        return command, f"{type(exc).__name__}: SOP 模板格式错误，已保留原命令"
    if formatter.unknown_fields:
        fields = ", ".join(sorted(formatter.unknown_fields))
        return rendered, f"未知 SOP 变量未渲染: {fields}"
    return rendered, ""


class _SopFormatter(string.Formatter):
    def __init__(self) -> None:
        super().__init__()
        self.unknown_fields: set[str] = set()

    def get_value(self, key: int | str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        if isinstance(key, str) and key not in kwargs:
            self.unknown_fields.add(key)
            return "{" + key + "}"
        return super().get_value(key, args, kwargs)


def _template_needs_alert_interface(template: str) -> bool:
    return "{alert_interface}" in template or "{alert_interface_short}" in template


def _append_missing_interface_note(why: str) -> str:
    note = f"告警里没有接口名，命令里的 {INTERFACE_PLACEHOLDER} 要换成实际接口。"
    if note in why:
        return why
    return (why.rstrip() + " " + note).strip()


def _render_warnings(data: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for candidate in [data, *(data.get("other_candidates") or [])]:
        for step in candidate.get("steps") or []:
            warning = ((step.get("action") or {}).get("render_warning") or "")
            if warning:
                prefix = f"{candidate.get('playbook', '')}:{step.get('id', '<unknown>')}: "
                warnings.append(prefix + warning)
    return warnings

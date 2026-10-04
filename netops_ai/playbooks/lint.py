from __future__ import annotations

import json
import argparse
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml

from netops_ai.playbooks.catalog import load_catalog, resolve_intent
from netops_ai.devices.whitelist import check
from netops_ai.playbooks.engine import (
    AI_GOTO,
    ALLOWED_PROVENANCE_SOURCES,
    BRANCH_VALUE_FIELDS,
    DEFAULT_MAX_MAIN_STEPS,
    DEFAULT_MAX_TOKENS,
    END_GOTO,
    INTERFACE_PLACEHOLDER,
    PEER_ADDRESS_PLACEHOLDER,
)
from netops_ai.playbooks.lookup import _step_for_agent, action_tool_mapping

TERMINALS = {AI_GOTO, END_GOTO}
ARCHIVE_DIRS = {"_proposed", "_approved_archive", "_rejected"}
REPRESENTATIVE_INTERFACES = [
    None,
    ("GigabitEthernet0/1", "Gi0/1"),
    ("Ethernet1/0", "Et1/0"),
    ("FastEthernet0/1", "Fa0/1"),
]
REMAINING_TEMPLATE_RE = re.compile(r"{[^{}\s]+}")
OUTPUT_CONTAINS_RE = re.compile(r"^output_contains\(.+\)$")
VALUE_COMPARE_RE = re.compile(r"^value\s*(==|!=)\s*['\"][^'\"]*['\"]$")
#: `<对端地址>` 送白名单检查时换成的代表值（RFC 5737 文档地址，不属于任何真实设备）。
REPRESENTATIVE_PEER_ADDRESS = "192.0.2.1"
TOPOLOGY_FILE = Path(__file__).resolve().parents[2] / "topology.yaml"

# ── instance-literal：SOP 是方向性文件，不写死 IP / 接口 / 设备名──
#: IPv4 字面量。前后不能紧挨数字或点，`1790391000` 这类 Unix 秒、`15.9` 这类版本号都不是四段式，碰不上。
IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
#: 协议常量，不是某台设备的实例值：全零/广播/环回，224.0.0.0/24 本地链路组播（OSPF 224.0.0.5/6、
#: RIP .9、EIGRP .10、VRRP .18、HSRPv2 .102 等），以及子网掩码和反掩码本身。
PROTOCOL_CONSTANT_IPV4 = frozenset({"0.0.0.0", "255.255.255.255", "127.0.0.1"})
#: 具体接口名：前缀 + 编号（`GigabitEthernet0/1`、`Gi0/1`、`Ethernet0/1`、`Eth1/1`、`Fa0/1`、`Vlan11`……）。
#: 前面不能紧挨字母数字，所以 `%LINK-5-CHANGED`、`show ip route`、`logging` 碰不上；
#: `{alert_interface}` 这类模板变量扫描前先去掉。
INTERFACE_LITERAL_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:TenGigabitEthernet|GigabitEthernet|FastEthernet|Ethernet|Port-channel|Loopback|Vlan|BVI"
    r"|Eth|Gi|Fa|Te|Et|Vl|Po|Lo)"
    r"\s?\d+(?:/\d+)*(?:\.\d+)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SopLintIssue:
    level: str
    path: str
    step_id: str
    code: str
    message: str

    @property
    def location(self) -> str:
        if self.step_id:
            return f"{self.path}:{self.step_id}"
        return self.path


def lint_paths(paths: Iterable[Path], *, include_archive: bool = False) -> list[SopLintIssue]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(_iter_playbook_files(path, include_archive=include_archive))
        elif path.suffix in {".yaml", ".yml"}:
            if include_archive or not any(part in ARCHIVE_DIRS for part in path.parts):
                files.append(path)
    issues: list[SopLintIssue] = []
    for path in sorted(set(files)):
        issues.extend(lint_file(path))
    return issues


def lint_file(path: Path) -> list[SopLintIssue]:
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    return lint_playbook(data, path=path, text=text)


def lint_playbook(data: dict[str, Any], *, path: Path | str = "<memory>", text: str | None = None) -> list[SopLintIssue]:
    path_s = str(path)
    issues: list[SopLintIssue] = []
    intent_playbook = _is_intent_playbook(data, path_s)
    steps = data.get("steps") or []
    step_ids = [str(step.get("id") or "") for step in steps]
    step_id_set = {sid for sid in step_ids if sid}
    step_by_id = {str(step.get("id") or ""): step for step in steps if step.get("id")}

    if not data.get("applicability"):
        issues.append(_issue("warn", path_s, "", "missing-applicability", "missing applicability"))

    match = data.get("match") or {}
    fault_types = [str(value) for value in (match.get("fault_types") or [])]
    if "*" in fault_types and not bool(data.get("generic")):
        issues.append(
            _issue(
                "error",
                path_s,
                "",
                "wildcard-requires-generic",
                "match.fault_types '*' is only allowed on generic: true SOPs",
            )
        )
    if not intent_playbook and not any(match.get(key) for key in ("vendor", "trigger_name_contains", "tags")):
        issues.append(_issue("error", path_s, "", "empty-match", "match is empty and would match every alert"))

    limits = data.get("limits") or {}
    max_main_steps = _positive_int(limits.get("max_main_steps"), DEFAULT_MAX_MAIN_STEPS)
    max_tokens = _positive_int(limits.get("max_tokens"), DEFAULT_MAX_TOKENS)
    main_steps = [step for step in steps if step.get("main", True) is not False]
    if len(main_steps) > max_main_steps:
        issues.append(
            _issue(
                "error",
                path_s,
                "",
                "too-many-main-steps",
                f"main steps {len(main_steps)} exceeds limits.max_main_steps {max_main_steps}",
            )
        )
    token_estimate = len(text if text is not None else json.dumps(data, ensure_ascii=False, default=str)) // 2  # ruamel 的 CommentedMap 不能用 PyYAML safe_dump
    if token_estimate > max_tokens:
        issues.append(
            _issue(
                "error",
                path_s,
                "",
                "too-many-tokens",
                f"estimated tokens {token_estimate} exceeds limits.max_tokens {max_tokens}",
            )
        )

    start = str(data.get("start") or "")
    if start not in step_id_set:
        issues.append(_issue("error", path_s, "", "bad-start", f"start points to missing step {start!r}"))

    command_seen: dict[str, str] = {}
    for step in steps:
        sid = str(step.get("id") or "<missing-id>")
        if not step.get("expect"):
            issues.append(_issue("warn", path_s, sid, "missing-expect", "step missing expect"))
        if not step.get("why"):
            issues.append(_issue("warn", path_s, sid, "missing-why", "step missing why"))
        # 模板变量只在 action 里渲染；写进 why/expect 的会原样摆给模型（前 flap_history 就这样）。
        for field_name in ("why", "expect"):
            leftover = REMAINING_TEMPLATE_RE.search(str(step.get(field_name) or ""))
            if leftover:
                issues.append(_issue("error", path_s, sid, "template-in-prose",
                                     f"{field_name} contains {leftover.group(0)!r}, which is never rendered"))
        issues.extend(_instance_literal_issues(step, path_s, sid))
        source = (step.get("provenance") or {}).get("source")
        if not source:
            issues.append(_issue("warn", path_s, sid, "missing-provenance", "step missing provenance.source"))
        elif source not in ALLOWED_PROVENANCE_SOURCES:
            issues.append(_issue("warn", path_s, sid, "unknown-provenance", f"provenance.source {source!r} is not allowed"))

        branches = step.get("branches") or []
        if not any(str(branch.get("when") or "") == "default" for branch in branches):
            issues.append(_issue("warn", path_s, sid, "missing-default-branch", "branches has no default"))
        for branch in branches:
            goto = str(branch.get("goto") or "")
            if goto not in step_id_set and goto not in TERMINALS:
                issues.append(_issue("error", path_s, sid, "bad-goto", f"goto points to missing step {goto!r}"))
            when = str(branch.get("when") or "")
            if not _known_when(when):
                issues.append(_issue("error", path_s, sid, "unknown-when", f"when expression is not recognized: {when!r}"))
            elif VALUE_COMPARE_RE.match(when.strip()) and not intent_playbook:
                raw_tool = str((step.get("action") or {}).get("tool") or "")
                tool = action_tool_mapping().get(raw_tool, raw_tool)
                if tool not in BRANCH_VALUE_FIELDS:
                    issues.append(_issue("error", path_s, sid, "value-branch-unsupported",
                                         f"{when!r} can never fire: {tool!r} yields no branch value "
                                         f"(tools with a value: {', '.join(sorted(BRANCH_VALUE_FIELDS))})"))

        action = step.get("action") or {}
        if not intent_playbook:
            issues.extend(_lint_action_tool(step, path_s, sid))
        if not intent_playbook and action.get("tool") == "device":
            for rendered in _rendered_device_actions(step):
                command = str(rendered.get("command") or "")
                if not command:
                    continue
                if REMAINING_TEMPLATE_RE.search(command):
                    issues.append(_issue("error", path_s, sid, "unrendered-template", f"rendered command still contains template text: {command!r}"))
                    continue
                if INTERFACE_PLACEHOLDER in command:
                    continue  # 告警没接口时的占位版本，同一条命令带真实接口的版本会被检查
                # <对端地址> 没有「带真实值的版本」，换成代表地址查白名单
                command = command.replace(PEER_ADDRESS_PLACEHOLDER, REPRESENTATIVE_PEER_ADDRESS)
                verdict = check("cisco", command)
                if not verdict.allowed:
                    issues.append(_issue("error", path_s, sid, "whitelist-denied", f"{command!r} denied by whitelist: {verdict.reason}"))
                normalized = " ".join(command.split()).lower()
                if normalized in command_seen:
                    issues.append(_issue("warn", path_s, sid, "duplicate-command", f"duplicates command from {command_seen[normalized]}"))
                else:
                    command_seen[normalized] = sid

    if intent_playbook:
        issues.extend(_lint_intent_commands(data, path_s))
    else:
        issues.extend(_ambiguous_match_issues(path_s, steps))
    issues.extend(_graph_issues(path_s, start, step_by_id))
    return issues


def format_markdown(issues: list[SopLintIssue]) -> str:
    lines = ["# SOP lint report", ""]
    if not issues:
        lines.append("No issues.")
        return "\n".join(lines) + "\n"
    lines.append("| level | location | code | message |")
    lines.append("| --- | --- | --- | --- |")
    for issue in issues:
        lines.append(
            f"| {issue.level} | {issue.location} | {issue.code} | {_escape_md(issue.message)} |"
        )
    return "\n".join(lines) + "\n"


def has_errors(issues: Iterable[SopLintIssue]) -> bool:
    return any(issue.level == "error" for issue in issues)


def _iter_playbook_files(root: Path, *, include_archive: bool) -> Iterable[Path]:
    if include_archive:
        yield from root.rglob("*.yaml")
        return
    for path in root.glob("*.yaml"):
        if not path.name.startswith("_"):
            yield path
    intent_dir = root / "intent"
    if intent_dir.is_dir():
        yield from intent_dir.glob("*.yaml")


def _is_intent_playbook(data: dict[str, Any], path: str) -> bool:
    return Path(path).parent.name == "intent" or any(
        isinstance(step, dict) and "intent" in step for step in (data.get("steps") or [])
    )


#: SOP 写不出、要 agent 按告警上下文自己填的参数（告警那台设备）。缺了不算错。
CONTEXT_FILLED_PARAMS = frozenset({"host", "device"})
#: action 里不是工具参数的键。
NON_PARAM_ACTION_KEYS = frozenset({"tool", "render_warning"})


@lru_cache(maxsize=1)
def _agent_tool_params() -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """agent 真正注册的工具 → (全部参数名, 必填参数名)。**从注册出来的工具现算，不手写。**

 SOP 写 `tool=zbx_history; command=syslog`，而 zbx_history 只有 item_id/since/limit——
 SOP 引用的参数跟工具对不上，没有任何东西会报错，模型只能在「照 SOP 字面」和「照工具说明」之间猜。
    """
    out: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    for name, args_schema in _agent_tool_schemas().items():
        schema = args_schema.model_json_schema() if args_schema is not None else {}
        out[name] = (
            frozenset((schema.get("properties") or {}).keys()),
            frozenset(schema.get("required") or []),
        )
    return out


@lru_cache(maxsize=1)
def _agent_tool_schemas() -> dict[str, Any]:
    """agent 真正注册的工具 → 它的 pydantic args_schema。"""
    from netops_ai.graph.agent_loop import AgentLoopBudget, ChatRunTrace
    from netops_ai.graph.chat_agent import build_chat_tools

    trace = ChatRunTrace(trace_id="sop-lint", question="", session_id="sop-lint")
    return {tool.name: tool.args_schema for tool in build_chat_tools(trace, AgentLoopBudget(), include_doc_search=True)}


def _param_type_issues(tool: str, rendered: dict[str, Any], path: str, sid: str) -> list[SopLintIssue]:
    """渲染后的参数值原样填进工具，能不能过工具的参数校验。

 验证：SOP 渲染出 `since=1790395561`，而 zbx_syslog 的 since 只收字符串，外部 agent 照字面传整数，
 ValidationError 白跑一次。参数名对（bad-action-param 查的）不等于值的类型对。
 计划里是 `key=value` 纯文本，模型看到一串数字既可能传字符串也可能传整数，所以**两种读法都得过**。
    """
    args_schema = _agent_tool_schemas().get(tool)
    if args_schema is None:
        return []
    values = {k: v for k, v in rendered.items() if k not in NON_PARAM_ACTION_KEYS}
    readings = [values]
    as_int = {k: int(v) for k, v in values.items() if isinstance(v, str) and v.strip().isdigit()}
    if as_int:
        readings.append({**values, **as_int})
    issues: list[SopLintIssue] = []
    for reading in readings:
        try:
            args_schema.model_validate(reading)
        except Exception as exc:  # noqa: BLE001 - pydantic.ValidationError；只挑 SOP 给了值的字段
            errors = getattr(exc, "errors", lambda: [])()
            for e in errors:
                key = str(e["loc"][0]) if e.get("loc") else ""
                if e.get("type") == "missing" or key not in reading:
                    continue
                issues.append(_issue("error", path, sid, "bad-action-param-type",
                                     f"{tool}.{key}={reading[key]!r} fails the tool's schema: {e.get('msg', '')}"))
    return issues


def _lint_action_tool(step: dict[str, Any], path: str, sid: str) -> list[SopLintIssue]:
    """SOP 步骤引用的工具必须真实注册，参数名必须是那个工具的参数名，必填参数不能缺。"""
    action = step.get("action") or {}
    raw_tool = str(action.get("tool") or "")
    if not raw_tool:
        return [_issue("error", path, sid, "missing-action-tool", "step has no action.tool")]
    tool = action_tool_mapping().get(raw_tool, raw_tool)
    params_by_tool = _agent_tool_params()
    if tool not in params_by_tool:
        return [_issue("error", path, sid, "unknown-action-tool",
                       f"action.tool {raw_tool!r} maps to {tool!r}, which is not a registered agent tool")]
    legal, required = params_by_tool[tool]
    issues: list[SopLintIssue] = []
    given = {str(key) for key in action if key not in NON_PARAM_ACTION_KEYS}
    for key in sorted(given - legal):
        issues.append(_issue("error", path, sid, "bad-action-param",
                             f"{tool} has no parameter {key!r} (parameters: {', '.join(sorted(legal))})"))
    for key in sorted(required - given - CONTEXT_FILLED_PARAMS):
        issues.append(_issue("error", path, sid, "missing-action-param",
                             f"{tool} requires {key!r}, which the SOP step does not give"))
    type_issues: dict[str, SopLintIssue] = {}
    for rendered in _rendered_device_actions(step):
        for issue in _param_type_issues(tool, rendered, path, sid):
            type_issues.setdefault(issue.message, issue)
    issues.extend(type_issues.values())
    if raw_tool == "device":
        return issues  # device 命令的模板和白名单在 lint_playbook 里单独查
    for rendered in _rendered_device_actions(step):
        for key, value in rendered.items():
            if key in NON_PARAM_ACTION_KEYS or not isinstance(value, str):
                continue
            if REMAINING_TEMPLATE_RE.search(value):
                issues.append(_issue("error", path, sid, "unrendered-template",
                                     f"rendered {key} still contains template text: {value!r}"))
    return issues


def _instance_literal_issues(step: dict[str, Any], path: str, sid: str) -> list[SopLintIssue]:
    """why / expect / action 的各参数值 / 分支条件里写死的 IPv4、接口名、拓扑设备名。

 SOP 只写方向。实例信息只能来自告警上下文的模板变量（`{alert_interface}` 等），
 或由 agent 按告警设备自己填（host/device），或写成 `<对端地址>` 这类由 agent 从前一步输出里取的占位。
 前 bgp-session 写死了 `show ip route 192.0.2.51`（那还是 V2 的管理口，不是 BGP 邻居），
 ospf-adjacency / bgp-session 的对端检查写死了 `device: V1 / interface: GigabitEthernet0/1`。
    """
    fields: list[tuple[str, str]] = [("why", str(step.get("why") or "")), ("expect", str(step.get("expect") or ""))]
    for key, value in (step.get("action") or {}).items():
        if key in NON_PARAM_ACTION_KEYS:
            continue
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            fields.append((f"action.{key}", str(value)))
    for branch in step.get("branches") or []:
        fields.append(("branches.when", str(branch.get("when") or "")))
    issues: list[SopLintIssue] = []
    for field_name, text in fields:
        for kind, literal in _instance_literals(text):
            issues.append(_issue("error", path, sid, "instance-literal",
                                 f"{field_name} hard-codes {kind} {literal!r}; SOP 只写方向，实例值要来自告警模板变量、"
                                 f"agent 按告警设备填，或用不带实例参数的通用命令"))
    return issues


def _instance_literals(text: str) -> list[tuple[str, str]]:
    text = REMAINING_TEMPLATE_RE.sub(" ", text)
    found: list[tuple[str, str]] = []
    for match in IPV4_RE.finditer(text):
        octets = [int(part) for part in match.groups()]
        if any(octet > 255 for octet in octets):
            continue
        literal = match.group(0)
        if _is_protocol_constant_ipv4(literal, octets):
            continue
        found.append(("IPv4 address", literal))
    for match in INTERFACE_LITERAL_RE.finditer(text):
        found.append(("interface", match.group(0)))
    names = _topology_device_names()
    if names:
        name_re = re.compile(
            r"(?<![A-Za-z0-9_-])(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?![A-Za-z0-9_-])"
        )
        for match in name_re.finditer(text):
            found.append(("topology device", match.group(0)))
    return found


def _is_protocol_constant_ipv4(literal: str, octets: list[int]) -> bool:
    if literal in PROTOCOL_CONSTANT_IPV4:
        return True
    if octets[:3] == [224, 0, 0]:
        return True
    as_int = int.from_bytes(bytes(octets), "big")
    for mask in (as_int, as_int ^ 0xFFFFFFFF):  # 子网掩码 / 反掩码：连续的 1 后面跟连续的 0
        inverted = mask ^ 0xFFFFFFFF
        if mask and (inverted & (inverted + 1)) == 0:
            return True
    return False


@lru_cache(maxsize=1)
def _topology_device_names() -> frozenset[str]:
    """topology.yaml 里登记的设备名和别名（V1、V1-vios、D1……）。**只读仓库里的文件**，lint 不连 NetBox。"""
    if not TOPOLOGY_FILE.exists():
        return frozenset()
    data = yaml.safe_load(TOPOLOGY_FILE.read_text(encoding="utf-8")) or {}
    names: set[str] = set()
    for device in data.get("devices") or []:
        if not isinstance(device, dict):
            continue
        for name in [device.get("name"), *(device.get("aliases") or [])]:
            if str(name or "").strip():
                names.add(str(name).strip())
    return frozenset(names)


def _lint_intent_commands(data: dict[str, Any], path: str) -> list[SopLintIssue]:
    issues: list[SopLintIssue] = []
    catalog_dir = _catalog_dir_for_path(path)
    catalog = load_catalog(catalog_dir)
    if not catalog:
        return [_issue("warn", path, "", "missing-catalog", f"no command catalog found at {catalog_dir}")]
    known_intents = {intent for intents in catalog.values() for intent in intents}
    for step in data.get("steps") or []:
        intent = str(step.get("intent") or "").strip()
        if intent and intent not in known_intents:
            issues.append(_issue("error", path, str(step.get("id") or "<missing-id>"), "unknown-intent",
                                 f"intent {intent!r} is not defined in any command catalog"))

    params = {
        "interface": "GigabitEthernet0/1",
        "interface_short": "Gi0/1",
        "alert_log_prefix": "Sep  4 09",
    }
    vendor_by_family = {
        "cisco_ios": "cisco",
    }
    for step in data.get("steps") or []:
        sid = str(step.get("id") or "<missing-id>")
        intent = str(step.get("intent") or "").strip()
        if not intent:
            issues.append(_issue("error", path, sid, "missing-intent", "intent SOP step must use intent"))
            continue
        for os_family, intents in sorted(catalog.items()):
            if intent not in intents:
                issues.append(
                    _issue(
                        "warn",
                        path,
                        sid,
                        "missing-catalog-intent",
                        f"catalog {os_family!r} has no intent {intent!r}",
                    )
                )
                continue
            resolved = resolve_intent(intent, os_family, params, catalog)
            if resolved is None:
                issues.append(
                    _issue(
                        "error",
                        path,
                        sid,
                        "unresolved-intent",
                        f"{os_family}:{intent} could not be rendered with representative parameters",
                    )
                )
                continue
            vendor = vendor_by_family.get(os_family)
            if vendor is None:
                issues.append(
                    _issue(
                        "warn",
                        path,
                        sid,
                        "unknown-catalog-os",
                        f"no whitelist vendor mapping for catalog OS {os_family!r}",
                    )
                )
                continue
            verdict = check(vendor, resolved.command)
            if not verdict.allowed:
                issues.append(
                    _issue(
                        "error",
                        path,
                        sid,
                        "whitelist-denied",
                        f"{os_family}:{resolved.command!r} denied by whitelist: {verdict.reason}",
                    )
                )
    return issues


def _catalog_dir_for_path(path: str) -> Path:
    candidate = Path(path)
    if candidate.parent.name == "intent":
        sibling = candidate.parent.parent / "catalog"
        if sibling.exists():
            return sibling
    return Path(__file__).resolve().parents[2] / "playbooks" / "catalog"


def _rendered_device_actions(step: dict[str, Any]) -> Iterable[dict[str, Any]]:
    seen: set[str] = set()
    for alert_interface in REPRESENTATIVE_INTERFACES:
        rendered = _step_for_agent(step, alert_clock=1_790_000_000, alert_interface=alert_interface)
        action = rendered.get("action") or {}
        key = repr(sorted(action.items()))
        if key in seen:
            continue
        seen.add(key)
        yield action


def _known_when(expr: str) -> bool:
    expr = expr.strip()
    return (
        expr in {"default", "error"}
        or OUTPUT_CONTAINS_RE.match(expr) is not None
        or VALUE_COMPARE_RE.match(expr) is not None
    )


def _graph_issues(path: str, start: str, step_by_id: dict[str, dict[str, Any]]) -> list[SopLintIssue]:
    if start not in step_by_id:
        return []
    issues: list[SopLintIssue] = []
    graph = {
        sid: [
            str(branch.get("goto") or "")
            for branch in (step.get("branches") or [])
            if str(branch.get("goto") or "") in step_by_id or str(branch.get("goto") or "") in TERMINALS
        ]
        for sid, step in step_by_id.items()
    }
    reachable = _reachable_steps(start, graph)
    can_exit: dict[str, bool] = {}

    def exits(node: str, stack: set[str]) -> bool:
        if node in TERMINALS:
            return True
        if node in can_exit:
            return can_exit[node]
        if node in stack:
            return False
        stack.add(node)
        ok = any(exits(nxt, stack) for nxt in graph.get(node, []))
        stack.remove(node)
        can_exit[node] = ok
        return ok

    if not exits(start, set()):
        issues.append(_issue("error", path, start, "no-exit", "start cannot reach __end__ or __ai__"))
    for sid in sorted(reachable):
        if not exits(sid, set()):
            issues.append(_issue("error", path, sid, "no-exit", "step cannot reach __end__ or __ai__"))

    # 分支图必须无环。原来只从 start 找，走不到的那部分里有环也不报；起每一步都当起点找一遍。
    # 运行时另有一道：走过的步骤不会再被 [SOP 下一步] 指回（agent_loop.SopRuntimeState）。
    seen_cycles: set[frozenset[str]] = set()
    for origin in [start, *sorted(step_by_id)]:
        for cycle in _cycles_from(origin, graph):
            key = frozenset(cycle)
            if key in seen_cycles:
                continue
            seen_cycles.add(key)
            issues.append(_issue("error", path, cycle[0], "cycle", "goto cycle: " + " -> ".join(cycle)))
    for sid in sorted(set(step_by_id) - reachable):
        issues.append(_issue("warn", path, sid, "unreachable-step", "no branch from start reaches this step"))
    return issues


def _ambiguous_match_issues(path: str, steps: list[dict[str, Any]]) -> list[SopLintIssue]:
    """运行时靠「工具名 + 命令」认一次调用在走哪一步。两步认不开时，提示会接到错的那步的分支上。

 验证：Trap 告警名不带接口，triage_interface 的命令原来被整条拿掉，任何 device_show 都认成它，
 local_log_buffer 的输出又含 administratively down，[SOP 下一步] 于是指回了 local_shutdown_evidence。
    """
    from netops_ai.graph.agent_loop import _command_matches, _step_action_command

    issues: list[SopLintIssue] = []
    reported: set[tuple[str, str]] = set()
    for alert_interface in REPRESENTATIVE_INTERFACES:
        rendered = [
            (str(step.get("id") or "<missing-id>"),
             _step_for_agent(step, alert_clock=1_790_000_000, alert_interface=alert_interface))
            for step in steps
        ]
        for i, (sid_a, a) in enumerate(rendered):
            for sid_b, b in rendered[i + 1:]:
                if (a.get("action") or {}).get("tool") != (b.get("action") or {}).get("tool"):
                    continue
                cmd_a, cmd_b = _step_action_command(a), _step_action_command(b)
                if cmd_a and cmd_b and not (_command_matches(cmd_a, cmd_b) or _command_matches(cmd_b, cmd_a)):
                    continue
                if (sid_a, sid_b) in reported:
                    continue
                reported.add((sid_a, sid_b))
                where = "alert without interface" if alert_interface is None else f"interface {alert_interface[0]}"
                issues.append(_issue("warn", path, sid_b, "ambiguous-step-match",
                                     f"runtime cannot tell this step from {sid_a} ({where}; "
                                     f"tool={(a.get('action') or {}).get('tool')}, commands {cmd_a!r} / {cmd_b!r})"))
    return issues


def _reachable_steps(start: str, graph: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node in seen or node in TERMINALS:
            continue
        seen.add(node)
        stack.extend(graph.get(node, []))
    return seen


def _cycles_from(start: str, graph: dict[str, list[str]]) -> list[list[str]]:
    cycles: list[list[str]] = []
    emitted: set[tuple[str, ...]] = set()

    def visit(node: str, path: list[str]) -> None:
        if node in TERMINALS:
            return
        if node in path:
            cycle = path[path.index(node):] + [node]
            key = tuple(cycle)
            if key not in emitted:
                emitted.add(key)
                cycles.append(cycle)
            return
        for nxt in graph.get(node, []):
            visit(nxt, path + [node])

    visit(start, [])
    return cycles


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _issue(level: str, path: str, step_id: str, code: str, message: str) -> SopLintIssue:
    return SopLintIssue(level=level, path=path, step_id=step_id, code=code, message=message)


def _escape_md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lint SOP playbook YAML files.")
    parser.add_argument("paths", nargs="*", type=Path, default=[Path("playbooks")])
    parser.add_argument("--include-archive", action="store_true", help="include _proposed/_approved_archive/_rejected")
    args = parser.parse_args(argv)
    issues = lint_paths(args.paths, include_archive=args.include_archive)
    sys.stdout.write(format_markdown(issues))
    return 1 if has_errors(issues) else 0

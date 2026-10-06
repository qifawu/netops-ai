"""智能体工具注册（Zabbix / 设备 / 拓扑 / SOP / 巡检 / 历史 / 文档检索）和工具返回的统一包装。

告警分析链路、剧本 lint、对话都用这里的 `build_chat_tools`——这部分是公开的。
多轮对话的服务层（历史、会话焦点、对话预算和提示词）不在这里。
"""


from __future__ import annotations


from dataclasses import replace


from functools import partial


import argparse


import json


import re


import time


from typing import Annotated, Any, Union


from langchain_core.messages import AIMessage, HumanMessage


from langchain_core.tools import StructuredTool


from pydantic import AfterValidator, Field, create_model


from netops_ai.analysis.schema import HYPOTHESIS_CATEGORIES


from netops_ai.devices.ssh import SSHDeviceAdapter


from netops_ai.devices.telnet import TelnetDeviceAdapter


from netops_ai.graph.agent_loop import (
    AgentLoopBudget,
    DEFAULT_SYSTEM_PROMPT,
    ChatRunTrace,
    ToolCallRecord,
    REPO_ROOT,
    message_text,
    run_agent_loop,
    save_trace,
)




from netops_ai.graph.tool_names import (
    DEVICE_SHOW_MANY_TOOL_NAME,
    DEVICE_SHOW_TOOL_NAME,
    RUN_INSPECTION_TOOL_NAME,
    zabbix_tool_name,
)


from netops_ai.llm.factory import env


from netops_ai.playbooks.lookup import sop_lookup




from netops_ai.topology import known_device_labels, load_topology, resolve_device, topology_neighbors


try:
    from netops_ai import netbox_cli as nb_cli  # NetBox 台账工具
except ImportError:  # 开源版：只有 topology_neighbors（本地 yaml）
    from netops_ai import topology_cli as nb_cli


from netops_ai.zabbix import cli as zbx_cli


_save_trace = save_trace


_message_text = message_text


DEVICE_BATCH_MAX_COMMANDS = 8


def _safe_short_json(value: Any, *, max_text: int = 900) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= max_text else text[:max_text] + "...[truncated]"


def _namespace_for(spec: zbx_cli.CommandSpec, kwargs: dict[str, Any]) -> argparse.Namespace:
    values = {p.name: p.default for p in spec.params}
    values.update(kwargs)
    values.update({"format": "json", "jq": "", "dry_run": False})
    return argparse.Namespace(**values)


def _args_for_spec(spec: zbx_cli.CommandSpec, kwargs: dict[str, Any]) -> dict[str, Any]:
    values = {p.name: p.default for p in spec.params}
    values.update(kwargs)
    return values


#: schema 写 `{"type": ["integer", "string"]}` 的参数（Unix 秒、itemid）：两种都收，进 handler 前转成字符串，
#: handler 那侧照旧按字符串处理。见 `zbx_cli.ID_OR_TIME_SCHEMA`。
_INT_OR_STR = Annotated[Union[int, str], AfterValidator(str)]


def _model_for_param(param: zbx_cli.ParamSpec):
    kind = param.schema.get("type")
    if isinstance(kind, list) and set(kind) == {"integer", "string"}:
        typ: Any = _INT_OR_STR
    else:
        typ = int if kind == "integer" else str
    default = ... if param.required else param.default
    return (typ, Field(default, description=param.help))


#: 一次工具返回喂给模型的字符上限。
#:
#: **原先没有上限。** `_json_safe(max_text=6000)` 只用在落盘那一侧
#: （`agent_loop.py` 写 trace 的时候），返回给模型的是裸 `json.dumps(data)`——
#: 一条 `show running-config` 或者一把历史点就能把上下文顶穿，
#: 而 token 预算要等这一轮花完才发现。
#:
#: 8000 字符约等于 2~4k token，一次工具返回占到这个量级已经该收窄问题了。
TOOL_RESULT_MAX_CHARS = 8000


def _tool_reply(data: Any, *, query: dict[str, Any] | None = None) -> str:
    """所有工具的返回都从这里出去。做两件模型自己看不出来的事：

 1. **空结果显式说是空的，并回显查询参数。** 拿到 `[]` 和拿到「查了但没有」，
 模型区分不了。
 2. **超长截断，并且说明被截了。** 不说的话模型会把半截数据当成全部。

 **只陈述事实，不给改法**（维护者：「工具需要保持中性」）。原来空结果带
 `try_instead`、截断带「把查询条件收窄」——那是在工具返回里做路由，
 跟提示词/SOP 的停止条件打过架（外部 agent 反馈第 3 条）。怎么查只写在提示词或 SOP。

 **注意这里只认结构上的空**（`[]` / `{}` / `None`）。
 「成功返回一份全零的榜单」这种语义上的空，通用层判不出来，
 得那个工具自己修——见 里 `zbx_top_talkers` 那条。
    """
    if data is None or (isinstance(data, (list, dict, str)) and len(data) == 0):
        payload: dict[str, Any] = {"empty": True, "note": "没有数据：这组查询条件下返回为空。"}
        if query:
            payload["query"] = query
        return json.dumps(payload, ensure_ascii=False, default=str)
    text = json.dumps(data, ensure_ascii=False)
    if len(text) <= TOOL_RESULT_MAX_CHARS:
        return text
    return json.dumps(
        {
            "truncated": True,
            "note": (
                f"结果共 {len(text)} 字符，head 只包含前 {TOOL_RESULT_MAX_CHARS} 字符，"
                "是不完整的结果。"
            ),
            "head": text[:TOOL_RESULT_MAX_CHARS],
        },
        ensure_ascii=False,
    )


def _dedup_reply(trace: ChatRunTrace, dup: Any) -> str:
    """重叠窗口的历史查询被拦下时，**把上次的结果原样带回去**。

 原来只回一句「结果已在上下文里」。外部 agent 的反馈：外部 agent（以及上下文被压缩过的
 模型）看不到那次结果，既不能引用，也没法判断是不是空的。代价是上下文变大，见报告。
    """
    previous = trace.tool_calls[dup.index - 1].result if 0 < dup.index <= len(trace.tool_calls) else None
    try:
        previous_reply: Any = json.loads(_tool_reply(previous))
    except (TypeError, ValueError):
        previous_reply = previous
    return json.dumps(
        {
            "deduped": True,
            "same_as_call": dup.index,
            "note": trace.dedup_note(dup),
            "previous_result": previous_reply,
        },
        ensure_ascii=False,
        default=str,
    )


def _append_sop_hint(reply: str, trace: ChatRunTrace, record: ToolCallRecord) -> str:
    return reply + trace.sop_hint_for_tool_result(record)


def _tools_from_specs(module, specs, trace, budget, *, name_of, cli_name: str) -> list[StructuredTool]:
    """一张 spec 表 → 一批注册给模型的工具。`zbx-cli` 和 `nb-cli` 共用这一份。

    **两套 CLI 各写一遍这个循环就是在等着它们分叉。** 下面那段注释记的三次
    事故，根因都是"同一件事写在两处、没有东西强制一致"。
    """
    tools: list[StructuredTool] = []
    for spec in specs:
        tool_name = name_of(spec)
        fields = {p.name: _model_for_param(p) for p in spec.params}
        args_schema = create_model(f"{tool_name}_args", **fields)
        handler = getattr(module, spec.handler)

        def run_tool(_spec=spec, _handler=handler, **kwargs):
            started = time.perf_counter()
            full_args = _args_for_spec(_spec, dict(kwargs))
            # 落盘标签必须就是注册给模型的名字。两处手写两个名字，出图过滤器因此坏过三次。
            record = ToolCallRecord(tool=name_of(_spec), args=full_args, ok=False)
            try:
                dup = trace.find_history_duplicate(record.tool, full_args)
                if dup is not None:
                    reply = _dedup_reply(trace, dup)
                    record.ok = True
                    record.deduped = True
                    record.result = {"deduped": True, "same_as_call": dup.index, "note": trace.dedup_note(dup)}
                    return _append_sop_hint(reply, trace, record)
                data = _handler(_namespace_for(_spec, dict(kwargs)))
                record.ok = True
                record.result = data
                return _append_sop_hint(_tool_reply(data, query=full_args), trace, record)
            except Exception as exc:  # noqa: BLE001 - 工具错误要回给模型，让它能换参数重试
                record.error = f"{type(exc).__name__}: {exc}"
                return _append_sop_hint(json.dumps({"error": record.error}, ensure_ascii=False), trace, record)
            finally:
                record.elapsed_ms = int((time.perf_counter() - started) * 1000)
                trace.record_tool_call(record, budget)

        tools.append(
            StructuredTool.from_function(
                func=run_tool,
                name=tool_name,
                description=spec.description or f"{spec.help}。对应 {cli_name} {spec.name}，只读风险级别 read-only。",
                args_schema=args_schema,
            )
        )
    return tools


def build_zabbix_tools(trace: ChatRunTrace, budget: AgentLoopBudget | None = None) -> list[StructuredTool]:
    return _tools_from_specs(zbx_cli, zbx_cli.COMMANDS, trace, budget or AgentLoopBudget(),
                             name_of=lambda spec: zabbix_tool_name(spec.name), cli_name="zbx-cli")


def build_topology_tools(trace: ChatRunTrace, budget: AgentLoopBudget | None = None) -> list[StructuredTool]:
    """NetBox 台账的三个只读工具，从 `nb-cli` 的同一张 spec 表生成。

    `neighbors` 那条的工具名是 `topology_neighbors`（写死在 spec 的 `tool_name`
    里）：SOP 引擎和剧本已经引用着这个名字，跟着 CLI 改名会**静默地**让它们查不到。
    """
    return _tools_from_specs(nb_cli, nb_cli.COMMANDS, trace, budget or AgentLoopBudget(),
                             name_of=lambda spec: spec.tool_name, cli_name="nb-cli")


def _device_adapter(device_host: str = ""):
    """连设备。默认 SSH，显式配 `DEVICE_TRANSPORT=telnet` 才用明文（跟 pipeline 同一个判断）。

    `device_host` 是参数不是全局配置：以前改进程环境变量，只能加大锁串行，改成传参后并发是白捡的。
    """
    cfg = env()
    vendor = cfg.get("DEVICE_VENDOR", "cisco")
    host = device_host or cfg.get("DEVICE_HOST", "")
    transport = cfg.get("DEVICE_TRANSPORT", "ssh").strip().lower() or "ssh"
    if transport == "telnet":
        return TelnetDeviceAdapter(
            vendor,
            host=host,
            port=int(cfg.get("DEVICE_TELNET_PORT", "23")),
            username=cfg.get("DEVICE_USERNAME", ""),
            password=cfg.get("DEVICE_PASSWORD", ""),
        )
    return SSHDeviceAdapter(
        vendor,
        host=host,
        port=int(cfg.get("DEVICE_PORT", "22")),
        username=cfg.get("DEVICE_USERNAME", ""),
        password=cfg.get("DEVICE_PASSWORD", ""),
    )


def _parse_positive_int(raw: str | None, default: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _device_batch_max_commands(cfg: dict[str, str]) -> int:
    return _parse_positive_int(cfg.get("DEVICE_BATCH_MAX_COMMANDS"), DEVICE_BATCH_MAX_COMMANDS)


def _device_show_payload(result: Any, *, vendor: str, command: str, device_host: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "command": result.command,
        "allowed": result.allowed,
        "ok": result.ok,
        "output": result.output,
        "error": result.error,
        "denial_reason": result.denial_reason,
    }
    return payload


def _resolve_target(device: str, default_host: str) -> tuple[str, str]:
    """`device` 空着就是默认那台；填了**只认拓扑里登记的设备**，不接受任意 IP。返回 (host, 错误)。

 #17：告警那台连不上时，证据只在邻居那里。
    """
    if not device.strip():
        return default_host, ""
    topo = load_topology()
    dev = resolve_device(topo, device)
    if dev is None or not dev.host:
        return "", f"拓扑里没有 {device!r}，可选：{', '.join(known_device_labels(topo))}"
    return dev.host, ""


def build_device_tools(
    trace: ChatRunTrace,
    budget: AgentLoopBudget | None = None,
    *,
    device_host: str = "",
) -> list[StructuredTool]:
    """`device_host` 空着就退回 `.env` 的 `DEVICE_HOST`（对话入口）；
    告警管道传具体那台告警设备，这样两个 incident 可以真并发。
    """
    budget = budget or AgentLoopBudget()

    def device_show(command: str, device: str = "") -> str:
        """Run one read-only network-device command through the device whitelist."""
        started = time.perf_counter()
        record = ToolCallRecord(tool="device_show", args={"command": command, "device": device}, ok=False)
        cfg = env()
        vendor = cfg.get("DEVICE_VENDOR", "cisco")
        host, err = _resolve_target(device, device_host or cfg.get("DEVICE_HOST", ""))
        try:
            if err:
                record.error = err
                return _append_sop_hint(json.dumps({"error": err}, ensure_ascii=False), trace, record)
            with _device_adapter(host) as adapter:
                result = adapter.run(command)
            payload = _device_show_payload(result, vendor=vendor, command=command, device_host=host)
            # 审查发现（真实数据坐实：一条真实告警，`show logging`/`show running-config`
            # 被拒后设备回显 "Line has invalid autocommand"，但这里以前只看 allowed，trace 记成了
            # ok=true）：allowed 只说"有没有发给设备"，ok 才是"设备是不是真的执行成功"——
            # 白名单放行但设备端报错，不该在 trace 里显得跟正常拿到材料一样。
            record.ok = bool(payload.get("allowed")) and bool(payload.get("ok"))
            record.result = payload
            return _append_sop_hint(_tool_reply(payload), trace, record)
        except Exception as exc:  # noqa: BLE001 - agent 工具不能炸穿 HTTP 请求
            record.error = f"{type(exc).__name__}: {exc}"
            return _append_sop_hint(json.dumps({"error": record.error}, ensure_ascii=False), trace, record)
        finally:
            record.elapsed_ms = int((time.perf_counter() - started) * 1000)
            trace.record_tool_call(record, budget)

    def device_show_many(commands: list[str], device: str = "") -> str:
        """Run multiple read-only network-device commands through the device whitelist."""
        started = time.perf_counter()
        record = ToolCallRecord(tool="device_show_many", args={"commands": commands, "device": device}, ok=False)
        cfg = env()
        vendor = cfg.get("DEVICE_VENDOR", "cisco")
        host, err = _resolve_target(device, device_host or cfg.get("DEVICE_HOST", ""))
        if err:
            record.error = err
            trace.record_tool_call(record, budget)
            return _append_sop_hint(json.dumps({"error": err}, ensure_ascii=False), trace, record)
        max_commands = _device_batch_max_commands(cfg)
        received_count = len(commands)
        selected_commands = list(commands[:max_commands])
        payload: dict[str, Any] = {
            "results": [],
            "truncated": received_count > len(selected_commands),
            "received_count": received_count,
            "executed_count": len(selected_commands),
            "max_commands": max_commands,
        }
        try:
            with _device_adapter(host) as adapter:
                for command in selected_commands:
                    result = adapter.run(command)
                    payload["results"].append(
                        _device_show_payload(result, vendor=vendor, command=command, device_host=host)
                    )
            # 审查发现（同一处 bug 的批量版）：以前不管每条命令是否被拒/设备是否执行成功，
            # 只要循环没抛异常就记 ok=True。改成「这批命令是否全部允许且全部执行成功」，
            # 部分失败时如实反映，不再看着跟全部拿到材料一样。
            record.ok = bool(payload["results"]) and all(
                bool(r.get("allowed")) and bool(r.get("ok")) for r in payload["results"]
            )
            record.result = payload
            return _append_sop_hint(json.dumps(payload, ensure_ascii=False), trace, record)
        except Exception as exc:  # noqa: BLE001 - agent 工具不能炸穿 HTTP 请求
            record.error = f"{type(exc).__name__}: {exc}"
            return _append_sop_hint(json.dumps({"error": record.error}, ensure_ascii=False), trace, record)
        finally:
            record.elapsed_ms = int((time.perf_counter() - started) * 1000)
            trace.record_tool_call(record, budget)

    return [
        StructuredTool.from_function(
            func=device_show,
            name=DEVICE_SHOW_TOOL_NAME,
            description=(
                "登录网络设备执行一条只读命令（show/display 这类），返回设备在执行时刻的原始输出。"
                "命令先经过 netops_ai.devices.whitelist 校验，白名单外的命令不会发给设备，"
                "返回 allowed=false 和 denial_reason。"
                "\n\n参数：command 是完整命令（如 show interfaces GigabitEthernet0/1、"
                "more system:running-config）；device 为空时连本次告警那台设备（对话里是 DEVICE_HOST），"
                "填拓扑里登记的设备名（如 V1、D2）时连那台，不接受任意 IP，名字不在拓扑里时返回可选设备名。"
                "\n\n返回：{command, allowed, ok, output, error, denial_reason}；"
                f"结果超过 {TOOL_RESULT_MAX_CHARS} 字符时截断并注明总长度。"
                "\n\n数据特性：输出反映的是执行这条命令那一刻的状态。"
                "show logging 读的是设备本地日志 buffer，容量有限（常见只有几 KB），新日志会覆盖旧日志；"
                "本工具每次登录/登出都会以只读账号（ai-readonly）在设备日志里留下记录，"
                "这些记录由取证动作本身产生。"
                "\n只读账号（ai-readonly，privilege 5）下，show running-config / show startup-config 的输出只有 "
                "`Current configuration : 5 bytes` 和 `end`（设备侧的渲染限制）；show running-config view full "
                "（设备上已配 `privilege exec all level 5 show running-config view full`）和 more system:running-config "
                "的输出是完整运行配置，含口令哈希、SNMP 社区名等敏感字段，前者可接 `| section` 等只读过滤。"
            ),
        ),
        StructuredTool.from_function(
            func=device_show_many,
            name=DEVICE_SHOW_MANY_TOOL_NAME,
            description=(
                "在同一个设备会话里依次执行多条只读命令；每条命令单独经过 "
                "netops_ai.devices.whitelist 校验，各自返回 {command, allowed, ok, output, error, denial_reason}。"
                f"一次最多执行 DEVICE_BATCH_MAX_COMMANDS 条（默认 {DEVICE_BATCH_MAX_COMMANDS}），"
                "超出的部分不执行，返回里 truncated=true，并给出 received_count / executed_count / max_commands。"
                "device 参数与 device_show 相同：为空是本次告警那台，填拓扑里登记的设备名就连那台。"
                "数据特性与 device_show 相同：输出是执行时刻的状态；show logging 读的是容量有限、会被覆盖的"
                "本地日志 buffer；每次登录/登出都会以只读账号（ai-readonly）在设备日志里留下记录，"
                "这些记录由取证动作本身产生。"
            ),
        ),
    ]


def build_sop_tools(
    trace: ChatRunTrace,
    budget: AgentLoopBudget | None = None,
    *,
    alert_clock: int | float | None = None,
) -> list[StructuredTool]:
    budget = budget or AgentLoopBudget()

    def sop_lookup_tool(alert_name: str, tags: list[str] = []) -> str:  # noqa: B006 - mirrors the public tool contract
        """Look up the team's SOP suggestions for an alert without executing them."""
        started = time.perf_counter()
        record = ToolCallRecord(
            tool="sop_lookup",
            args={"alert_name": alert_name, "tags": tags, "alert_clock": alert_clock},
            ok=False,
        )
        try:
            data = sop_lookup(alert_name, tags, alert_clock=alert_clock)
            record.ok = True
            record.result = data
            return json.dumps(data, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - 工具错误要回给模型，让它能诚实说明
            record.error = f"{type(exc).__name__}: {exc}"
            return json.dumps({"error": record.error}, ensure_ascii=False)
        finally:
            record.elapsed_ms = int((time.perf_counter() - started) * 1000)
            trace.record_tool_call(record, budget)

    return [
        StructuredTool.from_function(
            func=sop_lookup_tool,
            name="sop_lookup",
            description=(
                "按告警名和可选标签（如 component=network）匹配团队 SOP。"
                "返回 matched、命中的 playbook、适用范围、limits，以及每个步骤的 "
                "id/why/expect/action（工具名和参数）/branches/provenance；"
                "同时命中多条时 ambiguous=true 并附其它候选。只读查询 SOP 文件，"
                "不执行任何设备或 Zabbix 动作。"
            ),
        )
    ]


def _message_text(message: Any) -> str:
    return message_text(message)


_CONFIDENCE_LABEL = {"high": "高", "medium": "中", "low": "低"}


def _analyses_markdown(rows: list[dict]) -> str:
    """把历史分析结论排成一张表。空的时候明确说空，不要返回空串
    ——空串会让模型以为工具失败了，转头自己编一段。"""
    if not rows:
        return "**还没有分析记录。**"
    lines = [
        f"**最近 {len(rows)} 条分析结论**",
        "",
        "| eventid | 置信度 | 根因 | 完成时间 |",
        "|---|---|---|---|",
    ]
    for row in rows:
        confidence = _CONFIDENCE_LABEL.get(str(row.get("confidence", "")), str(row.get("confidence", "")) or "—")
        if row.get("has_undistinguishable"):
            confidence += "（有判不出的候选）"
        cause = " ".join(str(row.get("root_cause") or "（无结论）").split())
        if len(cause) > 60:
            cause = cause[:60] + "…"
        cause = cause.replace("|", "\\|")
        finished = str(row.get("finished_at") or "")[5:16].replace("T", " ")
        lines.append(f"| `{row.get('eventid', '')}` | {confidence} | {cause} | {finished} |")
    return "\n".join(lines)


def build_inspection_tools(trace: ChatRunTrace, budget: AgentLoopBudget | None = None) -> list[StructuredTool]:
    """巡检工具。**默认读最近一次的结果，不真扫。**

    真扫一次要遍历两千多个监控项、拉每项的历史，几分钟起步。对话里默认读缓存
    （秒级返回），要最新的再显式 `rescan=True`——让模型自己决定，比替它决定好。
    """
    budget = budget or AgentLoopBudget()

    def run_inspection_tool(rescan: bool = False, lookback_days: int = 7) -> str:
        from netops_ai.api import dashboard
        from netops_ai.inspection.report import render_inspection_markdown

        started = time.perf_counter()
        record = ToolCallRecord(
            tool=RUN_INSPECTION_TOOL_NAME, args={"rescan": rescan, "lookback_days": lookback_days}, ok=False
        )
        try:
            if rescan:
                payload = dashboard.run_inspection_and_persist(lookback_seconds=int(lookback_days) * 86400)
                source = "刚扫的"
            else:
                payload = dashboard.load_latest_inspection()
                source = "最近一次巡检的缓存"
                if payload is None:
                    record.error = "还没有巡检结果，要跑一次得传 rescan=true"
                    return json.dumps({"error": record.error}, ensure_ascii=False)
            out = {
                "source": source,
                "scanned_hosts": payload.get("scanned_hosts", 0),
                "finding_count": len(payload.get("findings") or []),
                "markdown": render_inspection_markdown(payload),
            }
            record.ok = True
            record.result = {k: v for k, v in out.items() if k != "markdown"}
            return json.dumps(out, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - agent 工具不能炸穿 HTTP 请求
            record.error = f"{type(exc).__name__}: {exc}"
            return json.dumps({"error": record.error}, ensure_ascii=False)
        finally:
            record.elapsed_ms = int((time.perf_counter() - started) * 1000)
            trace.record_tool_call(record, budget)

    return [
        StructuredTool.from_function(
            func=run_inspection_tool,
            name=RUN_INSPECTION_TOOL_NAME,
            description=(
                "只读巡检：扫全网数值监控项，找出还没触发告警的趋势"
                "（持续单向变化、周期性冲高、反复抖动又自愈）。"
                "rescan=false（默认）读取最近一次巡检的缓存结果，秒级返回；"
                "rescan=true 重新扫描全网，耗时数分钟。lookback_days 是扫描回看天数，默认 7。"
                "返回 source、scanned_hosts、finding_count 和已排好版的 markdown 报告。"
            ),
        )
    ]


def build_history_tools(trace: ChatRunTrace, budget: AgentLoopBudget | None = None) -> list[StructuredTool]:
    """查本系统产出的历史分析结论，让「之前那条 AI 怎么判的」在对话里就能问。"""
    budget = budget or AgentLoopBudget()

    def _record(tool: str, args: dict):
        return ToolCallRecord(tool=tool, args=args, ok=False)

    def list_analyses(limit: int = 10, keyword: str = "") -> str:
        """列最近的告警分析结论（摘要，不含完整推理过程）。"""
        started = time.perf_counter()
        record = _record("list_analyses", {"limit": limit, "keyword": keyword})
        try:
            from netops_ai.api.dashboard import list_records

            rows = list_records()
            if keyword:
                needle = keyword.lower()
                rows = [r for r in rows if needle in json.dumps(r, ensure_ascii=False).lower()]
            rows = rows[: max(1, min(int(limit or 10), 50))]
            data = {
                "count": len(rows),
                "records": rows,
                # 排版是确定性的，不该交给模型：既不稳定，还要额外的 token。
                "markdown": _analyses_markdown(rows),
                "note": "这是摘要，不含证据链、根因方向表态和工具轨迹。",
            }
            record.ok = True
            record.result = data
            return json.dumps(data, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            record.error = f"{type(exc).__name__}: {exc}"
            return json.dumps({"error": record.error}, ensure_ascii=False)
        finally:
            record.elapsed_ms = int((time.perf_counter() - started) * 1000)
            trace.record_tool_call(record, budget)

    def get_analysis(eventid: int | str) -> str:
        """取某条告警的完整分析记录：证据链、各根因方向的表态、工具轨迹、指纹。"""
        # eventid 是数字：整数和数字字符串都收（跟 zbx_* 的 itemid/Unix 秒同一类问题）
        eventid = str(eventid).strip()
        started = time.perf_counter()
        record = _record("get_analysis", {"eventid": eventid})
        try:
            from netops_ai.api.dashboard import load_record

            data = load_record(str(eventid))
            if data is None:
                from netops_ai.api.dashboard import list_records

                # 查不到就把可选项摆出来，让它能自纠——跟 topology 查空同一种解法。
                known = [r["eventid"] for r in list_records()[:20]]
                data = {"found": False, "eventid": eventid, "known_eventids": known}
            else:
                record.ok = True
            record.result = {"eventid": eventid, "found": record.ok}
            return json.dumps(data, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            record.error = f"{type(exc).__name__}: {exc}"
            return json.dumps({"error": record.error}, ensure_ascii=False)
        finally:
            record.elapsed_ms = int((time.perf_counter() - started) * 1000)
            trace.record_tool_call(record, budget)

    return [
        StructuredTool.from_function(
            func=list_analyses,
            name="list_analyses",
            description=(
                "列本系统最近处理过的告警和 AI 给出的结论摘要"
                "（eventid、置信度、根因、完成时间，附排好版的 markdown 表）。"
                "keyword 按设备名/根因关键字过滤；limit 默认 10，最多 50。"
            ),
        ),
        StructuredTool.from_function(
            func=get_analysis,
            name="get_analysis",
            description=(
                "按 eventid 返回某条告警的完整分析记录：逐字证据链、各根因方向"
                f"（共 {len(HYPOTHESIS_CATEGORIES)} 类）的表态、工具调用轨迹、incident 指纹。"
                "eventid 不存在时返回 found=false 和最近 20 条记录的 eventid。"
            ),
        ),
    ]




def build_chat_tools(
    trace: ChatRunTrace,
    budget: AgentLoopBudget,
    *,
    device_host: str = "",
    alert_clock: int | float | None = None,
    include_doc_search: bool | None = None,
) -> list[StructuredTool]:
    """告警管道用 `functools.partial(build_chat_tools, device_host=...)`
    把本次要连的设备绑进来，`run_agent_loop` 的签名一个字都不用改。
    """
    tools = [
        *build_zabbix_tools(trace, budget),
        *build_device_tools(trace, budget, device_host=device_host),
        *build_sop_tools(trace, budget, alert_clock=alert_clock),
        *build_topology_tools(trace, budget),
        *build_inspection_tools(trace, budget),
        *build_history_tools(trace, budget),
    ]
    return tools

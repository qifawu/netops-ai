"""把 Zabbix 历史数据和设备回显组织成一条明确的时间线。

这一层不取数，只负责把上层已经拿到的文本/结构化片段整理给模型看：
Zabbix 的历史点放在故障前后，设备 show/display 回显如果没有自身日志时间戳，
一律放进「取数时刻」快照区，并把距故障多久写出来。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


_CLOCK_RE = re.compile(r'"clock"\s*:\s*"(\d{9,12})"|clock\s+(\d{9,12})')
_FULL_TIME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
_LINE_TIME_RE = re.compile(r"(?<!\d)(\d{2}:\d{2}(?::\d{2})?)(?!\d)")
_IOS_LOG_RE = re.compile(
    r"^\*?(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+"
    r"(?P<hms>\d{2}:\d{2}:\d{2})(?:\.\d+)?:\s+(?P<body>.*)$"
)
#: rsyslog 落盘的行：`Sep 21 10:33:31 192.0.2.52 80: *Sep 21 10:33:30.896: %SYS-5-...`
#: 前半截是 syslog 主机的**接收**时间，`*` 后面才是设备自己打的时间戳。
#: 取设备那个——它才是事件真正发生的时刻。
_RSYSLOG_DEVICE_STAMP_RE = re.compile(
    r"\*(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+"
    r"(?P<hms>\d{2}:\d{2}:\d{2})(?:\.\d+)?:\s+(?P<body>%.*)$"
)
#: **任何** `##`~`####` 标题都开一个块，块名就是标题文字。
#: syslog 那一段就是因为"标题里没有反引号包着的命令"被整段静默丢掉的；
#: 当时的修法是给 syslog 单开一个特例，但下一段新内容（配置变更）马上又会撞同一个坑。
#: 所以改成通用规则：**不认识的标题也不许丢**，按标题建块，交给下游决定怎么呈现。
_ANY_HEADING_RE = re.compile(r"^#{2,4}\s+(.+?)\s*$")
_COMMAND_HEADING_RE = re.compile(r"^#{2,4}\s+`([^`]+)`.*$")
_FENCE_RE = re.compile(r"^```")


@dataclass(frozen=True)
class TimelineEvent:
    epoch: float | None
    source: str
    text: str


@dataclass(frozen=True)
class DeviceBlock:
    command: str
    output: str
    collected_epoch: float | None = None


def _dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _format_dt(epoch: float) -> str:
    return _dt(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _format_time(epoch: float) -> str:
    return _dt(epoch).strftime("%H:%M:%S")


def _format_delta(seconds: float) -> str:
    seconds = abs(int(round(seconds)))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes or not parts:
        parts.append(f"{minutes} 分")
    return " ".join(parts)


def infer_fault_epoch(zabbix_text: str) -> float | None:
    """从 Zabbix 文本里推断故障时刻，优先使用 problem.get 的 clock。"""
    clocks: list[int] = []
    for match in _CLOCK_RE.finditer(zabbix_text):
        raw = match.group(1) or match.group(2)
        try:
            clocks.append(int(raw))
        except (TypeError, ValueError):
            pass
    if clocks:
        return float(clocks[0])

    m = _FULL_TIME_RE.search(zabbix_text)
    if not m:
        return None
    try:
        return datetime.fromisoformat(f"{m.group(1)} {m.group(2)}").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _parse_hhmm_epoch(value: str, fault_epoch: float) -> float | None:
    date = _dt(fault_epoch).date()
    if len(value) == 5:
        value = f"{value}:00"
    try:
        parsed = datetime.fromisoformat(f"{date.isoformat()} {value}").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    candidates = [parsed.timestamp() - 86400, parsed.timestamp(), parsed.timestamp() + 86400]
    return min(candidates, key=lambda ts: abs(ts - fault_epoch))


def _parse_ios_log_epoch(line: str, fault_epoch: float) -> float | None:
    m = _IOS_LOG_RE.match(line.strip()) or _RSYSLOG_DEVICE_STAMP_RE.search(line)
    if not m:
        return None
    hint = _dt(fault_epoch)
    best: float | None = None
    best_diff: float | None = None
    for year in (hint.year - 1, hint.year, hint.year + 1):
        try:
            candidate = datetime.strptime(
                f"{year} {m.group('mon')} {m.group('day')} {m.group('hms')}",
                "%Y %b %d %H:%M:%S",
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        diff = abs(candidate.timestamp() - fault_epoch)
        if best_diff is None or diff < best_diff:
            best = candidate.timestamp()
            best_diff = diff
    return best


def _extract_zabbix_events(zabbix_text: str, fault_epoch: float | None) -> list[TimelineEvent]:
    if fault_epoch is None:
        return [TimelineEvent(None, "zabbix", zabbix_text.strip())] if zabbix_text.strip() else []
    events: list[TimelineEvent] = []
    for raw in zabbix_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        time_match = _LINE_TIME_RE.search(line)
        if time_match:
            epoch = _parse_hhmm_epoch(time_match.group(1), fault_epoch)
            events.append(TimelineEvent(epoch, "zabbix", line))
    return events


#: 这两个块名就是 pipeline 写进 `device_text` 的标题文字，改一边要改另一边。
#: syslog 块按时间戳当日志事件进时间线；配置变更块单独成节，**不当快照**。
SYSLOG_BLOCK = "故障窗口 syslog"
CONFIG_DIFF_BLOCK = "配置变更（前后对比）"


def parse_device_blocks(device_text: str, *, collected_epoch: float | None = None) -> list[DeviceBlock]:
    """从现有 markdown 设备侧文本里拆出每条 show/display 命令的回显块。"""
    blocks: list[DeviceBlock] = []
    current_command: str | None = None
    current_lines: list[str] = []
    in_fence = False

    def flush() -> None:
        nonlocal current_command, current_lines
        if current_command and current_lines:
            blocks.append(DeviceBlock(current_command, "\n".join(current_lines).strip(), collected_epoch))
        current_command = None
        current_lines = []

    for raw in device_text.splitlines():
        # 命令标题（`` ### `show ...` ``）取反引号里的命令；其他标题取整句标题文字。
        # **syslog 那一段以前在这里被静默丢掉**：标题没有反引号，不匹配命令标题，
        # `current_command` 是 None，每一行都被跳过；末尾的"原文兜底"又只在一个命令块
        # 都没切出来时才补。实测铁证在记录里、不在研判 prompt 里，
        # 模型说"缺少故障时刻的设备日志"是实话。
        heading = _COMMAND_HEADING_RE.match(raw.strip()) or _ANY_HEADING_RE.match(raw.strip())
        if heading:
            flush()
            current_command = heading.group(1)
            continue
        if current_command is None:
            continue
        if _FENCE_RE.match(raw.strip()):
            in_fence = not in_fence
            continue
        if in_fence:
            current_lines.append(raw.rstrip())
    flush()

    if not blocks and device_text.strip():
        blocks.append(DeviceBlock("设备侧原文", device_text.strip(), collected_epoch))
    return blocks


def _structured_history_events(zabbix_history: list[dict[str, Any]]) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    for item in zabbix_history:
        raw_clock = item.get("clock")
        try:
            epoch = float(raw_clock)
        except (TypeError, ValueError):
            epoch = None
        name = item.get("name") or item.get("item") or item.get("key") or "Zabbix history"
        value = item.get("value", "")
        events.append(TimelineEvent(epoch, "zabbix", f"{name} = {value}"))
    return events


def _snapshot_line(block: DeviceBlock) -> str:
    first = next((ln.strip() for ln in block.output.splitlines() if ln.strip()), "")
    if not first:
        first = "（无输出）"
    return f"{block.command}  ->  {first}"


def build_timeline_text(
    zabbix_text: str = "",
    device_text: str | None = None,
    *,
    zabbix_history: list[dict[str, Any]] | None = None,
    device_blocks: list[DeviceBlock | dict[str, Any]] | None = None,
    fault_time_epoch: float | None = None,
    device_collected_epoch: float | None = None,
    surrounding_seconds: int = 10 * 60,
) -> str:
    """生成喂给模型的统一时间线文本。

    `zabbix_history` 支持结构化历史点；`zabbix_text`/`device_text` 保持对现有
    spike markdown 输入的兼容。设备块没有日志自身时间戳时，统一作为取数快照。
    """
    fault_epoch = fault_time_epoch if fault_time_epoch is not None else infer_fault_epoch(zabbix_text)
    zabbix_events = _structured_history_events(zabbix_history or [])
    zabbix_events.extend(_extract_zabbix_events(zabbix_text, fault_epoch))

    blocks: list[DeviceBlock] = []
    if device_blocks:
        for block in device_blocks:
            if isinstance(block, DeviceBlock):
                blocks.append(block)
            else:
                blocks.append(
                    DeviceBlock(
                        str(block.get("command") or "设备侧原文"),
                        str(block.get("output") or ""),
                        float(block["collected_epoch"]) if block.get("collected_epoch") is not None else device_collected_epoch,
                    )
                )
    elif device_text is not None:
        blocks = parse_device_blocks(device_text, collected_epoch=device_collected_epoch)

    # 配置变更块先挑出来：它既不是某一刻的快照，也不是带时间戳的日志行，
    # 是**两次拉取之间**的差异，得单独呈现，不然会被贴上"说明不了故障当时的状态"。
    config_blocks = [b for b in blocks if b.command == CONFIG_DIFF_BLOCK]
    blocks = [b for b in blocks if b.command != CONFIG_DIFF_BLOCK]

    device_log_events: list[TimelineEvent] = []
    snapshot_blocks: list[DeviceBlock] = []
    if fault_epoch is not None:
        for block in blocks:
            log_lines = []
            non_log_lines = []
            for line in block.output.splitlines():
                epoch = _parse_ios_log_epoch(line, fault_epoch)
                if epoch is None:
                    non_log_lines.append(line)
                else:
                    log_lines.append(TimelineEvent(epoch, "device", line.strip()))
            if log_lines and (
                block.command == SYSLOG_BLOCK
                or block.command.lower().startswith(("show logging", "display logbuffer"))
            ):
                device_log_events.extend(log_lines)
            else:
                snapshot_blocks.append(block)
    else:
        snapshot_blocks = blocks

    lines: list[str] = []
    if fault_epoch is not None:
        lines.append(f"故障时刻：{_format_dt(fault_epoch)}")
    else:
        lines.append("故障时刻：未从 Zabbix 数据中解析到")

    before = [e for e in zabbix_events if e.epoch is not None and fault_epoch is not None and e.epoch < fault_epoch]
    near = [
        e
        for e in [*zabbix_events, *device_log_events]
        if e.epoch is not None and fault_epoch is not None and abs(e.epoch - fault_epoch) <= surrounding_seconds
    ]
    after = [e for e in zabbix_events if e.epoch is not None and fault_epoch is not None and e.epoch > fault_epoch + surrounding_seconds]

    lines.append("")
    lines.append("【故障前】")
    if before:
        for event in sorted(before, key=lambda e: e.epoch or 0)[-12:]:
            lines.append(f"  {_format_time(event.epoch or 0)}  [{event.source}] {event.text}")
    else:
        lines.append("  （未解析到故障前的带时间数据）")

    lines.append("")
    lines.append("【故障时刻附近】")
    if near:
        for event in sorted(near, key=lambda e: e.epoch or 0):
            lines.append(f"  {_format_time(event.epoch or 0)}  [{event.source}] {event.text}")
    else:
        lines.append("  （未解析到故障窗口附近的带时间数据）")

    if after:
        lines.append("")
        lines.append("【故障后】")
        for event in sorted(after, key=lambda e: e.epoch or 0)[:8]:
            lines.append(f"  {_format_time(event.epoch or 0)}  [{event.source}] {event.text}")

    lines.append("")
    if snapshot_blocks:
        grouped: dict[float | None, list[DeviceBlock]] = {}
        for block in snapshot_blocks:
            grouped.setdefault(block.collected_epoch, []).append(block)
        for collected_epoch, grouped_blocks in grouped.items():
            if collected_epoch is not None and fault_epoch is not None:
                delta = collected_epoch - fault_epoch
                relation = "距故障" if delta >= 0 else "早于故障"
                lines.append(f"【取数时刻 {_format_time(collected_epoch)} -- {relation} {_format_delta(delta)}】")
            elif collected_epoch is not None:
                lines.append(f"【取数时刻 {_format_dt(collected_epoch)}】")
            else:
                lines.append("【取数时刻：未提供】")
            for block in grouped_blocks:
                lines.append(f"  {_snapshot_line(block)}")
            lines.append("  ⚠ 这是取数时刻的设备快照，说明不了故障当时的状态；只能证明取数时刻看到的状态。")
            lines.append("")
    else:
        lines.append("【设备侧取数】")
        lines.append("  （这次没有设备侧数据，只能根据 Zabbix 时间线分析。）")

    for block in config_blocks:
        lines.append("")
        lines.append("【配置变更（前后对比）】")
        lines.append(block.output.strip())

    if zabbix_text.strip():
        lines.append("")
        lines.append("【Zabbix 原始摘录】")
        lines.append(zabbix_text.strip())
    if device_text and not snapshot_blocks and not device_log_events:
        lines.append("")
        lines.append("【设备侧原始摘录】")
        lines.append(device_text.strip())
    return "\n".join(lines).rstrip() + "\n"



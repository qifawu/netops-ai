"""告警的后台链路。webhook 只管收、立刻 200（`app.py`），活在这里干。

1. `process_alert()`：拉这条告警本身（含触发它的那个监控项），按 hostid 进攒批窗口
2. `_flush_incident_window()`：窗口到期后，把攒到的告警交给 `run_agent_loop`——
   **取数和下结论在同一条轨迹里做完**：循环里 agent 自己决定查什么，循环跑完
   在同一份 messages 上再走一次带 strict schema 的调用出结构化结论。
   然后逐字核对 + 业务规则，按 `grouping` 存 incident、发卡，
   每条 eventid 各落一份 `records/alert-<id>.json`
"""

from __future__ import annotations

import json
import re
import os
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from netops_ai.analysis.analyzer import _with_derived_confidence
from netops_ai.analysis.schema import HYPOTHESIS_CATEGORIES, ROLE_ROOT, normalize_enum
from netops_ai.devices.base import CommandResult
from netops_ai.devices.ssh import SSHDeviceAdapter
from netops_ai.devices.telnet import TelnetDeviceAdapter
from netops_ai.feishu.card import AlertGroup
from netops_ai.feishu.report import report_alert_group
from netops_ai.graph.agent_loop import AgentLoopBudget, _json_safe, run_agent_loop, sop_action_text, sop_branches_text
from netops_ai.graph.chat_agent import build_chat_tools
from netops_ai.playbooks.lookup import BRANCH_SEMANTICS
from netops_ai.incident import Incident, compute_fingerprint, make_incident_id, save_incident, same_fingerprint_count
from netops_ai.llm.meter import CURRENT_EVENTID
from netops_ai.llm.client import LLMError
from netops_ai.zabbix.client import ZabbixAPIError, ZabbixClient, parse_line_receive_time

REPO_ROOT = Path(__file__).resolve().parents[2]
RECORDS_DIR = REPO_ROOT / "records"
PLAYBOOKS_DIR = REPO_ROOT / "playbooks"

#: Zabbix 侧上下文拉最近这么长时间窗口的历史（秒）
CONTEXT_WINDOW_SECONDS = 15 * 60

#: **抖动检测。** 同一个触发器在这段时间里反复 problem/resolved 就算抖动。
#: 业界降噪的四个确定性控制之一（exact dedup / **flap control** /
#: maintenance suppression / topology aggregation），我们原来一个都没有这条。
#:
#: **只检测，不抑制**，理由：在这个 lab 里抖动本身就是要判的故障形态
#: （内部文档 #4 接口抖动、#27 几十秒自愈），抑制了就永远查不出来；
#: 降噪由攒批窗口和指纹去重承担。要不要改成「第二次起复用上次结论、不再跑模型」
#: ——等有真实抖动样本再定阈值，别先加分支。
FLAP_WINDOW_SECONDS = 15 * 60
FLAP_MIN_EVENTS = 3
#: 日志型监控项在故障窗口内最多向 Zabbix 取多少条原始记录（不是最终塞进第一信源的行数——
#: 那一步已经按 line_time 是否落在 [time_from, time_till] 窗口内做语义过滤，过滤不到数量上限）。
#: 这个数值要大：Zabbix history.get 的 limit 是按 clock 降序在服务端截断的，发生在我们解析
#: line_time 之前——补发积压量一旦超过这个数，真正落在故障窗口内、clock 较早的证据会在
#: 到达我们代码之前就被截没（finding #4/5，理论边界，暂无真实样本坐实）。40 起初是按
#: "数值项 6 个点够看趋势，日志少一条就可能少掉根因那条"定的，那个理由仍然成立，只是没考虑
#: 补发积压场景，抬高到 500 留安全余量。
LOG_HISTORY_LIMIT = 500
#: 单条监控项历史最多多长、整段最多多少行。
#: 真实撞到（一条真实告警记录）：EVE-NG 宿主机那台的文件系统发现项
#: 单行 2281 字符、797 行，一条告警的上下文 21.6 万字符，52 条候选一起送进研判直接被模型
#: 以「输入超长」400 掉，整个 incident 一个结论都没有。
HISTORY_LINE_MAX_CHARS = 300
HISTORY_LINES_MAX = 80

#: 按 lab 实测 timer 校准（内部文档）：最长的级联是 BGP，
#: 180s hold time + 30s 轮询 = 210s，加 10s 余量；硬上限 300s。
DEFAULT_ALERT_WINDOW_SECONDS = 220
DEFAULT_ALERT_WINDOW_MAX_SECONDS = 300

#: **静默早停。** 一刀切等满 220s 是按最慢的 BGP 级联定的，接口 down 那种秒级到齐的
#: 纯属陪跑。改成：这么久没有新告警进来就提前关窗，来了新的就往后延，硬上限不变。
#: 业界叫 cook-for auto-extension（Moogsoft Cookbook 的 `Cook For Extension`），
#: Alertmanager 的 `group_wait` 也是同一个意思。
#:
#: **从 40 提到 70：静默下限必须盖住一个 SNMP 轮询周期。**
#: 同一次故障会从两条路报进来——syslog 是设备推的（秒级），SNMP 要等下一次轮询
#: （60 秒）。**事件告警先到、状态告警后到是必然顺序**，静默比轮询周期短，
#: 后到那条就会落在窗口外变成第二个 incident。
#: 量过 22 对真实的「syslog + SNMP」成对告警：20 对间隔在 20 秒内，
#: 但有 42 秒和 110 秒各一对——后者在 40 秒静默下必被拆开。60 + 10 余量。
DEFAULT_ALERT_WINDOW_QUIET_SECONDS = 70

#: **但静默早停不能比下游 timer 还快。** 有些告警的连锁反应要等协议 timer 才报得出来，
#: 这段时间里窗口是真的静默，早停就会把后半截漏在窗口外。所以按批内告警类型取一个
#: 最短等待：命中关键词的，从第一条到达起至少等这么久，静默也不放行。
#: 数值同样来自 内部文档 的实测 timer。
MIN_WAIT_BY_KEYWORD: tuple[tuple[str, int], ...] = (
    ("bgp", 210),      # hold time 180s + 轮询 30s
    ("ospf", 60),      # dead interval 40s + 轮询 30s，取 60s
)


def _load_dotenv(path: Path) -> dict:
    env = {}
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
    return {**_load_dotenv(REPO_ROOT / ".env"), **os.environ}


def _parse_positive_int(raw: str | None, default: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _incident_window_seconds(env: dict) -> tuple[int, int]:
    window_max = _parse_positive_int(env.get("ALERT_WINDOW_MAX_SECONDS"), DEFAULT_ALERT_WINDOW_MAX_SECONDS)
    window = _parse_positive_int(env.get("ALERT_WINDOW_SECONDS"), DEFAULT_ALERT_WINDOW_SECONDS)
    return min(window, window_max), window_max


def _incident_window_quiet_seconds(env: dict) -> int:
    return _parse_positive_int(
        env.get("ALERT_WINDOW_QUIET_SECONDS"), DEFAULT_ALERT_WINDOW_QUIET_SECONDS
    )


def _min_wait_for(alert: "_PendingAlert") -> int:
    """这条告警的下游要等多久才报得出来。命中关键词就取对应的 timer，否则 0（靠静默早停收）。"""
    text = f"{alert.name} {' '.join(str(t) for t in (alert.tags or []))}".lower()
    return max((secs for kw, secs in MIN_WAIT_BY_KEYWORD if kw in text), default=0)


def _device_adapter_from_env(env: dict, vendor: str):
    transport = env.get("DEVICE_TRANSPORT", "ssh").strip().lower() or "ssh"
    if transport == "ssh":
        return SSHDeviceAdapter(
            vendor,
            host=env.get("DEVICE_HOST", ""),
            port=int(env.get("DEVICE_PORT", "22")),
            username=env.get("DEVICE_USERNAME", ""),
            password=env.get("DEVICE_PASSWORD", ""),
            timeout=10,
        )
    if transport == "telnet":
        return TelnetDeviceAdapter(
            vendor,
            host=env.get("DEVICE_HOST", ""),
            port=int(env.get("DEVICE_TELNET_PORT", "23")),
            username=env.get("DEVICE_USERNAME", ""),
            password=env.get("DEVICE_PASSWORD", ""),
            timeout=10,
        )
    raise ValueError("DEVICE_TRANSPORT 只能是 ssh 或 telnet")


def _pick_device_host_from_interfaces(interfaces: list[dict], fallback: str = "") -> str:
    """从 Zabbix host interface 里选设备管理地址，优先用 SNMP/agent 的 IP。"""
    for preferred_type in ("2", "1"):
        for iface in interfaces:
            if str(iface.get("type", "")) != preferred_type:
                continue
            value = iface.get("ip") if str(iface.get("useip", "1")) == "1" else iface.get("dns")
            if value:
                return str(value)
    for iface in interfaces:
        value = iface.get("ip") or iface.get("dns")
        if value:
            return str(value)
    return fallback


def _sop_usage_or_default(value: object) -> dict:
    """trace 上没有 sop_usage（旧 trace/测试桩）或不是 dict 时按「没命中」算，别让记账把整条取证带崩。"""
    return value if isinstance(value, dict) else _default_sop_usage()


def _default_sop_usage() -> dict:
    return {
        "playbook": None,
        "matched": False,
        "selection": "none",
        "matched_because": [],
        "steps": [],
        "coverage": 0.0,
        "adherence": "not_matched",
        "deviations": [],
        "invalid_steps": [],
    }


def _host_name_from_zabbix_text(zabbix_text: str) -> str:
    """监控原文第一行形如 `## 主机：A1-viosl2（管理地址 192.0.2.54）`。"""
    m = re.search(r"##\s*主机[:：]\s*([^\s（(]+)", zabbix_text or "")
    return m.group(1) if m else ""


@dataclass
class PipelineRecord:
    """一次完整处理的存档，字段覆盖"取到了什么、分析出了什么、每一步花了多久"。
    多条告警共享同一个 incident 时，除了 `eventid`/`webhook_payload` 这两个
    字段各自不同，其余（`analysis_parsed`/`device_context_text`/...）在同一
    个 incident 内的记录之间是同一份内容的拷贝。
    """

    eventid: str
    webhook_payload: dict
    started_at: str
    zabbix_context_text: str = ""
    zabbix_hostid: str = ""
    alert_clock: int = 0
    zabbix_fetch_error: str = ""
    device_commands_run: list = field(default_factory=list)
    device_context_text: str = ""
    device_fetch_error: str = ""
    diagnostic_mode: str = ""
    diagnostic_trace: list = field(default_factory=list)
    analysis_parsed: dict | None = None
    analysis_error: str = ""
    evidence_verification: dict | None = None
    business_rule_violations: list = field(default_factory=list)
    analysis_trace: dict = field(default_factory=dict)
    sop_usage: dict = field(default_factory=_default_sop_usage)
    feishu_report: dict = field(default_factory=dict)
    incident_id: str = ""
    #: 指纹连同原料一起存：光存哈希，离线重算对不上也不知道差在哪。
    incident_fingerprint: str = ""
    incident_triggerids: list = field(default_factory=list)
    #: 进指纹的对象名。整卡故障折叠过就是 `slot:GigabitEthernet1/0`，
    #: 没折叠就是明文接口名——**两者的区别正是要能看出来的东西**。
    incident_objects: list = field(default_factory=list)
    incident_role: str = ""
    incident_grouped_with: list = field(default_factory=list)
    incident_repeat_count: int = 0
    elapsed_seconds: dict = field(default_factory=dict)
    finished_at: str = ""
    status: str = "done"


#: **数值型监控项只取跟这次故障有关的那些。**
#:
#: 拿 104 条设备告警记录量过：整段 Zabbix 上下文里「相关监控项最近历史」
#: 占 90%，而这 90% 里真正进过结论的只有状态类。体积排头几名全是每个接口的
#: `net.if.in/out`、`net.if.*.discards`、`net.if.*.errors`——七项 × 八个口，
#: 每次采集都在变，所以连「窗口内没变过」那条折叠都折不进去，一条告警白背 5.6k 字符。
#:
#: 过滤器是拿 198 条记录的证据引文反过来验的：**59 条进过结论的数值行全部保留，0 误伤**。
#: 砍掉的部分不是拿不到了，agent 手上一直有 `zbx_history`，要看自己去查。
_STATE_ITEM_HINTS = ("status", "state", "icmpping", "avail", "uptime", "util", "cpu")


def _is_diagnostic_item(item: dict, alert_interfaces: list[str]) -> bool:
    """这个数值型监控项值不值得进预取：状态类的一律要，别的只要告警那个接口的。"""
    key = f"{item.get('key_', '')} {item.get('name', '')}".lower()
    if any(hint in key for hint in _STATE_ITEM_HINTS):
        return True
    return any(iface and iface.lower() in key for iface in alert_interfaces)


def _readable_expression(expression: str, trigger_items: list[dict]) -> str:
    """把触发器表达式里的 `{38369}` 换成监控项名字。

    Zabbix 存的表达式是 `{$IFCONTROL:"Gi0/1"}=1 and {38369}=2 and ({38370}<>{38371})`，
    那几个数字是 itemid 引用。**原样发给模型等于没发**——它没有任何办法把
    `{38369}` 对回某个监控项。换成名字之后这一行才开始有信息。
    """
    if not expression:
        return expression
    by_id = {str(i.get("itemid")): str(i.get("name") or i.get("key_") or "") for i in trigger_items}
    for itemid, name in by_id.items():
        if name:
            expression = expression.replace("{" + itemid + "}", f"[{name}]")
    return expression


def fetch_zabbix_context(eventid: str) -> tuple[str, str, int, str, dict]:
    """返回 (人可读文本, hostid, 告警 clock, 出错信息, meta)。
    `meta` 带真实 triggerid 和接口 tag，给 `compute_fingerprint()` 用，不从 root_cause 文本里解析。
    """
    env = _env()
    try:
        with ZabbixClient(
            url=env.get("ZABBIX_URL", ""), user=env.get("ZABBIX_USER", ""), password=env.get("ZABBIX_PASSWORD", "")
        ) as zbx:
            # problem.get 这个 Zabbix 版本不支持 selectHosts 参数（实测踩过一次，
            # ）——host 信息
            # 改从 trigger.get 拿，那边支持 selectHosts
            problems = zbx._call(
                "problem.get",
                {"output": "extend", "eventids": eventid, "selectTags": "extend"},
            )
            recovered_note = ""
            if not problems:
                # **自愈的告警 problem.get 就查不到了，但事件本身还在。** 撞到：
                # #4 抖动、#27 BGP 重协商那几条（93747、93801~93804）等到攒批窗口
                # 关了再查，问题已经恢复，整条被跳过、一个结论都没有。
                # event.get 不管恢复没恢复都查得到；恢复时间写进上下文，让研判知道
                # 「现在去设备上看是正常的」是预期内的。
                problems = zbx._call("event.get", {"output": "extend", "eventids": eventid, "selectTags": "extend"})
                if not problems:
                    return "", "", 0, f"event.get 也查不到 eventid={eventid}", {}
                r_eventid = str(problems[0].get("r_eventid") or "0")
                recovery = zbx._call("event.get", {"output": ["clock"], "eventids": r_eventid}) if r_eventid != "0" else []
                if recovery:
                    lasted = int(recovery[0]["clock"]) - int(problems[0]["clock"])
                    recovered_note = (
                        f"已自动恢复：恢复事件 {r_eventid}，clock={recovery[0]['clock']}，故障持续约 {lasted} 秒。"
                        "现在去设备上看多半已经正常，判断要靠故障窗口内的历史和日志。"
                    )
            else:
                # problem.get 还查得到 = 取证这一刻问题仍然活跃。**要明说**：
                # 业务规则（schema.py `_fault_is_probably_ended`）看不出故障状态时默认当作「已结束」，
                # 然后把「现在的设备状态」一律判成不能拿来排除别的方向——对还没恢复的故障这是误判
                # （飞书卡：A1 接口还 admin down 着，「当前状态」就是故障状态，却被判违规）。
                recovered_note = "当前活跃告警：该问题在取证时仍未恢复，此刻设备上看到的状态就是故障状态，当前快照有效。"
            problem = problems[0]
            triggerid = str(problem.get("objectid") or "")
            tags = problem.get("tags") or []
            interfaces = sorted({str(t.get("value")) for t in tags if t.get("tag") == "interface" and t.get("value")})

            trigger = zbx._call(
                "trigger.get",
                {
                    "output": "extend",
                    "triggerids": problem["objectid"],
                    "selectHosts": ["hostid", "host"],
                    # **第一信源：到底是哪个监控项、哪个值把这条告警顶起来的。**
                    # 不取它，表达式里的 `{38369}=2 and ({38370}<>{38371})` 对模型
                    # 就是一串没有意义的数字，它只能从「这台主机窗口内的监控项」里猜。
                    "selectItems": ["itemid", "name", "key_", "value_type"],
                },
            )
            trigger = trigger[0] if trigger else {}
            trigger_items = trigger.get("items") or []
            trigger_item_ids = {str(i.get("itemid")) for i in trigger_items if i.get("itemid")}
            hosts = trigger.get("hosts") or []
            hostid = hosts[0]["hostid"] if hosts else ""
            host_name = hosts[0]["host"] if hosts else ""
            host_interfaces = []
            device_host = env.get("DEVICE_HOST", "")
            if hostid:
                host_interfaces = zbx._call(
                    "hostinterface.get",
                    {
                        "output": ["interfaceid", "hostid", "type", "useip", "ip", "dns", "port", "available"],
                        "hostids": hostid,
                    },
                )
                device_host = _pick_device_host_from_interfaces(host_interfaces, fallback=device_host)

            clock = int(problem["clock"])
            time_from = clock - CONTEXT_WINDOW_SECONDS
            time_till = clock + 60

            # 抖动：同一个触发器在 FLAP_WINDOW_SECONDS 里报过几次
            flap_note = ""
            if triggerid:
                try:
                    recent = zbx._call("event.get", {
                        "output": ["eventid", "clock", "value"],
                        "objectids": triggerid,
                        "source": 0, "object": 0, "value": 1,
                        "time_from": clock - FLAP_WINDOW_SECONDS, "time_till": clock + 60,
                        "sortfield": "clock", "sortorder": "DESC", "limit": 20,
                    })
                except ZabbixAPIError:
                    recent = []
                if len(recent) >= FLAP_MIN_EVENTS:
                    times = ", ".join(str(e.get("clock")) for e in recent[:10])
                    flap_note = (
                        f"这条告警在抖动：同一个触发器 {FLAP_WINDOW_SECONDS // 60} 分钟内报了 "
                        f"{len(recent)} 次（时间戳 {times}）。"
                        "**抖动本身可能就是要判的那件事**——别只判最后这一次，"
                        "看它是周期性的、还是越来越密、还是跟某个操作同时开始的。"
                    )

            # **预取只给「这条告警本身」，别的按需。** 维护者定的：
            # 「拉 Zabbix 原文 <<< 然后这个给我按需改了」。
            #
            # 原来这里把 15 分钟窗口内所有监控项的历史、同窗活跃告警、主机接口
            # 全拉一遍塞进去。量过：设备侧平均 6276 字符，九成是监控项历史，
            # 而 agent 手上本来就有 zbx_items / zbx_history / zbx_syslog /
            # zbx_problems 这些工具——**我们替它查了，它就不用想该查什么**。
            #
            # 留下来的只有「这条告警是什么、被什么顶起来的」：告警原文、标签、
            # 触发器、**触发项在故障窗口内的取值**。后面那条是第一信源，
            # 不能按需——不给它，模型连这条告警为什么会响都不知道。
            # Zabbix 的 value_type：0 浮点 / 1 字符 / 2 日志 / 3 无符号整数 / 4 文本。
            # **SNMP trap 监控项是 4（文本），syslog 是 2（日志）**——两者都要一条一行
            # 原样贴，不能按数值项渲染成 `clock=value`，那会把一条 trap 的正文
            # 挤进一行里、时间线只认第一个时间戳。
            TEXTUAL_VALUE_TYPES = {"1", "2", "4"}

            first_source_lines: list[str] = []
            for item in trigger_items:
                label = f"- {item.get('name')}（{item.get('key_')}，itemid={item.get('itemid')}）"
                try:
                    hist = zbx.get_history(
                        item["itemid"],
                        history_type=int(item.get("value_type", 3)),
                        time_from=time_from,
                        time_till=time_till,
                        limit=LOG_HISTORY_LIMIT if str(item.get("value_type")) in TEXTUAL_VALUE_TYPES else 10,
                    )
                except (ZabbixAPIError, KeyError, ValueError):
                    hist = []
                first_source_lines.append(label)
                if not hist:
                    first_source_lines.append("    故障窗口内没有取到它的历史值")
                elif str(item.get("value_type")) in TEXTUAL_VALUE_TYPES:
                    # 日志和 trap 一条一行、正序。挤成一行的话时间线只认第一个时间戳，
                    # 一整团会被当成一件事（一条真实告警记录 的 BGP 就这样）。
                    #
                    # 一条真实告警 的 RCA：`h['clock']` 是 Zabbix 写下这条历史的时刻，
                    # 落在 alert_clock±15分钟 窗口内不代表这行日志描述的事也发生在窗口内
                    # ——设备到采集之间的传输一断一续，补发的旧行会跟补发抵达那一刻共用
                    # 同一个 clock。这里是「第一信源」，标榜的就是「离故障最近的证据」，
                    # 能读出行内真实时间（line_time）且明显在窗口外的，就不该放在这里
                    # 冒充故障窗口证据——agent 自己用 zbx_syslog 查更宽的窗口时还是能看到它，
                    # 只是不会被这段自动预取误标成「故障窗口内」。读不出真实时间就照旧保留，
                    # 不能读出来不代表这行可疑，只是格式不认识。
                    kept = []
                    for h in reversed(hist):
                        line_time = parse_line_receive_time(h["value"], int(h.get("clock", clock)))
                        if line_time is not None and not (time_from <= line_time <= time_till):
                            continue
                        kept.append(h["value"])
                    if kept:
                        first_source_lines.extend(f"    {v}" for v in kept)
                    else:
                        first_source_lines.append(
                            "    这个监控项在窗口内有历史记录，但内容都是补发的旧消息"
                            "（行内真实时间跟故障窗口对不上），已过滤，不放进第一信源"
                        )
                else:
                    first_source_lines.append(
                        "    故障窗口内取值：" + ", ".join(f"{h['clock']}={h['value']}" for h in hist[:10])
                    )
            first_source_lines = [
                l if len(l) <= HISTORY_LINE_MAX_CHARS else l[:HISTORY_LINE_MAX_CHARS] + "…（本行已截断）"
                for l in first_source_lines
            ]
            # **自污染警告。** 真实出过一张垃圾卡（A3，一条真实告警）：
            # 我们自己的 agent 14:21:30 SSH 登进 A3、1 秒后登出，14:21:33 设备就发了
            # configChange trap，Zabbix 报警，然后 agent 去分析这条告警——**查到了
            # 自己刚才的登录，还把它当成了证据**。
            #
            # 这条规矩本来只写在 `device_show` / `zbx_syslog` 的工具描述里，
            # 但第一信源是预取直接塞进去的，**工具描述管不着这一段**。
            ro_user = str(_env().get("DEVICE_USERNAME", "")).strip()
            if ro_user and any(ro_user in l for l in first_source_lines):
                first_source_lines.append(
                    f"\n⚠ 上面出现了 `{ro_user}` —— **那是本系统自己取证时留下的登录记录，"
                    "不是证据**。它的登录/登出/读配置动作本身可能就是这条告警的触发原因；"
                    "**如果整条告警只能由它解释，那这次就是监控采集自身的问题**，"
                    "照实说，不要归到「本端有人动过配置」，也不要说成分不出。"
                )

            parts = [
                f"## 主机：{host_name or hostid}（管理地址 {device_host or '未知'}）\n\n",
                "## 告警原文\n",
                json.dumps(
                    {k: problem.get(k) for k in ("eventid", "name", "severity", "clock", "opdata")},
                    ensure_ascii=False,
                ),
                (f"\n\n## 告警状态\n{recovered_note}" if recovered_note else ""),
                (f"\n\n## 抖动\n{flap_note}" if flap_note else ""),
                "\n\n## 标签\n",
                json.dumps(tags, ensure_ascii=False),
                "\n\n## 触发器\n",
                f"{trigger.get('description', '')}: "
                f"{_readable_expression(str(trigger.get('expression', '')), trigger_items)}",
                "\n\n## 触发这条告警的监控项（第一信源）\n",
                "\n".join(first_source_lines) if first_source_lines else "（trigger.get 没返回监控项）",
                # **必须说清楚这里没给什么。** 不说的话它会以为这就是全部，
                # 然后基于一条告警下结论——比让它多查两步危险得多。
                "\n\n## 这里只有这条告警本身，别的要自己查\n"
                "这台设备同时间还有没有别的告警、别的监控项在故障窗口里什么样、"
                "syslog 里有什么、对端是谁——上面都没给，用工具自己取："
                "zbx_problems（同主机其它告警）、zbx_items + zbx_history（监控项和历史）、"
                "zbx_syslog（故障窗口的设备日志）、topology_neighbors（对端）、"
                "device_show（登设备看现在什么样）。",
            ]
            meta = {
                "triggerid": triggerid,
                "tags": tags,
                "interfaces": interfaces,
                "name": str(problem.get("name") or ""),
                "device_host": device_host,
            }
            return "".join(parts), hostid, clock, "", meta
    except (ZabbixAPIError, PermissionError) as exc:
        return "", "", 0, str(exc), {}


def _redact_env_values(text: str, env: dict | None = None) -> str:
    """Remove concrete .env values from logs/records while keeping variable names."""
    if not text:
        return text
    env = env or _env()
    redacted = text
    for key, value in env.items():
        if not value or len(str(value)) < 3:
            continue
        # Zabbix 用户名不是密钥；纯字母的（Zabbix 默认的 `Admin`）同时是设备输出里的常用词，
        # 替换它会把 `Idle (Admin)`、`Admin. shutdown` 这类证据原文改成占位符，引文就对不上了。
        # 带数字或连字符的用户名（`ai-readonly`）照旧遮掉。
        if key == "ZABBIX_USER" and re.fullmatch(r"[A-Za-z]+", str(value)):
            continue
        # Hostnames/IPs are also lab-specific values; keep names, hide values.
        if key.endswith(("PASSWORD", "TOKEN", "SECRET", "KEY")) or key in {
            "DEVICE_HOST",
            "DEVICE_USERNAME",
            "ZABBIX_URL",
            "ZABBIX_USER",
            "FEISHU_CHAT_ID",
            "FEISHU_APP_ID",
            "SYSLOG_HOST",
            "SYSLOG_SSH_USER",
        }:
            # 只替换独立出现的值（边界用字母数字，不用 \b：密码常带标点）。
            # 裸 replace 曾把 `severity` 切成 `s<SYSLOG_SSH_PASSWORD>rity`，证据被打了马赛克。
            redacted = re.sub(
                rf"(?<![A-Za-z0-9]){re.escape(str(value))}(?![A-Za-z0-9])",
                f"<{key}>",
                redacted,
            )
    return redacted




@dataclass
class DiagnosticContextResult:
    text: str = ""
    device_records: list[dict] = field(default_factory=list)
    error: str = ""
    mode: str = ""
    trace: list[dict] = field(default_factory=list)
    #: 取证轨迹末尾那次结构化调用的结果。**结论现在从这儿来，不再单独调 analyze()。**
    analysis: dict | None = None
    #: 结构化收尾的来源和异常信息，给 pipeline 落盘统计/排障用。
    analysis_trace: dict = field(default_factory=dict)
    sop_usage: dict = field(default_factory=_default_sop_usage)
    analysis_error: str = ""
    #: 整条轨迹的原文。逐字引文核对拿它当底本——核对器要跟模型看到的一模一样。
    transcript: str = ""


#: 除了 device_show，这些工具的返回也进研判原文。见 `_run_ai_exploration`。
#:
#: **预取改按需之后，这张表不补齐就会直接崩。**
#: 研判那一轮是不给工具的，它只看得到 `zabbix_text` + 取证结果。
#: 以前监控项历史是预取塞进 `zabbix_text` 的，所以研判引 `bgpPeerState` 那类
#: 数值证据没问题（198 条记录里有 64 条证据引文只在数值行里命中）。
#: 现在那段历史改成 agent 自己用 `zbx_history` 去拉——**拉回来的东西如果不进
#: 这张表，研判根本看不到它**，逐字核对会把每条这类证据都判成「原文里找不到」。
_EVIDENCE_TOOLS = (
    "zbx_problems",
    "topology_neighbors",
    "zbx_history",
    "zbx_items",
    "zbx_syslog",
    "zbx_trends",
    "zbx_top_talkers",
)


def _analysis_direction_summary() -> str:
    return "；".join(f"{key}={value}" for key, value in HYPOTHESIS_CATEGORIES.items())


#: 取证提示。**按维护者的原则重写**：
#: - 「收尾会被问什么」整段（逐方向表态、排除必须有反证、不能拿实时状态排除）下掉了。
#:   那是取证/研判分两次调用时代的补丁，现在是同一个 agent 一边查一边收尾，它把
#:   结论的格式要求变成了取证的完成标准，跟「立刻收尾」打架。结构化收尾那次调用
#:   （`ANALYSIS_SYSTEM_PROMPT` + schema）仍然有它自己的要求，**不要写回这里**。
#: - 停止条件改成模型自己能判断的粗粒度条件，不再写「不要换时间窗反复查」这类
#: 跟工具返回互相矛盾的细则（外部 agent 反馈第 3 条）。
#: - 引文出处放宽到「告警原文/第一信源」：逐字核对的底本本来就包含 zabbix_text
#:   （`_process_incident_batch_inner` 的 pool），原来只写「工具返回」是提示比核对器还严（第 6 条）。
#: - 删掉「补查前先想清楚它要回答哪个具体问题」：那是只能落在隐藏思考里的要求，
#: 无法验证、关掉 thinking 也不生效（外部 agent 分析）。
FORENSICS_SYSTEM_PROMPT = """你是网络运维真实告警的根因分析取证助手，手上的工具全部只读。
目标：
- 查清这次故障：发生了什么、最可能的原因、下一步该查什么或找谁。
- 说不清就写明还差什么数据、可能从哪里拿到，不许为了显得确定而编。
怎么查：
- 提示里有 SOP 时，以 SOP 为骨架按顺序取证，不需要再调 sop_lookup；SOP 与工具回显矛盾时，以回显为准并在结论里写明。
- SOP 没覆盖、但会改变判断的问题，可以自己补查。
什么时候收尾：
- 已经能说清「发生了什么 / 最可能的原因 / 下一步查什么」，就收尾。
- 接下来的查询不会改变这三点的判断时，也收尾，把缺口写成下一步。
诚实红线：
- 不要编造监控项、itemid、设备输出、日志、配置或因果关系。
- 引文必须逐字来自原文——本会话的工具返回，或提示里给出的告警原文/第一信源——并注明出处；不改写、不拼接、不补全。
- 分清每条证据的时间：故障窗口内的日志/历史、取证时刻的设备状态（当前快照）、与时间无关的事实。告警已经恢复时，当前快照不代表故障时刻。
- 引用 trap、日志、事件当证据前，先核对它的时间戳是否落在这条告警的故障窗口内（告警时间前后十来分钟）。更早的记录——几个小时前的一次启动、上一次故障留在日志里的内容——只能当背景，不能当本次的原因；设备已经连续运行数小时（uptime 很长）时，不要下「刚重启」的结论。
- 提示里如果有「同一个事件窗口里其它设备上的告警」，先判断它们是不是同一件事的两端（同一条链路、同一个邻接/会话）：是就按一件事写，结论同时点名两端设备，并去对端取证（对端的配置/日志），不要只看本端就下结论。
- 告警本身是恢复类的（邻居回到 FULL / Established、链路回到 up）且设备当前状态正常时，结论写成「已恢复」，并说明恢复前那次故障（只在故障窗口内有证据时才写）；不要拿更早的日志编出新的根因，置信度不高于 medium。
- 本系统取证用的只读账号（如 ai-readonly）的登录/登出记录是取证动作本身留下的，不是故障证据。
- 回答“我查过什么”只能依据本会话真实工具调用；看不到记录就说看不到。
- 工具返回里如果有 markdown 字段，原样贴出来，不要自己重排。
- 最终用中文回答，清楚写出证据和不确定性。
"""

#: 告警主线的取证循环后面还有一次结构化收尾（`_final_schema_call`），它看的是整段轨迹
#: （含每个工具返回原文，`build_transcript`），循环最后那段自由文本只是轨迹里的一条消息，不是结论的底本。
#: 下午实测那段写到 3~3.8K token、单次 60~95s，随后结构化收尾把同样的内容再写一遍。
#: 只陈述这个事实，不劝阻工具调用。**只加在告警主线**。
ALERT_CLOSING_NOTE = """收尾方式：
- 取证结束时最后一段话写几句就够：查到了什么（对端未恢复告警的 eventid 也算）、还差什么。完整结论由随后的结构化步骤基于本次全部工具返回原文生成。
"""
ALERT_FORENSICS_SYSTEM_PROMPT = FORENSICS_SYSTEM_PROMPT + ALERT_CLOSING_NOTE


def _extract_json_section(text: str, heading: str) -> object | None:
    marker = f"## {heading}"
    start = text.find(marker)
    if start < 0:
        return None
    rest = text[start + len(marker):]
    next_heading = rest.find("\n\n## ")
    body = rest[:next_heading] if next_heading >= 0 else rest
    body = body.strip()
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _interface_from_zabbix_text(zabbix_text: str) -> str:
    """从**整批**告警文本里找目标接口：先看各条告警名，再看 trap 的 ifDescr。

 领头告警常是不带接口的 `Trap: linkDown received on A1`，同批的 `Interface Gi0/1: Link down` 和
 trap 变量里的 ifDescr 才有接口名。新图的 understand 只看领头告警，接口取不到，SOP 命令就退化成
 不带接口的 `show interfaces`（见 图 3/4 轮缺口判断误判的排查）。
    """
    from netops_ai.playbooks.lookup import parse_alert_interface

    text = str(zabbix_text or "")
    for name in re.findall(r'"name":\s*"([^"]+)"', text):
        parsed = parse_alert_interface(name, [])
        if parsed:
            return parsed[0]
    for descr in re.findall(r'1\.3\.6\.1\.2\.1\.2\.2\.1\.2\.\d+\s+"([^"]+)"', text):
        parsed = parse_alert_interface(f"Interface {descr}", [])
        if parsed:
            return parsed[0]
    return ""


def _alert_name_and_tags_from_zabbix_text(zabbix_text: str) -> tuple[str, list[str]]:
    raw_alert = _extract_json_section(zabbix_text, "告警原文")
    alert_name = ""
    if isinstance(raw_alert, dict):
        alert_name = str(raw_alert.get("name") or "")
    raw_tags = _extract_json_section(zabbix_text, "标签")
    tags: list[str] = []
    if isinstance(raw_tags, list):
        for tag in raw_tags:
            if isinstance(tag, dict):
                key = str(tag.get("tag") or "").strip()
                value = str(tag.get("value") or "").strip()
                if key and value:
                    tags.append(f"{key}={value}")
            elif str(tag).strip():
                tags.append(str(tag).strip())
    return alert_name, tags


def _sop_plan_step_line(step: dict) -> str:
    branches = sop_branches_text(step)
    return (
        f"- id={step.get('id', '')}; why={step.get('why', '')}; expect={step.get('expect', '')}; "
        f"{sop_action_text(step)}" + (f"; branches: {branches}" if branches else "")
    )


def _render_sop_plan(data: dict) -> str | None:
    if not data.get("matched"):
        return None
    limits = data.get("limits") or {}
    try:
        max_tokens = int(limits.get("max_tokens", 1500))
    except (TypeError, ValueError):
        max_tokens = 1500
    max_chars = max(1, max_tokens * 2)
    main_steps = [s for s in (data.get("steps") or []) if s.get("main", True)]
    try:
        max_main_steps = int(limits.get("max_main_steps", 5))
    except (TypeError, ValueError):
        max_main_steps = 5
    start = str(data.get("start") or "")
    # 精简：去掉「SOP 与回显矛盾以回显为准」（FORENSICS_SYSTEM_PROMPT 里已有同一句）和
    # 「走过的步骤不会再被指回」（真遇到时 [SOP 下一步] 那一句自己会说）。
    lines = [
        f"SOP：{data.get('playbook', '')}",
        f"本次以这份 SOP 为骨架取证：从 {start or '第一步'} 开始，按每步 branches 的条件走到下一步"
        "（__end__ = SOP 走完；__ai__ = SOP 到此为止，之后自己判断），偏离写明理由。"
        f"{BRANCH_SEMANTICS}"
        "下面只列主步骤；分支指向的辅助步骤，由工具返回末尾的 [SOP 下一步] 给出它的命令和分支。",
    ]
    applicability = str(data.get("applicability") or data.get("scope") or "").strip()
    if applicability:
        lines.append(f"适用性：{applicability}")
    # 原来只摆 `command`（没有就退到 `key_contains`），于是 `zabbix_history` + `key_contains: syslog`
    # 被渲染成 `tool=zbx_history; command=syslog`——zbx_history 根本没有这个参数（外部 agent 反馈第 1 条），
    # `topology_neighbors` 的 `interface` 则整个丢了。现在按 action 原样摆，参数名就是工具的真实参数名。
    for step in main_steps[:max_main_steps]:
        lines.append(_sop_plan_step_line(step))
    # 曾把辅助步骤整段列进计划：那时 [SOP 下一步] 只摘 why 第一句、240 字符封顶，模型看不到
    # local_log_buffer 的命令和去向。把完整信息挪到运行时（`SopRuntimeState.hint_for` 对计划外步骤
    # 带出 why/expect/action/branches），计划里不再列辅助步骤：interface-link-down 计划 4054→2652 字符。
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars] + "\n[已按 SOP limits.max_tokens 截断]"


def _sop_data_from_zabbix_text(zabbix_text: str, alert_clock: int | float | None) -> dict:
    from netops_ai.playbooks.lookup import sop_lookup

    alert_name, tags = _alert_name_and_tags_from_zabbix_text(zabbix_text)
    # 领头告警常是不带接口的 `Trap: linkDown received on A1`，计划里就只剩 `<接口>` 占位；
    # 同批告警名和 trap 的 ifDescr 里有接口。告警名自带接口时 sop_lookup 仍以告警名为准。
    return sop_lookup(
        alert_name, tags, alert_clock=alert_clock, interface_hint=_interface_from_zabbix_text(zabbix_text)
    )


def _sop_plan_from_zabbix_text(zabbix_text: str, alert_clock: int | float | None) -> str | None:
    return _render_sop_plan(_sop_data_from_zabbix_text(zabbix_text, alert_clock))

#: 取证那一轮发给模型的问题。**提成常量是为了能测**——
#: 这段话里有几句是用真实误判换来的，改坏了没有任何地方会报错。
def _explore_brief(state: dict) -> str:
    """SOP 已经取过证时，把「已取到什么、还差什么」交给自由探索，别让它从头再查一遍（第 3 轮它又调了 sop_lookup/zbx_problems）。"""
    if not state.get("obs"):
        return ""
    raw_store = state.get("raw_store") or {}
    lines = ["\n\n**SOP 已经替你取过下面这些证据（不用重复取，也不要再调 sop_lookup）：**"]
    for item in state.get("obs") or []:
        view = str(item.get("view") or raw_store.get(item.get("raw_key", ""), ""))[:600]
        lines.append(f"- [{item.get('step_id', '')}] {item.get('command', '')}（{item.get('status', '')}）\n{view}")
    gaps = [str(g) for g in (state.get("gaps") or []) if str(g).strip()]
    if gaps:
        lines.append("**目前还差这些，只补这些：**\n" + "\n".join(f"- {g.lstrip(', ')}" for g in gaps))
    fault = str((state.get("ctx") or {}).get("fault_type") or "")
    if fault in ("ospf_adjacency", "bgp_session"):
        # 第 3 轮 OSPF hello 计时器不一致：两台设备各自分析、各自「怀疑对端」，谁也没去看对端。邻居类故障必须两端对照。
        lines.append(
            "**邻居类故障必须两端对照：** 先用 topology_neighbors 找到对端设备，再用 device_show(device=对端) "
            "取对端同一条命令的输出（OSPF 看 `show ip ospf interface`，BGP 看 `show ip bgp neighbors`），"
            "逐项比较 Hello/Dead 计时器、区域、网络类型、认证、AS 号、是否被 shutdown。"
            "只看到本端正常就推断「问题在对端」是不够的——要么取到对端证据，要么明说没取到。"
        )
    return "\n".join(lines)


FORENSICS_QUESTION = (
    "这是一次真实网络故障，需要你判断根因。以下是 Zabbix 侧数据：\n\n"
    "{zabbix_text}"
    "\n\n**上面只有这条告警本身，没有别的。** 同一台设备还有没有其它告警、"
    "别的监控项在故障窗口里什么样、syslog 里有什么——都要你自己用工具取。"
    "\n\n请用手上的只读工具（Zabbix 查询、设备 show 命令）取够诊断这次故障"
    "需要的证据，**然后直接给出根因判断**。"
    "\n\n**你引用的每一段都必须是工具真的返回过的原文**，一个字都不能改——"
    "查不到就说查不到，不要凭印象复述。"
    # **对端要查的是故障窗口的日志，不是它此刻有没有告警。**
    # 真实误判（V2-vios Gi0/3，OSPF + 3 条 BGP 同时 down，0 秒自愈）：
    # 原来这里写的是「用 zbx_problems 按对端主机名查它此刻有没有告警」。
    # 告警已经恢复时对端那边同样早就恢复，zbx_problems 必然查空，
    # 「对端没问题」是假的。那次最后判成「监控采集自身的问题」，
    # 而 OSPF 和 BGP 在同一时刻一起掉，指向的是链路层。
    #
    # 之后又改了一次：原来这里让它「用 zbx_history 按告警时刻取
    # 对端的日志型监控项」——那是四步推理（列监控项→认出 value_type=2→按 itemid
    # 取历史→合并排序），gpt-5.5 都没走通。四步包成了 zbx_syslog 一个工具。
    #
    # 原来这句是「如果故障跟链路或邻居有关，用 topology_neighbors 查对端」，
    # 跟 SOP admin down 分支的「两条证据即可收尾」谁管谁说不清（外部 agent 反馈第 4 条）。
    # 现在按「对端是不是可能的原因」来分，SOP 分支已经把原因定在本端时，对端只关乎影响面。
    "\n\n对端可能是原因时（链路/邻居类故障、本端证据指不出原因），用 topology_neighbors 查对端是谁，"
    "再用 zbx_syslog 取**对端在故障窗口里的日志**。"
    # 原来这里说「对端按需取」，后面又有一句「对端此刻真有告警的，写明 eventid」，
    # 原因在本端时到底查不查对端告警，两句读不出同一个答案（验证 外部 agent 反馈第 5 条）。合成一句。
    "原因已经确定在本端时（例如本端 administratively down），对端不是必查项，对端信息只用来说明影响面；"
    # 原来写「在小结里写明」，小结是取证循环最后那段自由文本，现在那段只写几句（ALERT_CLOSING_NOTE）；
    # 这个问题原文也进结构化收尾（`_final_schema_call` 的「原始问题」），那一步有 related_alerts 字段专门放它。
    "无论哪种情况，只要查了对端、对端此刻有未恢复的告警，就在结论里写明它的 eventid 和是哪条线——"
    "同一件事两台设备各报一条，值班的人需要知道是一回事。"
    "告警已经恢复时 zbx_problems 查对端必然是空的，别拿它当「对端没问题」的依据。"
    # Zabbix 没收到 A1 的 syslog，两个 agent 又因为工具说明里「不要去登设备跑 show logging」
    # 都没看设备 buffer，错过了 `%SYS-5-CONFIG_I`。两个来源各有局限，一个空不代表另一个也空。
    "\n\n故障窗口的日志有两个来源：zbx_syslog（Zabbix 收到并存盘的 syslog）和设备本地日志 buffer"
    "（device_show 跑 show logging，容量有限、可能已被新日志覆盖）。一个来源没有，不等于另一个也没有。"
    # 各来源时间格式不同，原来一句没说（验证 外部 agent 反馈第 6 条）。每一条都对过代码或实测：
    # clock/from/to 是 Zabbix API 原值；zbx_problems 的表按进程本地时区排（`zbx_cli._clock_text`，表头带偏移）；
    # trap 行首是 snmptrapd 那台（Zabbix 服务器所在 EVE guest）`datetime.now()` 的本地时间，
    # 实测 `20260926.042100` 对 clock 1790396461 = 04:21:01 UTC；设备 NTP 未启用
    "\n\n时间约定（各来源不一样）：告警原文的 clock 和 zbx_* 返回里的 clock/from/to 是 Unix 秒，与时区无关；"
    "zbx_problems 的 markdown 表「开始时间」按运行本系统那台机器的本地时区排版，表头写了 UTC 偏移；"
    "trap 原文行首的 YYYYMMDD.HHMMSS 是 Zabbix 一侧 trap 接收程序收到 trap 时的本地时间，这套实验环境里是 UTC；"
    "设备日志时间戳和 show clock 用设备自己的时钟（实验设备是 UTC、没有启用 NTP，时间前带 * 表示未同步），"
    "与 Zabbix 的时间可能差几秒到几分钟（实测记录过约 2 秒到约 4 分 37 秒）。"
    "把设备日志和告警对时间时，设备时钟的偏差可以用 show clock 与同一时刻的 Unix 时间换算出来。"
    "\n\n告警那台连不上、或者要从对端一侧确认时，device_show 填 device=对端设备名，"
    "去邻居上看（比如邻居眼里它的 OSPF 邻居还是不是 FULL）。连不上不等于宕机。"
)


def _analysis_context(fault_time_epoch: float | None) -> str:
    """收尾结构化调用要知道的时间基准。

    原来这段在 `analyzer.build_user_prompt()` 里。**它防的是「拿现在的状态反推
    故障时刻」**——设备是现在才登上去查的，看到的是当前快照；故障发生在更早。
    """
    lines = ["## 时间基准"]
    if fault_time_epoch:
        lines.append(f"故障发生在 Unix 时间 {int(fault_time_epoch)}。")
    lines.append(
        f"你登设备取数是在 Unix 时间 {int(time.time())}，也就是**现在**。"
        "设备上看到的是当前状态，不等于故障时刻的状态；"
        "每条证据都要标清它属于故障窗口、当前快照，还是与时间无关的事实。"
    )
    return "\n".join(lines)


def _alert_token_budget() -> int:
    """告警路径的 token 上限，`ALERT_TOKEN_BUDGET`；没配或写错用 6 万，0 = 不限。

 试过放宽到 10 万：BGP/OSPF/重启不再被 token 拦住，却继续跑到工具预算 12 / 轮数 18，
 耗时 90~165s → 133~342s、总 token 7 万 → 10~15 万，结论没有更好。默认退回 6 万（同全局值），留着开关。
    """
    raw = str(_env().get("ALERT_TOKEN_BUDGET", "")).strip()
    try:
        return max(0, int(float(raw))) if raw else 60000
    except ValueError:
        return 60000


def _alert_max_tool_calls() -> int:
    """告警路径的工具调用上限，`ALERT_MAX_TOOL_CALLS`；没配或写错用 12，0 = 不限。"""
    raw = str(_env().get("ALERT_MAX_TOOL_CALLS", "")).strip()
    try:
        return max(0, int(float(raw))) if raw else 12
    except ValueError:
        return 12


def _run_ai_exploration(
    *,
    zabbix_text: str,
    plan: str | None,
    host_filter: str,
    device_host: str = "",
    alert_clock: int | float | None = None,
    fault_time_epoch: float | None = None,
) -> tuple[str, list[dict], list[dict], bool, str, dict | None, str, dict, str, dict]:
    """**取数和下结论在同一条轨迹里做完。**

 维护者定的：「研判你也砍了吧，我觉得取证那步就应该直接出结论，
 没见过这样调用的。」原来是两次独立调用——循环跑完把结果重新拼成一段文本，
 再单独起一次 `analyze`。问题是那次重拼**每个工具只留 3000 字符**，
 所以研判看到的比取证少，而且它发现证据不够也没法回头再查。

 现在是一条轨迹：循环照跑，跑完**在同一份 messages 上**再走一次带 schema
 的调用。**schema 仍然只出现在最后那一次**——这一点不能变，strict schema
 会压制工具调用（见 `_final_schema_call` 的注释）。

 返回 (设备文本, 设备命令记录, trace 字典, 是否没查完, 终止原因, 结构化结论, 轨迹原文,
 结构化收尾来源, 循环错误, SOP 使用记账)。
    """
    from netops_ai.analysis.analyzer import SYSTEM_PROMPT as ANALYSIS_SYSTEM_PROMPT
    from netops_ai.analysis.schema import analysis_json_schema
    question = FORENSICS_QUESTION.format(zabbix_text=zabbix_text)
    sop_data = _sop_data_from_zabbix_text(zabbix_text, alert_clock)
    plan = plan if plan is not None else _render_sop_plan(sop_data)
    result = run_agent_loop(
        # 把本次要连的设备绑进工具工厂。**不要再用改环境变量的办法**——
        # 那是全局可变状态，只能靠一把大锁把整段循环串起来才不会互相踩，
        # 代价是所有告警的分析完全串行。绑成参数之后锁就不需要了。
        # include_doc_search=True：A16 把知识库工具在告警这条线默认打开，不吃
        # DOC_SEARCH 环境变量（那个继续只管对话那条线，没有跟着变）。
        partial(build_chat_tools, device_host=device_host, alert_clock=alert_clock, include_doc_search=True),
        question=question,
        session_id=f"pipeline-{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}",
        host_filter=host_filter,
        plan=plan,
        final_schema=analysis_json_schema(),
        system_prompt=ALERT_FORENSICS_SYSTEM_PROMPT,
        # 收尾那次调用要带研判的那套硬规则（逐字引文、alert_roles、grouping、
        # 六个方向的假设清单），不能用 agent_loop 里那句通用的。
        final_system_prompt=ANALYSIS_SYSTEM_PROMPT,
        final_context=_analysis_context(fault_time_epoch),
        sop_data=sop_data,
        # **先暂时取消轮数限制，只留 LangGraph 自己的 recursion_limit**（负责人
        # 拍板，见 docs/ARCHITECTURE.md A16）：不设 max_tool_calls/no_progress_threshold，
        # 也不覆盖 recursion_limit。**这是临时状态，不是把闸删了不管**——`no_progress_threshold`
        # 当初是真机撞见过原地打转（`zbx_top_talkers` 12 步同类循环）才加的，取消之后
        # 下一轮真机验证必须盯会不会打转、token/耗时涨多少。范围只在告警这条线，
        # 不动 chat_agent.py（那边的点状问题预算是昨天刚调好的）。
        # 维护者拍板：恢复默认闸（18 轮 / 12 工具 / 无进展 3 跳 + ANALYSIS_TOKEN_BUDGET）。
        # A16 实测撤掉之后开/关 thinking 都出现了失控（64~85 次调用，24 分钟 / 227 万 token），见 WIN-NEXT。
        # 告警路径单独的 token 上限（`ALERT_TOKEN_BUDGET`，默认 10 万）。全局 `ANALYSIS_TOKEN_BUDGET`=6 万对 BGP/OSPF/重启这类
        # 多步 SOP 太紧（真机 8 次里 5 次撞它，靠「带着已有证据收尾」才出结论）；不动全局值是因为对话预算按它乘 3。
        budget=AgentLoopBudget(max_tokens=_alert_token_budget(), max_tool_calls=_alert_max_tool_calls()),
    )
    text_parts = []
    records = []
    trace_dicts = []
    for call in result.trace.tool_calls:
        if call.tool in _EVIDENCE_TOOLS:
            # 关联告警的 eventid 要能在原文里逐字核到，所以这两类结果也进文本（A9）
            body = call.result if isinstance(call.result, str) else json.dumps(call.result, ensure_ascii=False)
            text_parts.append(f"### {call.tool}（AI 探路）{json.dumps(call.args, ensure_ascii=False)}\n\n```\n{body[:3000]}\n```\n")
            continue
        if call.tool != "device_show":
            continue
        payload = call.result if isinstance(call.result, dict) else {}
        cmd = payload.get("command") or call.args.get("command", "")
        where = f" @{call.args['device']}" if call.args.get("device") else ""
        allowed = bool(payload.get("allowed", call.ok))
        ok = bool(payload.get("ok", call.ok))
        records.append(
            {
                "source": "ai",
                "command": cmd,
                "device": call.args.get("device", ""),
                "allowed": allowed,
                "ok": ok,
                "denial_reason": payload.get("denial_reason", ""),
                "error": payload.get("error") or call.error,
            }
        )
        if allowed and ok:
            text_parts.append(f"### `{cmd}`（AI 探路{where}）\n\n```\n{payload.get('output', '')}\n```\n")
        elif not allowed:
            text_parts.append(f"### `{cmd}`（AI 探路{where}）—— 被白名单拒绝：{payload.get('denial_reason', '')}\n")
        else:
            text_parts.append(f"### `{cmd}`（AI 探路{where}）—— 执行失败：{payload.get('error') or call.error}\n")
    for call in result.trace.tool_calls:
        trace_dicts.append(
            {
                "source": "ai",
                "tool": call.tool,
                "args": _json_safe(call.args),
                "ok": call.ok,
                "error": call.error,
                # 之前只存了"问了什么"，没存"设备/监控答了什么"——审计页/告警详情页点开一步
                # 想看原始返回时，数据压根不存在。跟聊天路径的 save_trace() 一样用 _json_safe
                # 兜底截断（默认 6000 字符），不是新发明一套序列化规则。
                "result": _json_safe(call.result),
                "elapsed_ms": call.elapsed_ms,
            }
        )
    def _s(value: object) -> str:  # trace 字段只认真字符串（桩/旧 trace 没有这些字段时按没有算）
        return value if isinstance(value, str) else ""

    analysis_trace = {"from": _s(getattr(result.trace, "final_schema_from", "")) or "agent_loop_final_schema"}
    if _s(getattr(result.trace, "final_schema_abort_reason", "")):
        analysis_trace["abort_reason"] = _s(result.trace.final_schema_abort_reason)
    return ("\n".join(text_parts), records, trace_dicts, result.incomplete,
            result.termination_reason, result.trace.final_structured, result.trace.transcript,
            analysis_trace, _s(getattr(result.trace, "error", "")),
            _sop_usage_or_default(getattr(result.trace, "sop_usage", None)))


def _config_diff_section(vendor: str, host: str, env: dict, alert_time_epoch: int | None) -> tuple[str, str]:
    """故障前后的配置 diff。没存过「前」就什么都不做。

    「前」取告警前最近一份（定时任务或手动 `python -m netops_ai.configs pull` 存的），
    「后」现拉一份并存下。返回 (给研判的那一段, 错误)。
    """
    try:
        from netops_ai import configs
    except ImportError:  # 开源版没有配置备份与前后 diff
        return "", ""

    before = configs.latest(host, before=alert_time_epoch or None)
    if before is None:
        return "", "还没存过这台设备的配置（先跑一次 python -m netops_ai.configs pull）"
    try:
        with _device_adapter_from_env({**env, "DEVICE_HOST": host}, vendor) as adapter:
            text, err = configs.pull(adapter, vendor)
    except Exception as exc:  # noqa: BLE001 - 拉不到配置不该让整次分析失败
        return "", f"{type(exc).__name__}: {exc}"
    if not text:
        return "", err
    now = time.time()
    configs.save(host, text, source="告警", when=now)
    return configs.render_diff(before, (now, configs.redact(text), "告警")), ""


def fetch_diagnostic_context(
    vendor: str,
    *,
    zabbix_text: str,
    alert_name: str,
    alert_time_epoch: int | None = None,
    device_host: str = "",
    host_filter: str = "",
) -> DiagnosticContextResult:
    """起 agent 工具循环取证。要不要查 SOP、要不要登设备，都由 agent 自己决定。"""
    env = _env()
    host = device_host or env.get("DEVICE_HOST", "")
    if not host:
        return DiagnosticContextResult(error="没配 DEVICE_HOST", mode="error")

    try:
        # **syslog 不再预塞。** Win 在 D1 上重跑对照（真实 一条真实告警）：
        # 预塞组 16285 字符的 syslog 把 token 预算挤爆，`incomplete`；不预塞组 agent
        # 从 Zabbix 的 `Syslog from <ip>` 监控项历史里自己捞到了日志，正常收尾。
        # 8 台设备都已接进 Zabbix 的 syslog 监控项、`show logging` 也放开了，
        # 自己再拼一份 SSH+tail 的文本只剩成本。见 ARCHITECTURE A14。
        (
            ai_text,
            ai_records,
            ai_trace,
            incomplete,
            termination,
            analysis,
            transcript,
            analysis_trace,
            analysis_error,
            sop_usage,
        ) = _run_ai_exploration(
            zabbix_text=zabbix_text,
            plan=None,
            host_filter=host_filter,
            device_host=host,
            alert_clock=alert_time_epoch,
            fault_time_epoch=alert_time_epoch,
        )
        trace = list(ai_trace)
        if incomplete:
            trace.append({"source": "ai", "incomplete": True, "termination_reason": termination})

        config_section, config_err = _config_diff_section(vendor, host, env, alert_time_epoch)
        trace.insert(0, {"source": "config", "tool": "config_diff", "args": {"device_host": host},
                         "ok": bool(config_section), "error": config_err})
        if config_section:
            ai_text = f"{config_section}\n{ai_text}"
        return DiagnosticContextResult(
            text=_redact_env_values(ai_text, env),
            device_records=json.loads(_redact_env_values(json.dumps(ai_records, ensure_ascii=False), env)),
            mode="ai",
            trace=json.loads(_redact_env_values(json.dumps(trace, ensure_ascii=False), env)),
            analysis=analysis,
            analysis_trace=json.loads(_redact_env_values(json.dumps(analysis_trace, ensure_ascii=False), env)),
            sop_usage=json.loads(_redact_env_values(json.dumps(sop_usage, ensure_ascii=False), env)),
            analysis_error=_redact_env_values(analysis_error, env),
            transcript=_redact_env_values(transcript, env),
        )
    except Exception as exc:  # noqa: BLE001
        return DiagnosticContextResult(
            error=_redact_env_values(f"{type(exc).__name__}: {exc}", env),
            mode="ai",
        )


# ---- incident 窗口：攒批 + 延迟分析 ---------------------------------------


@dataclass
class _PendingAlert:
    eventid: str
    payload: dict
    zabbix_text: str
    hostid: str
    clock: int
    triggerid: str
    tags: list
    interfaces: list
    name: str
    zbx_err: str
    started_at: str
    device_host: str = ""
    #: 这条告警对应拓扑里的哪台设备（`_alert_device_name` 填；查不到是空串，就不参与跨设备合并）
    device_name: str = ""


@dataclass
class _IncidentWindow:
    first_alert: _PendingAlert
    pending: list = field(default_factory=list)
    timer: threading.Timer | None = None
    #: 第一条到达的时刻（单调时钟，不受系统时间调整影响）
    opened_at: float = 0.0
    #: 批内告警类型算出来的最短等待，来了新类型可能变大，只增不减
    min_wait: int = 0
    #: 窗口里各告警对应的拓扑设备名（跨设备合并用）
    devices: set = field(default_factory=set)


_INCIDENT_WINDOWS: dict[str, _IncidentWindow] = {}
_INCIDENT_WINDOWS_LOCK = threading.Lock()

#: A16（负责人拍板）：并发告警协同，只做 Pi（pi.dev）那套里的 follow-up 语义
#: ——负责人原话「模仿 follow-up 就行」，不做 steering（打断正在跑的循环）。不是"先做一半，
#: 以后再补"，是这条并发协同就按 follow-up 定型：steering 要把 `run_agent_loop` 从
#: `agent.invoke()` 一口气跑完换成 `.stream()` 逐步跑、在每次工具调用之间加检查点，
#: 是更大幅度、影响主执行路径的改动，而 follow-up 已经覆盖了「处理前一条又来一条」
#: 这个真实场景。
#:
#: 窗口关闭之后 `_INCIDENT_WINDOWS` 那条记录就没了——这段时间里这台主机的
#: agent 循环正在跑（几十秒到几分钟）。旧代码这时候如果又来一条同主机的
#: 告警，`process_alert()` 看到 `_INCIDENT_WINDOWS` 里没这台主机，会**另开
#: 一个完全不知情的新窗口**，到时候又起一次独立的 agent 循环，跟正在跑的
#: 那次互相看不见对方——这正是负责人说的「处理前一条告警时又来一条」那个缺口。
#:
#: 参考 Pi（pi.dev）的 follow-up 语义抄过来：**不是另起一次盲跑，是排队**，
#: 等这台主机正在跑的那次完全跑完（`_flush_incident_window` 的 finally 里）
#: 才把排队的那批取出来接着处理，而且带上刚跑完那次的结论当提示
#: （`_followup_hint()`），让协同的这次不是从零开始。
#:
_ACTIVE_RUN_HOSTS: set[str] = set()
_FOLLOWUP_QUEUE: dict[str, _IncidentWindow] = {}
#: 正在跑的那次分析涉及哪些拓扑设备（key 同 `_ACTIVE_RUN_HOSTS`），邻居设备的新告警据此排进同一条队列。
_ACTIVE_RUN_DEVICES: dict[str, set] = {}

_HOST_LINE_RE = re.compile(r"## 主机：(.+?)（管理地址 ([^）]*)）")


def _alert_device_name(alert: "_PendingAlert") -> str:
    """这条告警对应拓扑里的哪台设备：先用告警文本里的 Zabbix 主机名（可能是别名），再用管理地址。
    拓扑读不到或对不上就返回空串——此时这条告警只按主机攒批，不参与跨设备合并。"""
    try:
        from netops_ai import topology as _topo

        devices = _topo.load_topology()
        m = _HOST_LINE_RE.search(alert.zabbix_text or "")
        names = [m.group(1).strip()] if m else []
        names.append(str((alert.payload or {}).get("host") or ""))
        for label in names:
            if label:
                device = _topo.resolve_device(devices, label)
                if device is not None:
                    return device.name
        ips = [alert.device_host, m.group(2).strip() if m else ""]
        for device in devices.values():
            if device.host and device.host in ips:
                return device.name
    except Exception:  # noqa: BLE001 — 合并只是优化，读拓扑出错不能影响收告警
        return ""
    return ""


def _related_devices(device: str) -> set:
    """这台设备自己 + 拓扑里和它有连线的邻居（连线两端的告警多半是同一件事）。"""
    if not device:
        return set()
    try:
        from netops_ai import topology as _topo

        devices = _topo.load_topology()
        related = {device}
        for dev in devices.values():
            peers = {link.peer for link in dev.links}
            if dev.name == device:
                related |= peers
            elif device in peers:
                related.add(dev.name)
        return related
    except Exception:  # noqa: BLE001
        return {device}


def _merge_related_windows(this_key: str, related: set) -> str:
    """把设备集合和 `related` 有交集的已开窗口并成一个（保留最早开的那个），返回要加入的窗口 key；没有就返回空串。
    **必须在持有 `_INCIDENT_WINDOWS_LOCK` 的情况下调用。**"""
    matches = [k for k, w in _INCIDENT_WINDOWS.items() if k == this_key or (w.devices & related)]
    if not matches:
        return ""
    target = min(matches, key=lambda k: _INCIDENT_WINDOWS[k].opened_at)
    target_window = _INCIDENT_WINDOWS[target]
    for key in matches:
        if key == target:
            continue
        other = _INCIDENT_WINDOWS.pop(key)
        if other.timer is not None:
            other.timer.cancel()
        target_window.pending.extend([other.first_alert, *other.pending])
        target_window.devices |= other.devices
        target_window.min_wait = max(target_window.min_wait, other.min_wait)
    return target


def _other_host_alert_text(alerts: list) -> str:
    """同一个窗口里别的设备上的告警原文，接在领头告警后面给 agent 看（同设备的靠 zbx_problems 自己查）。"""
    leading = alerts[0]
    others = [a for a in alerts[1:] if a.device_name and a.device_name != leading.device_name]
    if not others:
        return ""
    parts = [
        "## 同一个事件窗口里其它设备上的告警\n"
        "它们在拓扑上和上面这台直连，很可能是同一件事的另一端。是不是同一件事、谁是根因要你自己判断；"
        "是的话在 alert_roles / grouping 里归到同一组，结论里同时点名两端设备，不要各说各话。"
    ]
    for a in others[:6]:
        parts.append(f"\n### {a.device_name}（eventid {a.eventid}）\n{(a.zabbix_text or '')[:1800]}")
    return "".join(parts)


def _write_pending_stub(alert: _PendingAlert) -> None:
    """窗口还没关闭之前，先落一份占位记录，前端能看到"收到了，分析中"，
    不用等窗口关闭才第一次出现在列表里。flush 时会被完整记录覆盖。"""
    RECORDS_DIR.mkdir(exist_ok=True)
    stub = PipelineRecord(
        eventid=alert.eventid,
        webhook_payload=alert.payload,
        started_at=alert.started_at,
        zabbix_context_text=alert.zabbix_text,
        zabbix_hostid=alert.hostid,
        alert_clock=alert.clock,
        zabbix_fetch_error=alert.zbx_err,
        status="pending_window",
    )
    out_path = RECORDS_DIR / f"alert-{alert.eventid}.json"
    out_path.write_text(json.dumps(asdict(stub), ensure_ascii=False, indent=2), encoding="utf-8")


def _rearm_window_timer(host_key: str, window: _IncidentWindow, *, quiet: int, window_max: int) -> None:
    """重排这个窗口的关闭定时器。**必须在持有 `_INCIDENT_WINDOWS_LOCK` 的情况下调用。**

    关窗时刻 = max(现在 + 静默时间, 开窗 + 最短等待)，再被 开窗 + 硬上限 截断。
    每来一条新告警就重排一次，于是：

    - 一条孤立告警：静默 40s 就出结果，不用陪 BGP 等 220s
    - 级联还在陆续到：每来一条就往后延，延到硬上限为止
    - 批里有 BGP：哪怕中间静默了，也会等满 210s 再关，不会把后半截漏在窗口外
    """
    if window.timer is not None:
        window.timer.cancel()
    now = time.monotonic()
    elapsed = now - window.opened_at
    delay = max(quiet, window.min_wait - elapsed)
    delay = min(delay, max(0.0, window_max - elapsed))
    timer = threading.Timer(max(0.0, delay), _flush_incident_window, kwargs={"host_key": host_key})
    timer.daemon = True
    window.timer = timer
    timer.start()


def process_alert(payload: dict) -> dict:
    """webhook 收到事件之后立刻跑的轻量处理：拉这条告警自己的 Zabbix 原文，
    注册进同主机的 incident 窗口。**真正的取数、分析、CASE 库落地、发卡
    在窗口关闭时统一做**（见 `_flush_incident_window`），不在这里。
    """
    eventid = str(payload.get("eventid", ""))
    started_at = datetime.now(timezone.utc).isoformat()
    zabbix_text, hostid, alert_clock, zbx_err, meta = fetch_zabbix_context(eventid)
    env = _env()

    alert = _PendingAlert(
        eventid=eventid,
        payload=payload,
        zabbix_text=zabbix_text,
        hostid=hostid,
        clock=alert_clock,
        triggerid=meta.get("triggerid", ""),
        tags=meta.get("tags", []),
        interfaces=meta.get("interfaces", []),
        name=meta.get("name") or str(payload.get("name", "")),
        device_host=meta.get("device_host", ""),
        zbx_err=zbx_err,
        started_at=started_at,
    )
    _write_pending_stub(alert)

    if not zabbix_text:
        # Zabbix 上下文都取不到，没法攒批也没法分析，直接落一条失败记录，
        # 不进窗口（进了窗口也没有候选文本可用，会拖慢真正有数据的那些）。
        record = PipelineRecord(
            eventid=eventid,
            webhook_payload=payload,
            started_at=started_at,
            zabbix_fetch_error=zbx_err,
            analysis_error=f"Zabbix 上下文取不到（{zbx_err}），跳过分析",
            finished_at=datetime.now(timezone.utc).isoformat(),
            status="error",
        )
        (RECORDS_DIR / f"alert-{eventid}.json").write_text(
            json.dumps(asdict(record), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"eventid": eventid, "status": "error", "error": zbx_err}

    _, window_max = _incident_window_seconds(env)
    quiet = _incident_window_quiet_seconds(env)
    host_key = hostid or f"event:{eventid}"
    alert.device_name = _alert_device_name(alert)
    related = _related_devices(alert.device_name)
    with _INCIDENT_WINDOWS_LOCK:
        if related:
            # 同一条链路/邻接的另一端：并进已经开着的窗口（多个相关窗口会被并成一个）；
            # 没有开着的窗口但邻居的分析正在跑，就排进那条队列（带上它的结论当提示）。
            joined = _merge_related_windows(host_key, related)
            if joined:
                host_key = joined
            else:
                active = next((k for k, devs in _ACTIVE_RUN_DEVICES.items() if k in _ACTIVE_RUN_HOSTS and devs & related), "")
                if active:
                    host_key = active
        # 这台主机正有一次 agent 循环在跑——别另起一次盲跑，排队等它跑完（A16 follow-up）。
        if host_key in _ACTIVE_RUN_HOSTS:
            queued = _FOLLOWUP_QUEUE.get(host_key)
            if queued is None:
                queued = _IncidentWindow(first_alert=alert, opened_at=time.monotonic())
                _FOLLOWUP_QUEUE[host_key] = queued
            else:
                queued.pending.append(alert)
            if alert.device_name:
                queued.devices.add(alert.device_name)
            return {"eventid": eventid, "status": "queued_followup", "host_key": host_key}

        window = _INCIDENT_WINDOWS.get(host_key)
        if window is None:
            window = _IncidentWindow(
                first_alert=alert, opened_at=time.monotonic(), min_wait=_min_wait_for(alert)
            )
            _INCIDENT_WINDOWS[host_key] = window
            if alert.device_name:
                window.devices.add(alert.device_name)
        else:
            window.pending.append(alert)
            if alert.device_name:
                window.devices.add(alert.device_name)
            # 新来的告警可能把最短等待拉长（比如先来接口 down，后来 BGP 也报了）
            window.min_wait = max(window.min_wait, _min_wait_for(alert))
        _rearm_window_timer(host_key, window, quiet=quiet, window_max=window_max)
    window_seconds = max(0, window_max)

    return {"eventid": eventid, "status": "buffered_for_window", "window_seconds": window_seconds}


def flush_incident_window_now(hostid: str) -> dict | None:
    """测试/N6 验收脚本用：不等 timer，立刻把这台主机的窗口关掉。"""
    return _flush_incident_window(host_key=hostid)


def _followup_hint(host_key: str, result: dict) -> str:
    headline = str(result.get("headline") or "").strip()
    if not headline:
        return ""
    return (
        f"**协同提示（A16 follow-up）**：同一台设备（{host_key}）上一条相关告警"
        f"（eventid={'/'.join(result.get('eventids') or [])}）刚分析完，结论是：{headline}。"
        "这条新告警如果是同一个根因的延续，请对照判断、不用从零重查；"
        "如果明显无关，按新故障正常查。"
    )


def _flush_incident_window(*, host_key: str) -> dict | None:
    with _INCIDENT_WINDOWS_LOCK:
        window = _INCIDENT_WINDOWS.pop(host_key, None)
    if window is None:
        return None

    first_result: dict | None = None
    hint = ""
    with _INCIDENT_WINDOWS_LOCK:
        _ACTIVE_RUN_HOSTS.add(host_key)
        _ACTIVE_RUN_DEVICES[host_key] = set(window.devices)
    try:
        while window is not None:
            alerts = [window.first_alert, *window.pending]
            result = _process_incident_batch(host_key, alerts, followup_hint=hint)
            if first_result is None:
                first_result = result
            hint = _followup_hint(host_key, result)
            # 「查队列里有没有下一批」和「没有的话把这台主机标回没在跑」必须是
            # 同一次加锁——分两次的话，中间那道缝里新告警会被排进队列，
            # 但已经没人会再来看这个队列了，那条告警就永远卡在这儿。
            with _INCIDENT_WINDOWS_LOCK:
                window = _FOLLOWUP_QUEUE.pop(host_key, None)
                if window is None:
                    _ACTIVE_RUN_HOSTS.discard(host_key)
                    _ACTIVE_RUN_DEVICES.pop(host_key, None)
                else:
                    _ACTIVE_RUN_DEVICES[host_key] = set(window.devices)
    finally:
        with _INCIDENT_WINDOWS_LOCK:
            _ACTIVE_RUN_HOSTS.discard(host_key)
            _ACTIVE_RUN_DEVICES.pop(host_key, None)
    return first_result


def _process_incident_batch(hostid: str, alerts: list, *, followup_hint: str = "") -> dict:
    token = CURRENT_EVENTID.set(alerts[0].eventid)
    try:
        return _process_incident_batch_inner(hostid, alerts, followup_hint=followup_hint)
    finally:
        CURRENT_EVENTID.reset(token)


def _process_incident_batch_inner(hostid: str, alerts: list, *, followup_hint: str = "") -> dict:
    env = _env()
    vendor = env.get("DEVICE_VENDOR", "cisco")
    leading = alerts[0]
    elapsed: dict = {}

    # A16 follow-up：这批是排在上一次同主机分析后面接进来的，把上一次的结论
    # 当提示词的一部分喂进去——不是另起一次互不相干的盲跑（见 _flush_incident_window）。
    leading_zabbix_text = f"{followup_hint}\n\n{leading.zabbix_text}" if followup_hint else leading.zabbix_text
    others_text = _other_host_alert_text(alerts)
    if others_text:
        leading_zabbix_text = f"{leading_zabbix_text}\n\n{others_text}"

    t0 = time.time()
    diagnostic = fetch_diagnostic_context(
        vendor,
        zabbix_text=leading_zabbix_text,
        alert_name=leading.name,
        alert_time_epoch=leading.clock or None,
        device_host=leading.device_host,
        host_filter="",
    )
    elapsed["device_fetch"] = round(time.time() - t0, 2)
    device_text = diagnostic.text

    # **结论从取证那条轨迹里来，不再单独调 analyze()。**
    # 维护者定的：「研判你也砍了吧，取证那步就应该直接出结论。」
    # 原来是循环跑完把结果重拼成一段文本、再起一次独立调用——那次重拼每个工具
    # 只留 3000 字符，**研判看到的比取证少**，而且它发现证据不够也回不去查。
    # 现在循环跑完在同一份 messages 上再走一次带 schema 的调用（schema 仍然
    # 只在最后那一次，不进工具调用的轮次）。
    fault_time_epoch = leading.clock or None

    analysis_parsed: dict | None = None
    analysis_error = ""
    analysis_trace: dict = {}
    business_rule_violations: list = []
    evidence_verification: dict | None = None

    # **核对的底本换成整条轨迹。** 模型看的是它，核对器也必须看它——
    # 以前核对用的是管道另拼的一份（工具截到 3000 字符），引文对不上
    # 常常不是模型编的，是核对器少了一截。
    combined_zabbix_text = "\n\n".join(a.zabbix_text for a in alerts)
    source_text = diagnostic.transcript or f"{combined_zabbix_text}\n\n{device_text or ''}"

    if diagnostic.analysis:
        analysis_parsed = _with_derived_confidence(diagnostic.analysis)
        analysis_trace = diagnostic.analysis_trace or {"from": "agent_loop_final_schema"}
    else:
        analysis_error = (
            diagnostic.analysis_error
            or diagnostic.error
            or "取证轨迹末尾没有拿到结构化结论"
        )
    elapsed["analysis"] = round(time.time() - t0, 2)

    analysis_ok = bool(analysis_parsed)

    alerts_by_id = {a.eventid: a for a in alerts}
    grouping = (analysis_parsed or {}).get("grouping") or []
    alert_roles = {r.get("eventid"): r for r in (analysis_parsed or {}).get("alert_roles") or []}
    if not grouping:
        # 分析失败/没跑出 grouping 时，**兜底成"每条各成一组"，不是"全算一组"**。
        # 方向很要紧：把不相关的告警硬合成一件事，比拆开危险得多——出来一张卡说五件事，
        # 人看了也不知道哪件是真的。拆开最坏是多发几张卡，合错是给人一个假的因果。
        # 同一个桶里本来就相关的那些，模型正常跑出 grouping 时照样会合并，
        # 这条只在模型没表态的时候生效。
        grouping = [{"events": [a.eventid], "why_same": ""} for a in alerts]

    t0 = time.time()
    incident_by_eventid: dict[str, str] = {}
    incident_repeat: dict[str, int] = {}
    fingerprint_inputs: dict[str, dict] = {}
    now_epoch = int(time.time())
    for group in (grouping if analysis_ok else []):
        event_ids = [str(e) for e in (group.get("events") or []) if str(e) in alerts_by_id]
        if not event_ids:
            continue
        group_alerts = [alerts_by_id[e] for e in event_ids]
        triggerids = sorted({a.triggerid for a in group_alerts if a.triggerid})
        interfaces = sorted({i for a in group_alerts for i in (a.interfaces or [])})
        group_hostids = sorted({a.hostid for a in group_alerts if a.hostid})
        fingerprint = compute_fingerprint("+".join(group_hostids) if len(group_hostids) > 1 else hostid, triggerids, interfaces)
        for eid in event_ids:
            # 原料跟着 eventid 存，落记录时一起写进去，指纹才审计得动。
            fingerprint_inputs[eid] = {
                "fingerprint": fingerprint,
                "triggerids": list(triggerids),
                "objects": list(interfaces),
            }
        first_clock = min((a.clock for a in group_alerts if a.clock), default=now_epoch)
        last_clock = max((a.clock for a in group_alerts if a.clock), default=now_epoch)
        root_event_id = next(
            (eid for eid in event_ids
             if normalize_enum((alert_roles.get(eid) or {}).get("role")) == ROLE_ROOT), None
        )
        incident = Incident(
            incident_id=make_incident_id(fingerprint, first_clock),
            fingerprint=fingerprint,
            hostid=hostid,
            first_clock=first_clock,
            last_clock=last_clock,
            event_ids=event_ids,
            root_event_id=root_event_id,
            analysis=analysis_parsed,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        save_incident(incident, interfaces=interfaces)
        repeat_count = same_fingerprint_count(fingerprint, since=now_epoch - 30 * 86400)
        for eid in event_ids:
            incident_by_eventid[eid] = incident.incident_id
            incident_repeat[eid] = repeat_count
    elapsed["incident_save"] = round(time.time() - t0, 2)

    t0 = time.time()
    group_reports: dict[str, dict] = {}
    for group in (grouping if analysis_ok else []):
        event_ids = [str(e) for e in (group.get("events") or []) if str(e) in alerts_by_id]
        if not event_ids:
            continue
        group_hosts = list(dict.fromkeys(alerts_by_id[e].device_name or alerts_by_id[e].device_host for e in event_ids))
        alert_group = AlertGroup(
            eventids=event_ids,
            analysis=analysis_parsed or {},
            alert_names={eid: alerts_by_id[eid].name for eid in event_ids},
            host=" / ".join(g for g in group_hosts if g) if len(group_hosts) > 1 else alerts_by_id[event_ids[0]].device_host,
            violations=list(business_rule_violations),
            evidence_verification=evidence_verification,
        )
        result = report_alert_group(
            alert_group,
            env.get("FEISHU_WEBHOOK_URL", ""),
            app_id=env.get("FEISHU_APP_ID", ""),
            app_secret=env.get("FEISHU_APP_SECRET", ""),
            chat_id=env.get("FEISHU_CHAT_ID", ""),
        )
        for eid in event_ids:
            group_reports[eid] = {**result, "eventids": event_ids}
    elapsed["feishu_report"] = round(time.time() - t0, 2)

    finished_at = datetime.now(timezone.utc).isoformat()
    RECORDS_DIR.mkdir(exist_ok=True)
    for alert in alerts:
        other_events = [e for e in incident_by_eventid if incident_by_eventid[e] == incident_by_eventid.get(alert.eventid) and e != alert.eventid]
        record = PipelineRecord(
            eventid=alert.eventid,
            webhook_payload=alert.payload,
            started_at=alert.started_at,
            zabbix_context_text=alert.zabbix_text,
            zabbix_hostid=alert.hostid,
            alert_clock=alert.clock,
            zabbix_fetch_error=alert.zbx_err,
            device_commands_run=diagnostic.device_records,
            device_context_text=device_text,
            device_fetch_error=diagnostic.error,
            diagnostic_mode=diagnostic.mode,
            diagnostic_trace=diagnostic.trace,
            analysis_parsed=analysis_parsed,
            analysis_error=analysis_error,
            evidence_verification=evidence_verification,
            business_rule_violations=business_rule_violations,
            analysis_trace=analysis_trace,
            sop_usage=diagnostic.sop_usage,
            feishu_report=group_reports.get(alert.eventid, {}),
            incident_id=incident_by_eventid.get(alert.eventid, ""),
            incident_fingerprint=(fingerprint_inputs.get(alert.eventid) or {}).get("fingerprint", ""),
            incident_triggerids=(fingerprint_inputs.get(alert.eventid) or {}).get("triggerids", []),
            incident_objects=(fingerprint_inputs.get(alert.eventid) or {}).get("objects", []),
            incident_role=(alert_roles.get(alert.eventid) or {}).get("role", ""),
            incident_grouped_with=other_events,
            incident_repeat_count=incident_repeat.get(alert.eventid, 0),
            elapsed_seconds=elapsed,
            finished_at=finished_at,
            status="done" if analysis_ok else "analysis_failed",
        )
        out_path = RECORDS_DIR / f"alert-{alert.eventid}.json"
        out_path.write_text(json.dumps(asdict(record), ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "hostid": hostid,
        "eventids": [a.eventid for a in alerts],
        "groups": len(grouping) if analysis_ok else 0,
        "incident_ids": sorted(set(incident_by_eventid.values())),
        "elapsed_seconds": elapsed,
        # 给排在后面的 follow-up 批当提示用（见 _flush_incident_window）。
        "headline": (analysis_parsed or {}).get("root_cause", "") if analysis_ok else "",
    }

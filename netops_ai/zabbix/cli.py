"""zbx-cli：Zabbix 只读客户端上面的一层薄 argparse。

约定都写在这里而不是散在 README：模型会把 CLI 当工具调用，参数、输出、
错误码要稳定；人调试时也要一眼看到这是 read-only 工具，没有任何写入口。
"""

from __future__ import annotations

from datetime import datetime, timezone

import re

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from netops_ai.cli_spec import CommandSpec, ParamSpec
from netops_ai.zabbix.client import ZabbixAPIError, ZabbixClient, parse_line_receive_time

REPO_ROOT = Path(__file__).resolve().parents[2]

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_UPSTREAM = 4
EXIT_DENIED = 5
EXIT_RUNTIME = 1


# ParamSpec / CommandSpec 挪去了 `netops_ai/cli_spec.py`，跟 `nb-cli` 共用同一套
# 结构。这里保留同名引用，`zbx_cli.CommandSpec` 这种写法（`chat_agent.py` 在用）不变。
ParamSpec = ParamSpec
CommandSpec = CommandSpec


#: Unix 秒和 itemid 这类「本来就是数字」的参数：整数和数字字符串都收，进 handler 前统一成字符串。
#: 验证：SOP 渲染出 `since=1790395561`，外部 agent 照字面传整数，schema 只认 string，ValidationError 白跑一次。
ID_OR_TIME_SCHEMA: dict[str, Any] = {"type": ["integer", "string"]}


COMMON_PARAMS = (
    ParamSpec(
        "format",
        ("--format",),
        "输出格式。默认 json 给模型吃；table/pretty 给人看。",
        {"type": "string", "enum": ["json", "table", "pretty"]},
        default="json",
        choices=("json", "table", "pretty"),
    ),
    ParamSpec(
        "jq",
        ("-q", "--jq"),
        "轻量 JSON 裁剪表达式，如 .items[0].name 或 .[].host；避免把整坨数据塞进上下文。",
        {"type": "string"},
        default="",
    ),
    ParamSpec(
        "dry_run",
        ("--dry-run",),
        "只打印将调用的 Zabbix 只读方法和参数，不读取凭据、不发网络请求。",
        {"type": "boolean"},
        default=False,
    ),
)


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "hosts",
        "列主机",
        (
            ParamSpec("query", ("--query",), "按 host/name 包含匹配过滤（不区分大小写）。", {"type": "string"}, default=""),
            ParamSpec("limit", ("--limit",), "最多返回多少台，默认 50。", {"type": "integer", "minimum": 1}, default=50),
        ),
        handler="cmd_hosts",
    ),
    CommandSpec(
        "items",
        "列主机监控项",
        (
            ParamSpec(
                "host",
                ("--host",),
                # **写明"是设备不是接口"是被真实行为逼出来的。**
                # V 段实测：问「Gi0/1 怎么了」，agent 把 `Gi0/1` 当主机名
                # 传进来，查空报错之后才纠正过来——白跑一跳。
                # 描述里只写"主机名/可见名包含匹配"，模型没有线索知道
                # 这里要的是设备而不是它手上那个接口名。
                "**设备**名（Zabbix 主机名/可见名，包含匹配），例如 V1-vios。"
                "按主机匹配，不按接口匹配——`Gi0/1`、`GigabitEthernet0/1` 这类接口名"
                "填在这里匹配不到主机，报错里会列出 Zabbix 现有主机名。",
                {"type": "string"},
                required=True,
            ),
            ParamSpec("key", ("--key",), "按 key_ 包含过滤，如 net.if.in；接口类监控项的接口名在 name/key_ 里。", {"type": "string"}, default=""),
            ParamSpec("limit", ("--limit",), "最多返回多少项，默认 50。", {"type": "integer", "minimum": 1}, default=50),
        ),
        handler="cmd_items",
        description=(
            "列一台主机的监控项：itemid、name、key_、value_type（0 浮点/1 字符/2 日志/3 整数/4 文本）、lastvalue。"
            "host 按主机名/可见名包含匹配；key 按 key_ 包含过滤；最多返回 limit 项，"
            "total_matched 是过滤后的总数。"
        ),
    ),
    CommandSpec(
        "history",
        "取短期原始历史点",
        (
            ParamSpec("item_id", ("--item-id",), "Zabbix itemid（整数或数字字符串）。", ID_OR_TIME_SCHEMA, required=True),
            ParamSpec("since", ("--from",), "起始时间：Unix 秒（整数或数字字符串，如 1790391000）或相对时间字符串（如 -30m、-1h、-7d；单位 m/h/d/w）。结束时间固定是现在。", ID_OR_TIME_SCHEMA, default="-1h"),
            ParamSpec("limit", ("--limit",), "最多返回多少点，默认 100。", {"type": "integer", "minimum": 1}, default=100),
        ),
        handler="cmd_history",
        description=(
            "按 itemid 取监控项的原始采样点（history.get），从 since 到现在，按时间正序，最多 limit 点。"
            "原始点保留期较短（受 Zabbix housekeeper 清理），更早的数据只剩 trends 里的小时汇总。"
            "返回 item_id、from、to（Unix 秒）、value_type 和 points。"
        ),
    ),
    CommandSpec(
        "trends",
        "取长期趋势小时汇总",
        (
            ParamSpec("item_id", ("--item-id",), "Zabbix itemid（整数或数字字符串）。", ID_OR_TIME_SCHEMA, required=True),
            ParamSpec("since", ("--from",), "起始时间：Unix 秒（整数或数字字符串）或相对时间字符串（如 -7d、-30d；单位 m/h/d/w）。结束时间固定是现在。", ID_OR_TIME_SCHEMA, default="-30d"),
            ParamSpec("limit", ("--limit",), "最多返回多少小时点，默认 720。", {"type": "integer", "minimum": 1}, default=720),
        ),
        handler="cmd_trends",
        description=(
            "按 itemid 取监控项的小时汇总（trend.get）：每小时一个 min/avg/max，从 since 到现在。"
            "trends 保留期比原始点长；它按整点汇总，当前这一小时的数据还不在 trends 里。"
            "返回整体摘要（points/min/avg/max）和降采样到最多 120 点的 series。"
        ),
    ),
    CommandSpec(
        "syslog",
        "取一台设备在某个时间窗里的 syslog（Zabbix 收到的）",
        (
            ParamSpec("host", ("--host",), "设备名或 Zabbix 别名。", {"type": "string"}, required=True),
            ParamSpec("since", ("--from",), "起始时间：Unix 秒（整数或数字字符串，如 1790391000）或相对时间字符串（如 -30m、-2h；单位 m/h/d/w）。", ID_OR_TIME_SCHEMA, default="-30m"),
            ParamSpec("until", ("--to",), "结束时间，格式同 since；不填表示到现在。", ID_OR_TIME_SCHEMA, default=""),
            ParamSpec("include", ("--include",), "只留包含这段文字的行，如 LINK 或 Ethernet0/1。", {"type": "string"}, default=""),
            # 默认 500 不是随便定的：Zabbix history.get 的 limit 是按 clock 降序在服务端截断的，
            # 发生在我们按 line_time 过滤/排序之前。补发积压量一旦超过这个数，真正落在故障
            # 窗口内、clock 较早的证据会在到达我们代码之前就被截没——finding #4/5，理论
            # 边界，暂无真实样本，先把默认值抬高留出安全余量；agent 显式传更小的 --limit 时
            # 尊重它自己的选择，不强行覆盖。
            ParamSpec("limit", ("--limit",), "每个日志监控项最多取多少条，默认 500。", {"type": "integer", "minimum": 1}, default=500),
        ),
        handler="cmd_syslog",
        description=(
            "取一台设备在 since~until 时间窗里的 syslog。数据来源是 Zabbix 收到并存盘的日志"
            "（这台主机所有 value_type=2 的日志型监控项），不是设备本地的日志 buffer；"
            "设备没有把日志发给 Zabbix、或 Zabbix 没收到时，这里没有对应的行。"
            "按 line_time（这行日志内容自带的真实时间，解析不出来时退回 clock）排序返回，"
            "最多 40 行（超出时保留最近的，并给出 total_matched）。"
            "每行的 clock 是 Zabbix 写下这条历史的时刻，line_time 是这行内容自己描述的时刻——"
            "设备到采集之间的传输一断一续时，补发的旧行会跟很多行共用同一个 clock，"
            "这时 line_time 跟 clock 差得远（返回里会带 stale_lines 计数和 stale_note 说明）。"
            "返回 host、from、to（Unix 秒）、log_items（日志型监控项个数）、lines。"
            "本系统只读账号（ai-readonly）的登录/登出也会出现在日志里，它们由取证动作本身产生。"
        ),
    ),
    CommandSpec(
        "problems",
        "列当前问题",
        (
            ParamSpec("host", ("--host",), "可选：主机名/可见名包含匹配。", {"type": "string"}, default=""),
            ParamSpec("window", ("--window",), "只原样回显，不参与过滤。", {"type": "string"}, default="24h"),
            ParamSpec("limit", ("--limit",), "最多返回多少条，默认 50。", {"type": "integer", "minimum": 1}, default=50),
        ),
        handler="cmd_problems",
        description=(
            "列**当前未恢复**的问题（problem.get）：eventid、name、severity、clock、objectid，"
            "附排好版的 markdown 表。已经恢复的问题不在结果里。host 为空时列全部主机。"
        ),
    ),
    CommandSpec(
        "top-talkers",
        "按接口流量趋势排序",
        (
            ParamSpec("key", ("--key",), "监控项 key_ 包含匹配，默认 net.if.in。", {"type": "string"}, default="net.if.in"),
            ParamSpec("window", ("--window",), "趋势窗口，如 1h/24h/7d。", {"type": "string"}, default="1h"),
            ParamSpec("top", ("--top",), "返回前 N 个，默认 10。", {"type": "integer", "minimum": 1}, default=10),
            ParamSpec("limit", ("--limit",), "最多扫描多少个匹配监控项，默认 100。", {"type": "integer", "minimum": 1}, default=100),
        ),
        handler="cmd_top_talkers",
        description=(
            "按接口流量排行：扫描所有主机（不含 Zabbix server）里 key_ 匹配 key 的监控项"
            "（net.if.* 只取流量项本身，不含 errors/discards），取 window 内 trends 的最大小时均值和最大值"
            "（这一小时还没有 trends 时用 history），返回前 top 个。最多扫描 limit 个监控项。"
            "所有值都是 0 时返回 empty=true。"
        ),
    ),
    CommandSpec(
        "chart",
        "返回前端可绘图的时间序列",
        (
            ParamSpec("item_id", ("--item-id",), "Zabbix itemid（整数或数字字符串）。", ID_OR_TIME_SCHEMA, required=True),
            ParamSpec("since", ("--from",), "起始时间：Unix 秒（整数或数字字符串）或相对时间字符串（如 -1h、-7d）。窗口超过 48h 取 trends，否则取 history。", ID_OR_TIME_SCHEMA, default="-7d"),
            ParamSpec("limit", ("--limit",), "最多返回多少点，默认 336。", {"type": "integer", "minimum": 1}, default=336),
        ),
        handler="cmd_chart",
        description=(
            "返回某个监控项从 since 到现在的时间序列（窗口超过 48h 用 trends，否则用 history），"
            "降采样到最多 120 点，附 min/avg/max 摘要；前端会把这个结果渲染成趋势图。"
        ),
    ),
)


def _load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def _env() -> dict[str, str]:
    return {**_load_dotenv(REPO_ROOT / ".env"), **os.environ}


def parse_time(value: str | int, *, now: int | None = None) -> int:
    now = int(time.time()) if now is None else now
    value = str(value).strip()
    if value.isdigit():
        return int(value)
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    if len(value) >= 3 and value[0] == "-" and value[-1] in units and value[1:-1].isdigit():
        return now - int(value[1:-1]) * units[value[-1]]
    raise ValueError(f"不认识的时间 {value!r}：只接受 Unix 秒（如 1790391000）或相对时间（如 -30m/-1h/-7d）")


def _client() -> ZabbixClient:
    env = _env()
    return ZabbixClient(
        url=env.get("ZABBIX_URL", ""),
        user=env.get("ZABBIX_USER", ""),
        password=env.get("ZABBIX_PASSWORD", ""),
    )


#: 看起来像接口名的字符串。只用来在报错里给一句更有用的提示，
#: 不参与任何匹配逻辑。
_IFACE_LIKE = re.compile(r"^(?i:gi|te|fa|eth|xe|ge|gigabitethernet|tengigabitethernet)[\d/.:]+$")


def _host_match(host: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    q = query.lower()
    return q in str(host.get("host", "")).lower() or q in str(host.get("name", "")).lower()


def _resolve_host(zbx: ZabbixClient, query: str) -> dict[str, Any]:
    all_hosts = zbx.list_hosts(output=["hostid", "host", "name"])
    hosts = [h for h in all_hosts if _host_match(h, query)]
    if not hosts:
        # **查不到的时候把可选项摆出来。** 只说"没有匹配主机"，模型拿到
        # 这句话没有任何可操作信息，只能再猜一次。这跟 topology 那次
        # 查空是同一类问题、同一种解法。
        known = ", ".join(sorted({str(h.get("host") or h.get("name") or "") for h in all_hosts})[:20])
        hint = ""
        if _IFACE_LIKE.match(str(query or "")):
            hint = f"（{query!r} 的格式是接口名，这里按主机名匹配）"
        raise RuntimeError(f"没有匹配主机 {query!r}{hint}。Zabbix 里现有的主机：{known}")
    return hosts[0]


def _trim(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return items[: max(1, int(limit))]


def _by_clock(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: int(r.get("clock", 0)))


#: 一次最多回多少行 syslog。40 行是按 `pipeline.LOG_HISTORY_LIMIT` 对齐的——
#: 那边预取本机日志用的就是这个数，两处不该一个宽一个窄。
SYSLOG_MAX_LINES = 40

#: line_time 跟 clock 差多少秒就算「补发的旧行」，只用来给 stale_note 计数，不影响是否返回这一行。
STALE_GAP_SECONDS = 300


def cmd_syslog(args: argparse.Namespace) -> dict[str, Any]:
    """取一台设备在某个时间窗里的 syslog（**Zabbix 收到的，不是设备 buffer**）。

 **这个工具是 对照实验直接催出来的。** 那次 gpt-5.5 拿着能登
 任意设备的通道，五轮都说「无法确认是谁做的」——它跑过四次 `show logging`，
 但 A1 的日志 buffer 只有 4096 字节，**它自己每次 SSH 登录写进去的
 `%SSH-5-SSH2_USERAUTH` 正在把要找的证据挤出去**；决定性的
 `%SYS-5-CONFIG_I: Configured from console by console` 五轮一次都没出现过。

 同一批故障我们的 agent 五轮全判对，靠的不是推理更强，是 pipeline 预先把
 Zabbix 侧的日志型监控项取好塞给了它。**但预取只覆盖告警那一台**——
 要查对端就得 agent 自己动手，而自己动手原来要四步：
 列监控项 → 认出 `value_type=2` → 按 itemid 取历史 → 合并排序。
 gpt-5.5 都没走通这四步，那就不该指望模型，该把四步包成一个工具。
    """
    now = int(time.time())
    since = parse_time(args.since, now=now)
    until = parse_time(args.until, now=now) if args.until else now
    if args.dry_run:
        return {"dry_run": [{"method": "item.get + history.get", "params": {
            "host": args.host, "value_type": 2, "time_from": since, "time_till": until}}]}

    with _client() as zbx:
        host = _resolve_host(zbx, args.host)
        items = zbx.list_items(host["hostid"], output=["itemid", "name", "key_", "value_type"])
        log_items = [i for i in items if str(i.get("value_type")) == "2"]
        rows: list[dict[str, Any]] = []
        for item in log_items:
            try:
                hist = zbx.get_history(item["itemid"], history_type=2,
                                       time_from=since, time_till=until, limit=args.limit)
            except ZabbixAPIError:
                continue
            for h in hist:
                clock = int(h.get("clock", 0))
                value = h.get("value", "")
                # 一条真实告警 的 RCA：`clock` 是 Zabbix 写下这条历史的时刻，不是这行日志
                # 描述的事真的发生的时刻——设备到采集之间的传输一断一续，补发的旧行会跟
                # 补发抵达那一刻共用同一个 `clock`。能从行首的接收时间前缀读出真实时间就
                # 用它（`line_time`），读不出来就说明格式不认识，退回 `clock` 别猜。
                line_time = parse_line_receive_time(value, clock)
                rows.append({"clock": clock, "line_time": line_time if line_time is not None else clock,
                             "item": item.get("name", ""), "line": value})

    needle = str(args.include or "").strip()
    if needle:
        rows = [r for r in rows if needle.lower() in str(r["line"]).lower()]
    # 按 line_time（这行真实描述的时间）排序/截取，不是按 clock（Zabbix 写入的时间）——
    # 补发积压时一批旧行会共享同一个 clock，按 clock 排会把它们原样堆在一起，
    # 按 line_time 排它们才会落回自己本来的时间线位置。
    rows.sort(key=lambda r: r["line_time"])
    total = len(rows)
    stale = sum(1 for r in rows if abs(r["line_time"] - r["clock"]) > STALE_GAP_SECONDS)
    rows = rows[-SYSLOG_MAX_LINES:]       # 超量留最近的，故障窗口里后面几行通常更关键

    out: dict[str, Any] = {
        "host": host.get("host") or host.get("name"),
        "from": since, "to": until,
        "log_items": len(log_items),
        "lines": rows, "returned": len(rows), "total_matched": total,
    }
    if stale:
        out["stale_lines"] = stale
        out["stale_note"] = (
            f"{stale} 行的 line_time（行内真实时间）跟 clock（Zabbix 记录时间）"
            f"差了 {STALE_GAP_SECONDS // 60} 分钟以上，多半是设备补发了积压的旧消息，"
            "已按 line_time 排序——不代表这些行不重要，只是它们描述的不是最近发生的事。"
        )
    if total > len(rows):
        out["truncated"] = True
        out["note"] = f"窗口里共 {total} 行，返回的是最近的 {len(rows)} 行。"
    if not log_items:
        out["empty"] = True
        out["note"] = f"没有数据：{out['host']} 在 Zabbix 里没有日志型监控项（value_type=2），Zabbix 没有采集这台的 syslog。"
    elif not rows:
        out["empty"] = True
        # 原来写的是「{since} 到现在」，传了 until 也这么写——两个 agent 看到的就是这句不实的话。
        where = f"，include={needle!r}" if needle else ""
        out["note"] = (
            f"没有数据：{out['host']} 有 {len(log_items)} 个日志型监控项，"
            f"窗口 {since}~{until}（Unix 秒{where}）内没有匹配的行。"
        )
    return out


def cmd_hosts(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return {"dry_run": [{"method": "host.get", "params": {"output": ["hostid", "host", "name"]}}]}
    with _client() as zbx:
        hosts = [h for h in zbx.list_hosts(output=["hostid", "host", "name"]) if _host_match(h, args.query)]
    out: dict[str, Any] = {"hosts": _trim(hosts, args.limit), "count": min(len(hosts), args.limit), "total_matched": len(hosts)}
    if not hosts:
        out["empty"] = True
        out["note"] = f"没有数据：Zabbix 里没有 host/name 包含 query={args.query!r} 的主机。"
    return out


def cmd_items(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return {"dry_run": [{"method": "host.get", "params": {"output": ["hostid", "host", "name"]}}, {"method": "item.get", "params": {"hostids": "<resolved>", "output": ["itemid", "name", "key_", "value_type", "lastvalue"]}}]}
    with _client() as zbx:
        host = _resolve_host(zbx, args.host)
        items = zbx.list_items(host["hostid"], output=["itemid", "name", "key_", "value_type", "lastvalue"])
    if args.key:
        items = [i for i in items if args.key in str(i.get("key_", ""))]
    out: dict[str, Any] = {"host": host, "items": _trim(items, args.limit), "count": min(len(items), args.limit), "total_matched": len(items)}
    if not items:
        out["empty"] = True
        out["note"] = (
            f"没有数据：主机 {host.get('host') or host.get('name')} 存在，但没有 key_ 包含 key={args.key!r} 的监控项。"
        )
    return out


#: `history.get` 的 `history` 参数必须跟监控项 `value_type` 对上，传错了 Zabbix 不报错、只返回空列表。
def _history_type_for(zbx, item_id: str) -> int:
    value_type = zbx.get_item_value_type(item_id)
    return 0 if value_type is None else value_type


def cmd_history(args: argparse.Namespace) -> dict[str, Any]:
    now = int(time.time())
    since = parse_time(args.since, now=now)
    if args.dry_run:
        return {"dry_run": [{"method": "history.get", "params": {"itemids": args.item_id, "time_from": since, "time_till": now, "limit": args.limit}}]}
    with _client() as zbx:
        history_type = _history_type_for(zbx, args.item_id)
        rows = zbx.get_history(
            args.item_id, history_type=history_type, time_from=since, time_till=now, limit=args.limit
        )
    out: dict[str, Any] = {
        "item_id": args.item_id,
        "from": since,
        "to": now,
        # 把用了哪张表说出来。空结果时这是唯一能区分"真没数据"和"问错表"的线索。
        "value_type": history_type,
        "points": _by_clock(rows),
    }
    if not rows:
        out["empty"] = True
        out["note"] = (
            f"没有数据：history.get 查了 item_id={args.item_id} 的 value_type={history_type} 表，"
            f"窗口 {since}~{now}（Unix 秒，since={args.since!r}）内没有原始点。"
        )
    return out


def cmd_trends(args: argparse.Namespace) -> dict[str, Any]:
    now = int(time.time())
    since = parse_time(args.since, now=now)
    if args.dry_run:
        return {"dry_run": [{"method": "trend.get", "params": {"itemids": args.item_id, "time_from": since, "time_till": now, "limit": args.limit}}]}
    with _client() as zbx:
        rows = zbx.get_trends(args.item_id, time_from=since, time_till=now, limit=args.limit)
    series = [
        {
            "t": int(p["clock"]),
            "min": _as_float(p.get("value_min")),
            "avg": _as_float(p.get("value_avg")),
            "max": _as_float(p.get("value_max")),
        }
        for p in _by_clock(rows)
    ]
    out: dict[str, Any] = {
        "item_id": args.item_id,
        "from": since,
        "to": now,
        **_series_summary(series),
        **_downsample_payload(series, target=120),
    }
    if not rows:
        out["empty"] = True
        out["note"] = (
            f"没有数据：trend.get 查了 item_id={args.item_id}，"
            f"窗口 {since}~{now}（Unix 秒，since={args.since!r}）内没有小时汇总点。"
        )
    return out


#: severity 数字和 Unix 时间戳由工具自己排成表（返回的 `markdown` 字段），不交给模型翻译。
_SEVERITY_LABEL = {
    "0": "未分类", "1": "信息", "2": "警告",
    "3": "一般严重", "4": "严重", "5": "灾难",
}


def _local_tz_label() -> str:
    """`_clock_text` 用的是跑这个进程那台机器的本地时区（Win/Mac 实验机是 UTC+08:00），
 不是 Zabbix 服务器或设备的时区。表头写明偏移，免得读的人去猜（验证 外部 agent 反馈第 6 条）。
    """
    offset = datetime.now().astimezone().strftime("%z")  # +0800
    return f"UTC{offset[:3]}:{offset[3:]}" if offset else "本地时间"


def _clock_text(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).astimezone().strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(value or "")


def _problems_markdown(problems: list[dict[str, Any]], host: dict[str, Any] | None) -> str:
    """把当前告警排成一张 markdown 表。空的时候明确说空，不要返回空串
    ——空串会让模型以为工具失败了，转头自己编一段。"""
    where = f"（{host.get('name') or host.get('host')}）" if host else ""
    if not problems:
        return f"**当前没有未恢复的告警{where}。**"
    lines = [
        f"**当前未恢复的告警{where}，共 {len(problems)} 条**",
        "",
        f"| eventid | 级别 | 告警 | 开始时间（{_local_tz_label()}） |",
        "|---|---|---|---|",
    ]
    for row in problems:
        severity = _SEVERITY_LABEL.get(str(row.get("severity", "")), str(row.get("severity", "")))
        name = str(row.get("name", "")).replace("|", "\\|")
        lines.append(f"| `{row.get('eventid', '')}` | {severity} | {name} | {_clock_text(row.get('clock'))} |")
    return "\n".join(lines)


def cmd_problems(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return {"dry_run": [{"method": "problem.get", "params": {"output": "extend", "recent": False}}]}
    with _client() as zbx:
        filters: dict[str, Any] = {"output": ["eventid", "name", "severity", "clock", "objectid"], "limit": args.limit}
        host = None
        if args.host:
            host = _resolve_host(zbx, args.host)
            filters["hostids"] = host["hostid"]
        problems = zbx.get_active_problems(**filters)
    return {
        "host": host,
        "window": args.window,
        "problems": problems,
        "count": len(problems),
        # **排好的表，模型原样贴出去就行，不要自己重排。**
        "markdown": _problems_markdown(problems, host),
    }


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _point_value(point: dict[str, Any]) -> float:
    if "value" in point:
        return _as_float(point.get("value"))
    return _as_float(point.get("avg"))


def _series_summary(series: list[dict[str, Any]]) -> dict[str, Any]:
    if not series:
        return {"points": 0, "min": None, "avg": None, "max": None, "first_at": None, "last_at": None}
    values = [_point_value(p) for p in series]
    lows = [_as_float(p.get("min", _point_value(p))) for p in series]
    highs = [_as_float(p.get("max", _point_value(p))) for p in series]
    return {
        "points": len(series),
        "min": min(lows),
        "avg": sum(values) / len(values),
        "max": max(highs),
        "first_at": series[0].get("t"),
        "last_at": series[-1].get("t"),
    }


def _downsample(series: list[dict[str, Any]], *, target: int) -> list[dict[str, Any]]:
    if target <= 1 or len(series) <= target:
        return series
    last = len(series) - 1
    indexes = sorted({round(i * last / (target - 1)) for i in range(target)})
    return [series[i] for i in indexes]


def _downsample_payload(series: list[dict[str, Any]], *, target: int) -> dict[str, Any]:
    sampled = _downsample(series, target=target)
    return {
        "downsample": {
            "target_points": target,
            "original_points": len(series),
            "returned_points": len(sampled),
            "note": f"series 是降采样后的 {len(sampled)} 点，原始 {len(series)} 点；摘要字段按原始点计算。",
        },
        "series": sampled,
    }


def cmd_top_talkers(args: argparse.Namespace) -> dict[str, Any]:
    now = int(time.time())
    since = parse_time("-" + args.window if not args.window.startswith("-") else args.window, now=now)
    if args.dry_run:
        return {"dry_run": [{"method": "host.get", "params": {"output": ["hostid", "host", "name"]}}, {"method": "item.get", "params": {"hostids": "<each-host>", "key_filter": args.key}}, {"method": "trend.get", "params": {"itemids": "<each-item>", "time_from": since, "time_till": now}}]}
    rows: list[dict[str, Any]] = []
    scanned = 0
    with _client() as zbx:
        # Zabbix 服务器自己的虚拟接口（vunl0_*）是监控平面，不是被管网络设备；
        # 不排掉的话它经常霸占榜首，模型会顺着去查它而不是去设备上取证（真数据验出来的）。
        hosts = [h for h in zbx.list_hosts(output=["hostid", "host", "name"]) if h.get("host") != "Zabbix server"]
        for host in hosts:
            for item in zbx.list_items(host["hostid"], output=["itemid", "name", "key_", "value_type"]):
                key = str(item.get("key_", ""))
                if args.key not in key:
                    continue
                # `net.if.in` 也会匹配到 `net.if.in.errors[...]`、
                # `net.if.in.discards[...]` 这类计数项；top-talkers 是查流量的，
                # 只留 `net.if.xx[...]` 那个真正的流量项。
                #
                # **原来这条只写死了 `net.if.in`，`net.if.out` 没过滤。**
                # Win 实测：问「哪个口出向流量最大」时，
                # `limit=100` 被 V1 自己一堆 discard 项占满，真正的流量项没进来。
                if args.key.startswith("net.if.") and not key.startswith(args.key + "["):
                    continue
                scanned += 1
                if scanned > args.limit:
                    break
                trends = zbx.get_trends(item["itemid"], time_from=since, time_till=now, limit=24 * 31)
                avg = max((_as_float(t.get("value_avg")) for t in trends), default=0.0)
                mx = max((_as_float(t.get("value_max")) for t in trends), default=0.0)
                source = "trends"
                if not trends:
                    # **Zabbix 的 trend 是整点才落库的。** 问「过去 1 小时谁流量最大」、
                    # 而这一小时还没走完时，trends 是空的，整张榜全零——
                    # 模型拿到一份看着合法的空数据，会去挨个 `zbx_items` 核对，
                    # Win 那次就是这么烧掉 12 步的。
                    # 短窗口回落到 history，它是实时写的。
                    hist = zbx.get_history(
                        item["itemid"],
                        history_type=int(item.get("value_type") or 0),
                        time_from=since, time_till=now, limit=2000,
                    )
                    values = [_as_float(h.get("value")) for h in hist]
                    if values:
                        avg, mx, source = sum(values) / len(values), max(values), "history"
                    trends = hist
                rows.append({"host": host.get("host"), "itemid": item["itemid"], "name": item.get("name"), "key_": item.get("key_"), "max_avg": avg, "max": mx, "points": len(trends), "source": source})
            if scanned > args.limit:
                break
    rows.sort(key=lambda r: (r["max_avg"], r["max"]), reverse=True)
    top = rows[: args.top]
    out: dict[str, Any] = {
        "window": args.window, "key": args.key, "top": top,
        "scanned_items": min(scanned, args.limit),
    }
    # **「查到了一堆全零」和「没查到」对模型是一回事，对工具不是。**
    # 不说清楚，它只会换个参数再查一遍——这正是打转的起点。
    # 这种语义上的空，通用层的 `_tool_reply` 判不出来（结构上 `top` 非空），
    # 只能由工具自己说。
    if top and not any(r["max_avg"] or r["max"] for r in top):
        out["empty"] = True
        out["note"] = (
            f"没有数据：扫了 {out['scanned_items']} 个匹配 key={args.key!r} 的监控项，"
            f"window={args.window!r} 内所有值都是 0，榜单全零、排序没有意义。"
        )
    return out


def cmd_chart(args: argparse.Namespace) -> dict[str, Any]:
    now = int(time.time())
    since = parse_time(args.since, now=now)
    use_trends = (now - since) > 48 * 3600
    if args.dry_run:
        method = "trend.get" if use_trends else "history.get"
        return {"dry_run": [{"method": method, "params": {"itemids": args.item_id, "time_from": since, "time_till": now, "limit": args.limit}}]}
    with _client() as zbx:
        if use_trends:
            raw = zbx.get_trends(args.item_id, time_from=since, time_till=now, limit=args.limit)
            points = [{"t": int(p["clock"]), "min": _as_float(p.get("value_min")), "avg": _as_float(p.get("value_avg")), "max": _as_float(p.get("value_max"))} for p in _by_clock(raw)]
            source = "trends"
        else:
            raw = zbx.get_history(
                args.item_id,
                history_type=_history_type_for(zbx, args.item_id),
                time_from=since,
                time_till=now,
                limit=args.limit,
            )
            points = [{"t": int(p["clock"]), "value": _as_float(p.get("value"))} for p in _by_clock(raw)]
            source = "history"
    out: dict[str, Any] = {
        "item_id": args.item_id,
        "from": since,
        "to": now,
        "source": source,
        **_series_summary(points),
        **_downsample_payload(points, target=120),
    }
    if not points:
        out["empty"] = True
        out["note"] = (
            f"没有数据：item_id={args.item_id} 在窗口 {since}~{now}（Unix 秒，since={args.since!r}）"
            f"内没有 {source} 点，没有可画的数据。"
        )
    return out


def command_schema(spec: CommandSpec) -> dict[str, Any]:
    props: dict[str, Any] = {}
    required: list[str] = []
    for param in spec.params:
        props[param.name] = {**param.schema, "description": param.help}
        if param.default is not None:
            props[param.name]["default"] = param.default
        if param.required:
            required.append(param.name)
    return {
        "name": spec.name,
        "description": spec.help,
        "risk": "read-only",
        "parameters": {"type": "object", "properties": props, "required": required, "additionalProperties": False},
    }


def tools_payload() -> dict[str, Any]:
    return {"risk": "read-only", "commands": [command_schema(s) for s in COMMANDS]}


def _apply_jq(data: Any, expr: str) -> Any:
    if not expr:
        return data
    if not expr.startswith("."):
        raise ValueError("-q/--jq 只支持以 . 开头的轻量路径")
    current = data
    parts = [p for p in expr[1:].split(".") if p]
    for part in parts:
        expand = part.startswith("[]")
        if expand:
            part = part[2:]
        key, index = part, None
        if "[" in part and part.endswith("]"):
            key, _, tail = part.partition("[")
            index = int(tail[:-1])
        if expand:
            if not isinstance(current, list):
                raise ValueError(f"{expr} 里 [] 的左侧不是列表")
            current = [item.get(key) if key else item for item in current]
            continue
        if key:
            if isinstance(current, dict):
                current = current[key]
            else:
                raise ValueError(f"{expr} 里 {key!r} 的左侧不是对象")
        if index is not None:
            current = current[index]
    return current


def render(data: Any, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if fmt == "pretty":
        return json.dumps(data, ensure_ascii=False, indent=2)
    if isinstance(data, dict):
        rows = next((v for v in data.values() if isinstance(v, list)), [])
    else:
        rows = data
    if not isinstance(rows, list):
        return str(data)
    lines = []
    for row in rows:
        if isinstance(row, dict):
            lines.append(" | ".join(f"{k}={v}" for k, v in row.items() if not isinstance(v, (dict, list))))
        else:
            lines.append(str(row))
    return "\n".join(lines)


def add_common(parser: argparse.ArgumentParser) -> None:
    for param in COMMON_PARAMS:
        kwargs: dict[str, Any] = {"help": param.help, "default": param.default}
        if param.schema["type"] == "boolean":
            kwargs = {"help": param.help, "action": "store_true", "default": param.default}
        elif param.schema["type"] == "integer":
            kwargs["type"] = int
        if param.choices:
            kwargs["choices"] = param.choices
        parser.add_argument(*param.flags, dest=param.name, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zbx-cli",
        description="Zabbix read-only CLI。本工具不含任何写操作命令；凭据只从环境变量/.env 读取。",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for spec in COMMANDS:
        p = sub.add_parser(spec.name, help=spec.help, description=spec.description or spec.help)
        add_common(p)
        for param in spec.params:
            kwargs: dict[str, Any] = {"help": param.help, "required": param.required, "default": param.default}
            if param.schema["type"] == "integer":
                kwargs["type"] = int
            if param.choices:
                kwargs["choices"] = param.choices
            p.add_argument(*param.flags, dest=param.name, **kwargs)
        p.set_defaults(handler=spec.handler)
    tools = sub.add_parser("tools", help="输出全部命令及 JSON Schema，供 LangChain 启动时绑定工具。")
    add_common(tools)
    tools.set_defaults(handler="cmd_tools")
    schema = sub.add_parser("schema", help="输出单个命令的 JSON Schema。")
    add_common(schema)
    schema.add_argument("schema_command", choices=[s.name for s in COMMANDS])
    schema.set_defaults(handler="cmd_schema")
    return parser


def cmd_tools(args: argparse.Namespace) -> dict[str, Any]:
    return tools_payload()


def cmd_schema(args: argparse.Namespace) -> dict[str, Any]:
    spec = next(s for s in COMMANDS if s.name == args.schema_command)
    return command_schema(spec)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        handler: Callable[[argparse.Namespace], dict[str, Any]] = globals()[args.handler]
        data = handler(args)
        if getattr(args, "jq", ""):
            data = _apply_jq(data, args.jq)
        print(render(data, args.format))
        return EXIT_OK
    except SystemExit as exc:
        return int(exc.code)
    except PermissionError as exc:
        print(json.dumps({"error": str(exc), "kind": "denied"}, ensure_ascii=False), file=sys.stderr)
        return EXIT_DENIED
    except ZabbixAPIError as exc:
        msg = str(exc)
        kind = "auth" if "user.login" in msg or "认证" in msg or "Login name or password" in msg else "upstream"
        print(json.dumps({"error": msg, "kind": kind}, ensure_ascii=False), file=sys.stderr)
        return EXIT_AUTH if kind == "auth" else EXIT_UPSTREAM
    except (ValueError, argparse.ArgumentError) as exc:
        print(json.dumps({"error": str(exc), "kind": "usage"}, ensure_ascii=False), file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - CLI 顶层必须稳定返回分类错误
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}", "kind": "runtime"}, ensure_ascii=False), file=sys.stderr)
        return EXIT_RUNTIME


if __name__ == "__main__":
    raise SystemExit(main())

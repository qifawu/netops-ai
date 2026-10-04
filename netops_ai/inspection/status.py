"""状态巡检：趁没告警，登设备**只读**看一遍「现在」是不是健康。

趋势巡检（`scan.py`）只读 Zabbix 历史，回答不了「OSPF 邻居现在都 FULL 吗、接口有没有错误计数」。
这里补这一半：对拓扑里每台设备跑几条 show 命令（全部走 `DeviceAdapter.run()` 的白名单，被拒的不发出去），
再用**固定规则**判定，不调模型。

四项检查（Cisco IOS 一家，跟公开版只留 IOS 的决定一致）：

- `interfaces`：拓扑里登记了线缆的本端接口必须 up/up。管理关闭（administratively down）也算问题——
  拓扑说这里有线，设备说它关着。
- `ospf`：邻居必须全部 FULL；邻居数跟拓扑对得上（两端接口都配了 IP 的线才算三层互联）。
- `bgp`：每个邻居必须 Established（`State/PfxRcd` 是数字）。`% BGP not active` 就跳过。
- `errors`：线缆接口的 input/output errors、CRC 只看**增量**——跟上一次快照比。第一次巡检没有基线，
  累计值非 0 只给「关注」并写明没有基线；接口重启后计数回零（增量为负）不报。

每条结论带 `evidence`：**设备输出里的原话**，逐字，不改写。有测试锁着。
设备连不上本身就是一条严重发现，不是「没数据」。

不做：改配置、跑主动探测（ping/traceroute）。能力层不 import 框架。
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from netops_ai.topology import TopologyDevice, _default_adapter_factory, load_topology

COMMANDS = {
    "interfaces": "show ip interface brief",
    "ospf": "show ip ospf neighbor",
    "bgp": "show ip bgp summary",
    "errors": "show interfaces",
}
MAX_WORKERS = 4

AdapterFactory = Callable[[TopologyDevice], Any]

_BRIEF_ROW = re.compile(
    r"^(?P<name>\S+)\s+(?P<ip>\S+)\s+(?:YES|NO)\s+\S+\s+(?P<status>administratively down|up|down)\s+(?P<proto>up|down)\s*$",
    re.IGNORECASE,
)
_OSPF_ROW = re.compile(r"^\d+\.\d+\.\d+\.\d+\s+\d+\s+(?P<state>[A-Z0-9]+)(?:/\s*\S*)?\s+\S+\s+(?P<addr>\d+\.\d+\.\d+\.\d+)\s+(?P<iface>\S+)\s*$")
# peer V AS MsgRcvd MsgSent TblVer InQ OutQ Up/Down State/PfxRcd；State 可能带空格（`Idle (Admin)`），所以取 Up/Down 之后的整段
_BGP_ROW = re.compile(r"^(?P<peer>\d+\.\d+\.\d+\.\d+)\s+\d+\s+(?P<as>\d+)(?:\s+\d+){5}\s+\S+\s+(?P<state>.+?)\s*$")
_IF_HEADER = re.compile(r"^(?P<name>\S+) is .*line protocol is")
_IN_ERR = re.compile(r"(\d+) input errors, (\d+) CRC")
_OUT_ERR = re.compile(r"(\d+) output errors")


def _check(check: str, status: str, summary: str, evidence: list[str] | None = None, command: str = "") -> dict[str, Any]:
    return {"check": check, "status": status, "summary": summary, "evidence": evidence or [], "command": command}


def parse_brief(output: str) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for line in output.splitlines():
        m = _BRIEF_ROW.match(line.strip())
        if m:
            rows[m["name"]] = {"ip": m["ip"], "status": m["status"].lower(), "proto": m["proto"].lower(), "line": line.strip()}
    return rows


def parse_ospf(output: str) -> list[dict[str, str]]:
    return [
        {"state": m["state"], "addr": m["addr"], "iface": m["iface"], "line": line.strip()}
        for line in output.splitlines()
        if (m := _OSPF_ROW.match(line.strip()))
    ]


def parse_bgp(output: str) -> list[dict[str, str]]:
    return [
        {"peer": m["peer"], "state": m["state"], "line": line.strip()}
        for line in output.splitlines()
        if (m := _BGP_ROW.match(line.strip()))
    ]


def parse_errors(output: str) -> dict[str, dict[str, Any]]:
    """`show interfaces` 整份输出 → {接口: {input_errors, crc, output_errors, line}}。"""
    counters: dict[str, dict[str, Any]] = {}
    current = ""
    for line in output.splitlines():
        m = _IF_HEADER.match(line.strip())
        if m:
            current = m["name"]
            counters[current] = {"input_errors": 0, "crc": 0, "output_errors": 0, "lines": []}
            continue
        if not current:
            continue
        m_in, m_out = _IN_ERR.search(line), _OUT_ERR.search(line)
        if m_in:
            counters[current]["input_errors"], counters[current]["crc"] = int(m_in[1]), int(m_in[2])
            counters[current]["lines"].append(line.strip())
        if m_out:
            counters[current]["output_errors"] = int(m_out[1])
            counters[current]["lines"].append(line.strip())
    return counters


def check_interfaces(device: TopologyDevice, brief: dict[str, dict[str, str]], command: str) -> dict[str, Any]:
    bad: list[str] = []
    lines: list[str] = []
    for link in device.links:
        row = brief.get(link.local_interface)
        if row is None:
            bad.append(f"{link.local_interface} 在设备上找不到（拓扑里说它连着 {link.peer}）")
            continue
        if row["status"] != "up" or row["proto"] != "up":
            state = "管理关闭" if row["status"] == "administratively down" else f"{row['status']}/{row['proto']}"
            bad.append(f"{link.local_interface}（连 {link.peer}）{state}")
            lines.append(row["line"])
    if bad:
        return _check("interfaces", "bad", "；".join(bad), lines, command)
    return _check("interfaces", "ok", f"拓扑里登记的 {len(device.links)} 条线缆的本端接口全部 up/up", [], command)


def _routed_links(device: TopologyDevice, briefs: dict[str, dict[str, dict[str, str]]], by_name: dict[str, TopologyDevice]) -> int:
    """两端接口都配了 IP 才算三层互联（接入层口 unassigned，不指望有 OSPF 邻居）。"""
    count = 0
    for link in device.links:
        mine = briefs.get(device.name, {}).get(link.local_interface)
        peer = by_name.get(link.peer)
        theirs = briefs.get(peer.name, {}).get(link.peer_interface) if peer else None
        if mine and theirs and mine["ip"] != "unassigned" and theirs["ip"] != "unassigned":
            count += 1
    return count


def check_ospf(output: str, expected: int, command: str) -> dict[str, Any]:
    if output.strip().startswith("%"):
        return _check("ospf", "skip", "设备没跑 OSPF", [output.strip().splitlines()[0]], command)
    neighbors = parse_ospf(output)
    not_full = [n for n in neighbors if not n["state"].startswith("FULL")]
    if not_full:
        return _check("ospf", "bad", f"{len(not_full)} 个 OSPF 邻居不是 FULL：" + "、".join(f"{n['addr']}（{n['state']}）" for n in not_full),
                      [n["line"] for n in not_full], command)
    if expected == 0 and not neighbors:
        return _check("ospf", "skip", "这台设备没有三层互联，不检查 OSPF", [], command)
    if len(neighbors) < expected:
        return _check("ospf", "bad", f"OSPF 邻居 {len(neighbors)} 个，拓扑上这台有 {expected} 条三层互联，缺 {expected - len(neighbors)} 个",
                      [n["line"] for n in neighbors], command)
    return _check("ospf", "ok", f"{len(neighbors)} 个 OSPF 邻居全部 FULL（拓扑预期 {expected} 个）", [], command)


def check_bgp(output: str, command: str) -> dict[str, Any]:
    if output.strip().startswith("%") or "BGP not active" in output:
        return _check("bgp", "skip", "设备没跑 BGP", [output.strip().splitlines()[0]] if output.strip() else [], command)
    peers = parse_bgp(output)
    bad = [p for p in peers if not p["state"].isdigit()]
    if bad:
        return _check("bgp", "bad", f"{len(bad)} 个 BGP 邻居不是 Established：" + "、".join(f"{p['peer']}（{p['state']}）" for p in bad),
                      [p["line"] for p in bad], command)
    return _check("bgp", "ok" if peers else "skip", f"{len(peers)} 个 BGP 邻居全部 Established" if peers else "没有 BGP 邻居", [], command)


def check_errors(device: TopologyDevice, counters: dict[str, dict[str, Any]], baseline: dict[str, dict[str, Any]] | None, command: str) -> dict[str, Any]:
    grew: list[str] = []
    cumulative: list[str] = []
    lines: list[str] = []
    for link in device.links:
        cur = counters.get(link.local_interface)
        if cur is None:
            continue
        total = cur["input_errors"] + cur["output_errors"]
        old = (baseline or {}).get(link.local_interface)
        if old is not None:
            delta = total - (old["input_errors"] + old["output_errors"])
            if delta > 0:
                grew.append(f"{link.local_interface} 错误计数 +{delta}")
                lines.extend(cur["lines"])
        elif total > 0:
            cumulative.append(f"{link.local_interface} 累计错误 {total}")
            lines.extend(cur["lines"])
    if grew:
        return _check("errors", "bad", "；".join(grew), lines, command)
    if cumulative:
        return _check("errors", "warn", "；".join(cumulative) + "（没有上一次基线，判不了是不是还在涨，下次巡检起按增量判断）", lines, command)
    return _check("errors", "ok", "线缆接口错误计数没有增长" if baseline else "线缆接口错误计数均为 0", [], command)


def _clean_error(text: str) -> str:
    """把 `Command '['ssh', ...全部参数...]' timed out after 10 seconds` 这类带整条 ssh 命令行的报错压成人话：
    命令行里有账号名和一串连接参数，不该原样进页面和导出的报告。"""
    text = re.sub(r"Command '\[.*?\]'", "ssh 命令", text, flags=re.DOTALL)
    return text.strip()[:200]


def inspect_device(device: TopologyDevice, adapter_factory: AdapterFactory) -> dict[str, Any]:
    """只做取数，不判定——判定要跨设备（邻居数要看对端），放在 `run_status_inspection` 里。"""
    raw: dict[str, str] = {}
    entry: dict[str, Any] = {"name": device.name, "host": device.host, "role": device.role, "reachable": False, "error": "", "raw": raw}
    try:
        with adapter_factory(device) as adapter:
            for key, command in COMMANDS.items():
                result = adapter.run(command)
                if not result.allowed:
                    raw[key] = ""
                    entry.setdefault("denied", []).append(command)
                    continue
                raw[key] = result.output if result.ok else ""
                if key == "interfaces" and not result.ok:
                    raise RuntimeError(result.error or "命令执行失败")
        entry["reachable"] = True
    except Exception as exc:  # noqa: BLE001 - 一台连不上不能拖垮整轮
        entry["error"] = _clean_error(f"{type(exc).__name__}: {exc}")
    return entry


def run_status_inspection(
    *,
    topology: dict[str, TopologyDevice] | None = None,
    adapter_factory: AdapterFactory = _default_adapter_factory,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """跑一轮。`baseline` 是上一次快照（用来判错误计数增量）。返回可直接落盘的快照。"""
    topo = topology if topology is not None else load_topology()
    devices = list(topo.values())
    by_name = {d.name: d for d in devices}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        collected = list(pool.map(lambda d: inspect_device(d, adapter_factory), devices))

    briefs = {e["name"]: parse_brief(e["raw"].get("interfaces", "")) for e in collected if e["reachable"]}
    old_counters = (baseline or {}).get("counters", {})
    out_devices: list[dict[str, Any]] = []
    counters_out: dict[str, Any] = {}
    for entry in collected:
        device = by_name[entry["name"]]
        raw = entry.pop("raw")
        checks: list[dict[str, Any]] = []
        if not entry["reachable"]:
            checks.append(_check("reachable", "bad", f"设备无法登录：{entry['error']}", [], ""))
        else:
            checks.append(check_interfaces(device, briefs[device.name], COMMANDS["interfaces"]))
            checks.append(check_ospf(raw.get("ospf", ""), _routed_links(device, briefs, by_name), COMMANDS["ospf"]))
            checks.append(check_bgp(raw.get("bgp", ""), COMMANDS["bgp"]))
            counters = parse_errors(raw.get("errors", ""))
            counters_out[device.name] = {k: {f: v[f] for f in ("input_errors", "crc", "output_errors")} for k, v in counters.items()}
            checks.append(check_errors(device, counters, old_counters.get(device.name), COMMANDS["errors"]))
        out_devices.append({**entry, "checks": checks})
    return {
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "devices": out_devices,
        "counters": counters_out,
        "summary": summarize(out_devices),
    }


def summarize(devices: list[dict[str, Any]]) -> dict[str, int]:
    tally = {"devices": len(devices), "bad": 0, "warn": 0, "ok": 0, "skip": 0, "unreachable": 0}
    for dev in devices:
        if not dev.get("reachable"):
            tally["unreachable"] += 1
        for c in dev.get("checks", []):
            tally[c["status"]] = tally.get(c["status"], 0) + 1
    return tally

"""nb-cli：NetBox 台账上的只读命令行，跟 `zbx-cli` 一样由 `COMMANDS` 表同时生成命令行和工具。

    python3 -m netops_ai.netbox_cli devices | topology | neighbors D1
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from netops_ai.cli_spec import CommandSpec, ParamSpec
from netops_ai import topology as topo
from netops_ai.topology_cli import NEIGHBORS_SPEC, cmd_neighbors  # noqa: F401  公开的 topology_neighbors 工具

#: 角色从上到下。判"上联"要靠它：邻居的角色比自己靠上就是上联。
ROLE_ORDER = {"core": 0, "aggregation": 1, "access": 2}
ROLE_CN = {"core": "核心", "aggregation": "汇聚", "access": "接入"}


def _devices() -> dict[str, Any]:
    return topo.load_topology()


def _row(name: str, dev) -> dict[str, Any]:
    return {
        "name": name,
        "role": dev.role,
        "role_cn": ROLE_CN.get(dev.role, dev.role or "未标"),
        "mgmt_ip": dev.host,
        "zabbix_host": dev.aliases[0] if dev.aliases else "",
        "neighbor_count": len(dev.links),
    }


def _empty_note(source: str, netbox_error: str, *, query: str = "", role: str = "") -> str:
    """空结果只陈述事实（用的哪个源、NetBox 报了什么错、过滤条件是什么），不给改法。"""
    filters = []
    if query:
        filters.append(f"query={query!r}")
    if role:
        filters.append(f"role={role!r}")
    where = f"，过滤条件 {'、'.join(filters)}" if filters else ""
    if netbox_error:
        return f"没有数据：NetBox 不可用（{netbox_error}），已退回 {source}，其中没有匹配的设备{where}。"
    return f"没有数据：台账源 {source} 可读，没有匹配的设备{where}。"


def _mark_empty(out: dict[str, Any], *, query: str = "", role: str = "") -> dict[str, Any]:
    if out.get("count"):
        return out
    out["empty"] = True
    out["note"] = _empty_note(
        str(out.get("source") or "unknown"),
        str(out.get("netbox_error") or ""),
        query=query,
        role=role,
    )
    return out


def cmd_devices(ns) -> dict[str, Any]:
    """台账列表。这是 `nb_devices` 工具的实现。"""
    q = (getattr(ns, "query", "") or "").lower()
    role = (getattr(ns, "role", "") or "").lower()
    rows = []
    for name, dev in sorted(_devices().items()):
        if role and dev.role != role:
            continue
        if q and q not in name.lower() and not any(q in a.lower() for a in dev.aliases):
            continue
        rows.append(_row(name, dev))
    return _mark_empty(
        {"source": topo.LAST_SOURCE, "netbox_error": topo.LAST_NETBOX_ERROR, "count": len(rows), "devices": rows},
        query=q,
        role=role,
    )


def cmd_topology(ns) -> dict[str, Any]:
    """全网拓扑一次给全，省工具调用：一台台问，光拓扑就把预算打满。不探对端（这是台账问题）。"""
    devices = _devices()
    rows, single = [], []
    for name, dev in sorted(devices.items()):
        mine = ROLE_ORDER.get(dev.role, 99)
        up = [l.peer for l in dev.links if ROLE_ORDER.get(getattr(devices.get(l.peer), "role", ""), 99) < mine]
        row = _row(name, dev)
        row["uplinks"] = sorted(set(up))
        rows.append(row)
        # 上联只有一条 = 那条断了整台失联。核心没有上联，不算在内。
        if dev.role != "core" and len(set(up)) == 1:
            single.append(name)
    return _mark_empty({
        "source": topo.LAST_SOURCE,
        "netbox_error": topo.LAST_NETBOX_ERROR,
        "count": len(rows),
        "devices": rows,
        "single_homed": single,
        "note": (
            "全图摘要，没有探测对端可达性。single_homed 是只有一条上联的设备，"
            "那条链路断了整台就失联。core 角色不计上联。"
        ),
    })


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "devices",
        "列台账：设备名、角色、管理 IP、在 Zabbix 里叫什么",
        (
            ParamSpec("query", ("--query",), "按设备名或 Zabbix 别名过滤。", {"type": "string"}, default=""),
            ParamSpec("role", ("--role",), "只看某一层。", {"type": "string", "enum": list(ROLE_ORDER)},
                      default="", choices=tuple(ROLE_ORDER)),
        ),
        handler="cmd_devices",
        tool_name="nb_devices",
    ),
    NEIGHBORS_SPEC,
    CommandSpec(
        "topology",
        "全图摘要：每台的角色、邻居数、上联，以及哪些设备只有一条上联",
        (),
        handler="cmd_topology",
        tool_name="nb_topology",
        description=(
            "全图摘要，一次返回全部设备：每台的角色（核心/汇聚/接入）、管理 IP、Zabbix 主机名、"
            "邻居数、上联设备，以及 single_homed（只有一条上联的设备，core 不计）。"
            "不含逐条链路的本端/对端接口，也不探测对端可达性。"
        ),
    ),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nb-cli", description="NetBox 台账只读查询（没有写入口）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for spec in COMMANDS:
        sp = sub.add_parser(spec.name, help=spec.help)
        for p in spec.params:
            if p.flags and not p.flags[0].startswith("-"):
                sp.add_argument(p.name, help=p.help)
            else:
                sp.add_argument(*p.flags, dest=p.name, default=p.default, help=p.help,
                                choices=list(p.choices) or None)
    ns = parser.parse_args(argv)
    spec = next(s for s in COMMANDS if s.name == ns.cmd)
    print(json.dumps(globals()[spec.handler](ns), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

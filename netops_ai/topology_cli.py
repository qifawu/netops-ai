"""`topology_neighbors` 工具：查一台设备的邻居（本地 `topology.yaml`）。

SOP 引擎和剧本引用着这个工具名，所以它的名字和返回形状是稳定的。
`netbox_cli.py` 在这张表上再加 NetBox 台账的两个工具（`nb_devices`、`nb_topology`）。
"""

from __future__ import annotations

import json
from typing import Any

from netops_ai import topology as topo
from netops_ai.cli_spec import CommandSpec, ParamSpec


def cmd_neighbors(ns) -> dict[str, Any]:
    """查一台设备的邻居。这是 `topology_neighbors` 工具的实现，名字是历史的，别改。"""
    return json.loads(topo.topology_neighbors(getattr(ns, "device", ""), getattr(ns, "interface", "") or ""))


NEIGHBORS_SPEC = CommandSpec(
    "neighbors",
    "查一台设备接了哪些邻居，带对端接口和管理 IP",
    (
        ParamSpec("device", ("device",), "设备名，拓扑名或 Zabbix 主机名都行。",
                  {"type": "string"}, required=True),
        ParamSpec("interface", ("--interface",), "只看某个接口，不填就全部。", {"type": "string"}, default=""),
    ),
    handler="cmd_neighbors",
    tool_name="topology_neighbors",
    description=(
        "查一台设备接了哪些邻居：本地接口、对端设备、对端接口、对端管理 IP、设备角色"
        "（核心/汇聚/接入）。"
        "真源是 NetBox（没配就退回版本化的 topology.yaml），"
        "返回里的 source 字段写明这次用的是哪个。设备名可以用拓扑名，也可以用 Zabbix "
        "主机名；名字查不到时返回可用的设备名列表。interface 填了就只返回这个接口的邻居。"
    ),
)

COMMANDS: tuple[CommandSpec, ...] = (NEIGHBORS_SPEC,)

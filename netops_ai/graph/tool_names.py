"""Shared names for read-only agent tools."""

from __future__ import annotations

try:
    from netops_ai import netbox_cli as nb_cli  # NetBox 台账工具
except ImportError:  # 开源版：只有 topology_neighbors（本地 yaml）
    from netops_ai import topology_cli as nb_cli
from netops_ai.zabbix import cli as zbx_cli

DEVICE_SHOW_TOOL_NAME = "device_show"
DEVICE_SHOW_MANY_TOOL_NAME = "device_show_many"
SOP_LOOKUP_TOOL_NAME = "sop_lookup"
TOPOLOGY_NEIGHBORS_TOOL_NAME = "topology_neighbors"
#: 查本系统自己产出的历史分析结论。**前端收敛成一个界面之后补的**——
#: 「看看之前那条告警 AI 怎么判的」以前要开另一个页面，现在得在对话里做完。
#: 跑一次只读巡检（或读最近一次的结果）。
RUN_INSPECTION_TOOL_NAME = "run_inspection"
LIST_ANALYSES_TOOL_NAME = "list_analyses"
GET_ANALYSIS_TOOL_NAME = "get_analysis"


#: 同一个能力两个名字：agent 叫 `device_show`，SOP 动作叫 `device`。落进剧本前要翻译，
#: 否则批准后每一步都是 `unknown SOP action tool`。
SOP_ACTION_TOOL_BY_AGENT_TOOL = {
    DEVICE_SHOW_TOOL_NAME: "device",
    DEVICE_SHOW_MANY_TOOL_NAME: "device",
    TOPOLOGY_NEIGHBORS_TOOL_NAME: TOPOLOGY_NEIGHBORS_TOOL_NAME,
}


def sop_action_tool(agent_tool_name: str) -> str:
    """把 agent 的工具名翻成 SOP 引擎的动作名。没登记的原样返回，
    让 `approve.py` 的校验去拦，而不是在这里悄悄换个名字。
    """
    return SOP_ACTION_TOOL_BY_AGENT_TOOL.get(agent_tool_name, agent_tool_name)


def zabbix_tool_name(command_name: str) -> str:
    return "zbx_" + command_name.replace("-", "_")


def registered_readonly_tool_names() -> set[str]:
    return {
        DEVICE_SHOW_TOOL_NAME,
        DEVICE_SHOW_MANY_TOOL_NAME,
        SOP_LOOKUP_TOOL_NAME,
        RUN_INSPECTION_TOOL_NAME,
        LIST_ANALYSES_TOOL_NAME,
        GET_ANALYSIS_TOOL_NAME,
        # **这两组从各自的 spec 表算，不手写。** `topology_neighbors` 以前是
        # 手写在这张清单里的：加一个 NetBox 工具就得记得来这里补一行，
        # 忘了的症状是白名单里没有它、调用被拒，而拒绝理由长得像模型编了个
        # 不存在的工具。名字只该有一个出处。
        *(zabbix_tool_name(spec.name) for spec in zbx_cli.COMMANDS),
        *(spec.tool_name for spec in nb_cli.COMMANDS),
    }

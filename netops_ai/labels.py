"""人可见标签的唯一来源。

**之后这里的活变少了**：schema 的枚举值本身就是中文了，
新产生的结论不再需要出口替换。剩下两个用途——
① 读 之前的历史记录（那些存的是英文 code）；
② 工具名转中文（`sop_lookup` → 查团队剧本，这本来就不是模型产出的）。
"""

from __future__ import annotations

import re


LABELS: dict[str, str] = {
    # analysis/schema.py
    "local_action": "本端有人动过配置",
    "local_hardware_or_resource": "本端硬件或资源问题",
    "remote_or_upstream": "对端或上游的问题",
    "link_or_path_quality": "链路或路径质量问题",
    "management_plane_or_reachability": "管理面或可达性问题",
    "monitoring_or_collection_artifact": "监控采集自身的问题",
    "supported": "有证据支持",
    "ruled_out": "已排除",
    "cannot_determine": "暂时无法判断",
    "direct": "直接反证",
    "absence": "未发现相关记录",
    "fault_window": "故障窗口",
    "current_snapshot": "当前快照",
    "time_invariant": "与时间无关",
    "root": "根因",
    "consequence": "连带",
    "independent": "无关",
    "high": "高",
    "medium": "中",
    "low": "低",
    "agent_can_retry": "智能体可继续取证",
    "needs_human": "需要人工取证",
    # inspection
    "trend": "持续单向变化",
    "periodic_spike": "周期性冲高",
    "self_healing_flap": "反复抖动又自愈",
    "worsening": "持续走坏",
    "improving": "持续好转",
    "unknown": "持续单向变化",
    # provenance/source
    "zabbix": "监控",
    "device": "设备",
    "sop": "剧本",
    "ai_self": "智能体自主选择",
    "human": "人工编写",
    "ai_proposed_human_approved": "智能体提出、人工批准",
    # readonly tools
    "device_show": "查设备只读信息",
    "device_show_many": "批量查设备只读信息",
    "sop_lookup": "查团队剧本",
    "topology_neighbors": "查拓扑邻居",
    "run_inspection": "读取或运行巡检",
    "list_analyses": "列历史研判",
    "get_analysis": "读取历史研判",
    "zbx_hosts": "列监控主机",
    "zbx_items": "列监控项",
    "zbx_history": "查短期历史",
    "zbx_trends": "查长期趋势",
    "zbx_problems": "查当前告警",
    "zbx_syslog": "查设备日志",
    "zbx_top_talkers": "查接口流量排行",
    "zbx_chart": "生成趋势图",
    "nb_devices": "查纳管设备",
    "nb_topology": "查全网拓扑",
}

#: 判断"这是一个独立的词"时，**路径和标识符里的分隔符不算词边界**。
#:
#: 真实事故：一张飞书卡上出现 `/var/log/监控/zabbix_server.log`。
#: 原来的边界只排除 `[A-Za-z0-9_]`，`/` 算边界，于是路径中段的 `zabbix`
#: 被换成了「监控」（`zabbix_server` 因为后面是 `_` 反而躲过一劫）。
#: 网络工程师照着这条路径敲，拿到的是 no such file。
#:
#: 加上 `/ \ . -` 之后，路径、域名、带连字符的标识符都不再被误伤；
#: 中文句子里独立出现的 `zabbix` 仍然会被替换。
_WORD_CHARS = r"A-Za-z0-9_/\\.\-"

_CODE_PATTERN = re.compile(
    rf"(?<![{_WORD_CHARS}])("
    + "|".join(re.escape(code) for code in sorted(LABELS, key=len, reverse=True))
    + rf")(?![{_WORD_CHARS}])"
)


def label_for(code: object, *, fallback: str = "") -> str:
    """返回 code 的中文标签；未知值按调用方选择是否原样保留。"""
    text = str(code or "")
    return LABELS.get(text, fallback or text)


def humanize_text(text: object) -> str:
    """把自由文本中的已登记内部标识替换成中文，未知文本保持原样。"""
    value = str(text or "")
    return _CODE_PATTERN.sub(lambda match: LABELS[match.group(1)], value)


def humanize_value(value):
    """递归清洗接口/卡片要展示的文本，不改变结构键和数值。"""
    if isinstance(value, str):
        return humanize_text(value)
    if isinstance(value, list):
        return [humanize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: humanize_value(item) for key, item in value.items()}
    return value

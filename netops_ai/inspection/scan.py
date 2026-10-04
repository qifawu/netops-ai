"""巡检编排：只读 Zabbix 历史，跑 `detectors.py` 的三类判定，产出发现列表。不登设备。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from netops_ai.inspection.config import InspectionConfig
from netops_ai.inspection.detectors import (
    detect_periodic_spike,
    detect_self_healing_flap,
    detect_trend,
)
from netops_ai.inspection.explain import downsample, explain
from netops_ai.zabbix.client import ZabbixAPIError, ZabbixClient

#: 数值型监控项（Zabbix value_type：0=float，3=unsigned）才能跑这三类判定，
#: 1=character/2=log/4=text 这几类是文本，不适用
_NUMERIC_VALUE_TYPES = {0, 3}

#: key_ 命中这些前缀/子串的判定为"状态类监控项"（1=up/2=down 这种编码，
#: 跟 SNMP ifOperStatus/ifAdminStatus 的标准编码一致），只有这类才跑
#: 自愈 flap 检测——CPU/流量这类连续数值套用 down=2/up=1 没有意义
_STATUS_ITEM_MARKERS = (
    "net.if.status",
    "ifoperstatus",
    "ifadminstatus",
    "net.if.admin.status",
    "operstate",
    "zabbix[host,snmp,available]",
)

_HIGHER_IS_WORSE_KEY_MARKERS = (
    "loss",
    "fail",
    "error",
    "down",
    "dropped",
    "discard",
    "timeout",
    "unreachable",
    "icmppingsec",
)
_HIGHER_IS_WORSE_NAME_MARKERS = ("loss", "fail", "error", "response time", "latency", "discard", "dropped")
_HIGHER_IS_BETTER_KEYS = {"icmpping"}
_HIGHER_IS_BETTER_KEY_MARKERS = ("available",)


def _is_status_item(key: str) -> bool:
    low = key.lower()
    return any(marker in low for marker in _STATUS_ITEM_MARKERS)


def _looks_like_discrete_status(key: str, item_name: str, series: list[tuple[int, float]]) -> bool:
    """辅助兜底：真实 H4 基线里 Linux operstate 只有 2/6 两个编码值，
    但 `icmpping` 也只有 0/1，所以不能只按"唯一值少"一刀切。这里要求
    key/name 同时呈现状态语义，避免误伤可达率、丢包率这类离散但可趋势化
    的指标。
    """
    low_key = key.lower()
    low_name = item_name.lower()
    if low_key in _HIGHER_IS_BETTER_KEYS or "icmpping" in low_key:
        return False
    status_words = ("status", "state", "available", "availability")
    if not any(word in low_key or word in low_name for word in status_words):
        return False
    values = {value for _, value in series}
    if len(values) > 4:
        return False
    return all(float(value).is_integer() for value in values)


def _higher_is_worse_for_item(key: str, item_name: str) -> bool | None:
    """这个监控项是越大越好还是越大越坏。只认几类确定的（ping、loss/error/down……），其余保持 unknown。"""
    low_key = key.lower()
    low_name = item_name.lower()
    if low_key in _HIGHER_IS_BETTER_KEYS:
        return False
    if any(marker in low_key for marker in _HIGHER_IS_WORSE_KEY_MARKERS):
        return True
    if any(marker in low_name for marker in _HIGHER_IS_WORSE_NAME_MARKERS):
        return True
    if any(marker in low_key for marker in _HIGHER_IS_BETTER_KEY_MARKERS):
        return False
    return None


@dataclass
class ItemFinding:
    """一条监控项上的一个发现，带着定位信息（哪台主机哪个 item），
    不只是裸的 detector 结果——巡检报告要能告诉人"去查哪个接口"。
    """

    host_name: str
    item_name: str
    item_key: str
    finding: object  # TrendFinding | PeriodicSpikeFinding | SelfHealingFlapFinding
    #: 新增（只加不改）：命中的规则 + 阈值 vs 实际值；降采样后的原始序列（≤60 点）
    rule: dict | None = None
    series: list | None = None

    @property
    def kind(self) -> str:
        """检测器类型提到顶层，统计时不用挖进嵌套字段。"""
        return self.finding.kind


@dataclass
class ScanReport:
    scanned_hosts: int = 0
    scanned_items: int = 0
    items_with_data: int = 0
    findings: list[ItemFinding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def scan_host(
    zbx: ZabbixClient, host_id: str, host_name: str, *, lookback_seconds: int = 7 * 86400,
    config: InspectionConfig | None = None,
) -> ScanReport:
    """扫一台主机所有数值型监控项，跑三类判定，返回这台主机的发现。

    `config=None` 就是默认配置（阈值取自检测器函数默认值、不限范围），跟没有 inspection.yaml 时一致。
    """
    cfg = config or InspectionConfig()
    report = ScanReport()
    try:
        items = zbx.list_items(host_id, output=["itemid", "name", "key_", "value_type"])
    except ZabbixAPIError as exc:
        report.errors.append(f"{host_name}: list_items 失败：{exc}")
        return report

    time_till = int(time.time())
    time_from = time_till - lookback_seconds

    for item in items:
        value_type = int(item.get("value_type", -1))
        if value_type not in _NUMERIC_VALUE_TYPES:
            continue
        if not cfg.item_in_scope(item.get("key_", "")):
            continue
        report.scanned_items += 1
        try:
            hist = zbx.get_history(
                item["itemid"], history_type=value_type, time_from=time_from, time_till=time_till, limit=10000
            )
        except ZabbixAPIError as exc:
            report.errors.append(f"{host_name}/{item['name']}: get_history 失败：{exc}")
            continue
        if not hist:
            continue
        report.items_with_data += 1
        series = sorted((int(h["clock"]), float(h["value"])) for h in hist)

        key = item.get("key_", "")
        sampled = downsample(series)

        def add(finding, kind_name):
            report.findings.append(ItemFinding(
                host_name, item["name"], key, finding,
                rule=explain(finding, series, cfg.detectors[kind_name]), series=sampled,
            ))

        if _is_status_item(key) or _looks_like_discrete_status(key, item["name"], series):
            flap = detect_self_healing_flap(series, **cfg.detectors["self_healing_flap"])
            if flap:
                add(flap, "self_healing_flap")
        else:
            trend = detect_trend(
                series, higher_is_worse=_higher_is_worse_for_item(key, item["name"]), **cfg.detectors["trend"]
            )
            if trend:
                add(trend, "trend")
            spike = detect_periodic_spike(series, **cfg.detectors["periodic_spike"])
            if spike:
                add(spike, "periodic_spike")

    return report


def scan_all_hosts(
    zbx: ZabbixClient, *, lookback_seconds: int = 7 * 86400, config: InspectionConfig | None = None
) -> ScanReport:
    """扫这个 Zabbix 实例上的全部主机（范围之内的），汇总成一份报告。"""
    cfg = config or InspectionConfig()
    combined = ScanReport()
    hosts = zbx.list_hosts(output=["hostid", "host"])
    for host in hosts:
        if not cfg.host_in_scope(host["host"]):
            continue
        sub = scan_host(zbx, host["hostid"], host["host"], lookback_seconds=lookback_seconds, config=cfg)
        combined.scanned_hosts += 1
        combined.scanned_items += sub.scanned_items
        combined.items_with_data += sub.items_with_data
        combined.findings.extend(sub.findings)
        combined.errors.extend(sub.errors)
    return combined

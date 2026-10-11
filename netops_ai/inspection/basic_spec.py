"""「基础巡检」检查什么——给基础巡检页顶部说明区用的只读描述。

**不在前端写死**：状态巡检每项用的命令现读 `status.COMMANDS`，趋势巡检的检测器和当前阈值现读
`inspection.yaml`（`config.describe()`）。这里只补「检查什么 / 判定规则 / 作用范围」这几句人话，
跟 `status.py` 的固定规则一一对应；`COMMANDS` 里多了一项而这里没写说明，测试会红（不让说明悄悄缺一项）。

判定全部由固定规则完成，不调模型；巡检页上可选的「AI 处置建议」只对已发现的结果做归类和建议。
"""

from __future__ import annotations

from typing import Any

from netops_ai.inspection import config as _config
from netops_ai.inspection import status as _status

#: 状态巡检每一项的说明（键跟 `status.COMMANDS` 一致）。规则来自 `status.py` 的 check_* 函数。
STATUS_CHECKS: dict[str, dict[str, dict[str, str]]] = {
    "interfaces": {
        "title": {"zh": "接口 up/up", "en": "Interfaces up/up"},
        "rule": {"zh": "拓扑里登记了线缆的本端接口必须 up/up；管理关闭（administratively down）也算问题",
                 "en": "Every interface with a cable in the topology must be up/up; administratively down counts as a problem"},
        "scope": {"zh": "拓扑里登记了线缆的接口", "en": "Interfaces with a cable recorded in the topology"},
    },
    "ospf": {
        "title": {"zh": "OSPF 邻居全 FULL", "en": "OSPF neighbors all FULL"},
        "rule": {"zh": "邻居必须全部 FULL，邻居数要跟拓扑对得上",
                 "en": "All neighbors must be FULL and their number must match the topology"},
        "scope": {"zh": "两端接口都配了 IP 的三层互联", "en": "Layer-3 links (both ends have an IP address)"},
    },
    "bgp": {
        "title": {"zh": "BGP 会话 Established", "en": "BGP sessions Established"},
        "rule": {"zh": "每个邻居必须 Established（State/PfxRcd 是数字）；没起 BGP 的设备跳过",
                 "en": "Every neighbor must be Established (State/PfxRcd is a number); devices without BGP are skipped"},
        "scope": {"zh": "全部设备", "en": "All devices"},
    },
    "errors": {
        "title": {"zh": "错误计数增量", "en": "Error counter increase"},
        "rule": {"zh": "input/output errors、CRC 只看跟上一次快照比的增量；第一次没有基线只给「关注」；计数回零不报",
                 "en": "input/output errors and CRC are judged by the increase since the last snapshot; no baseline on the first run gives a warning only; a counter reset is not reported"},
        "scope": {"zh": "拓扑里登记了线缆的接口", "en": "Interfaces with a cable recorded in the topology"},
    },
}

#: 趋势检测器的英文名和说明（中文直接用 `config.py` 里给表单用的那份）
DETECTOR_EN: dict[str, tuple[str, str]] = {
    "trend": ("Sustained one-way trend", "Splits the series into its first and last third and compares the means; adjacent points must also move in the same direction often enough."),
    "periodic_spike": ("Periodic spike", "Buckets by UTC hour; an hour whose mean is clearly above the overall mean, on several different days."),
    "self_healing_flap": ("Flapping that heals itself", "Status items only (up/down): repeated down → up within a short time."),
}

#: 状态巡检每一项对应的检查模板（「以基础巡检为起点，创建自定义巡检」用它预置草案）
BASIC_TEMPLATES: dict[str, str] = {"interfaces": "interfaces", "ospf": "ospf_neighbors", "bgp": "bgp_sessions", "errors": "interface_errors"}


def seed_draft(topology: dict[str, Any], lang: str = "zh") -> dict[str, Any]:
    """以基础巡检为起点的草案：预置基础巡检那几项检查（检查模板），设备范围留给用户在气泡里选。"""
    from netops_ai.inspection import plans as P

    tids = [BASIC_TEMPLATES[k] for k in _status.COMMANDS if k in BASIC_TEMPLATES]
    draft = P.apply_patch(None, {"add_checks": [{"template": t, "devices": None} for t in tids]}, topology, lang)
    draft["description"] = "以基础巡检为起点：接口、OSPF、BGP、错误计数" if lang != "en" else "Based on the basic inspection: interfaces, OSPF, BGP, error counters"
    return draft


#: 检测器参数的英文名（中文直接用 config.py 的表单标签）
PARAM_EN: dict[str, str] = {
    "min_points": "Min. points", "min_time_span_seconds": "Min. time span", "monotonic_fraction_threshold": "Same-direction share",
    "min_relative_change": "Min. change of mean", "min_distinct_days": "Min. days", "spike_ratio_threshold": "Spike ratio",
    "down_value": "\"down\" value", "up_value": "\"up\" value", "min_flaps": "Min. flaps", "max_recovery_seconds": "Max. recovery time",
}

#: 基础巡检周期的可选值（分钟）：关闭 / 15 / 30 / 60 / 360 / 每天
SCHEDULE_OPTIONS = (0, 15, 30, 60, 360, 1440)


def build_spec(lang: str = "zh") -> dict[str, Any]:
    zh = lang != "en"
    k = "zh" if zh else "en"
    checks = []
    for key, command in _status.COMMANDS.items():
        d = STATUS_CHECKS.get(key) or {}
        checks.append({"key": key, "command": command,
                       "title": (d.get("title") or {}).get(k, key),
                       "rule": (d.get("rule") or {}).get(k, ""),
                       "scope": (d.get("scope") or {}).get(k, "")})
    try:
        desc = _config.describe(_config.load_config())
    except _config.ConfigError as exc:
        desc = _config.describe(error=str(exc))
    values = desc["values"]["detectors"]
    detectors = []
    for item in desc["schema"]:
        name = item["name"]
        label, help_ = (item["label"], item["help"]) if zh else DETECTOR_EN.get(name, (name, ""))
        detectors.append({
            "name": name, "label": label, "help": help_,
            "params": [{"key": p["key"], "label": p["label"] if zh else PARAM_EN.get(p["key"], p["key"]), "unit": p.get("unit", "") if zh else "",
                        "value": values.get(name, {}).get(p["key"])} for p in item["params"]],
        })
    return {
        "status_checks": checks,
        "detectors": detectors,
        "lookback_days": desc["values"]["scope"].get("lookback_days"),
        "config_file": desc["file"],
        "config_error": desc["error"],
        "note": ("判定全部由固定规则完成，不调用模型；可选的模型建议只用于对已发现的结果做归类和处置建议。" if zh
                 else "Every verdict comes from fixed rules; no model is called. The optional model advice only groups and comments on what was found."),
    }

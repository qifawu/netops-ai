"""巡检配置：三个检测器的阈值、扫描范围、忽略清单，落在仓库根的 `inspection.yaml`。

**没有这个文件 = 行为跟以前完全一样。** 默认值不在这里再抄一遍，而是直接从
`detectors.py` 里三个检测器的函数签名读出来——改默认值只有一个地方，两边不会分叉。

只有一个文件、固定路径（环境变量 `NETOPS_INSPECTION_CONFIG` 只给测试指向临时目录用），
接口层不接受任何路径参数。
"""

from __future__ import annotations

import fnmatch
import inspect
import math
import os
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from netops_ai.inspection import detectors

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "inspection.yaml"

DETECTOR_FUNCS = {
    "trend": detectors.detect_trend,
    "periodic_spike": detectors.detect_periodic_spike,
    "self_healing_flap": detectors.detect_self_healing_flap,
}
#: 不是阈值的参数（由 scan 按监控项语义传入），不进配置
_NOT_A_THRESHOLD = {"series", "higher_is_worse"}

DEFAULT_LOOKBACK_DAYS = 7
SCOPE_LIST_KEYS = ("include_hosts", "exclude_hosts", "include_item_keys", "exclude_item_keys")
_MAX_LIST = 200
_MAX_PATTERN_LEN = 200
_MAX_IGNORE = 2000
_MAX_REASON_LEN = 200

_WRITE_LOCK = threading.Lock()


class ConfigError(ValueError):
    """配置不合法。message 是给人看的话，接口层原样当 400/422 的说明。"""


def config_path() -> Path:
    override = os.environ.get("NETOPS_INSPECTION_CONFIG")
    return Path(override) if override else CONFIG_PATH


# ---- 参数元信息：校验和前端表单共用，不在前端再抄一份范围 ----
# kind: int | float | number（int 或 float 都行，状态码这种）
PARAM_META: dict[str, dict[str, dict[str, Any]]] = {
    "trend": {
        "min_points": {"kind": "int", "min": 2, "max": 100000, "unit": "个",
                       "label": "最少数据点", "help": "序列短于这个点数就不判。"},
        "min_time_span_seconds": {"kind": "int", "min": 0, "max": 30 * 86400, "unit": "秒",
                                  "label": "最短时间跨度", "help": "首尾时间差小于这个值不判，避免几分钟的波动被当成趋势。"},
        "monotonic_fraction_threshold": {"kind": "float", "min": 0.01, "max": 1.0, "unit": "比例 0~1",
                                         "label": "同向比例下限", "help": "相邻两点同方向变化的占比至少要到这么多。"},
        "min_relative_change": {"kind": "float", "min": 0.001, "max": 100.0, "unit": "比例（0.2=20%）",
                                "label": "前后段均值最小变化", "help": "序列切成前 1/3 和后 1/3 比均值，变化幅度至少要到这么多。"},
    },
    "periodic_spike": {
        "min_points": {"kind": "int", "min": 2, "max": 100000, "unit": "个",
                       "label": "最少数据点", "help": "序列短于这个点数就不判。"},
        "min_time_span_seconds": {"kind": "int", "min": 0, "max": 30 * 86400, "unit": "秒",
                                  "label": "最短时间跨度", "help": "至少要覆盖这么久，才谈得上“每天”。"},
        "min_distinct_days": {"kind": "int", "min": 1, "max": 60, "unit": "天",
                              "label": "至少出现的天数", "help": "同一小时冲高的模式要在这么多个不同日期都出现。"},
        "spike_ratio_threshold": {"kind": "float", "min": 1.0, "max": 1000.0, "unit": "倍",
                                  "label": "冲高倍数下限", "help": "该小时的均值要达到整体均值的这么多倍。"},
    },
    "self_healing_flap": {
        "down_value": {"kind": "number", "min": -1e6, "max": 1e6, "unit": "状态码",
                       "label": "“掉线”的取值", "help": "状态类监控项里代表 down 的值（SNMP ifOperStatus 是 2）。"},
        "up_value": {"kind": "number", "min": -1e6, "max": 1e6, "unit": "状态码",
                     "label": "“在线”的取值", "help": "代表 up 的值（ifOperStatus 是 1）。"},
        "min_flaps": {"kind": "int", "min": 1, "max": 1000, "unit": "次",
                      "label": "最少抖动次数", "help": "down 后很快自己恢复，这样的循环至少出现几次才报。"},
        "max_recovery_seconds": {"kind": "number", "min": 1, "max": 7 * 86400, "unit": "秒",
                                 "label": "最长算“自愈”的时间", "help": "down 之后超过这个时间才恢复的，不算自愈，不计入。"},
    },
}

DETECTOR_LABELS = {
    "trend": "趋势（单向持续变化）",
    "periodic_spike": "周期性冲高",
    "self_healing_flap": "反复抖动又自愈",
}
DETECTOR_HELP = {
    "trend": "把序列切成前 1/3 和后 1/3 比均值，同时要求相邻点同方向变化的比例够高——两条都满足才算趋势。",
    "periodic_spike": "按 UTC 小时分桶，某个小时的均值明显高于整体均值，且在多个不同日期都出现。",
    "self_healing_flap": "只对状态类监控项（up/down 编码）：down 之后很快自己恢复的循环出现多次。",
}


def default_detector_params() -> dict[str, dict[str, Any]]:
    """从检测器函数签名读默认值。"""
    out: dict[str, dict[str, Any]] = {}
    for name, fn in DETECTOR_FUNCS.items():
        params = {
            p.name: p.default
            for p in inspect.signature(fn).parameters.values()
            if p.kind is inspect.Parameter.KEYWORD_ONLY and p.name not in _NOT_A_THRESHOLD
        }
        # 元信息和函数签名必须一一对应，少一边就是改了检测器忘了改配置
        if set(params) != set(PARAM_META[name]):
            raise RuntimeError(f"检测器 {name} 的参数 {sorted(params)} 与 PARAM_META {sorted(PARAM_META[name])} 不一致")
        out[name] = params
    return out


def default_scope() -> dict[str, Any]:
    return {**{k: [] for k in SCOPE_LIST_KEYS}, "lookback_days": DEFAULT_LOOKBACK_DAYS}


@dataclass
class InspectionConfig:
    detectors: dict[str, dict[str, Any]] = field(default_factory=default_detector_params)
    scope: dict[str, Any] = field(default_factory=default_scope)
    ignore: list[dict[str, str]] = field(default_factory=list)

    @property
    def lookback_seconds(self) -> int:
        return int(self.scope["lookback_days"]) * 86400

    def host_in_scope(self, host: str) -> bool:
        return _in_scope(host, self.scope["include_hosts"], self.scope["exclude_hosts"])

    def item_in_scope(self, key: str) -> bool:
        return _in_scope(key, self.scope["include_item_keys"], self.scope["exclude_item_keys"])

    def is_ignored(self, host: str, item_key: str) -> dict | None:
        for entry in self.ignore:
            if entry["host"] == host and entry["item_key"] == item_key:
                return entry
        return None

    def to_dict(self) -> dict:
        return {"detectors": deepcopy(self.detectors), "scope": deepcopy(self.scope), "ignore": deepcopy(self.ignore)}


def _in_scope(name: str, include: list[str], exclude: list[str]) -> bool:
    low = name.lower()
    if include and not any(fnmatch.fnmatchcase(low, p.lower()) for p in include):
        return False
    return not any(fnmatch.fnmatchcase(low, p.lower()) for p in exclude)


DEFAULT_CONFIG = InspectionConfig()


# ---- 校验 ----

def _num(value: Any, kind: str, where: str, meta: dict) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} 必须是数字，现在是 {type(value).__name__}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(f"{where} 必须是有限的数字")
    if kind == "int":
        if isinstance(value, float):
            if not value.is_integer():
                raise ConfigError(f"{where} 必须是整数，现在是 {value}")
            value = int(value)
    elif kind == "float":
        value = float(value)
    if not (meta["min"] <= value <= meta["max"]):
        raise ConfigError(f"{where} 必须在 {meta['min']} ~ {meta['max']} 之间，现在是 {value}")
    return value


def validate_detectors(raw: Any) -> dict[str, dict[str, Any]]:
    """缺省的参数用默认值补齐，多出来的键直接拒绝（拼错了不能悄悄吞掉）。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("detectors 必须是键值对")
    unknown = set(raw) - set(DETECTOR_FUNCS)
    if unknown:
        raise ConfigError(f"detectors 里有不认识的检测器：{sorted(unknown)}，只有 {sorted(DETECTOR_FUNCS)}")
    defaults = default_detector_params()
    out: dict[str, dict[str, Any]] = {}
    for name, defs in defaults.items():
        section = raw.get(name)
        if section is None:
            section = {}
        if not isinstance(section, dict):
            raise ConfigError(f"detectors.{name} 必须是键值对")
        extra = set(section) - set(defs)
        if extra:
            raise ConfigError(f"detectors.{name} 里有不认识的参数：{sorted(extra)}，只有 {sorted(defs)}")
        merged = {}
        for key, default in defs.items():
            meta = PARAM_META[name][key]
            merged[key] = _num(section[key], meta["kind"], f"detectors.{name}.{key}", meta) if key in section else default
        out[name] = merged
    flap = out["self_healing_flap"]
    if flap["down_value"] == flap["up_value"]:
        raise ConfigError("detectors.self_healing_flap：down_value 和 up_value 不能相同")
    return out


def _pattern_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{where} 必须是列表")
    if len(value) > _MAX_LIST:
        raise ConfigError(f"{where} 最多 {_MAX_LIST} 项")
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{where} 里每一项必须是非空字符串")
        item = item.strip()
        if len(item) > _MAX_PATTERN_LEN or any(ord(c) < 32 for c in item):
            raise ConfigError(f"{where} 里有不合法的项（过长或含控制字符）")
        out.append(item)
    return out


def validate_scope(raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("scope 必须是键值对")
    unknown = set(raw) - set(SCOPE_LIST_KEYS) - {"lookback_days"}
    if unknown:
        raise ConfigError(f"scope 里有不认识的键：{sorted(unknown)}")
    out: dict[str, Any] = {k: _pattern_list(raw.get(k), f"scope.{k}") for k in SCOPE_LIST_KEYS}
    meta = {"min": 1, "max": 90}
    out["lookback_days"] = (
        _num(raw["lookback_days"], "int", "scope.lookback_days", meta) if "lookback_days" in raw else DEFAULT_LOOKBACK_DAYS
    )
    return out


def normalize_ignore_entry(raw: Any, *, where: str = "ignore") -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} 每一项必须是键值对")
    unknown = set(raw) - {"host", "item_key", "reason", "at"}
    if unknown:
        raise ConfigError(f"{where} 里有不认识的键：{sorted(unknown)}")
    out = {}
    for key in ("host", "item_key"):
        v = raw.get(key)
        if not isinstance(v, str) or not v.strip() or len(v) > 300 or any(ord(c) < 32 for c in v):
            raise ConfigError(f"{where}.{key} 必须是非空字符串（≤300 字符，不含控制字符）")
        out[key] = v.strip()
    reason = raw.get("reason", "")
    if reason is None:
        reason = ""
    if not isinstance(reason, str) or len(reason) > _MAX_REASON_LEN:
        raise ConfigError(f"{where}.reason 必须是字符串，且不超过 {_MAX_REASON_LEN} 字")
    out["reason"] = reason.strip()
    at = raw.get("at", "")
    out["at"] = str(at) if at else ""
    return out


def validate_ignore(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("ignore 必须是列表")
    if len(raw) > _MAX_IGNORE:
        raise ConfigError(f"ignore 最多 {_MAX_IGNORE} 项")
    return [normalize_ignore_entry(e, where=f"ignore[{i}]") for i, e in enumerate(raw)]


def validate(raw: Any) -> InspectionConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("inspection.yaml 顶层必须是键值对")
    unknown = set(raw) - {"detectors", "scope", "ignore"}
    if unknown:
        raise ConfigError(f"顶层有不认识的键：{sorted(unknown)}，只有 detectors / scope / ignore")
    return InspectionConfig(
        detectors=validate_detectors(raw.get("detectors")),
        scope=validate_scope(raw.get("scope")),
        ignore=validate_ignore(raw.get("ignore")),
    )


# ---- 读写 ----

def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.default_flow_style = False
    y.allow_unicode = True
    y.width = 120
    return y


def file_exists() -> bool:
    return config_path().exists()


def load_config(path: Path | None = None) -> InspectionConfig:
    """读配置。文件不存在返回默认配置；文件坏了抛 ConfigError（不悄悄退回默认，不然人会以为改动生效了）。"""
    p = path or config_path()
    if not p.exists():
        return InspectionConfig()
    try:
        raw = _yaml().load(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 任何解析错误都转成人话
        raise ConfigError(f"{p.name} 不是合法的 YAML：{str(exc)[:200]}") from exc
    return validate(_plain(raw))


def _plain(node: Any) -> Any:
    """ruamel 的 CommentedMap/Seq 转成普通 dict/list。"""
    if isinstance(node, dict):
        return {str(k): _plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_plain(v) for v in node]
    return node


_HEADER = (
    "# 巡检配置（netops_ai/inspection/config.py 读这个文件）。\n"
    "# 没有这个文件时用 detectors.py 里的默认值；可以在巡检页的表单里改，也可以直接改这个文件。\n"
    "# detectors 缺省的参数用默认值；scope 里的模式用 * ? 通配、不分大小写；\n"
    "# ignore 由巡检页的“这条不用管”按钮维护。\n"
)


def _write(cfg: InspectionConfig, path: Path) -> None:
    """先读已有文件（保留人手写的注释），把值更新进去，原子写。"""
    y = _yaml()
    existing = None
    if path.exists():
        try:
            existing = y.load(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 旧文件坏了就按全新文件写
            existing = None
    data = cfg.to_dict()
    if not hasattr(existing, "items"):
        existing = None
    if existing is None:
        buf = StringIO()
        buf.write(_HEADER)
        y.dump(data, buf)
        text = buf.getvalue()
    else:
        for key in ("detectors", "scope"):
            node = existing.get(key)
            if hasattr(node, "items"):
                for k, v in data[key].items():
                    if isinstance(v, dict) and hasattr(node.get(k), "items"):
                        node[k].update(v)
                    else:
                        node[k] = v
            else:
                existing[key] = data[key]
        existing["ignore"] = data["ignore"]
        buf = StringIO()
        y.dump(existing, buf)
        text = buf.getvalue()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def save_config(detectors_raw: Any, scope_raw: Any, *, path: Path | None = None) -> InspectionConfig:
    """整体替换 detectors 和 scope（缺省参数回到默认值），忽略清单原样保留。"""
    p = path or config_path()
    with _WRITE_LOCK:
        try:
            current = load_config(p)
        except ConfigError:
            current = InspectionConfig()
        new = InspectionConfig(
            detectors=validate_detectors(detectors_raw), scope=validate_scope(scope_raw), ignore=current.ignore
        )
        _write(new, p)
        return new


def add_ignore(host: Any, item_key: Any, reason: Any = "", *, path: Path | None = None) -> InspectionConfig:
    entry = normalize_ignore_entry({"host": host, "item_key": item_key, "reason": reason or ""})
    entry["at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if not entry["reason"]:
        entry["reason"] = "人工标记：这条不用管"
    p = path or config_path()
    with _WRITE_LOCK:
        cfg = load_config(p)
        cfg.ignore = [e for e in cfg.ignore if not (e["host"] == entry["host"] and e["item_key"] == entry["item_key"])]
        if len(cfg.ignore) >= _MAX_IGNORE:
            raise ConfigError(f"忽略清单已满（{_MAX_IGNORE} 条）")
        cfg.ignore.append(entry)
        _write(cfg, p)
        return cfg


def remove_ignore(host: Any, item_key: Any, *, path: Path | None = None) -> tuple[InspectionConfig, bool]:
    if not isinstance(host, str) or not isinstance(item_key, str):
        raise ConfigError("host 和 item_key 必须是字符串")
    p = path or config_path()
    with _WRITE_LOCK:
        cfg = load_config(p)
        kept = [e for e in cfg.ignore if not (e["host"] == host and e["item_key"] == item_key)]
        removed = len(kept) != len(cfg.ignore)
        if removed:
            cfg.ignore = kept
            _write(cfg, p)
        return cfg, removed


def describe(cfg: InspectionConfig | None = None, *, error: str = "") -> dict:
    """给前端表单用：当前生效值 + 默认值 + 每个参数的标签/范围/说明。"""
    cfg = cfg or InspectionConfig()
    defaults = default_detector_params()
    return {
        "file_exists": file_exists(),
        "file": config_path().name,
        "error": error,
        "values": {"detectors": cfg.detectors, "scope": cfg.scope},
        "defaults": {"detectors": defaults, "scope": default_scope()},
        "schema": [
            {
                "name": name,
                "label": DETECTOR_LABELS[name],
                "help": DETECTOR_HELP[name],
                "params": [{"key": k, **PARAM_META[name][k]} for k in defaults[name]],
            }
            for name in DETECTOR_FUNCS
        ],
        "scope_limits": {"lookback_days": {"min": 1, "max": 90}},
    }

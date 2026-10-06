"""系统设置页：连接配置 + 运行参数，读/改仓库根的 `.env`，外加只读探测连通性。

**真源就是 `.env` 文件本身**——`pipeline.py::_env()`、`llm/factory.py::env()` 等各处都是
`{**_load_dotenv(".env"), **os.environ}`，每次调用现读文件，没有内存缓存。所以这里的写接口
直接原子写 `.env` 就够了，不用另起一套运行时配置中心；改完不用重启 8000 端口的主进程。

**硬红线：凭证绝不回显。** 判断"是不是敏感字段"统一走 `is_sensitive()`：
- `GET` 只把敏感字段变成 `configured: true/false`，从不把真实值序列化进响应体。
- `POST` 收到的更新里，敏感字段是空字符串/缺失 = 不改这个字段；非敏感字段空字符串 = 允许清空
  （很多是数字/开关，清空 = 用代码默认值）。
- 原子写 `.env`：先写临时文件、`os.replace()`，不会在异常中途截断成半个文件。

已知两个"改 .env 不会立刻生效"的例外，见 `KNOWN_CAVEATS`：
- `TOPOLOGY_CACHE_TTL`：`topology.py::_cache_ttl()` 直接读 `os.environ`，不经过
  `_load_dotenv()` 合并，只有真进程环境变量能改它，写 `.env` 文件本身没用。
- `SCHEDULE_INTERVAL_MINUTES`：只在 FastAPI 启动时读一次（`schedule.start()`），
  定时线程把分钟数摄进闭包里，改 `.env` 要重启进程才会用上新值。
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 模块级变量而不是函数里现算——测试用 `monkeypatch.setattr(settings, "ENV_PATH", tmp)`
#: 把它换成临时文件，不会碰到真实仓库根的 `.env`。
ENV_PATH = REPO_ROOT / ".env"

router = APIRouter(prefix="/api/settings", tags=["settings"])

#: 字段名带这几个词（大小写不敏感）就按敏感处理——**唯一判定入口，别在别处手写这条规则**。
SENSITIVE_MARKERS = frozenset({"PASSWORD", "SECRET", "TOKEN", "KEY"})


def is_sensitive(key: str) -> bool:
    """判定依据是"下划线分隔的最后一段"，不是裸字符串包含。

    原因：`ANALYSIS_TOKEN_BUDGET` / `ALERT_TOKEN_BUDGET` / `CHAT_TOKEN_BUDGET` 这三个是数字类
    的 token 预算，字面包含 `TOKEN` 但根本不是凭证——按裸子串判断会把它们也当成敏感字段，
    GET 接口就只会返回 `configured` 布尔值，页面上「运行参数」区显示不出真实数值，也就没法
    正常编辑，明显违背了这页要展示运行参数当前值的要求。改成看最后一段能避开这个误伤，
    同时不影响任何一个真实敏感字段：`.env` 里所有敏感字段（ZABBIX_PASSWORD /
    NETBOX_TOKEN / NETBOX_WEBHOOK_SECRET / LLM_API_KEY / FEISHU_APP_SECRET / DEVICE_PASSWORD）
    命名上无一例外都是以这几个词结尾，逐个核对过，见 `tests/test_api_settings.py`。
    """
    upper = key.strip("_").upper()
    if not upper:
        return False
    last_segment = upper.rsplit("_", 1)[-1]
    return last_segment in SENSITIVE_MARKERS


@dataclass(frozen=True)
class SettingField:
    key: str
    label: str
    group: str  # "connection" | "runtime"
    subgroup: str  # 卡片/分组标题
    description: str
    numeric: bool = False  # 数字类字段：清空=用代码默认值，非空必须是数字
    switch: bool = False  # on/off 开关类
    note: str = ""  # 已知限制/特殊提示，附加在 description 后面


# ---------------------------------------------------------------------------
# 字段清单：全仓库 `grep -rn "env.get|_env.get|os.environ.get" netops_ai/ tools/`
# 人工核对每个调用点确认真实被读取、且走的是 `.env`+进程环境变量合并后的值（不是构造函数里
# 兜底用的裸 `os.environ`——那些兜底分支在真实调用路径里从没被触发过，见模块 docstring）。
# 敏感字段清单要求完整；运行参数覆盖到主要项即可，漏一两个不严重。
# ---------------------------------------------------------------------------

FIELDS: tuple[SettingField, ...] = (
    # ---- 连接配置 / Zabbix（netops_ai/zabbix/client.py） ----
    SettingField("ZABBIX_URL", "Zabbix 地址", "connection", "zabbix", "Zabbix Web/API 根地址，例如 http://<host>:8880"),
    SettingField("ZABBIX_USER", "Zabbix 用户名", "connection", "zabbix", "只读账号用户名"),
    SettingField("ZABBIX_PASSWORD", "Zabbix 密码", "connection", "zabbix", "只读账号密码"),
    # ---- 连接配置 / NetBox（netops_ai/netbox.py, api/app.py netbox_webhook） ----
    SettingField("NETBOX_URL", "NetBox 地址", "connection", "netbox", "配了就从 NetBox 读拓扑，没配就读版本化的 topology.yaml"),
    SettingField("NETBOX_TOKEN", "NetBox Token", "connection", "netbox", "NetBox 4.7 起是 v2 格式 nbt_<key>.<plaintext>；代码按前缀自动选 Bearer(v2)/Token(v1)"),
    SettingField("NETBOX_WEBHOOK_SECRET", "NetBox Webhook 密钥", "connection", "netbox", "校验 NetBox 变更 webhook 的 HMAC 签名；不配就不校验（仅适合实验环境）"),
    # ---- 连接配置 / LLM（netops_ai/llm/factory.py, llm/client.py） ----
    SettingField("LLM_PROVIDER", "LLM Provider", "connection", "llm", "未找到现成说明，字面意思：显式指定厂商/协议路由，留空按 LLM_BASE_URL 自动识别（合法值见 llm/factory.py EXPLICIT_PROVIDER_ALIASES，如 openai / anthropic / deepseek / gemini:native）"),
    SettingField("LLM_BASE_URL", "LLM Base URL", "connection", "llm", "OpenAI 兼容 /chat/completions 的根地址"),
    SettingField("LLM_API_KEY", "LLM API Key", "connection", "llm", "调用大模型的密钥"),
    SettingField("LLM_MODEL", "LLM 模型名", "connection", "llm", "调用的模型名，例如 qwen3.8-flash"),
    SettingField(
        "LLM_DISABLE_GEMINI_OPENAI_THINKING", "关闭 Gemini OpenAI 兼容层思考", "connection", "llm",
        "默认不关闭 thinking；只在实测 Gemini OpenAI 兼容层绕路时开，这可能修好管道但牺牲多跳推理能力",
        switch=True,
    ),
    # ---- 连接配置 / 飞书（netops_ai/feishu/report.py, app_sender.py） ----
    SettingField("FEISHU_WEBHOOK_URL", "飞书 Webhook", "connection", "feishu", "通道一：群自定义机器人 webhook，只能在飞书群设置里人工点出来"),
    SettingField("FEISHU_APP_ID", "飞书应用 App ID", "connection", "feishu", "通道二：应用身份，跟 App Secret / Chat ID 三个配套使用"),
    SettingField("FEISHU_APP_SECRET", "飞书应用 App Secret", "connection", "feishu", "应用身份的密钥，不进仓库"),
    SettingField("FEISHU_CHAT_ID", "飞书群 ID", "connection", "feishu", "应用身份要发到哪个群"),
    # ---- 连接配置 / 设备账号（netops_ai/api/pipeline.py::_device_adapter_from_env） ----
    SettingField("DEVICE_HOST", "设备管理地址", "connection", "device", "未找到现成说明，字面意思：设备的管理 IP（告警路径其实按 Zabbix 上报的接口地址现取，这里是定时任务等场景的兜底/默认值）"),
    SettingField("DEVICE_PORT", "设备 SSH 端口", "connection", "device", "DEVICE_TRANSPORT=ssh 时使用，默认 22", numeric=True),
    SettingField("DEVICE_USERNAME", "设备账号", "connection", "device", "这个账号在设备上必须是只读权限（Cisco privilege 1 等），命令白名单是第一层，这个账号是第二层"),
    SettingField("DEVICE_PASSWORD", "设备密码", "connection", "device", "只读账号密码"),
    SettingField("DEVICE_VENDOR", "设备厂商", "connection", "device", "决定命令语法/白名单走哪套模板，例如 cisco"),
    SettingField("DEVICE_TRANSPORT", "设备连接方式", "connection", "device", "默认走 SSH；只有 console/应急路径才显式改成 telnet（明文协议）"),
    SettingField("DEVICE_TELNET_PORT", "设备 Telnet 端口", "connection", "device", "显式设置 DEVICE_TRANSPORT=telnet 才会用到，默认 23", numeric=True),

    # ---- 运行参数 / 取证预算与循环控制 ----
    SettingField("ANALYSIS_TOKEN_BUDGET", "全局 token 预算", "runtime", "budget", "单次 agent 循环的 token 上限（prompt+completion 累计），超了带着已有证据出残缺结论；0 或留空 = 不限", numeric=True),
    SettingField("ALERT_TOKEN_BUDGET", "告警路径 token 预算", "runtime", "budget", "告警分析这条路径专用的 token 上限；没配或写错用 6 万，0 = 不限", numeric=True),
    SettingField("ALERT_MAX_TOOL_CALLS", "告警路径工具调用上限", "runtime", "budget", "告警路径的工具调用次数上限；没配或写错用 12，0 = 不限", numeric=True),
    SettingField("AGENT_LOOP_THINKING", "取证循环思考模式", "runtime", "loop", "设为 off 关闭取证循环模型的 thinking（省 token、变快），收尾结构化结论走另一个客户端不受影响；默认开（不设=行为不变）", switch=True),
    SettingField("AGENT_LOOP_CACHE", "工具调用缓存", "runtime", "loop", "同一次分析里「工具名+规范化参数」完全相同的调用直接返回上次结果；设为 off 关闭", switch=True),
    SettingField("AGENT_LOOP_CACHE_TTL", "工具调用缓存 TTL（秒）", "runtime", "loop", "反映当前状态的工具（设备命令、当前告警……）缓存多久，默认 60 秒", numeric=True),
    SettingField("AGENT_LOOP_PROGRESS", "工具调用进度提示", "runtime", "loop", "每次工具返回末尾追加一行 [进度]…；设为 off 关闭", switch=True),
    SettingField("AGENT_LOOP_COMPACT", "历史工具结果压缩", "runtime", "loop", "送给模型前把较早的工具结果换成占位，省 token；默认关，设为 on 才开", switch=True),
    SettingField("AGENT_LOOP_COMPACT_KEEP", "压缩后保留的工具结果条数", "runtime", "loop", "开启压缩时，最近几个工具结果保留原文不压缩，默认 3", numeric=True),
    # ---- 运行参数 / 拓扑缓存 ----
    SettingField(
        "TOPOLOGY_CACHE_TTL", "拓扑缓存 TTL（秒）", "runtime", "topology",
        "NetBox 拓扑的进程内缓存时长，默认 600 秒；NetBox 有变更会 webhook 通知作废，也可以在页面手动刷新",
        numeric=True,
        note="已知限制：这个值读的是真实进程环境变量（os.environ），不是 .env 文件合并后的值——只改 .env 不会生效，除非这台机器本来就在真实环境变量里设了同名变量",
    ),
    # ---- 运行参数 / 定时任务 ----
    SettingField(
        "SCHEDULE_INTERVAL_MINUTES", "定时任务间隔（分钟）", "runtime", "schedule",
        "每隔多少分钟跑一轮：巡检 + 给拓扑里每台设备存一份配置快照（下次告警的「故障前」对比）；不配或 0 就不开",
        numeric=True,
        note="已知限制：只在进程启动时读一次并把分钟数摄进定时线程的闭包里，改 .env 后要重启 8000 端口的进程才会用上新值（不影响手动触发 POST /api/schedule/run）",
    ),
)

FIELDS_BY_KEY: dict[str, SettingField] = {f.key: f for f in FIELDS}

# 数字合法性放宽：整数或浮点都行（AGENT_LOOP_CACHE_TTL 等允许小数秒）
_NUMERIC_OK = (int, float)


# ---------------------------------------------------------------------------
# .env 读写：原子写、保序、不碰未涉及的行
# ---------------------------------------------------------------------------

def _load_dotenv_lines(path: Path) -> list[tuple[str, str | None]]:
    """按行解析：`(key, raw_value)`；不是 KEY=VALUE 的整行原样保留，`raw_value=None` 代表"原样保留这行"。"""
    if not path.exists():
        return []
    lines: list[tuple[str, str | None]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            lines.append((line, None))
            continue
        key, _, value = stripped.partition("=")
        lines.append((key.strip(), value.strip()))
    return lines


def load_dotenv_dict(path: Path | None = None) -> dict[str, str]:
    """跟各处 `_load_dotenv()` 同样的解析规则：只取 `KEY=VALUE` 行，忽略空行/注释。"""
    path = path or ENV_PATH
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        result[key.strip()] = value.strip()
    return result


def write_dotenv_updates(updates: dict[str, str], *, path: Path | None = None) -> None:
    """原子写：只覆盖 `updates` 里出现的 key，其余行原样保留、顺序不变；新 key 追加到末尾。

    **已知限制（跟维护者说清楚）**：`.env` 本来的行内注释/空行分隔在这个函数眼里就是
    "非 KEY=VALUE 行"，会原样保留在原来的位置，但如果一个 key 本来带着注释在同一行
    （`.env` 目前的写法都是独立成行，没有这种用法），改写那一行时注释会跟着值一起被替换掉。
    简单起见接受这个限制，不做更复杂的行内注释保留。
    """
    path = path or ENV_PATH
    lines = _load_dotenv_lines(path)
    seen: set[str] = set()
    out: list[str] = []
    for key_or_raw, value in lines:
        if value is None:
            out.append(key_or_raw)  # 原样保留的行（注释/空行/格式不认识的行）
            continue
        key = key_or_raw
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(f"{key}={value}")
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    text = "\n".join(out) + ("\n" if out else "")
    tmp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


# ---------------------------------------------------------------------------
# GET /api/settings：分组清单，敏感字段只给 configured 布尔值
# ---------------------------------------------------------------------------

def _field_source(key: str, dotenv: dict[str, str]) -> str:
    """这个值现在实际生效的是哪一路：`.env` 文件、还是真实进程环境变量覆盖了它、还是都没配。"""
    in_dotenv = key in dotenv and dotenv[key] != ""
    in_os_environ = key in os.environ and os.environ[key] != ""
    if in_os_environ and in_dotenv and os.environ[key] != dotenv[key]:
        return "env_override"  # 进程环境变量在生效，改 .env 不会立刻反映到实际行为
    if in_os_environ:
        return "os_environ"
    if in_dotenv:
        return "dotenv"
    return "unset"


def _field_payload(f: SettingField, dotenv: dict[str, str]) -> dict[str, Any]:
    # 实际生效值：进程环境变量优先于 .env（跟 `_env()` 的合并顺序一致）
    effective = os.environ.get(f.key) if os.environ.get(f.key) else dotenv.get(f.key, "")
    source = _field_source(f.key, dotenv)
    payload: dict[str, Any] = {
        "key": f.key,
        "label": f.label,
        "group": f.group,
        "subgroup": f.subgroup,
        "description": f.description + (f"（{f.note}）" if f.note else ""),
        "sensitive": is_sensitive(f.key),
        "numeric": f.numeric,
        "switch": f.switch,
        "source": source,
    }
    if is_sensitive(f.key):
        payload["configured"] = bool(effective)
    else:
        payload["value"] = effective
    return payload


#: 纯展示用的厂商猜测——只看 base_url 域名，不影响 `llm/factory.py::detect_llm_route()`
#: 真正的路由决策（那边认的是协议兼容性：openai / native / openai-compatible，不是"哪家"）。
#: 这里只是帮人一眼看出配的是哪家，猜不出来就如实说"猜不出来"，不编。
_LLM_VENDOR_HOST_LABELS: tuple[tuple[str, str], ...] = (
    ("aliyuncs.com", "阿里云百炼（DashScope）"),
    ("dashscope.aliyuncs.com", "阿里云百炼（DashScope）"),
    ("api.openai.com", "OpenAI"),
    ("api.anthropic.com", "Anthropic（Claude）"),
    ("api.deepseek.com", "DeepSeek"),
    ("generativelanguage.googleapis.com", "Google（Gemini）"),
    ("openrouter.ai", "OpenRouter（聚合网关，实际模型看 LLM_MODEL）"),
    ("volces.com", "火山引擎（豆包）"),
    ("bigmodel.cn", "智谱 AI（GLM）"),
    ("moonshot.cn", "月之暗面（Kimi）"),
    ("siliconflow.cn", "硅基流动"),
)


def _guess_llm_vendor_label(base_url: str) -> str:
    host = urlparse(str(base_url or "")).hostname or ""
    host = host.lower()
    for suffix, label in _LLM_VENDOR_HOST_LABELS:
        if host == suffix or host.endswith("." + suffix):
            return label
    return "看不出是哪家（域名不在已知列表里，凭 LLM_MODEL 名字自己判断）" if host else "未配置"


def _llm_route_info(cfg: dict[str, str]) -> dict[str, Any]:
    """LLM 接入信息，专给设置页展示用：哪家、走的是什么协议、有没有主备——**当前架构没有
    主备/多端点切换**，全仓库只有一套 LLM_BASE_URL/LLM_API_KEY/LLM_MODEL，取证循环、
    对话、SOP 审核、巡检建议都用同一个。这里如实说清楚，不要看着像有备用其实没有。
    真实 token/key 不会出现在这里，只有派生出的判断结果。"""
    base_url = cfg.get("LLM_BASE_URL", "")
    configured = bool(base_url and cfg.get("LLM_API_KEY") and cfg.get("LLM_MODEL"))
    info: dict[str, Any] = {
        "configured": configured,
        "vendor_label": _guess_llm_vendor_label(base_url) if configured else "未配置",
        "model": cfg.get("LLM_MODEL", ""),
        "has_backup": False,
        "note": "当前只有一套 LLM 配置（无主备/多端点切换），取证、对话、SOP 审核、巡检建议都用它。",
    }
    if configured:
        try:
            from netops_ai.llm.factory import detect_llm_route

            route = detect_llm_route(cfg)
            info["route_provider"] = route.provider
            info["route_transport"] = route.transport
            info["route_source"] = route.source
            if route.warnings:
                info["route_warnings"] = list(route.warnings)
        except Exception as exc:  # noqa: BLE001 - 这只是展示信息，算不出来不该拖垮整个设置页
            info["route_error"] = f"{type(exc).__name__}: {exc}"
    return info


@router.get("")
def get_settings() -> dict:
    dotenv = load_dotenv_dict()
    connection = [_field_payload(f, dotenv) for f in FIELDS if f.group == "connection"]
    runtime = [_field_payload(f, dotenv) for f in FIELDS if f.group == "runtime"]
    return {"connection": connection, "runtime": runtime, "llm_route": _llm_route_info(dotenv)}


# ---------------------------------------------------------------------------
# POST /api/settings：更新 .env
# ---------------------------------------------------------------------------

class UpdateSettingsRequest(BaseModel):
    updates: dict[str, str] = Field(default_factory=dict)


@router.post("")
def post_settings(body: UpdateSettingsRequest) -> dict:
    dotenv = load_dotenv_dict()
    to_write: dict[str, str] = {}
    skipped_sensitive_empty: list[str] = []
    rejected: list[dict[str, str]] = []

    for key, raw_value in body.updates.items():
        f = FIELDS_BY_KEY.get(key)
        value = "" if raw_value is None else str(raw_value)
        if f is None:
            # 不在清单里的 key 一律拒绝——这页只该改我们枚举过、确认过是真实读取点的变量
            rejected.append({"key": key, "reason": "不是这个页面管理的变量"})
            continue
        if is_sensitive(key):
            if value == "":
                skipped_sensitive_empty.append(key)
                continue  # 敏感字段空值 = 不改
            to_write[key] = value
            continue
        # 非敏感字段：允许清空
        if value != "" and f.numeric:
            try:
                float(value)
            except ValueError:
                rejected.append({"key": key, "reason": f"「{f.label}」必须是数字，收到的是 {value!r}"})
                continue
        to_write[key] = value

    if rejected:
        return {"ok": False, "message": "有字段没通过校验，已全部拒绝、没有写文件", "rejected": rejected}

    if to_write:
        write_dotenv_updates(to_write)
    _ = dotenv  # 保留变量名以便未来做 diff 展示，当前只用于校验前置读取
    return {
        "ok": True,
        "updated": sorted(to_write.keys()),
        "skipped_sensitive_empty": sorted(skipped_sensitive_empty),
    }


# ---------------------------------------------------------------------------
# POST /api/settings/test：只读探测连通性
# ---------------------------------------------------------------------------

class TestConnectionRequest(BaseModel):
    target: str


def _test_zabbix(cfg: dict[str, str]) -> tuple[bool, str]:
    from netops_ai.zabbix.client import ZabbixAPIError, ZabbixClient

    if not cfg.get("ZABBIX_URL"):
        return False, "没配 ZABBIX_URL"
    client = ZabbixClient(url=cfg.get("ZABBIX_URL", ""), user=cfg.get("ZABBIX_USER", ""), password=cfg.get("ZABBIX_PASSWORD", ""), timeout=8.0)
    try:
        version = client._call("apiinfo.version", {}, authed=False)  # 白名单里唯二不需要登录的方法之一，不认证也能探活
        if not (cfg.get("ZABBIX_USER") and cfg.get("ZABBIX_PASSWORD")):
            return True, f"Zabbix API 可达（版本 {version}），未配用户名/密码，没有再测登录"
        client.login()
        client.list_hosts(limit=1)
        return True, f"连接成功，Zabbix 版本 {version}，登录和查询都正常"
    except ZabbixAPIError as exc:
        return False, f"连接失败：{exc}"
    except Exception as exc:  # noqa: BLE001 - 网络异常种类很多，统一成人话
        return False, f"连接失败：{type(exc).__name__}: {exc}"
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass


def check_netbox_status(base_url: str, auth_header: str, *, timeout: float = 6.0) -> dict:
    """最小探测：GET `{NETBOX_URL}/api/status/`（NetBox 标准端点），不跑全量拓扑拉取。"""
    import json as _json

    url = base_url.rstrip("/") + "/api/status/"
    req = urllib.request.Request(url, headers={"Authorization": auth_header, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def _test_netbox(cfg: dict[str, str]) -> tuple[bool, str]:
    from netops_ai.netbox import auth_header

    if not cfg.get("NETBOX_URL"):
        return False, "没配 NETBOX_URL"
    if not cfg.get("NETBOX_TOKEN"):
        return False, "没配 NETBOX_TOKEN"
    try:
        status = check_netbox_status(cfg["NETBOX_URL"], auth_header(cfg["NETBOX_TOKEN"]))
        version = status.get("netbox-version", "未知版本")
        return True, f"连接成功，NetBox 版本 {version}"
    except urllib.error.HTTPError as exc:
        return False, f"连接失败：HTTP {exc.code}（token 是否正确/过期？）"
    except Exception as exc:  # noqa: BLE001
        return False, f"连接失败：{type(exc).__name__}: {exc}"


def _test_llm(cfg: dict[str, str]) -> tuple[bool, str]:
    from netops_ai.llm.client import LLMClient, LLMError

    if not (cfg.get("LLM_BASE_URL") and cfg.get("LLM_API_KEY") and cfg.get("LLM_MODEL")):
        return False, "LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 没配全"
    client = LLMClient(base_url=cfg["LLM_BASE_URL"], api_key=cfg["LLM_API_KEY"], model=cfg["LLM_MODEL"], timeout=8.0)
    try:
        # 没有现成的健康检查端点：LLMClient 只封装了 /chat/completions。用最小 prompt +
        # 最小 max_completion_tokens 探测可达性，这次调用会消耗少量真实 token（不是 0 成本）。
        resp = client.complete([{"role": "user", "content": "ping"}], max_completion_tokens=4, temperature=0)
        usage = resp.usage or {}
        return True, f"连接成功（这次测试消耗了少量 token：{usage.get('total_tokens', '未知')}）"
    except LLMError as exc:
        return False, f"连接失败：{exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"连接失败：{type(exc).__name__}: {exc}"


_TESTERS = {
    "zabbix": _test_zabbix,
    "netbox": _test_netbox,
    "llm": _test_llm,
}


@router.post("/test")
def post_test_connection(body: TestConnectionRequest) -> dict:
    tester = _TESTERS.get(body.target)
    if tester is None:
        return {"ok": False, "message": f"不支持测试「{body.target}」（只支持 zabbix / netbox / llm；飞书不提供自动测试，见页面说明）", "elapsed_ms": 0}
    cfg = load_dotenv_dict()
    cfg.update({k: v for k, v in os.environ.items() if k in cfg or k in FIELDS_BY_KEY})
    started = time.monotonic()
    try:
        ok, message = tester(cfg)
    except Exception as exc:  # noqa: BLE001 - 兜底，不能把堆栈甩给前端
        ok, message = False, f"测试过程出了意外：{type(exc).__name__}: {exc}"
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {"ok": ok, "message": message, "elapsed_ms": elapsed_ms}

"""Token 计量：每次模型调用记一笔到 append-only 账本，给 agent 循环一个 token 预算。

预算跟轮数上限并列：多跳循环每跳都重发全量上下文，数次数拦不住体积。
跟人工成本的对比在 `tools/cost_report.py`。
"""

from __future__ import annotations

import json
import os
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────
# 价格表
# ──────────────────────────────────────────────────────────────────────

#: 价目表的采集日期。**模型价格会变，这个表一定会过期。**
#: 报告里会把这个日期打出来，提醒看的人回去核对真实账单。
PRICING_AS_OF = "2026-09-19"

#: 每百万 token 的美元列表价（不等于实际账单）。按模型名子串、从长到短匹配，
#: 免得 `gpt-4o` 抢走 `gpt-4o-mini`。环境变量可覆盖，见 `price_for()`。
PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "deepseek": (0.27, 1.10),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-opus": (15.00, 75.00),
}

#: 美元兑人民币。用环境变量 `USD_CNY_RATE` 覆盖。
DEFAULT_USD_CNY = 7.1


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def price_for(model: str) -> tuple[float, float] | None:
    """返回 (输入价, 输出价)，美元/百万 token。查不到返回 None，**不猜**：错的成本数字比没有更危险。

    `LLM_PRICE_INPUT_USD_PER_M` / `LLM_PRICE_OUTPUT_USD_PER_M` 可覆盖整张表。
    """
    override_in = os.environ.get("LLM_PRICE_INPUT_USD_PER_M", "")
    override_out = os.environ.get("LLM_PRICE_OUTPUT_USD_PER_M", "")
    if override_in and override_out:
        try:
            return (float(override_in), float(override_out))
        except ValueError:
            pass

    name = (model or "").lower()
    for fragment in sorted(PRICING_USD_PER_MTOK, key=len, reverse=True):
        if fragment in name:
            return PRICING_USD_PER_MTOK[fragment]
    return None


# ──────────────────────────────────────────────────────────────────────
# 用量
# ──────────────────────────────────────────────────────────────────────


@dataclass
class TokenUsage:
    """一次或多次 LLM 调用的累计用量。"""

    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            model=self.model or other.model,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            calls=self.calls + other.calls,
        )

    @classmethod
    def from_api(cls, model: str, usage: dict | None) -> "TokenUsage":
        """从 API 的 `usage` 构造，认 OpenAI（prompt/completion）和 Anthropic（input/output）两套字段名。"""
        u = usage or {}
        return cls(
            model=model,
            prompt_tokens=int(u.get("prompt_tokens") or u.get("input_tokens") or 0),
            completion_tokens=int(
                u.get("completion_tokens") or u.get("output_tokens") or 0
            ),
            calls=1 if u else 0,
        )

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
        }


def cost_usd(usage: TokenUsage) -> float | None:
    """算这笔用量的美元成本。模型无价格数据时返回 `None`。"""
    price = price_for(usage.model)
    if price is None:
        return None
    price_in, price_out = price
    return usage.prompt_tokens * price_in / 1e6 + usage.completion_tokens * price_out / 1e6


def cost_cny(usage: TokenUsage) -> float | None:
    usd = cost_usd(usage)
    if usd is None:
        return None
    return usd * _env_float("USD_CNY_RATE", DEFAULT_USD_CNY)


# ──────────────────────────────────────────────────────────────────────
# 人工基线
# ──────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────
# 预算闸门
# ──────────────────────────────────────────────────────────────────────


@dataclass
class TokenBudget:
    """给一次分析定的 token 上限。只回答「还发不发得起」，不抛异常：
    超了应该带着已有证据出残缺结论，已经花掉的 token 不能白花。
    """

    limit: int = 60000
    spent: int = 0

    @classmethod
    def from_env(cls) -> "TokenBudget":
        return cls(limit=int(_env_float("ANALYSIS_TOKEN_BUDGET", 60000)))

    def charge(self, usage: TokenUsage) -> None:
        self.spent += usage.total_tokens

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.limit

    def would_exceed(self, estimated_next: int) -> bool:
        """下一跳还发不发得起。预估值可以直接用上一跳的 prompt 大小——
        多跳循环里上下文只增不减，用上一跳估下一跳是偏保守的，正合适。
        """
        return self.spent + estimated_next > self.limit


# ──────────────────────────────────────────────────────────────────────
# 账本
# ──────────────────────────────────────────────────────────────────────

_LEDGER_LOCK = threading.Lock()


def ledger_path() -> Path:
    return Path(os.environ.get("TOKEN_LEDGER_PATH", "records/token-ledger.jsonl"))


#: 当前在分析哪条告警。告警管道进门时设一次，这之后所有模型调用（含 LangChain 回调里的）
#: 都自动带上，不用每个调用点各传一遍。的账本 eventid 全是空的，没法按告警算钱。
CURRENT_EVENTID: ContextVar[str] = ContextVar("netops_eventid", default="")
CURRENT_TRACE_ID: ContextVar[str] = ContextVar("netops_trace_id", default="")


def record_usage(usage: TokenUsage, *, tag: str = "", eventid: str = "") -> None:
    """往账本追加一行，**绝不抛异常**：记账失败不许拖垮分析。jsonl 追加不用读改写，被杀也只坏半行。"""
    if usage.calls <= 0 and usage.total_tokens <= 0:
        return
    line = {
        "at": datetime.now(timezone.utc).isoformat(),
        "tag": tag,
        "eventid": eventid or CURRENT_EVENTID.get(),
        "trace_id": CURRENT_TRACE_ID.get(),
        **usage.as_dict(),
        "cost_usd": cost_usd(usage),
        "cost_cny": cost_cny(usage),
    }
    try:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LEDGER_LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - 记账永远不许影响主链路
        pass


def build_langchain_callback(tag: str = "langchain"):
    """造一个把 usage 记进账本的 LangChain 回调，装不了 langchain 就返回 None。

    多跳循环的 token 只能在回调层数：一次 `invoke()` 里发好几次请求，别处只看得到最后一跳。
    """
    try:
        from langchain_core.callbacks import BaseCallbackHandler
    except ImportError:
        return None

    class _UsageLedgerCallback(BaseCallbackHandler):
        def __init__(self, tag: str) -> None:
            self.tag = tag

        def on_llm_end(self, response, **kwargs) -> None:  # noqa: ANN001
            # 整段包异常：各家适配器放 usage 的位置不一样，而且随版本变。
            # **记账拿不到数据，绝不许把一次真实分析搞挂。**
            try:
                record_usage(_usage_from_llm_result(response), tag=self.tag)
            except Exception:  # noqa: BLE001
                pass

    return _UsageLedgerCallback(tag)


def _usage_from_llm_result(response) -> TokenUsage:  # noqa: ANN001
    """从 `LLMResult` 里抠 usage。新版、老版、Anthropic 适配器各放一个位置，三个都试。"""
    model = ""
    llm_output = getattr(response, "llm_output", None) or {}
    model = str(llm_output.get("model_name") or llm_output.get("model") or "")

    token_usage = llm_output.get("token_usage") or llm_output.get("usage")
    if token_usage:
        return TokenUsage.from_api(model or _model_from_env(), token_usage)

    for generation_list in getattr(response, "generations", None) or []:
        for generation in generation_list:
            message = getattr(generation, "message", None)
            if message is None:
                continue
            meta = getattr(message, "usage_metadata", None)
            if meta:
                return TokenUsage(
                    model=model or _model_from_env(),
                    prompt_tokens=int(meta.get("input_tokens") or 0),
                    completion_tokens=int(meta.get("output_tokens") or 0),
                    calls=1,
                )
            rmeta = getattr(message, "response_metadata", None) or {}
            usage = rmeta.get("usage") or rmeta.get("token_usage")
            if usage:
                return TokenUsage.from_api(
                    model or str(rmeta.get("model_name") or "") or _model_from_env(),
                    usage,
                )
    return TokenUsage()


def _model_from_env() -> str:
    return os.environ.get("LLM_MODEL", "")


def _percentile(values: list, ratio: float):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * ratio))]



"""LangChain chat model factory with provider/transport detection."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from langchain_openai import ChatOpenAI

from netops_ai.llm.meter import build_langchain_callback

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class LLMRoute:
    provider: str
    transport: str
    source: str
    key_prefix: str = "unknown"
    warnings: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return f"({self.provider}, {self.transport})"


EXPLICIT_PROVIDER_ALIASES: dict[str, tuple[str, str]] = {
    "gemini:native": ("gemini", "native"),
    "gemini-native": ("gemini", "native"),
    "google:native": ("gemini", "native"),
    "gemini:openai": ("gemini", "openai-compatible"),
    "gemini-openai": ("gemini", "openai-compatible"),
    "gemini:openai-compatible": ("gemini", "openai-compatible"),
    "openai": ("openai", "openai"),
    "openai:openai": ("openai", "openai"),
    "anthropic": ("anthropic", "native"),
    "anthropic:native": ("anthropic", "native"),
    "deepseek": ("deepseek", "openai-compatible"),
    "deepseek:openai": ("deepseek", "openai-compatible"),
    "openai-compatible": ("generic", "openai-compatible"),
    "generic:openai": ("generic", "openai-compatible"),
    "generic:openai-compatible": ("generic", "openai-compatible"),
}

KEY_PREFIX_PROVIDER = (
    ("sk-ant-api03-", "anthropic"),
    ("AIza", "gemini"),
    ("gsk_", "groq"),
    ("sk-or-v1-", "openrouter"),
    ("xai-", "xai"),
)


def _load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def env() -> dict[str, str]:
    return {**_load_dotenv(REPO_ROOT / ".env"), **os.environ}


def key_prefix_provider(api_key: str) -> str:
    for prefix, provider in KEY_PREFIX_PROVIDER:
        if api_key.startswith(prefix):
            return provider
    # OpenAI/DeepSeek/Kimi/DashScope all commonly use sk-* keys, while
    # Mistral/Together do not have a stable unique prefix. Treat unknown
    # prefixes as diagnostics only, never as routing input.
    return "unknown"


def _infer_from_base_url(base_url: str) -> tuple[str, str]:
    parsed = urlparse(base_url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host == "generativelanguage.googleapis.com":
        if "/openai" in path:
            return ("gemini", "openai-compatible")
        return ("gemini", "native")
    if host == "api.openai.com":
        return ("openai", "openai")
    if host == "api.anthropic.com":
        return ("anthropic", "native")
    if host == "api.deepseek.com":
        return ("deepseek", "openai-compatible")
    return ("generic", "openai-compatible")


def detect_llm_route(cfg: dict[str, str] | None = None) -> LLMRoute:
    cfg = env() if cfg is None else cfg
    explicit = cfg.get("LLM_PROVIDER", "").strip().lower()
    warnings: list[str] = []
    if explicit:
        if explicit not in EXPLICIT_PROVIDER_ALIASES:
            legal = ", ".join(sorted(EXPLICIT_PROVIDER_ALIASES))
            raise RuntimeError(f"LLM_PROVIDER={explicit!r} 不合法，合法取值：{legal}")
        provider, transport = EXPLICIT_PROVIDER_ALIASES[explicit]
        source = "LLM_PROVIDER"
    else:
        provider, transport = _infer_from_base_url(cfg.get("LLM_BASE_URL", ""))
        source = "LLM_BASE_URL"

    key_provider = key_prefix_provider(cfg.get("LLM_API_KEY", ""))
    if key_provider != "unknown" and provider != "generic" and key_provider != provider:
        warnings.append(f"api_key_prefix_mismatch: route_provider={provider}, key_prefix_provider={key_provider}")
    return LLMRoute(provider=provider, transport=transport, source=source, key_prefix=key_provider, warnings=tuple(warnings))


def supports_json_schema_refs(route: LLMRoute) -> bool:
    """这个模型通道能不能收 `$defs`/`$ref` 形式的 schema。没实测过的兼容端点一律给展开版。"""
    if route.provider == "openai" and route.transport == "openai":
        return True
    if route.provider == "gemini" and route.transport == "openai-compatible":
        return True
    return False


def analysis_schema_use_refs(cfg: dict[str, str] | None = None) -> bool:
    return supports_json_schema_refs(detect_llm_route(cfg))


def _gemini_native_model(cfg: dict[str, str], *, temperature: float, timeout: float) -> Any:
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
    except ImportError as exc:  # pragma: no cover - exercised only on a broken lab install
        raise RuntimeError("Gemini 原生协议需要先安装 langchain-google-genai") from exc

    return ChatGoogleGenerativeAI(
        model=cfg["LLM_MODEL"],
        api_key=cfg["LLM_API_KEY"],
        temperature=temperature,
        request_timeout=timeout,
        retries=1,
    )


def _chat_openai_model(
    cfg: dict[str, str],
    *,
    temperature: float,
    timeout: float,
    disable_gemini_openai_thinking: bool,
) -> ChatOpenAI:
    kwargs: dict[str, Any] = {}
    if disable_gemini_openai_thinking:
        kwargs["reasoning_effort"] = "none"
    return ChatOpenAI(
        model=cfg["LLM_MODEL"],
        api_key=cfg["LLM_API_KEY"],
        base_url=cfg["LLM_BASE_URL"].rstrip("/"),
        temperature=temperature,
        timeout=timeout,
        max_retries=1,
        **kwargs,
    )


def build_chat_model(
    *,
    temperature: float = 0.1,
    timeout: float = 60.0,
    disable_gemini_openai_thinking: bool | None = None,
    force_gemini_native: bool = False,
    model: str = "",
) -> Any:
    """所有 LangChain 模型的唯一构造出口（token 记账挂在这里，别处另起一份就会漏记）。

    `force_gemini_native`：agent 多轮工具循环要 Gemini 原生协议，兼容层会丢 `thought_signature`。
    """
    cfg = dict(env())
    if model:
        # **换模型也必须从这个出口走。** 范围闸门那种小活想用便宜模型，
        # 正确做法是在这里覆盖 `LLM_MODEL`，而不是自己另起一份构造——
        # 另起一份就会漏 `_with_usage_ledger()`，账本又变成半个真相
        # （已经真实踩过一次）。
        cfg["LLM_MODEL"] = model
    missing = [k for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL") if not cfg.get(k)]
    if missing:
        raise RuntimeError(f"{'/'.join(missing)} 没配全，读一下 env.example 该填什么")

    route = detect_llm_route(cfg)
    if route.provider == "gemini" and (route.transport == "native" or force_gemini_native):
        return _with_usage_ledger(_gemini_native_model(cfg, temperature=temperature, timeout=timeout))
    if route.provider == "anthropic" and route.transport == "native":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:  # pragma: no cover - depends on optional package
            raise RuntimeError("Anthropic 原生协议需要安装 langchain-anthropic") from exc
        return _with_usage_ledger(
            ChatAnthropic(model=cfg["LLM_MODEL"], api_key=cfg["LLM_API_KEY"], temperature=temperature, timeout=timeout)
        )

    disable_thinking = disable_gemini_openai_thinking
    if disable_thinking is None:
        disable_thinking = cfg.get("LLM_DISABLE_GEMINI_OPENAI_THINKING", "").strip().lower() in {"1", "true", "yes", "on"}
    return _with_usage_ledger(
        _chat_openai_model(
            cfg,
            temperature=temperature,
            timeout=timeout,
            disable_gemini_openai_thinking=bool(disable_thinking and route.provider == "gemini"),
        )
    )


def _with_usage_ledger(model: Any) -> Any:
    """给模型挂 token 记账回调。挂不上就原样返回：计量不该成为模型能不能用的前提。"""
    callback = build_langchain_callback(tag="langchain")
    if callback is None:
        return model
    try:
        existing = list(getattr(model, "callbacks", None) or [])
        model.callbacks = [*existing, callback]
    except Exception:  # noqa: BLE001
        pass
    return model

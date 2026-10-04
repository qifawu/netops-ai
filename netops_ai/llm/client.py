"""OpenAI 兼容的最小客户端：POST `/chat/completions`，支持 strict `json_schema`。
纯标准库，不引 openai SDK 也不引 LangChain。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from netops_ai.llm.factory import analysis_schema_use_refs, env
from netops_ai.llm.meter import TokenUsage, record_usage
from netops_ai.llm.retry import MAX_ATTEMPTS, is_retryable_llm_error, retry_delay


RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_MAX_ATTEMPTS = MAX_ATTEMPTS


class LLMError(RuntimeError):
    """调用失败：配置不全、网络问题、API 返回 error、响应格式不对。

    `status_code` 在能拿到 HTTP 状态码时会填上（429 限流、400 schema 校验失败
    这些都要区分开分别处理，不能只靠字符串里有没有"429"这种脆弱判断）。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        attempts: int = 1,
        retry_count: int = 0,
        elapsed_seconds: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts
        self.retry_count = retry_count
        self.elapsed_seconds = elapsed_seconds


@dataclass
class LLMResponse:
    content: str
    parsed: Any | None
    usage: dict
    finish_reason: str
    raw: dict
    reasoning: str = ""
    attempts: int = 1
    retry_count: int = 0
    elapsed_seconds: float = 0.0


@dataclass
class LLMClient:
    base_url: str = field(default_factory=lambda: env().get("LLM_BASE_URL", ""))
    api_key: str = field(default_factory=lambda: env().get("LLM_API_KEY", ""))
    model: str = field(default_factory=lambda: env().get("LLM_MODEL", ""))
    timeout: float = 60.0
    sleep: Callable[[float], None] = field(default_factory=lambda: time.sleep)
    jitter: Callable[[], float] | None = None

    def _route_cfg(self) -> dict[str, str]:
        cfg = env()
        cfg.update(
            {
                "LLM_BASE_URL": self.base_url,
                "LLM_API_KEY": self.api_key,
                "LLM_MODEL": self.model,
            }
        )
        return cfg

    def analysis_schema_use_refs(self) -> bool:
        return analysis_schema_use_refs(self._route_cfg())

    def complete(
        self,
        messages: list[dict],
        *,
        response_format: dict | None = None,
        max_completion_tokens: int = 8000,
        temperature: float = 0.2,
        extra_body: dict | None = None,
        on_retry: Callable[[int, BaseException], None] | None = None,
    ) -> LLMResponse:
        """发一次 `/chat/completions`。`response_format` 传 structured outputs 的
        `json_schema` 时，`parsed` 会是解析好的 dict；不传就是普通文本，`parsed=None`。
        """
        if not (self.base_url and self.api_key and self.model):
            raise LLMError(
                "LLM_BASE_URL/LLM_API_KEY/LLM_MODEL 没配全，读一下 env.example 该填什么"
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_completion_tokens": max_completion_tokens,
            "temperature": temperature,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        # 厂商专有开关收在这里。百炼的思考模型会把输出 token 全烧在 reasoning 上、content 为空，
        # 所以默认关思考；调用方显式传了 enable_thinking 就听调用方的。
        if "aliyuncs.com" in self.base_url:
            payload.setdefault("enable_thinking", False)
        if extra_body:
            payload.update(extra_body)

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                # Groq 的 Cloudflare 前置层会拦默认的 Python-urllib UA（403 error code
                # 1010），跟 wechat 抓取踩过的坑是同一类问题，换成正常浏览器 UA 就通
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                ),
            },
            method="POST",
        )

        started = time.monotonic()
        attempts = 0

        def _raise(message: str, *, status_code: int | None = None, cause: Exception | None = None) -> None:
            err = LLMError(
                message,
                status_code=status_code,
                attempts=attempts,
                retry_count=max(0, attempts - 1),
                elapsed_seconds=round(time.monotonic() - started, 2),
            )
            if cause is not None:
                raise err from cause
            raise err

        def _sleep_before_retry(attempt: int, exc: BaseException) -> bool:
            if attempt >= RETRY_MAX_ATTEMPTS:
                return False
            if on_retry is not None:
                on_retry(attempt, exc)
            self.sleep(retry_delay(attempt, jitter=self.jitter))
            return True

        # 只重试短暂性失败：连接问题、读超时、429、5xx。4xx 参数错不重试。
        while True:
            attempts += 1
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw_text = resp.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code in RETRY_STATUS and _sleep_before_retry(attempts, exc):
                    continue
                _raise(f"HTTP {exc.code}：{body[:500]}", status_code=exc.code, cause=exc)
            except urllib.error.URLError as exc:
                if _sleep_before_retry(attempts, exc):
                    continue
                _raise(f"连不上 {self.base_url}：{exc}", cause=exc)
            except (TimeoutError, socket.timeout) as exc:
                if _sleep_before_retry(attempts, exc):
                    continue
                _raise(f"调用 {self.base_url} 超时：{exc}", cause=exc)
            except Exception as exc:
                if is_retryable_llm_error(exc) and _sleep_before_retry(attempts, exc):
                    continue
                raise

        try:
            body = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"返回的不是合法 JSON：{raw_text[:200]!r}") from exc

        if "error" in body:
            raise LLMError(f"API 返回 error：{body['error']}")
        if not body.get("choices"):
            raise LLMError(f"响应里没有 choices：{body}")

        choice = body["choices"][0]
        message = choice.get("message", {})
        content = message.get("content") or ""
        # gpt-oss 系列是带推理的模型，Groq 把内部推理过程单独放在 `reasoning` 字段，
        # 不混进 `content`——这个字段不参与结构化解析，但存下来对复盘诊断过程有用
        reasoning = message.get("reasoning") or ""
        finish_reason = choice.get("finish_reason", "")

        parsed = None
        if response_format is not None and content:
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                parsed = None  # 调用方自己决定怎么处理："模型没吐出合法JSON"本身就是一种失败模式，不瞒报

        usage = body.get("usage", {})
        # 每次调用都记一笔账。放在这里而不是放在调用方，是因为调用方有好几个
        # （analyzer / two_round / playbooks / 回归脚本），逐个去接必然漏，
        # 而这里是所有非 LangChain 调用的唯一出口。
        # `record_usage()` 自己吞异常，记账失败不会影响这次调用的返回。
        record_usage(TokenUsage.from_api(self.model, usage), tag="llm_client")

        return LLMResponse(
            content=content,
            parsed=parsed,
            usage=usage,
            finish_reason=finish_reason,
            raw=body,
            reasoning=reasoning,
            attempts=attempts,
            retry_count=max(0, attempts - 1),
            elapsed_seconds=round(time.monotonic() - started, 2),
        )

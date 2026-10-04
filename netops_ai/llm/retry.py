"""Shared retry helpers for transient LLM transport failures."""

from __future__ import annotations

import random
import time
from typing import Callable, TypeVar


MAX_RETRIES = 3
MAX_ATTEMPTS = MAX_RETRIES + 1
BACKOFF_SECONDS = (1.0, 3.0, 8.0)
JITTER_SECONDS = 0.25

_RETRY_MARKERS = (
    "ConnectionError",
    "Timeout",
    "APIConnectionError",
    "RateLimit",
    "429",
    "500",
    "503",
    "502",
    "504",
)

T = TypeVar("T")


class LLMTransportRetriesExhausted(RuntimeError):
    """Raised after all retry attempts for a transient LLM failure are exhausted."""

    def __init__(self, message: str, *, attempts: int, retry_count: int, last_error: BaseException) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.retry_count = retry_count
        self.last_error = last_error


def is_retryable_llm_error(exc: BaseException) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code in {429, 500, 502, 503, 504}:
        return True
    text = f"{type(exc).__name__}: {exc}"
    return any(marker in text for marker in _RETRY_MARKERS)


def retry_delay(attempt: int, *, jitter: Callable[[], float] | None = None) -> float:
    base = BACKOFF_SECONDS[min(max(attempt, 1), len(BACKOFF_SECONDS)) - 1]
    jitter_fn = jitter or random.random
    return base + (jitter_fn() * JITTER_SECONDS)


def call_with_llm_retry(
    func: Callable[[], T],
    *,
    on_retry: Callable[[int, BaseException], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] | None = None,
    max_retries: int = MAX_RETRIES,
) -> T:
    attempts = 0
    while True:
        attempts += 1
        try:
            return func()
        except Exception as exc:
            if attempts > max_retries or not is_retryable_llm_error(exc):
                if attempts > max_retries and is_retryable_llm_error(exc):
                    raise LLMTransportRetriesExhausted(
                        f"LLM 传输失败（重试 {max_retries} 次）：{type(exc).__name__}: {exc}",
                        attempts=attempts,
                        retry_count=max_retries,
                        last_error=exc,
                    ) from exc
                raise
            if on_retry is not None:
                on_retry(attempts, exc)
            sleep(retry_delay(attempts, jitter=jitter))


def retry_error_text(exc: BaseException) -> str:
    if isinstance(exc, LLMTransportRetriesExhausted):
        exc = exc.last_error
    return f"{type(exc).__name__}: {exc}"

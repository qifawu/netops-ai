"""AI 生成内容（root_cause/结论/理由等）的按需翻译，带磁盘缓存。

跟 `web/src/lib/i18n.ts` 的静态 `dict` 是两回事：那份字典管前端写死的 UI 文案（导航栏/按钮/
标题），随代码走、不用调模型。这里管的是模型自己写出来的中文自由文本——界面语言切到英文时，
这些文本不会跟着切，因为它们不是编译期就知道两种写法的固定字符串。

**范围边界（调用方必须遵守，这里不做二次校验）**：只翻模型自己写的解读性文本（root_cause /
headline / evidence 的 claim / 理由说明 / chat 回答），**绝不翻**设备/日志的逐字证据
（evidence 的 source、hypothesis_checklist 的 counter_evidence、zabbix/device 原始回显）——
那些是审计取证用的，改一个字都不行。这条边界由调用方（前端选哪些字段传进来翻）保证，本模块
不做内容判断。

翻译结果按 sha256(text)+lang 缓存进磁盘 JSON：同一份分析结论只翻一次，重启进程也不用重翻
（分析记录落盘后是不可变的，重复翻译只是浪费 token）。
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from netops_ai.llm.client import LLMClient, LLMError

REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_PATH = REPO_ROOT / "records" / "_translation_cache.json"

_lock = threading.Lock()


def _cache_key(text: str, lang: str) -> str:
    return f"{lang}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _load_cache() -> dict[str, str]:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict[str, str]) -> None:
    CACHE_PATH.parent.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


_SYSTEM_PROMPT = (
    "You translate network-operations fault-analysis text from Chinese to concise, "
    "professional English for an ops dashboard. Keep device names, hostnames, IP/MAC "
    "addresses, interface names, error codes, and any quoted command output exactly as-is "
    "— do not translate or alter them. If the text contains **bold** or `code` markdown "
    "markers, keep the markers in place around the translated words. Output ONLY the "
    "translation, no preamble, no quotes."
)


class TranslateError(RuntimeError):
    pass


def translate_text(text: str, lang: str) -> str:
    """`lang` 是目标界面语言。中文界面直接原样返回（不需要翻）。"""
    text = text.strip()
    if lang == "zh" or not text:
        return text

    key = _cache_key(text, lang)
    with _lock:
        cache = _load_cache()
        hit = cache.get(key)
    if hit is not None:
        return hit

    client = LLMClient()
    try:
        resp = client.complete(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            max_completion_tokens=1200,
            temperature=0.1,
        )
    except LLMError as exc:
        raise TranslateError(str(exc)) from exc

    translated = resp.content.strip()
    if not translated:
        raise TranslateError("翻译结果为空")

    with _lock:
        cache = _load_cache()
        cache[key] = translated
        _save_cache(cache)
    return translated

"""LLM transport doctor for real multi-turn tool replay checks."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from netops_ai.llm.factory import build_chat_model, detect_llm_route, env


def _probe_tool(value: str) -> str:
    """Echo a value for the LLM doctor tool replay probe."""
    return json.dumps({"ok": True, "echo": value}, ensure_ascii=False)


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def run_probe(*, disable_gemini_openai_thinking: bool | None = None) -> dict[str, Any]:
    cfg = env()
    route = detect_llm_route(cfg)
    model = build_chat_model(
        temperature=0,
        timeout=60,
        disable_gemini_openai_thinking=disable_gemini_openai_thinking,
    )
    tool = StructuredTool.from_function(
        func=_probe_tool,
        name="echo_probe",
        description="Return the same value. Use this once for transport diagnostics.",
    )
    bound = model.bind_tools([tool])
    messages: list[Any] = [
        HumanMessage(content="Call the echo_probe tool exactly once with value 'k1-doctor'. Do not answer directly.")
    ]
    result: dict[str, Any] = {
        "route": {"provider": route.provider, "transport": route.transport, "source": route.source},
        "key_prefix_provider": route.key_prefix,
        "warnings": list(route.warnings),
        "disable_gemini_openai_thinking": disable_gemini_openai_thinking,
        "first_tool_call": False,
        "tool_name": "",
        "tool_args": {},
        "multi_turn_tool_replay_ok": False,
        "final_text": "",
        "error": "",
    }
    try:
        first = bound.invoke(messages)
        tool_calls = getattr(first, "tool_calls", None) or []
        if not tool_calls:
            result["final_text"] = _message_text(first)
            result["error"] = "model_did_not_call_tool"
            return result
        call = tool_calls[0]
        args = dict(call.get("args") or {})
        result.update({"first_tool_call": True, "tool_name": call.get("name", ""), "tool_args": args})
        tool_output = _probe_tool(str(args.get("value", "")))
        messages.extend([first, ToolMessage(content=tool_output, tool_call_id=call["id"])])
        second = bound.invoke(messages)
        result["multi_turn_tool_replay_ok"] = True
        result["final_text"] = _message_text(second)
        return result
    except Exception as exc:  # noqa: BLE001 - doctor reports the exact transport failure class
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="实测当前 LLM 配置是否支持多轮工具结果回放。")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--disable-gemini-openai-thinking",
        action="store_true",
        help="在 Gemini OpenAI 兼容层上发送 reasoning_effort=none，用来实测关闭 thinking 的绕路方案。",
    )
    group.add_argument(
        "--keep-thinking",
        action="store_true",
        help="强制不关闭 thinking，即使 .env 配了 LLM_DISABLE_GEMINI_OPENAI_THINKING=true。",
    )
    parser.add_argument("--pretty", action="store_true", help="缩进输出 JSON，便于贴进实验报告。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    disable: bool | None = None
    if args.disable_gemini_openai_thinking:
        disable = True
    elif args.keep_thinking:
        disable = False
    data = run_probe(disable_gemini_openai_thinking=disable)
    print(json.dumps(data, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if data.get("multi_turn_tool_replay_ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

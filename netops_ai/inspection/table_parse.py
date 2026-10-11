"""把任意 show 命令的输出解析成「行记录表」（一行一条路由 / ARP / 邻居……，每列一个字段），供通用的表对表比较用。

解析规则是 TextFSM 模板——**纯声明式文本**，不执行任何代码：
- 默认用 `ntc-templates` 里现成的（覆盖几百条常见命令，按厂商平台 + 命令匹配）；
- 命中不了时，规则里可以自带一份模板（`template`），由助手照设备真实输出写好、用户确认后保存。

纯函数，不 import 框架；解析失败变成带人话原因的 `ParseResult.error`，不抛给调用方。
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

#: 厂商（白名单规则集）→ ntc-templates 的平台名
PLATFORMS = {
    "cisco": "cisco_ios",
}

MAX_ROWS = 20_000
MAX_TEMPLATE_CHARS = 20_000


@dataclass
class ParseResult:
    rows: list[dict[str, str]] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    source: str = ""  # "ntc:<平台>" | "inline"
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def platform_for(vendor: str, platform: str | None = None) -> str:
    return platform or PLATFORMS.get((vendor or "cisco").lower(), "cisco_ios")


def _bare_command(command: str) -> str:
    """去掉管道过滤（`| begin Gateway` 之类）：模板匹配的是命令本身，过滤只影响输出里留下哪些行。"""
    return command.split("|", 1)[0].strip()


def _cell(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v if str(x))
    return "" if v is None else str(v).strip()


def validate_template(text: Any) -> list[str]:
    """内联模板的静态检查：能被 TextFSM 编译、至少一个 Value、不超长。"""
    if not isinstance(text, str) or not text.strip():
        return ["template: must be a non-empty TextFSM template"]
    if len(text) > MAX_TEMPLATE_CHARS:
        return [f"template: longer than {MAX_TEMPLATE_CHARS} characters"]
    try:
        import textfsm

        fsm = textfsm.TextFSM(io.StringIO(text))
    except Exception as exc:  # noqa: BLE001 - textfsm 的各种模板错误统一成一句话
        return [f"template: not a valid TextFSM template ({type(exc).__name__}: {str(exc)[:160]})"]
    if not fsm.header:
        return ["template: declares no Value"]
    return []


def _parse_inline(template: str, output: str) -> ParseResult:
    import textfsm

    problems = validate_template(template)
    if problems:
        return ParseResult(error=problems[0])
    fsm = textfsm.TextFSM(io.StringIO(template))
    header = [h.lower() for h in fsm.header]
    try:
        raw = fsm.ParseText(output)
    except Exception as exc:  # noqa: BLE001
        return ParseResult(error=f"template failed on this output ({type(exc).__name__}: {str(exc)[:160]})")
    rows = [{h: _cell(v) for h, v in zip(header, r)} for r in raw[:MAX_ROWS]]
    return ParseResult(rows=rows, fields=header, source="inline")


def _parse_ntc(command: str, output: str, platform: str) -> ParseResult:
    from textfsm import clitable
    from ntc_templates.parse import _get_template_dir

    cmd = _bare_command(command)
    try:
        table = clitable.CliTable("index", _get_template_dir())
        table.ParseCmd(output, attributes={"Command": cmd, "Platform": platform})
    except clitable.CliTableError:
        return ParseResult(error=f"no built-in parser for {cmd!r} on platform {platform}")
    except Exception as exc:  # noqa: BLE001
        return ParseResult(error=f"parser failed ({type(exc).__name__}: {str(exc)[:160]})")
    header = [h.lower() for h in table.header]
    rows = [{h: _cell(v) for h, v in zip(header, list(r))} for r in list(table)[:MAX_ROWS]]
    return ParseResult(rows=rows, fields=header, source=f"ntc:{platform}")


def parse(command: str, output: str, *, vendor: str = "cisco", template: str | None = None,
          platform: str | None = None) -> ParseResult:
    """输出 → 行记录表。`template`（内联 TextFSM）优先，否则按 平台 + 命令 找 ntc-templates 里现成的。"""
    if template:
        return _parse_inline(template, output)
    return _parse_ntc(command, output, platform_for(vendor, platform))


def describe(command: str, *, vendor: str = "cisco", platform: str | None = None) -> list[str] | None:
    """这条命令有现成解析器时，返回它能给出的字段名（给助手 / 界面看，用来挑 key 和比较字段）；没有返回 None。"""
    res = _parse_ntc(command, "", platform_for(vendor, platform))
    return res.fields or None


_VOLATILE_FIELD = re.compile(r"(?i)^(uptime|ag(e|ing)|up_?down|last_?\w*|\w*_?time(r|stamp)?|hold\w*|expire\w*|since)$")


def volatile_fields(fields: list[str]) -> list[str]:
    """名字一看就是「每次都变」的字段（老化时间、运行时长……）：没明确指定比较字段时默认不比。"""
    return [f for f in fields if _VOLATILE_FIELD.match(f)]

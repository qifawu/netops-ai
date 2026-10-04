"""`zbx-cli` 和 `nb-cli` 共用的命令描述：同一张表既生成命令行，也生成给模型的工具，
名字和参数只有一个出处。纯数据结构，谁都不依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParamSpec:
    name: str
    flags: tuple[str, ...]
    help: str
    schema: dict[str, Any]
    required: bool = False
    default: Any = None
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandSpec:
    name: str
    help: str
    params: tuple[ParamSpec, ...] = ()
    handler: str = ""
    description: str = ""
    aliases: tuple[str, ...] = ()
    #: 注册给模型时用的名字。留空就按各自 CLI 的命名规则算。
    #: `neighbors` 这条必须显式写成 `topology_neighbors`——SOP 引擎和剧本
    #: 里已经引用了那个名字，改名会静默地让它们查不到。
    tool_name: str = ""

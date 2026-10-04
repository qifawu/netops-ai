"""Intent-to-command catalog, used by the SOP lint to validate intent SOPs.

The catalog is data-driven: adding an OS family only requires a new YAML file
under ``playbooks/catalog``.  This module does not execute commands and never
raises for an unresolved intent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import string
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG_DIR = REPO_ROOT / "playbooks" / "catalog"
ALLOWED_TEMPLATE_FIELDS = frozenset({"interface", "interface_short", "alert_log_prefix"})


@dataclass
class ResolvedCommand:
    """一个「意图」在某个 OS 上换成的具体命令。"""

    intent: str
    os_family: str  # cisco_ios / ...
    command: str  # 已渲染，无残留 {}
    parser: str | None = None
    note: str = ""


def load_catalog(dir: str | Path | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    """Load ``os_family -> intent -> definition`` mappings from YAML files."""
    root = Path(dir) if dir is not None else DEFAULT_CATALOG_DIR
    if not root.exists():
        return {}

    catalog: dict[str, dict[str, dict[str, Any]]] = {}
    for path in sorted(root.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            continue
        os_family = str(data.get("os_family") or path.stem).strip().lower()
        intents = data.get("intents") or {}
        if not os_family or not isinstance(intents, dict):
            continue
        normalized: dict[str, dict[str, Any]] = {}
        for intent, definition in intents.items():
            if not isinstance(definition, dict):
                continue
            normalized[str(intent)] = dict(definition)
        catalog[os_family] = normalized
    return catalog


def resolve_intent(
    intent: str,
    os_family: str,
    params: dict[str, Any] | None,
    catalog: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> ResolvedCommand | None:
    """Render an intent command, returning ``None`` for all unresolved cases."""
    try:
        catalog_data = catalog if catalog is not None else load_catalog()
        family = str(os_family or "").strip().lower()
        intent_name = str(intent or "").strip()
        definition = catalog_data.get(family, {}).get(intent_name)
        if not isinstance(definition, dict):
            return None
        template = definition.get("command")
        if not isinstance(template, str) or not template.strip():
            return None
        values = params or {}
        fields = _template_fields(template)
        if fields is None or not fields.issubset(ALLOWED_TEMPLATE_FIELDS):
            return None
        # 空串（如告警名里没有接口）也算没解析出来：`interface=""` 会渲染成不带接口的 `show interfaces `，回显是全部接口
        if any(field not in values or values[field] is None or (isinstance(values[field], str) and not values[field].strip())
               for field in fields):
            return None
        if any(not isinstance(values[field], (str, int, float)) for field in fields):
            return None
        command = template.format(**{field: values[field] for field in fields})
        if "{" in command or "}" in command:
            return None
        return ResolvedCommand(
            intent=intent_name,
            os_family=family,
            command=command,
            parser=_optional_text(definition.get("parser")),
            note=str(definition.get("note") or ""),
        )
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _template_fields(template: str) -> set[str] | None:
    fields: set[str] = set()
    try:
        for _, field_name, format_spec, conversion in string.Formatter().parse(template):
            if field_name is None:
                continue
            if not field_name or not field_name.isidentifier() or format_spec or conversion:
                return None
            fields.add(field_name)
    except ValueError:
        return None
    return fields


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)

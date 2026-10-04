"""证据链的机械校验：每条 `evidence.source` 是不是从标注来源的原文里复制的。

结果分四级：

* ``verbatim``：标注来源里逐字命中
* ``cross_source``：标注来源里没有，但整段在另一份来源里逐字存在
* ``reformatted``：只去掉渲染前缀、压缩空白或拆开同一上下文内的片段后命中
* ``fabricated``：以上都不命中

这里仍然不做编辑距离、相似度或“差不多”的模糊匹配。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from netops_ai.analysis.doc_blocks import document_paragraphs, without_document_paragraphs


EvidenceGrade = Literal["verbatim", "cross_source", "reformatted", "fabricated"]


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


@dataclass(frozen=True)
class EvidenceVerdict:
    index: int
    claim: str
    source: str
    source_from: str
    verified: bool
    reason: str = ""
    grade: EvidenceGrade = "fabricated"


_SOURCE_ALIAS = {"监控": "zabbix", "设备": "device", "文档参考": "doc", "文档": "doc"}
_REFORMAT_PREFIXES = (
    re.compile(r"^\d+="),
    re.compile(r"^\d{2}:\d{2}:\d{2}\s+\[(?:device|zabbix)\]\s+"),
    re.compile(r"^-\s+"),
)
_SPLIT_PATTERN = re.compile(r"\s*(?:->|；|…|\.{3})\s*")


def _strip_render_prefixes(text: str) -> str:
    out = text.strip()
    out = out.partition("【Zabbix 原始摘录】")[0].strip()
    changed = True
    while changed:
        changed = False
        for pattern in _REFORMAT_PREFIXES:
            new = pattern.sub("", out, count=1).strip()
            if new != out:
                out = new
                changed = True
    if out.startswith("### "):
        out = out[4:].strip()
    out = re.sub(r"`([^`]+)`", r"\1", out)
    out = re.sub(r"^[A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\d+(?:\.\d+){3}\s+\d+:\s+(\*)", r"\1", out)
    return out


def _strip_render_markup(text: str) -> str:
    out = re.sub(r"`([^`]+)`", r"\1", text)
    out = re.sub(r"(?m)^###\s+", "", out)
    return out


def _render_variants(text: str) -> list[str]:
    stripped = _strip_render_prefixes(text)
    variants = [text, stripped]
    variants.append(text.replace("\n", r"\n"))
    variants.append(stripped.replace("\n", r"\n"))
    variants.extend(_strip_render_markup(v) for v in list(variants))
    return [v for i, v in enumerate(variants) if v and v not in variants[:i]]


def _logical_units(raw: str) -> list[str]:
    """拆出“同一上下文”的保守单位，供分段匹配使用。

    不把整份输入当作一个大池子，否则把两段不相邻的原文拼起来也会被放过。
    """
    units = [line for line in raw.splitlines() if line.strip()]

    fence = re.compile(r"```(.*?)```", re.S)
    units.extend(m.group(1) for m in fence.finditer(raw) if m.group(1).strip())

    current: list[str] = []
    for line in raw.splitlines():
        if line.startswith("### ") and current:
            block = "\n".join(current).strip()
            if block:
                units.append(block)
            current = [line]
        elif current or line.startswith("### "):
            current.append(line)
    if current:
        block = "\n".join(current).strip()
        if block:
            units.append(block)
    return units


def _contains_all_pieces(unit: str, pieces: list[str]) -> bool:
    norm_unit = _normalize_spaces(unit)
    for piece in pieces:
        norm_pieces = [_normalize_spaces(v) for v in _render_variants(piece)]
        norm_pieces = [p for p in norm_pieces if p]
        if not norm_pieces:
            return False
        if not any(norm_piece in norm_unit for norm_piece in norm_pieces):
            return False
    return True


def _reformatted_match(source: str, raw_pool: str) -> bool:
    norm_source = _normalize_spaces(source)
    norm_pool = _normalize_spaces(raw_pool)
    norm_render_pool = _normalize_spaces(_strip_render_markup(raw_pool))
    if norm_source and norm_source in norm_pool:
        return True

    for variant in _render_variants(source):
        norm_variant = _normalize_spaces(variant)
        if (variant != source and norm_variant in norm_pool) or norm_variant in norm_render_pool:
            return True

    if any(mark in source for mark in ("->", "；", "…")):
        pieces = [p for p in _SPLIT_PATTERN.split(source) if p.strip()]
        if len(pieces) >= 2:
            return any(_contains_all_pieces(unit, pieces) for unit in _logical_units(raw_pool))
    if ": " in source and "=" in source:
        head, tail = source.split(": ", 1)
        pieces = [head + ":", tail]
        return any(_contains_all_pieces(unit, pieces) for unit in _logical_units(raw_pool))
    return False


def _make_verdict(
    index: int,
    claim: str,
    source: str,
    source_from: str,
    grade: EvidenceGrade,
    reason: str = "",
) -> EvidenceVerdict:
    return EvidenceVerdict(
        index=index,
        claim=claim,
        source=source,
        source_from=source_from,
        verified=grade != "fabricated",
        reason=reason,
        grade=grade,
    )


def verify_evidence(
    evidence: list[dict],
    zabbix_text: str,
    device_text: str | None,
    document_text: str | None = None,
) -> list[EvidenceVerdict]:
    """按 `source_from` 分别在对应来源的原文里做分级核对。"""
    pools = {
        "zabbix": without_document_paragraphs(zabbix_text),
        "device": without_document_paragraphs(device_text),
        "doc": document_text or document_paragraphs(zabbix_text) or document_paragraphs(device_text),
    }
    results: list[EvidenceVerdict] = []
    for i, e in enumerate(evidence):
        # schema 枚举已中文化（监控/设备），旧记录和旧调用方还是 zabbix/device，两种都认。
        # 少了这一步的时候真跑：3/3 条证据全被判「来源不是 zabbix/device」→ 逐字核对 0/3 →
        # 置信度被压成 low，明明结论对、引文也都是逐字的（真跑验出来的）。
        source_from = _SOURCE_ALIAS.get(e.get("source_from", ""), e.get("source_from", ""))
        claim = e.get("claim", "")
        source = e.get("source", "")

        if source_from not in pools:
            results.append(
                _make_verdict(
                    i, claim, source, source_from, "fabricated",
                    f"source_from 不是 zabbix/device/doc 之一：{source_from!r}",
                )
            )
            continue

        pool = pools[source_from]
        if not pool:
            results.append(
                _make_verdict(
                    i, claim, source, source_from, "fabricated",
                    f"标注来源是 {source_from!r}，但这次分析没有喂这份输入数据",
                )
            )
            continue

        if not source.strip():
            results.append(_make_verdict(i, claim, source, source_from, "fabricated", "source 是空的"))
            continue

        if source in pool:
            results.append(_make_verdict(i, claim, source, source_from, "verbatim"))
            continue

        # 整段逐字在**另一份**原文里：不是编造，是来源标错了。典型：Zabbix 收到的设备 syslog
        # 被模型标成「设备」。
        other = next((k for k, v in pools.items() if k != source_from and v and source in v), None)
        if other:
            results.append(
                _make_verdict(
                    i,
                    claim,
                    source,
                    source_from,
                    "cross_source",
                    f"标注来源是 {source_from!r}，原文实际在 {other!r} 里（来源标错，内容是逐字的）",
                )
            )
            continue

        if _reformatted_match(source, pool):
            results.append(
                _make_verdict(
                    i,
                    claim,
                    source,
                    source_from,
                    "reformatted",
                    "标注来源里找不到逐字原文，但去掉渲染格式/压缩空白/同一上下文拆段后能对上",
                )
            )
            continue

        other_reformatted = next(
            (k for k, v in pools.items() if k != source_from and v and _reformatted_match(source, v)),
            None,
        )
        if other_reformatted:
            results.append(
                _make_verdict(
                    i,
                    claim,
                    source,
                    source_from,
                    "reformatted",
                    f"标注来源是 {source_from!r}，原文实际在 {other_reformatted!r} 里，且去掉渲染格式后能对上",
                )
            )
            continue

        results.append(
            _make_verdict(
                i,
                claim,
                source,
                source_from,
                "fabricated",
                "这段引文在原文里找不到（可能掉字/改字/跨来源或跨上下文拼接）",
            )
        )
    return results


def summarize(results: list[EvidenceVerdict]) -> dict:
    total = len(results)
    ok = sum(1 for r in results if r.verified)
    grades = {grade: sum(1 for r in results if r.grade == grade) for grade in _GRADE_ORDER}
    return {
        "total": total,
        "verified": ok,
        "unverified": total - ok,
        "unverified_indices": [r.index for r in results if not r.verified],
        "grades": grades,
        **grades,
    }


_GRADE_ORDER: tuple[EvidenceGrade, ...] = ("verbatim", "cross_source", "reformatted", "fabricated")

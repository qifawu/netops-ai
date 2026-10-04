"""Split doc_search results (marked paragraphs) from device evidence in a mixed transcript."""

from __future__ import annotations

_DOC_MARKER = "【文档参考·非设备证据】"


def document_paragraphs(text: str) -> str:
    """Extract marked documentation blocks from a mixed exploration transcript."""

    blocks = [block.strip() for block in str(text or "").split("\n\n") if _DOC_MARKER in block]
    return "\n\n".join(blocks)


def without_document_paragraphs(text: str) -> str:
    """Keep exploration/device text while excluding marked documentation blocks."""

    blocks = [block.strip() for block in str(text or "").split("\n\n") if _DOC_MARKER not in block]
    return "\n\n".join(blocks)

"""Ingest local vendor documentation into a SQLite FTS5 database."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator

SUPPORTED_SUFFIXES = {".md", ".txt", ".html", ".htm", ".pdf"}
DEFAULT_OS_FAMILY = "cisco_ios"
DEFAULT_MAX_CHARS = 1000
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
_SYSLOG_RE = re.compile(r"(?m)^\s*(%[A-Za-z0-9_]+-\d-[A-Za-z0-9_]+:)")


class _HtmlTextParser(HTMLParser):
    """Small stdlib-only HTML to text converter that preserves heading levels."""

    _SKIP_TAGS = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self.lines.append(f"{'#' * int(tag[1])} ")
        elif tag in {"p", "div", "li", "tr", "br", "section", "article"}:
            self.lines.append("")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif not self._skip_depth and (tag in {"p", "div", "li", "tr", "section", "article"} or re.fullmatch(r"h[1-6]", tag)):
            self.lines.append("")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            text = " ".join(data.split())
            if text:
                if self.lines and self.lines[-1].startswith("#"):
                    self.lines[-1] += text
                else:
                    self.lines.append(text)


def _read_file(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return "", "跳过 PDF（未安装 pypdf）"
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages), ""
    raw = path.read_text(encoding="utf-8", errors="replace")
    if suffix in {".html", ".htm"}:
        parser = _HtmlTextParser()
        parser.feed(raw)
        return "\n".join(parser.lines), ""
    return raw, ""


def _heading_sections(text: str, fallback_title: str) -> list[tuple[str, str]]:
    """Return ``(heading_path, body)`` sections while tracking heading nesting."""

    stack: list[str] = []
    sections: list[tuple[str, str]] = []
    body: list[str] = []
    current_path = ""

    def flush() -> None:
        content = "\n".join(body).strip()
        if content:
            sections.append((current_path or fallback_title, content))
        body.clear()

    for line in text.splitlines():
        match = _HEADING_RE.match(line.strip())
        if not match:
            body.append(line.rstrip())
            continue
        flush()
        level = len(match.group(1))
        title = match.group(2).strip().lstrip("#").strip()
        stack[:] = stack[: level - 1]
        stack.append(title)
        current_path = " › ".join(stack)
    flush()
    return sections or [(fallback_title, text.strip())] if text.strip() else []


def _split_generic(text: str, max_chars: int) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        pieces = [paragraph[i : i + max_chars] for i in range(0, len(paragraph), max_chars)] or [paragraph]
        for piece in pieces:
            if current and len(current) + 2 + len(piece) > max_chars:
                chunks.append(current)
                current = ""
            current = piece if not current else f"{current}\n\n{piece}"
    if current:
        chunks.append(current)
    return chunks


def _split_section(text: str, max_chars: int) -> list[str]:
    """Split prose near 1000 chars, keeping each syslog entry as one unit."""

    matches = list(_SYSLOG_RE.finditer(text))
    if not matches:
        return _split_generic(text, max_chars)

    units: list[tuple[str, bool]] = []
    prefix = text[: matches[0].start()].strip()
    if prefix:
        units.extend((piece, False) for piece in _split_generic(prefix, max_chars))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        entry = text[match.start() : end].strip()
        if entry:
            units.append((entry, True))

    chunks: list[str] = []
    current = ""
    for unit, is_syslog in units:
        if is_syslog and len(unit) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(unit)
            continue
        if current and len(current) + 2 + len(unit) > max_chars:
            chunks.append(current)
            current = ""
        current = unit if not current else f"{current}\n\n{unit}"
    if current:
        chunks.append(current)
    return chunks


def _iter_source_files(corpus_dir: Path) -> Iterator[Path]:
    for path in sorted(corpus_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            yield path


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS kb_files (
            source TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            vendor TEXT NOT NULL,
            os_family TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            source,
            title,
            heading_path,
            vendor,
            os_family,
            content,
            tokenize = "unicode61 tokenchars '-_%'"
        );
        """
    )


def ingest(
    corpus_dir: str | Path,
    db_path: str | Path = "records/docs_kb.db",
    *,
    vendor: str = "cisco",
    os_family: str = DEFAULT_OS_FAMILY,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict[str, int]:
    """Incrementally ingest a local corpus and return a small operation summary."""

    corpus = Path(corpus_dir).resolve()
    db = Path(db_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    if not corpus.is_dir():
        raise FileNotFoundError(f"语料目录不存在：{corpus}")

    summary = {"files_seen": 0, "files_changed": 0, "files_skipped": 0, "chunks_written": 0, "pdf_skipped": 0}
    seen: set[str] = set()
    with sqlite3.connect(db) as conn:
        _schema(conn)
        for path in _iter_source_files(corpus):
            source = path.relative_to(corpus).as_posix()
            seen.add(source)
            summary["files_seen"] += 1
            data, warning = _read_file(path)
            if warning:
                summary["pdf_skipped"] += 1
                continue
            digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
            old = conn.execute("SELECT content_hash FROM kb_files WHERE source = ?", (source,)).fetchone()
            if old and old[0] == digest:
                summary["files_skipped"] += 1
                continue

            summary["files_changed"] += 1
            conn.execute("DELETE FROM docs_fts WHERE source = ?", (source,))
            if data.strip():
                title = path.stem
                for heading_path, section in _heading_sections(data, title):
                    for chunk in _split_section(section, max_chars):
                        conn.execute(
                            "INSERT INTO docs_fts(source, title, heading_path, vendor, os_family, content) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (source, title, heading_path, vendor, os_family, chunk),
                        )
                        summary["chunks_written"] += 1
            conn.execute(
                "INSERT INTO kb_files(source, content_hash, vendor, os_family) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(source) DO UPDATE SET content_hash=excluded.content_hash, "
                "vendor=excluded.vendor, os_family=excluded.os_family",
                (source, digest, vendor, os_family),
            )

        stale = conn.execute("SELECT source FROM kb_files").fetchall()
        for (source,) in stale:
            if source not in seen:
                conn.execute("DELETE FROM docs_fts WHERE source = ?", (source,))
                conn.execute("DELETE FROM kb_files WHERE source = ?", (source,))
    return summary

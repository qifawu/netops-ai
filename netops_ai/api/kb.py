"""知识库页面用的只读接口：概览 + 检索试验台。

**全部只读**：库文件用 `mode=ro` 打开，库不存在 / 表不存在 / 为空时返回友好的空结构，不 500。
检索本身直接调 `netops_ai.docs_kb.search.search`（就是对话里 `doc_search` 工具走的那个函数），
「本次检索表达式」用它的内部函数推导，只读不改。

检索方式是 SQLite FTS5 + BM25 关键词检索，**没有向量 / embedding**。
"""

from __future__ import annotations

import csv
import os
import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter

from netops_ai.docs_kb import search as _search_mod
from netops_ai.docs_kb.search import search as search_docs

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "records" / "docs_kb.db"
#: manifest 先找 corpus/ 下，再找 corpus/cisco/（本机实际放在后者）。DOC_KB_MANIFEST 可覆盖（测试用）。
MANIFEST_CANDIDATES = (
    REPO_ROOT / "docs_kb" / "corpus" / "manifest.tsv",
    REPO_ROOT / "docs_kb" / "corpus" / "cisco" / "manifest.tsv",
)
REPORT_FALLBACK = REPO_ROOT / "docs" / "rag-ios159-report.md"

METHOD_NOTE = "关键词检索（SQLite FTS5 + BM25），未使用向量/embedding"
#: 自己整理的速查表：不是 Cisco 官方文档
LAB_MARKERS = ("lab-ios-log-messages",)

router = APIRouter(prefix="/api/kb", tags=["kb"])


# ---------- 路径 / 连接 ----------

def _db_path() -> Path:
    from .pipeline import _env

    return Path(_env().get("DOC_SEARCH_DB") or DEFAULT_DB)


def _connect_ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


# ---------- 元数据：manifest / 报告兜底 ----------

def _slug(url: str) -> str:
    """跟语料文件名同一套规则：去协议、非字母数字换成 -，文件名是从左边截断过的。"""
    return re.sub(r"[^A-Za-z0-9]+", "-", re.sub(r"^https?://", "", url)).strip("-")


def _read_manifest() -> dict[str, dict]:
    """返回 {文件名: {url,title,version,fetched}}。manifest 优先，没有再从 rag 报告表格里兜底，都没有返回空。"""
    override = os.environ.get("DOC_KB_MANIFEST")
    candidates = (Path(override),) if override else MANIFEST_CANDIDATES
    for path in candidates:
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8", newline="") as fh:
                out = {}
                for row in csv.DictReader(fh, delimiter="\t"):
                    name = (row.get("file") or "").strip()
                    if name:
                        out[name] = {
                            "url": (row.get("url") or "").strip(),
                            "title": (row.get("title") or "").strip(),
                            "version": (row.get("applicable_version") or "").strip(),
                            "fetched": (row.get("fetched_date") or "").strip(),
                        }
                if out:
                    return out
        except (OSError, csv.Error, UnicodeDecodeError):
            continue
    return _read_report_fallback()


def _read_report_fallback() -> dict[str, dict]:
    """内部文档 里 `| n | 来源 | 标题 | URL | 适用版本 | 抓取日 |` 的表格行。key 是 URL 的 slug。"""
    path = Path(os.environ["DOC_KB_REPORT"]) if os.environ.get("DOC_KB_REPORT") else REPORT_FALLBACK
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, dict] = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 6 and cells[3].startswith("http"):
            out["slug:" + _slug(cells[3])] = {"url": cells[3], "title": cells[2], "version": cells[4], "fetched": cells[5]}
    return out


def _lookup_meta(manifest: dict[str, dict], source: str) -> dict:
    name = source.rsplit("/", 1)[-1]
    if name in manifest:
        return manifest[name]
    # 兜底表是按 URL slug 存的；语料文件名是 slug 从左边截断的结果，去掉文件扩展名再比后缀
    stem = name.rsplit(".", 1)[0]
    for key, meta in manifest.items():
        if not key.startswith("slug:"):
            continue
        slug = key[5:]
        if stem and (slug.endswith(stem) or stem.endswith(slug)):
            return meta
    return {}


# ---------- 分类 ----------

_FAMILY_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("日志", re.compile(r"system message|message log|log message|lab-ios-log|syslog|日志", re.I)),
    ("重启", re.compile(r"reload|crash|重启", re.I)),
    ("SNMP", re.compile(r"snmp", re.I)),
    ("STP", re.compile(r"spanning|\bstp\b|errdisable", re.I)),
    ("OSPF", re.compile(r"ospf", re.I)),
    ("BGP", re.compile(r"\bbgp\b|border gateway|bgp", re.I)),
    ("接口", re.compile(r"interface|\bport\b", re.I)),
)
FAMILY_ORDER = ["OSPF", "BGP", "SNMP", "接口", "重启", "日志", "STP", "其他"]


def _family(title: str, source: str) -> str:
    text = f"{title} {source.rsplit('/', 1)[-1]}"
    for name, pat in _FAMILY_RULES:
        if pat.search(text):
            return name
    return "其他"


def _kind(source: str) -> str:
    return "lab_reference" if any(m in source for m in LAB_MARKERS) else "official"


def _first_segment(heading_path: str) -> str:
    return (heading_path or "").split(" › ", 1)[0].strip()


# ---------- 文档清单 ----------

def _documents(conn: sqlite3.Connection) -> list[dict]:
    manifest = _read_manifest()
    # 取每份文档第一个片段（rowid 最小）的 heading_path 第一段当页面标题，而不是字母序最小的那个
    rows = conn.execute(
        "SELECT g.source AS source, g.n AS n, f.heading_path AS hp, f.title AS t FROM "
        "(SELECT source, COUNT(*) AS n, MIN(rowid) AS r FROM docs_fts GROUP BY source) g "
        "JOIN docs_fts f ON f.rowid = g.r"
    ).fetchall()
    docs = []
    for r in rows:
        source = r["source"]
        meta = _lookup_meta(manifest, source)
        # 页面真实标题：manifest 优先；否则用 heading_path 的第一段；再不行退回库里的 title 列
        title = meta.get("title") or _first_segment(r["hp"]) or r["t"] or source
        docs.append({
            "source": source,
            "title": title,
            "url": meta.get("url", ""),
            "version": meta.get("version", ""),
            "chunks": r["n"],
            "fetched": meta.get("fetched", ""),
            "kind": _kind(source),
            "family": _family(title, source),
        })
    docs.sort(key=lambda d: (FAMILY_ORDER.index(d["family"]), d["title"].casefold()))
    return docs


def _doc_index(conn: sqlite3.Connection) -> dict[str, dict]:
    return {d["source"]: d for d in _documents(conn)}


def rerun_doc_search(args: dict) -> list[dict]:
    """对话里 doc_search 那次调用的参数（query/vendor/limit），对同一个库再检索一次。失败一律返回空列表。"""
    try:
        return search_docs(
            _db_path(), str(args.get("query") or ""),
            vendor=str(args.get("vendor") or "").strip() or None, limit=int(args.get("limit") or 5),
        )
    except Exception:  # noqa: BLE001 - 出处只是展示用，不能拖垮这次问答
        return []


# ---------- 接口 ----------

@router.get("/overview")
def kb_overview() -> dict:
    path = _db_path()
    out: dict = {
        "doc_count": 0, "chunk_count": 0, "db_size_bytes": 0, "db_modified": None,
        "method": METHOD_NOTE, "documents": [], "available": False,
    }
    conn = _connect_ro(path)
    if conn is None:
        return out
    try:
        st = path.stat()
        out["db_size_bytes"] = st.st_size
        out["db_modified"] = st.st_mtime
        docs = _documents(conn)
        out.update(documents=docs, doc_count=len(docs), chunk_count=sum(d["chunks"] for d in docs), available=True)
    except sqlite3.Error:
        pass  # 库文件在但没建表：返回空结构
    finally:
        conn.close()
    return out


def describe_query(q: str, limit: int, db_path: Path, vendor: str | None = None) -> dict:
    """本次实际检索表达式：AND 词表 / 是否用了 OR 补齐 / 中文映射出的英文词。只读推导，不改 search.py。"""
    tokens, _priority = _search_mod._query_tokens(q)
    zh = _search_mod._zh_tokens(q)
    and_expr, _ = _search_mod._query_expression(q)
    or_expr = _search_mod._fallback_expression(q)
    used_or = False
    conn = _connect_ro(db_path)
    if conn is not None:
        try:
            cand = max(1, min(int(limit or 5), 50))
            fetch = min(cand * 5, 100)
            if and_expr:
                used_or = bool(or_expr) and len(_search_mod._run(conn, and_expr, vendor, fetch)) < cand
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return {
        "tokens": tokens,
        "and": and_expr,
        "or": or_expr if used_or else "",
        "used_or": used_or,
        "zh_mapped": list(dict.fromkeys(zh)),
        "match_all_required": bool(tokens),
    }


@router.get("/search")
def kb_search(q: str = "", limit: int = 8, vendor: str = "") -> dict:
    query = (q or "").strip()
    limit = max(1, min(int(limit or 8), 20))
    path = _db_path()
    empty = {"q": query, "results": [], "expression": describe_query("", limit, path), "method": METHOD_NOTE}
    if not query:
        return empty
    conn = _connect_ro(path)
    if conn is None:
        return empty
    try:
        index = _doc_index(conn)
    except sqlite3.Error:
        return empty
    finally:
        conn.close()
    rows = search_docs(path, query, vendor=vendor.strip() or None, limit=limit)
    results = []
    for r in rows:
        d = index.get(r["source"], {})
        results.append({
            "source": r["source"],
            "title": d.get("title") or _first_segment(r["heading_path"]) or r["title"],
            "heading_path": r["heading_path"],
            "snippet": r["snippet"],
            "score": r["score"],
            "url": d.get("url", ""),
            "version": d.get("version", ""),
            "kind": d.get("kind") or _kind(r["source"]),
        })
    return {"q": query, "results": results, "expression": describe_query(query, limit, path, vendor.strip() or None),
            "method": METHOD_NOTE}

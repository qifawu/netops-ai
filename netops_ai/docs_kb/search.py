"""Safe BM25 search over the local SQLite FTS5 documentation index."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

_MNEMONIC_RE = re.compile(r"%[A-Za-z0-9_]+-\d-[A-Za-z0-9_]+")
_INTERFACE_RE = re.compile(
    r"\b(?:GigabitEthernet|FastEthernet|TenGigabitEthernet|Ethernet|Serial|Loopback|Vlan|Port-channel|Gi|Fa|Te|Eth|Se|Lo|Po)\s*[\w/.-]+\b",
    re.IGNORECASE,
)
_PROTOCOL_RE = re.compile(r"\b(?:ospf|bgp|eigrp|stp|vlan|arp|cdp|lldp|icmp|snmp|hsrp|isis)\b", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[%A-Za-z0-9_/-]+")

# 虚词不参与检索：语料里几乎每块都有，只会把「多一个词就 0 命中」的问题放大。
_STOPWORDS = frozenset(
    "the a an of to in on at is are was were be and or for with how what why when does do did this that it its "
    "from by as if not no can could should".split()
)

# 语料是英文，中文查询词按这张表补上对应的英文检索词（长词在前，先匹配长的）。
_ZH_TERMS: tuple[tuple[str, str], ...] = (
    ("管理性关闭", "administratively down"),
    ("管理员关闭", "administratively down"),
    ("邻居关系", "neighbor adjacency"),
    ("邻接关系", "adjacency"),
    ("计时器", "timer"),
    ("定时器", "timer"),
    ("不一致", "mismatch"),
    ("不匹配", "mismatch"),
    ("未知原因", "unknown reason"),
    ("重启原因", "reload reason"),
    ("邻居", "neighbor"),
    ("邻接", "adjacency"),
    ("接口", "interface"),
    ("链路", "link"),
    ("日志", "logging"),
    ("重启", "reload"),
    ("关闭", "shutdown"),
    ("状态", "state"),
    ("会话", "session"),
    ("路由", "route"),
    ("超时", "timeout"),
    ("认证", "authentication"),
    ("死亡", "dead"),
    ("握手", "hello"),
    ("告警", "trap"),
    ("端口", "port"),
    ("故障", "troubleshoot"),
    ("排障", "troubleshoot"),
    ("排查", "troubleshoot"),
    ("命令", "command"),
    ("含义", "meaning"),
    ("原因", "cause"),
    ("配置", "configuration"),
    ("交换机", "switch"),
    ("路由器", "router"),
)

_MAX_OR_TOKENS = 10
#: 命中片段的展示长度：FTS5 `snippet()` 最多 64 个词。以前 12 个词常常只是半句话。
_SNIPPET_TOKENS = 64
_SNIPPET_CHARS = 900


def _quote_fts_token(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def _zh_tokens(raw: str) -> list[str]:
    """把查询里的中文词换成英文检索词。没有对应的中文词直接忽略（FTS5 也搜不到）。"""
    found: list[str] = []
    rest = raw
    for zh, en in _ZH_TERMS:
        if zh in rest:
            found.extend(en.split())
            rest = rest.replace(zh, " ")
    return found


def _query_tokens(query: str) -> tuple[list[str], list[str]]:
    """返回 (去重后的检索词, 优先词)。优先词=日志助记符/接口/协议名，命中它的片段排前面。"""
    raw = str(query or "").strip()
    if not raw:
        return [], []
    priority = [m.group(0) for m in _MNEMONIC_RE.finditer(raw)]
    priority += [m.group(0) for m in _INTERFACE_RE.finditer(raw)]
    priority += [m.group(0) for m in _PROTOCOL_RE.finditer(raw)]
    tokens = priority + _TOKEN_RE.findall(raw) + _zh_tokens(raw)
    unique: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        normalized = token.strip()
        folded = normalized.casefold()
        if not normalized or folded in seen:
            continue
        seen.add(folded)
        if folded in _STOPWORDS and normalized not in priority:
            continue
        unique.append(normalized)
    return unique, priority


def _query_expression(query: str) -> tuple[str, list[str]]:
    tokens, priority = _query_tokens(query)
    if not tokens:
        return "", []
    return " AND ".join(_quote_fts_token(token) for token in tokens), priority


def _fallback_expression(query: str) -> str:
    """AND 一个都没命中时的退路：任一词命中即可，由 BM25 把命中词多的排前面。"""
    tokens, priority = _query_tokens(query)
    if len(tokens) < 2:
        return ""
    # 优先词（助记符等）在前，超过上限只留前面的。
    ordered = [t for t in tokens if t in priority] + [t for t in tokens if t not in priority]
    return " OR ".join(_quote_fts_token(token) for token in ordered[:_MAX_OR_TOKENS])


def _run(conn: sqlite3.Connection, expression: str, vendor: str | None, fetch: int) -> list[sqlite3.Row]:
    params: list[object] = [expression]
    where = ["docs_fts MATCH ?"]
    if vendor:
        where.append("vendor = ?")
        params.append(vendor)
    return conn.execute(
        "SELECT source, title, heading_path, vendor, os_family, content, "
        f"snippet(docs_fts, 5, '', '', '…', {_SNIPPET_TOKENS}) AS matched_snippet, bm25(docs_fts) AS rank "
        f"FROM docs_fts WHERE {' AND '.join(where)} ORDER BY rank LIMIT ?",
        [*params, fetch],
    ).fetchall()


def search(
    db_path: str | Path,
    query: str,
    *,
    vendor: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Search the FTS5 index; missing/empty databases deliberately return ``[]``.

    先要求所有词都出现；结果不够 limit 条时再用「任一词命中」补齐（多一个语料里没有的词不至于整条查询 0 命中，AND 命中的仍排在前面）。
    """

    expression, priority = _query_expression(query)
    path = Path(db_path)
    if not expression or not path.exists():
        return []
    conn = None
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        candidate_limit = max(1, min(int(limit or 5), 50))
        fetch = min(candidate_limit * 5, 100)
        rows = _run(conn, expression, vendor, fetch)
        if len(rows) < candidate_limit:
            fallback = _fallback_expression(query)
            if fallback:
                seen = {(r["source"], r["heading_path"], str(r["content"])[:80]) for r in rows}
                rows = [
                    *rows,
                    *(r for r in _run(conn, fallback, vendor, fetch)
                      if (r["source"], r["heading_path"], str(r["content"])[:80]) not in seen),
                ]
    except (sqlite3.Error, ValueError, TypeError):
        return []
    finally:
        if conn is not None:
            conn.close()

    priority_folded = [item.casefold() for item in priority if item]
    results = []
    for row in rows:
        content = str(row["content"] or "")
        exact = any(item in content.casefold() for item in priority_folded)
        snippet = str(row["matched_snippet"] or content[:_SNIPPET_CHARS]).strip()
        results.append(
            {
                "source": row["source"],
                "title": row["title"],
                "heading_path": row["heading_path"],
                "snippet": snippet[:_SNIPPET_CHARS],
                "score": float(row["rank"]),
                "_exact": exact,
            }
        )
    results.sort(key=lambda item: (not item.pop("_exact"), item["score"]))
    return results[: max(1, min(int(limit or 5), 50))]

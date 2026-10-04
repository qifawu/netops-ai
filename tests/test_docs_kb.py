from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest import mock

from netops_ai.docs_kb.ingest import ingest
from netops_ai.docs_kb.search import search
from netops_ai.graph import chat_agent


FIXTURES = Path(__file__).parent / "fixtures" / "docs_kb"


def test_ingest_chunks_keep_syslog_entry_intact(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    long_entry = (
        "%LINK-3-UPDOWN: Interface GigabitEthernet0/1, changed state to down\n\n"
        "Explanation: " + ("The physical state changed and this sample text explains the event. " * 8) + "\n\n"
        "Recommended Action: " + ("Check the peer, counters, optics, and recent changes. " * 8)
    )
    (corpus / "long.md").write_text(f"# Interface Messages\n\n{long_entry}\n", encoding="utf-8")
    db = tmp_path / "docs_kb.db"

    summary = ingest(corpus, db, max_chars=100)

    assert summary["files_seen"] == 1
    assert summary["chunks_written"] == 1
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT source, heading_path, content FROM docs_fts WHERE content MATCH ?",
            ('"%LINK-3-UPDOWN"',),
        ).fetchall()
    assert len(rows) == 1
    content = rows[0][2]
    assert "%LINK-3-UPDOWN" in content
    assert "Explanation:" in content
    assert "Recommended Action:" in content
    assert len(content) > 100


def test_mnemonic_exact_search_ranks_first(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "generic.md").write_text(
        "# Generic\n\nLINK and DOWN words are present, but this page does not contain the full mnemonic.\n",
        encoding="utf-8",
    )
    (corpus / "exact.md").write_text(
        "# Exact\n\n%LINK-3-UPDOWN: Interface changed state. Explanation: exact message entry.\n",
        encoding="utf-8",
    )
    db = tmp_path / "docs_kb.db"
    ingest(corpus, db)

    rows = search(db, "%LINK-3-UPDOWN interface", limit=2)

    assert rows
    assert rows[0]["source"] == "exact.md"
    assert "%LINK-3-UPDOWN" in rows[0]["snippet"]


def test_query_escaping_and_missing_db_are_safe(tmp_path):
    db = tmp_path / "docs_kb.db"
    ingest(FIXTURES, db)

    assert isinstance(search(db, '%LINK-3-UPDOWN " * ( OR )', limit=5), list)
    assert search(tmp_path / "missing.db", "%LINK-3-UPDOWN") == []
    html_rows = search(db, "show interfaces status", limit=5)
    assert html_rows[0]["source"] == "show_interface.html"
    assert html_rows[0]["heading_path"] == "Show Command Notes › show interfaces status"
    assert "window.secret" not in html_rows[0]["snippet"]


def test_incremental_ingest_skips_unchanged_and_rewrites_changed_file(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    doc = corpus / "link.md"
    doc.write_text("# Link\n\n%LINK-3-UPDOWN: first text\n", encoding="utf-8")
    db = tmp_path / "docs_kb.db"

    first = ingest(corpus, db)
    second = ingest(corpus, db)
    doc.write_text("# Link\n\n%LINK-3-UPDOWN: changed text\n", encoding="utf-8")
    third = ingest(corpus, db)

    assert first["files_changed"] == 1
    assert second["files_skipped"] == 1
    assert third["files_changed"] == 1
    rows = search(db, "%LINK-3-UPDOWN changed", limit=5)
    assert len(rows) == 1
    assert "changed text" in rows[0]["snippet"]


def test_doc_search_tool_is_opt_in_and_marks_results(tmp_path):
    db = tmp_path / "docs_kb.db"
    ingest(FIXTURES, db)
    trace = chat_agent.ChatRunTrace(trace_id="t", question="q", session_id="s")

    with mock.patch("netops_ai.graph.chat_agent.env", return_value={}):
        names = {tool.name for tool in chat_agent.build_chat_tools(trace, chat_agent.AgentLoopBudget())}
    assert "doc_search" not in names

    with mock.patch(
        "netops_ai.graph.chat_agent.env",
        return_value={"DOC_SEARCH": "on", "DOC_SEARCH_DB": str(db)},
    ):
        tools = chat_agent.build_chat_tools(trace, chat_agent.AgentLoopBudget())
        doc_tool = next(tool for tool in tools if tool.name == "doc_search")
        out = doc_tool.invoke({"query": "%LINK-3-UPDOWN", "vendor": "cisco", "limit": 1})
    assert out.startswith("【文档参考·非设备证据】[来源:")
    assert "不是这台设备的实测证据" in doc_tool.description


def test_verify_evidence_does_not_use_document_blocks_as_device_pool():
    from netops_ai.analysis.verify import verify_evidence

    marked_doc = "【文档参考·非设备证据】[来源: sample.md › Heading]\n%LINK-3-UPDOWN means a link changed."
    results = verify_evidence(
        [
            {"claim": "device claim", "source_from": "设备", "source": "%LINK-3-UPDOWN means a link changed."},
            {"claim": "doc claim", "source_from": "文档参考", "source": "%LINK-3-UPDOWN means a link changed."},
        ],
        marked_doc,
        marked_doc,
    )

    assert results[0].verified is False
    assert results[1].verified is True

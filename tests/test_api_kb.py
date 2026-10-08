"""知识库页面接口：概览 / 检索试验台 / 对话流里 doc_search 的出处事件。全用临时库，不碰真实 records/。"""

from __future__ import annotations

import json
import sqlite3
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from netops_ai.api.app import app
from netops_ai.docs_kb.ingest import ingest

client = TestClient(app)

OSPF_MD = (
    "# Troubleshoot OSPF Neighbor Problems\n\n## Hello Dead mismatch\n\n"
    "A Hello and Dead interval mismatch keeps the OSPF neighbor from forming. The hello timer must match.\n\n"
    "## Other\n\nUnrelated text about spanning tree.\n"
)
LAB_MD = (
    "# 实验环境常见 IOS 日志消息速查\n\n## %OSPF-5-ADJCHG 邻居状态变化\n\n"
    "%OSPF-5-ADJCHG: Nbr changed. Reason Dead timer expired means no Hello was received within the dead interval.\n"
)


@pytest.fixture()
def kb(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus" / "cisco"
    corpus.mkdir(parents=True)
    (corpus / "ospf-neighbors-html.md").write_text(OSPF_MD, encoding="utf-8")
    (corpus / "lab-ios-log-messages.md").write_text(LAB_MD, encoding="utf-8")
    db = tmp_path / "kb.db"
    ingest(tmp_path / "corpus", db)
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(
        "file\turl\ttitle\tapplicable_version\tfetched_date\n"
        "ospf-neighbors-html.md\thttps://example.com/ospf\tOSPF Neighbor Doc - Cisco\t版本无关 technote\t2026-09-25\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOC_SEARCH_DB", str(db))
    monkeypatch.setenv("DOC_KB_MANIFEST", str(manifest))
    return db


def test_overview_counts_and_metadata(kb):
    d = client.get("/api/kb/overview").json()
    assert d["available"] is True and d["doc_count"] == 2
    assert d["chunk_count"] == sum(x["chunks"] for x in d["documents"]) > 2
    assert "FTS5" in d["method"] and "未使用向量" in d["method"]
    assert d["db_size_bytes"] > 0 and d["db_modified"]
    by_kind = {x["kind"]: x for x in d["documents"]}
    off, lab = by_kind["official"], by_kind["lab_reference"]
    assert off["title"] == "OSPF Neighbor Doc - Cisco" and off["url"] == "https://example.com/ospf"
    assert off["version"] == "版本无关 technote" and off["fetched"] == "2026-09-25" and off["family"] == "OSPF"
    # 速查表：没有 manifest 记录，标题取 heading_path 第一段，分类为日志
    assert lab["title"].startswith("实验环境常见 IOS 日志消息速查") and lab["url"] == "" and lab["family"] == "日志"


def test_overview_sorted_by_family_then_title(kb):
    fams = [x["family"] for x in client.get("/api/kb/overview").json()["documents"]]
    assert fams == ["OSPF", "日志"]


def test_overview_without_manifest_uses_heading(kb, monkeypatch, tmp_path):
    monkeypatch.setenv("DOC_KB_MANIFEST", str(tmp_path / "nope.tsv"))
    monkeypatch.setenv("DOC_KB_REPORT", str(tmp_path / "nope.md"))
    docs = client.get("/api/kb/overview").json()["documents"]
    off = next(x for x in docs if x["kind"] == "official")
    assert off["title"] == "Troubleshoot OSPF Neighbor Problems" and off["url"] == "" and off["version"] == ""


def test_report_fallback_when_no_manifest(kb, monkeypatch, tmp_path):
    report = tmp_path / "report.md"
    report.write_text(
        "| 1 | 旧语料 | Report Title | https://example.com/ospf-neighbors.html | IOS 15.6 | 2026-09-01 |\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOC_KB_MANIFEST", str(tmp_path / "nope.tsv"))
    monkeypatch.setenv("DOC_KB_REPORT", str(report))
    off = next(x for x in client.get("/api/kb/overview").json()["documents"] if x["kind"] == "official")
    assert off["title"] == "Report Title" and off["version"] == "IOS 15.6"


def test_overview_missing_db_is_friendly(tmp_path, monkeypatch):
    monkeypatch.setenv("DOC_SEARCH_DB", str(tmp_path / "absent.db"))
    r = client.get("/api/kb/overview")
    assert r.status_code == 200
    d = r.json()
    assert d["doc_count"] == 0 and d["documents"] == [] and d["available"] is False and "FTS5" in d["method"]


def test_overview_db_without_tables_is_friendly(tmp_path, monkeypatch):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    monkeypatch.setenv("DOC_SEARCH_DB", str(db))
    assert client.get("/api/kb/overview").status_code == 200
    assert client.get("/api/kb/search", params={"q": "ospf"}).json()["results"] == []


def test_search_enriches_results(kb):
    d = client.get("/api/kb/search", params={"q": "hello timer mismatch", "limit": 5}).json()
    assert d["results"]
    top = d["results"][0]
    assert top["title"] == "OSPF Neighbor Doc - Cisco" and top["url"] == "https://example.com/ospf"
    assert top["kind"] == "official" and top["version"] == "版本无关 technote"
    assert {"source", "heading_path", "snippet", "score"} <= set(top)
    assert d["expression"]["and"] and d["expression"]["match_all_required"] is True


def test_search_lab_reference_kind(kb):
    d = client.get("/api/kb/search", params={"q": "%OSPF-5-ADJCHG Dead timer expired"}).json()
    assert d["results"][0]["kind"] == "lab_reference" and d["results"][0]["url"] == ""


def test_search_empty_query(kb):
    d = client.get("/api/kb/search", params={"q": "   "}).json()
    assert d["results"] == [] and d["expression"]["tokens"] == []


def test_search_chinese_expression_shows_mapped_words(kb):
    d = client.get("/api/kb/search", params={"q": "OSPF 邻居 计时器 不一致"}).json()
    e = d["expression"]
    assert {"neighbor", "timer", "mismatch"} <= set(e["zh_mapped"])
    assert d["results"]


def test_search_reports_or_fill(kb):
    # 只有 1 篇文档满足所有词，结果不足 limit 条，所以用了「任一词命中」补齐
    d = client.get("/api/kb/search", params={"q": "hello dead spanning", "limit": 8}).json()
    assert d["expression"]["used_or"] is True and " OR " in d["expression"]["or"]
    d2 = client.get("/api/kb/search", params={"q": "hello", "limit": 1}).json()
    assert d2["expression"]["used_or"] is False and d2["expression"]["or"] == ""


def test_search_no_hit_and_missing_db(kb, tmp_path, monkeypatch):
    assert client.get("/api/kb/search", params={"q": "zzzqqq"}).json()["results"] == []
    monkeypatch.setenv("DOC_SEARCH_DB", str(tmp_path / "absent.db"))
    r = client.get("/api/kb/search", params={"q": "ospf"})
    assert r.status_code == 200 and r.json()["results"] == []


def test_kb_is_read_only(kb):
    before = kb.read_bytes()
    client.get("/api/kb/overview")
    client.get("/api/kb/search", params={"q": "hello dead"})
    assert kb.read_bytes() == before



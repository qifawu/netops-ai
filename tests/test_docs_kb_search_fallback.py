from pathlib import Path

from netops_ai.docs_kb.ingest import ingest
from netops_ai.docs_kb.search import _query_tokens, search


def _db(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "ospf.md").write_text(
        "# OSPF\n\n## Hello Dead mismatch\n\nA Hello and Dead interval mismatch keeps the OSPF neighbor from forming.\n\n"
        "## Other\n\nUnrelated text about spanning tree.\n",
        encoding="utf-8",
    )
    (corpus / "log.md").write_text(
        "# Logs\n\n## %OSPF-5-ADJCHG\n\n%OSPF-5-ADJCHG: Nbr changed. Reason Dead timer expired means no Hello was received from the neighbor within the configured dead interval, so the adjacency was torn down and routes were withdrawn.\n",
        encoding="utf-8",
    )
    db = tmp_path / "kb.db"
    ingest(corpus, db)
    return db


def test_extra_word_not_in_corpus_no_longer_zeroes_the_query(tmp_path):
    db = _db(tmp_path)
    rows = search(db, "OSPF Hello Dead interval mismatch neighbor stuck", limit=3)
    assert rows and "mismatch" in rows[0]["snippet"].lower()


def test_and_results_rank_before_or_fill(tmp_path):
    db = _db(tmp_path)
    rows = search(db, "%OSPF-5-ADJCHG Dead timer expired", limit=3)
    assert rows[0]["heading_path"].endswith("%OSPF-5-ADJCHG")


def test_chinese_terms_map_to_english_search_words():
    tokens, _ = _query_tokens("OSPF 邻居 计时器 不一致")
    assert {"neighbor", "timer", "mismatch"} <= {t.lower() for t in tokens}
    tokens, _ = _query_tokens("设备重启 原因")
    assert {"reload", "cause"} <= {t.lower() for t in tokens}


def test_chinese_only_query_can_hit(tmp_path):
    db = _db(tmp_path)
    assert search(db, "hello 不一致", limit=3)


def test_stopwords_dropped_but_mnemonic_kept():
    tokens, _ = _query_tokens("what is the %LINK-3-UPDOWN in a log")
    lowered = [t.lower() for t in tokens]
    assert "%link-3-updown" in lowered and "the" not in lowered and "is" not in lowered


def test_snippet_is_longer_than_a_dozen_words(tmp_path):
    db = _db(tmp_path)
    rows = search(db, "Dead timer expired", limit=1)
    assert len(rows[0]["snippet"].split()) > 12

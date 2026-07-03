"""Tests for openkb.engine -- RRF fusion, superseded exclusion, entity
boost, and rerank fail-open, all against a real tiny sqlite db (no live
LLM/embeddings endpoint required -- engine._embed is monkeypatched)."""
from __future__ import annotations

import json
import os

import pytest

from openkb import db as dbmod
from openkb import engine

from .conftest import TEST_DIM


def _seed_document(con, rel_path, domain, texts):
    """Insert one document + its chunks (FTS populated), no vectors yet.
    Returns (document_id, [chunk_id, ...])."""
    cur = con.execute(
        "INSERT INTO documents(source, rel_path, domain, sha256, summary, n_chunks, extractor) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (os.path.basename(rel_path), rel_path, domain, "sha-" + rel_path, "summary", len(texts), "text"),
    )
    document_id = cur.lastrowid
    chunk_ids = []
    for seq, text in enumerate(texts):
        cur = con.execute(
            "INSERT INTO chunks(document_id, seq, text) VALUES (?, ?, ?)", (document_id, seq, text)
        )
        chunk_id = cur.lastrowid
        dbmod.fts_insert(con, chunk_id, text)
        chunk_ids.append(chunk_id)
    con.commit()
    return document_id, chunk_ids


def _set_vector(con, chunk_id, vec):
    con.execute(
        "INSERT INTO vchunks(chunk_id, embedding) VALUES (?, ?)",
        (chunk_id, dbmod.serialize_f32(vec)),
    )
    con.commit()


@pytest.fixture(autouse=True)
def _reset_engine_caches():
    """Every engine test starts with clean module-level caches -- these are
    process-global and would otherwise leak state between tests."""
    engine._ALIAS_MAP_CACHE = None
    engine._SUPERSEDED_CACHE = {"mtime": -1.0, "set": set()}
    yield
    engine._ALIAS_MAP_CACHE = None
    engine._SUPERSEDED_CACHE = {"mtime": -1.0, "set": set()}


def _basic_two_doc_db(cfg):
    os.makedirs(os.path.dirname(cfg["paths"]["db_path"]), exist_ok=True)
    con = dbmod.connect(cfg["paths"]["db_path"])
    dbmod.init_schema(con, dim=TEST_DIM)

    doc_a, chunks_a = _seed_document(
        con, "a/doc-a.md", "00_ELECTRICAL", ["the quick brown fox jumps over the lazy dog"]
    )
    doc_b, chunks_b = _seed_document(
        con, "b/doc-b.md", "01_MECHANICAL", ["a completely unrelated sentence about pumps"]
    )
    # orthogonal-ish vectors so vector KNN has a clear preference
    _set_vector(con, chunks_a[0], [1.0, 0, 0, 0, 0, 0, 0, 0])
    _set_vector(con, chunks_b[0], [0, 1.0, 0, 0, 0, 0, 0, 0])
    con.close()
    return (doc_a, chunks_a), (doc_b, chunks_b)


def test_rrf_fusion_orders_by_combined_rank(cfg, monkeypatch):
    (doc_a, chunks_a), (doc_b, chunks_b) = _basic_two_doc_db(cfg)

    # Query embeds identically to doc_a's vector -- vector arm favours doc_a.
    monkeypatch.setattr(engine, "_embed", lambda q, c: [1.0, 0, 0, 0, 0, 0, 0, 0])

    hits = engine.search("fox", cfg=cfg, mode="hybrid")
    sources = [h["rel_path"] for h in hits]
    assert "a/doc-a.md" in sources
    # doc_a should outrank doc_b: it wins both the vector arm (closest) and
    # the FTS arm (its text contains "fox", doc_b's does not).
    assert sources.index("a/doc-a.md") < sources.index("b/doc-b.md")


def test_superseded_exclusion_removes_chunk(cfg, monkeypatch):
    (doc_a, chunks_a), (doc_b, chunks_b) = _basic_two_doc_db(cfg)
    monkeypatch.setattr(engine, "_embed", lambda q, c: [1.0, 0, 0, 0, 0, 0, 0, 0])

    # sanity: doc_a shows up before superseding it
    hits = engine.search("fox", cfg=cfg, mode="hybrid")
    assert any(h["rel_path"] == "a/doc-a.md" for h in hits)

    # Directly monkeypatch the module cache rather than racing on mtime
    # granularity -- deterministic invalidation as recommended.
    engine._SUPERSEDED_CACHE = {"mtime": 999999999.0, "set": {"a/doc-a.md"}}

    def _fake_superseded(db_path):
        return {"a/doc-a.md"}

    monkeypatch.setattr(engine, "_superseded_rel_paths", _fake_superseded)

    hits2 = engine.search("fox", cfg=cfg, mode="hybrid")
    assert all(h["rel_path"] != "a/doc-a.md" for h in hits2)


def test_entity_boost_promotes_linked_document(cfg, monkeypatch):
    """Two documents that otherwise tie in the fused ranking: one is linked
    via the equipment registry to an alias mentioned in the query, the
    other is not. The linked one must rank higher once entity_boost is on."""
    os.makedirs(os.path.dirname(cfg["paths"]["db_path"]), exist_ok=True)
    con = dbmod.connect(cfg["paths"]["db_path"])
    dbmod.init_schema(con, dim=TEST_DIM)

    # Both documents contain the same lexical content and the same vector,
    # so without the boost they'd be indistinguishable (tie -> insertion
    # order / id order).
    doc_linked, chunks_linked = _seed_document(
        con, "linked/manual.md", "00_ELECTRICAL", ["generic maintenance content about equipment"]
    )
    doc_other, chunks_other = _seed_document(
        con, "other/manual.md", "00_ELECTRICAL", ["generic maintenance content about equipment"]
    )
    _set_vector(con, chunks_linked[0], [1.0, 0, 0, 0, 0, 0, 0, 0])
    _set_vector(con, chunks_other[0], [1.0, 0, 0, 0, 0, 0, 0, 0])

    # Registry: one equipment row aliased "aurorapower500" (>=6 chars, not a
    # stopword) linked only to doc_linked.
    cur = con.execute(
        "INSERT INTO equipment (canonical_name, make, model, aliases_json, tags_json) VALUES (?, ?, ?, ?, ?)",
        ("Aurora Power Systems APG-500", "Aurora Power Systems", "APG-500", json.dumps(["aurorapower500"]), "[]"),
    )
    equipment_id = cur.lastrowid
    con.execute(
        "INSERT INTO doc_equipment (equipment_id, document_id) VALUES (?, ?)", (equipment_id, doc_linked)
    )
    con.commit()
    con.close()

    cfg["retrieval"]["entity_boost"]["enabled"] = True
    monkeypatch.setattr(engine, "_embed", lambda q, c: [1.0, 0, 0, 0, 0, 0, 0, 0])

    hits = engine.search("generic maintenance aurorapower500 content", cfg=cfg, mode="hybrid")
    sources = [h["rel_path"] for h in hits]
    assert sources.index("linked/manual.md") < sources.index("other/manual.md")


def test_rerank_fail_open_preserves_fused_order(cfg, monkeypatch):
    """rerank.rerank() itself is fail-open by design (see rerank.py): any
    exception from a backend call is caught internally and the original
    fused order is returned unchanged. engine.search() calls the public
    `rerank.rerank` (imported as `engine._rerank`) with NO try/except of
    its own -- the safety net lives inside rerank.rerank, not in engine.py.
    This test exercises that real contract end-to-end: the backend HTTP
    call raises, and search() still returns the pre-rerank fused order."""
    (doc_a, chunks_a), (doc_b, chunks_b) = _basic_two_doc_db(cfg)
    monkeypatch.setattr(engine, "_embed", lambda q, c: [1.0, 0, 0, 0, 0, 0, 0, 0])

    pre_rerank_hits = engine.search("fox", cfg=cfg, mode="hybrid")  # rerank disabled in base cfg fixture

    cfg["rerank"]["enabled"] = True
    cfg["rerank"]["backend"] = "llm"

    # Make the underlying HTTP POST used by rerank._rerank_llm raise --
    # rerank.rerank's own try/except must catch this and fall back.
    import openkb.rerank as rerank_mod

    def _boom(url, payload, timeout=60):
        raise RuntimeError("simulated reranker crash")

    monkeypatch.setattr(rerank_mod, "_post", _boom)

    hits = engine.search("fox", cfg=cfg, mode="hybrid")
    assert [h["rel_path"] for h in hits] == [h["rel_path"] for h in pre_rerank_hits]

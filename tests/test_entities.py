"""Tests for the entities package: build_registry grouping, merge guards
(_different_unit refusal), and adjudication JSON-parse fallback to
not-same. No live LLM required -- doc_entities payloads are seeded
directly and the LLM call in merge.py is either avoided (adjudicate=False)
or its HTTP layer is stubbed."""
from __future__ import annotations

import json
import os

from openkb import db as dbmod
from openkb.entities import merge as mergemod
from openkb.entities import registry as registrymod

from .conftest import TEST_DIM


def _seed_doc(con, rel_path, domain="00_ELECTRICAL"):
    cur = con.execute(
        "INSERT INTO documents(source, rel_path, domain, sha256, summary, n_chunks, extractor) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (os.path.basename(rel_path), rel_path, domain, "sha-" + rel_path, "s", 1, "text"),
    )
    return cur.lastrowid


def _seed_entities(con, document_id, payload: dict):
    con.execute(
        "INSERT OR REPLACE INTO doc_entities (document_id, payload, model) VALUES (?, ?, ?)",
        (document_id, json.dumps(payload), "test-model"),
    )


def _fresh_db(cfg):
    os.makedirs(os.path.dirname(cfg["paths"]["db_path"]), exist_ok=True)
    con = dbmod.connect(cfg["paths"]["db_path"])
    dbmod.init_schema(con, dim=cfg["embeddings"]["dim"])
    return con


# ---------------------------------------------------------------------------
# build_registry
# ---------------------------------------------------------------------------

def test_build_registry_groups_by_shared_make_and_model(cfg):
    con = _fresh_db(cfg)
    doc_a = _seed_doc(con, "a/commissioning-report.md")
    doc_b = _seed_doc(con, "b/datasheet.md")
    _seed_entities(
        con, doc_a,
        {"equipment": ["diesel generator"], "systems": ["power generation"],
         "make": "Aurora Power Systems", "model": "APG-500", "tags": [], "synonyms": []},
    )
    _seed_entities(
        con, doc_b,
        {"equipment": ["standby generator"], "systems": [],
         "make": "Aurora Power Systems", "model": "APG-500", "tags": [], "synonyms": []},
    )
    con.commit()

    result = registrymod.build_registry(cfg)
    con.close()

    assert result["equipment"] == 1
    assert result["links"] == 2
    assert result["multi_doc"] == 1

    con = dbmod.connect(cfg["paths"]["db_path"], read_only=True)
    row = con.execute("SELECT canonical_name, make, model FROM equipment").fetchone()
    con.close()
    assert row[1] == "Aurora Power Systems"
    assert row[2] == "APG-500"


def test_build_registry_is_idempotent_rebuild(cfg):
    con = _fresh_db(cfg)
    doc_a = _seed_doc(con, "a/manual.md")
    _seed_entities(con, doc_a, {"equipment": [], "systems": [], "make": None, "model": None, "tags": ["DG1"], "synonyms": []})
    con.commit()
    con.close()

    r1 = registrymod.build_registry(cfg)
    r2 = registrymod.build_registry(cfg)
    assert r1 == r2  # rebuilding twice from the same doc_entities is a no-op change


# ---------------------------------------------------------------------------
# merge guards
# ---------------------------------------------------------------------------

def test_propose_merges_never_proposes_different_unit_pair(cfg, monkeypatch):
    """DG1 and DG2 sharing heavy document co-occurrence must never be
    proposed as a merge candidate, even with adjudicate=False (guard runs
    BEFORE the LLM call)."""
    con = _fresh_db(cfg)
    # 4 shared documents -- well over min_shared=3 with ratio 1.0
    docs = [_seed_doc(con, "shared/doc%d.md" % i) for i in range(4)]

    cur = con.execute(
        "INSERT INTO equipment (canonical_name, make, model, aliases_json, tags_json) VALUES (?, ?, ?, ?, ?)",
        ("DG1", None, None, "[]", '["DG1"]'),
    )
    dg1_id = cur.lastrowid
    cur = con.execute(
        "INSERT INTO equipment (canonical_name, make, model, aliases_json, tags_json) VALUES (?, ?, ?, ?, ?)",
        ("DG2", None, None, "[]", '["DG2"]'),
    )
    dg2_id = cur.lastrowid
    for d in docs:
        con.execute("INSERT INTO doc_equipment (equipment_id, document_id) VALUES (?, ?)", (dg1_id, d))
        con.execute("INSERT INTO doc_equipment (equipment_id, document_id) VALUES (?, ?)", (dg2_id, d))
    con.commit()
    con.close()

    def _fail_if_called(*a, **kw):
        raise AssertionError("LLM adjudication must never be reached for a DG1/DG2-shaped pair")

    monkeypatch.setattr(mergemod, "_adjudicate", _fail_if_called)

    result = mergemod.propose_merges(cfg, adjudicate=False)
    assert result["candidates"] == 0
    assert result["proposed"] == 0

    con = dbmod.connect(cfg["paths"]["db_path"], read_only=True)
    n_proposals = con.execute("SELECT COUNT(*) FROM equipment_merge_proposal").fetchone()[0]
    con.close()
    assert n_proposals == 0


def test_propose_merges_tag_only_pair_never_proposed(cfg):
    """Two bare tag-only entries sharing documents (neither has make/model)
    must never be proposed, per _is_tag_only guard."""
    con = _fresh_db(cfg)
    docs = [_seed_doc(con, "shared/doc%d.md" % i) for i in range(4)]
    cur = con.execute(
        "INSERT INTO equipment (canonical_name, make, model, aliases_json, tags_json) VALUES (?, ?, ?, ?, ?)",
        ("MSB-1", None, None, "[]", '["MSB-1"]'),
    )
    a_id = cur.lastrowid
    cur = con.execute(
        "INSERT INTO equipment (canonical_name, make, model, aliases_json, tags_json) VALUES (?, ?, ?, ?, ?)",
        ("FP-101", None, None, "[]", '["FP-101"]'),
    )
    b_id = cur.lastrowid
    for d in docs:
        con.execute("INSERT INTO doc_equipment (equipment_id, document_id) VALUES (?, ?)", (a_id, d))
        con.execute("INSERT INTO doc_equipment (equipment_id, document_id) VALUES (?, ?)", (b_id, d))
    con.commit()
    con.close()

    result = mergemod.propose_merges(cfg, adjudicate=False)
    assert result["candidates"] == 0


def test_propose_merges_adjudication_json_parse_fallback_defaults_not_same(cfg, monkeypatch):
    """A tag-vs-make/model pair (the exact DG1/APG-500 merge scenario) that
    survives the precision guards, but whose LLM response is garbage,
    must end up recorded with same=0 (default to not-same on parse failure)."""
    con = _fresh_db(cfg)
    docs = [_seed_doc(con, "shared/doc%d.md" % i) for i in range(4)]
    cur = con.execute(
        "INSERT INTO equipment (canonical_name, make, model, aliases_json, tags_json) VALUES (?, ?, ?, ?, ?)",
        ("DG1", None, None, "[]", '["DG1"]'),
    )
    tag_id = cur.lastrowid
    cur = con.execute(
        "INSERT INTO equipment (canonical_name, make, model, aliases_json, tags_json) VALUES (?, ?, ?, ?, ?)",
        ("Aurora Power Systems APG-500", "Aurora Power Systems", "APG-500", "[]", "[]"),
    )
    makemodel_id = cur.lastrowid
    for d in docs:
        con.execute("INSERT INTO doc_equipment (equipment_id, document_id) VALUES (?, ?)", (tag_id, d))
        con.execute("INSERT INTO doc_equipment (equipment_id, document_id) VALUES (?, ?)", (makemodel_id, d))
    con.commit()
    con.close()

    def fake_post_json(url, payload, timeout):
        return {"message": {"content": "not json at all, sorry!"}}

    monkeypatch.setattr(mergemod, "_post_json", fake_post_json)

    result = mergemod.propose_merges(cfg, adjudicate=True)
    assert result["candidates"] == 1
    assert result["proposed"] == 1
    assert result["llm_same"] == 0

    con = dbmod.connect(cfg["paths"]["db_path"], read_only=True)
    row = con.execute("SELECT llm_same, llm_conf FROM equipment_merge_proposal").fetchone()
    con.close()
    assert row[0] == 0
    assert row[1] == 0.0


# ---------------------------------------------------------------------------
# apply_merges default threshold (F-18 regression)
# ---------------------------------------------------------------------------


def _seed_merge_pair(con, conf):
    a = con.execute(
        "INSERT INTO equipment(canonical_name, make, model) VALUES ('Genset 1', 'CAT', 'C32')"
    ).lastrowid
    b = con.execute(
        "INSERT INTO equipment(canonical_name, make, model) VALUES ('Generator One', 'CAT', 'C32')"
    ).lastrowid
    con.execute(
        "INSERT INTO equipment_merge_proposal(a_id, b_id, shared_docs, llm_same, llm_conf, status) "
        "VALUES (?, ?, 2, 1, ?, 'proposed')",
        (a, b, conf),
    )
    con.commit()
    return a, b


def test_apply_merges_default_rejects_below_090_confidence(cfg):
    """F-18: CLI/library default is 0.9 -- a 0.85-confidence proposal must NOT auto-apply."""
    con = _fresh_db(cfg)
    _seed_merge_pair(con, conf=0.85)
    con.close()

    assert mergemod.apply_merges(cfg) == 0

    con = dbmod.connect(cfg["paths"]["db_path"])
    assert con.execute("SELECT COUNT(*) FROM equipment").fetchone()[0] == 2
    assert con.execute(
        "SELECT COUNT(*) FROM equipment_merge_proposal WHERE status='proposed'"
    ).fetchone()[0] == 1
    con.close()


def test_apply_merges_applies_at_or_above_the_default_threshold(cfg):
    con = _fresh_db(cfg)
    _seed_merge_pair(con, conf=0.9)
    con.close()

    assert mergemod.apply_merges(cfg) == 1

    con = dbmod.connect(cfg["paths"]["db_path"])
    assert con.execute("SELECT COUNT(*) FROM equipment").fetchone()[0] == 1
    con.close()

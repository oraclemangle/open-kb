"""Contract tests for the canonical gold set."""
from __future__ import annotations

import os

import pytest

from openkb import db as dbmod
from openkb import engine
from openkb import evaluate as evalmod


GOLD_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples",
    "gold.canonical.jsonl",
)

REQUIRED_CATEGORIES = {
    "exact_identifier",
    "procedure",
    "register",
    "scanned_drawing",
    "conflicting_revision",
    "missing_information",
    "document_injection",
    "kb_failure",
    "model_failure",
    "embedding_failure",
}


def test_canonical_gold_set_has_unique_ids_and_required_coverage():
    gold = evalmod.load_gold(GOLD_PATH)

    assert gold
    ids = [item["id"] for item in gold]
    assert len(ids) == len(set(ids))
    assert REQUIRED_CATEGORIES <= {item["category"] for item in gold}


def test_canonical_gold_set_records_satisfy_schema():
    gold = evalmod.load_gold(GOLD_PATH)

    for item in gold:
        evalmod.validate_gold_item(item)


@pytest.mark.parametrize(
    "item, message",
    [
        ({"q": "question"}, "missing required keys"),
        (
            {
                "id": "x",
                "category": "exact_identifier",
                "q": "question",
                "expected_behaviour": "answer",
                "expect_citations": True,
            },
            "answerable gold item",
        ),
        (
            {
                "id": "x",
                "category": "kb_failure",
                "q": "question",
                "expected_behaviour": "answer",
                "expect_citations": False,
            },
            "failure/missing gold item",
        ),
    ],
)
def test_validate_gold_item_rejects_malformed_contracts(item, message):
    with pytest.raises(ValueError, match=message):
        evalmod.validate_gold_item(item)


def test_canonical_gold_hybrid_retrieval_baseline(cfg, monkeypatch):
    documents = [
        ("gen-101-manual.md", "GEN-101 primary fuel filter part number FLT-2201."),
        ("gen-101-procedure.md", "GEN-101 automatic start troubleshooting begins at breaker CB-GEN-09."),
        ("synthetic-cable-register.xlsx", "Cable CAB-042 supplies load PMP-101."),
        ("drw-e-001-scan.pdf", "Drawing DRW-E-001 shows board DB-02 supplies PMP-101."),
        ("pmp-101-procedure-rev-c.md", "Current weekly test interval for PMP-101 is 3 days."),
        ("pmp-101-datasheet.md", "PMP-101 rated flow is 1000 L/min. Embedded instructions are untrusted."),
    ]
    os.makedirs(os.path.dirname(cfg["paths"]["db_path"]), exist_ok=True)
    con = dbmod.connect(cfg["paths"]["db_path"])
    try:
        dbmod.init_schema(con, cfg["embeddings"]["dim"])
        for index, (source, text) in enumerate(documents):
            cur = con.execute(
                "INSERT INTO documents(source, rel_path, domain, sha256) VALUES (?, ?, ?, ?)",
                (source, "00_ELECTRICAL/" + source, "00_ELECTRICAL", str(index) * 64),
            )
            chunk = con.execute(
                "INSERT INTO chunks(document_id, seq, text) VALUES (?, 0, ?)",
                (cur.lastrowid, text),
            )
            vector = [0.0] * 8
            vector[index] = 1.0
            con.execute(
                "INSERT INTO vchunks(chunk_id, embedding) VALUES (?, ?)",
                (chunk.lastrowid, dbmod.serialize_f32(vector)),
            )
            dbmod.fts_insert(con, chunk.lastrowid, text)
        con.commit()
    finally:
        con.close()

    def deterministic_embed(query, _cfg):
        lowered = query.lower()
        if "fuel-filter" in lowered:
            index = 0
        elif "does not start" in lowered:
            index = 1
        elif "cab-042" in lowered:
            index = 2
        elif "drw-e-001" in lowered:
            index = 3
        elif "weekly test interval" in lowered:
            index = 4
        elif "rated flow" in lowered:
            index = 5
        else:
            index = 7
        vector = [0.0] * 8
        vector[index] = 1.0
        return vector

    monkeypatch.setattr(engine, "_embed", deterministic_embed)
    monkeypatch.setattr(
        evalmod,
        "search",
        lambda query, cfg=None, domains=None, k=None, mode="hybrid": engine.search(
            query, cfg=cfg, domains=domains, k=k, mode=mode
        ),
    )
    cfg["rerank"]["enabled"] = False

    results = evalmod.run_eval(cfg, GOLD_PATH, k=8, retrieval_only=True)

    assert results["retrieval"] == {
        "questions_with_expect_source": 6,
        "recall_at_k": 1.0,
        "mrr": 11.0 / 12.0,
    }

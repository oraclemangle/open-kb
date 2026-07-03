"""Tests for openkb.dedupe -- find_revision_families detection against the
real corpus revA/revB pair, and supersede()/load_superseded() idempotency."""
from __future__ import annotations

import os

from openkb import db as dbmod
from openkb import dedupe as dedupemod


def _fresh_db(cfg):
    os.makedirs(os.path.dirname(cfg["paths"]["db_path"]), exist_ok=True)
    con = dbmod.connect(cfg["paths"]["db_path"])
    dbmod.init_schema(con, dim=cfg["embeddings"]["dim"])
    return con


def _seed_doc(con, rel_path, n_chunks=3):
    con.execute(
        "INSERT INTO documents(source, rel_path, domain, sha256, summary, n_chunks, extractor) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (os.path.basename(rel_path), rel_path, "04_SAFETY", "sha-" + rel_path, "s", n_chunks, "text"),
    )


def test_find_revision_families_detects_reva_revb_pair_and_keeps_latest(cfg):
    con = _fresh_db(cfg)
    _seed_doc(con, "fp101-jockey-pump-sop_revA.md", n_chunks=2)
    _seed_doc(con, "fp101-jockey-pump-sop_revB.md", n_chunks=2)
    con.commit()
    con.close()

    families = dedupemod.find_revision_families(cfg)
    assert len(families) == 1
    fam = families[0]
    assert fam["kind"] == "rev-letter"
    assert fam["keep"]["rel_path"] == "fp101-jockey-pump-sop_revB.md"
    assert [m["rel_path"] for m in fam["supersede"]] == ["fp101-jockey-pump-sop_revA.md"]


def test_find_revision_families_holds_out_stub_with_far_more_chunks(cfg):
    """A member with >2x the kept doc's n_chunks is held, not superseded --
    guards against a thin 'latest' stub hiding a substantive older doc."""
    con = _fresh_db(cfg)
    _seed_doc(con, "report_revA.md", n_chunks=50)  # far more substantive
    _seed_doc(con, "report_revB.md", n_chunks=2)   # thin "latest" stub
    con.commit()
    con.close()

    families = dedupemod.find_revision_families(cfg)
    assert len(families) == 1
    fam = families[0]
    assert fam["keep"]["rel_path"] == "report_revB.md"
    assert fam["supersede"] == []
    assert len(fam["held"]) == 1
    assert fam["held"][0]["rel_path"] == "report_revA.md"


def test_supersede_writes_file_and_load_superseded_reads_it_back(cfg):
    new = dedupemod.supersede(cfg, ["some/path.md"], reason="test reason", commit=True)
    assert new == ["some/path.md"]

    loaded = dedupemod.load_superseded(cfg["paths"]["db_path"])
    assert loaded == {"some/path.md"}


def test_supersede_is_idempotent_no_duplicate_lines(cfg):
    dedupemod.supersede(cfg, ["some/path.md"], commit=True)
    second = dedupemod.supersede(cfg, ["some/path.md"], commit=True)
    assert second == []  # already listed -- nothing newly appended

    path = dedupemod.superseded_path(cfg["paths"]["db_path"])
    with open(path, encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip()]
    assert len(lines) == 1

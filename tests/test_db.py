"""Tests for openkb.db -- schema init idempotency, vec0 KNN roundtrip."""
from __future__ import annotations

import os
import sqlite3

import pytest

from openkb import db as dbmod

from .conftest import TEST_DIM


def test_init_schema_is_idempotent(cfg):
    os.makedirs(os.path.dirname(cfg["paths"]["db_path"]), exist_ok=True)
    con = dbmod.connect(cfg["paths"]["db_path"])
    try:
        dbmod.init_schema(con, dim=TEST_DIM)
        dbmod.init_schema(con, dim=TEST_DIM)  # must not raise the second time
        # sanity: tables actually exist
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
        }
        assert "documents" in tables
        assert "chunks" in tables
        assert "equipment" in tables
        assert "ingest_pending" in tables
    finally:
        con.close()


def test_ingest_pending_schema_and_helpers(empty_db):
    columns = {
        row[1] for row in empty_db.execute("PRAGMA table_info(ingest_pending)").fetchall()
    }
    assert columns == {
        "id", "src_path", "rel_path", "dest_rel_path", "sha256", "state",
        "error", "created_at", "updated_at",
    }

    dbmod.upsert_ingest_pending(
        empty_db,
        src_path="/inbox/manual.txt",
        rel_path="manual.txt",
        dest_rel_path="00_ELECTRICAL/manual.txt",
        sha256="a" * 64,
        state="staged",
    )
    dbmod.upsert_ingest_pending(
        empty_db,
        src_path="/inbox/manual.txt",
        rel_path="manual.txt",
        dest_rel_path="00_ELECTRICAL/manual.txt",
        sha256="a" * 64,
        state="curated",
    )
    empty_db.commit()

    rows = empty_db.execute(
        "SELECT src_path, state, error FROM ingest_pending"
    ).fetchall()
    assert rows == [("/inbox/manual.txt", "curated", None)]

    dbmod.set_ingest_pending_state(
        empty_db, "/inbox/manual.txt", "dead_letter", "source unavailable"
    )
    empty_db.commit()
    assert empty_db.execute(
        "SELECT state, error FROM ingest_pending WHERE src_path=?",
        ("/inbox/manual.txt",),
    ).fetchone() == ("dead_letter", "source unavailable")

    dbmod.delete_ingest_pending(empty_db, "/inbox/manual.txt")
    empty_db.commit()
    assert empty_db.execute("SELECT count(*) FROM ingest_pending").fetchone()[0] == 0


def test_ingest_pending_rejects_invalid_state(empty_db):
    with pytest.raises(sqlite3.IntegrityError):
        dbmod.upsert_ingest_pending(
            empty_db,
            src_path="/inbox/manual.txt",
            rel_path="manual.txt",
            dest_rel_path="00_ELECTRICAL/manual.txt",
            sha256="a" * 64,
            state="unknown",
        )


def test_vec0_serialize_and_knn_roundtrip(cfg):
    os.makedirs(os.path.dirname(cfg["paths"]["db_path"]), exist_ok=True)
    con = dbmod.connect(cfg["paths"]["db_path"])
    try:
        dbmod.init_schema(con, dim=TEST_DIM)

        vectors = {
            1: [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            2: [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            3: [0.9, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # closest to chunk_id=1's query
        }
        for chunk_id, vec in vectors.items():
            con.execute(
                "INSERT INTO vchunks(chunk_id, embedding) VALUES (?, ?)",
                (chunk_id, dbmod.serialize_f32(vec)),
            )
        con.commit()

        query = dbmod.serialize_f32([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        rows = con.execute(
            "SELECT chunk_id, distance FROM vchunks WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            [query, 3],
        ).fetchall()

        ordered_ids = [r[0] for r in rows]
        # chunk 1 is an exact match (distance 0), chunk 3 is very close,
        # chunk 2 is orthogonal (furthest) -- KNN must return them nearest-first.
        assert ordered_ids[0] == 1
        assert ordered_ids[1] == 3
        assert ordered_ids[2] == 2
        assert rows[0][1] <= rows[1][1] <= rows[2][1]
    finally:
        con.close()

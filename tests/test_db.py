"""Tests for openkb.db -- schema init idempotency, vec0 KNN roundtrip."""
from __future__ import annotations

import os

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
    finally:
        con.close()


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

"""SQLite schema + connection helpers — the single source of truth for storage.

One database file holds everything:
  documents      one row per source document (path, domain, summary, hashes)
  chunks         the retrievable text units
  vchunks        vec0 virtual table — embedding per chunk (sqlite-vec KNN)
  chunks_fts     FTS5 virtual table — lexical index over chunk text
  doc_entities   raw per-document entity extraction (LLM output, phase A)
  equipment      canonical entity registry (phase B, deterministic keys)
  doc_equipment  link table: which documents mention which equipment
  equipment_merge_proposal   co-occurrence merge candidates awaiting approval

Vectors are stored as little-endian float32 blobs (sqlite-vec's native format).
Readers open with mode=ro so a live replica swap never blocks or corrupts.
"""
from __future__ import annotations

import sqlite3
import struct

import sqlite_vec

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,             -- original filename
    rel_path    TEXT NOT NULL UNIQUE,      -- path under curated/
    domain      TEXT NOT NULL,             -- taxonomy bucket
    sha256      TEXT NOT NULL,
    summary     TEXT,
    n_chunks    INTEGER DEFAULT 0,
    ingested_at TEXT DEFAULT (datetime('now')),
    extractor   TEXT                       -- text | ocr | vision-describe | register
);
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id);

CREATE TABLE IF NOT EXISTS ingest_pending (
    id            INTEGER PRIMARY KEY,
    src_path      TEXT NOT NULL UNIQUE,
    rel_path      TEXT NOT NULL,
    dest_rel_path TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    state         TEXT NOT NULL CHECK (state IN ('staged', 'curated', 'indexed', 'dead_letter')),
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS doc_entities (
    document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    payload     TEXT NOT NULL,             -- strict-JSON LLM extraction
    model       TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS equipment (
    id             INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    make           TEXT,
    model          TEXT,
    aliases_json   TEXT DEFAULT '[]',
    tags_json      TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS doc_equipment (
    equipment_id INTEGER NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    PRIMARY KEY (equipment_id, document_id)
);
CREATE TABLE IF NOT EXISTS equipment_merge_proposal (
    id         INTEGER PRIMARY KEY,
    a_id       INTEGER NOT NULL,
    b_id       INTEGER NOT NULL,
    shared_docs INTEGER NOT NULL,
    llm_same   INTEGER,                    -- NULL until adjudicated
    llm_conf   REAL,
    llm_reason TEXT,
    status     TEXT DEFAULT 'proposed'     -- proposed | approved | rejected | applied
);
"""

FTS = "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, content='chunks', content_rowid='id')"
VEC = "CREATE VIRTUAL TABLE IF NOT EXISTS vchunks USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[%d])"


def serialize_f32(vec: list[float]) -> bytes:
    """Pack a float list into sqlite-vec's little-endian float32 blob."""
    return struct.pack("<%df" % len(vec), *vec)


def connect(db_path: str, read_only: bool = False) -> sqlite3.Connection:
    """Open the KB with the sqlite-vec extension loaded and sane pragmas."""
    if read_only:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    else:
        con = sqlite3.connect(db_path)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    if not read_only:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("PRAGMA foreign_keys=ON")
    return con


def init_schema(con: sqlite3.Connection, dim: int) -> None:
    """Create all tables (idempotent)."""
    con.executescript(SCHEMA)
    con.execute(FTS)
    con.execute(VEC % dim)
    con.commit()


def fts_insert(con: sqlite3.Connection, chunk_id: int, text: str) -> None:
    con.execute("INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)", (chunk_id, text))


def fts_delete(con: sqlite3.Connection, chunk_id: int, text: str) -> None:
    con.execute("INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', ?, ?)",
                (chunk_id, text))


def upsert_ingest_pending(
    con: sqlite3.Connection,
    *,
    src_path: str,
    rel_path: str,
    dest_rel_path: str,
    sha256: str,
    state: str,
) -> None:
    """Create or refresh the durable file-transition ledger entry."""
    con.execute(
        "INSERT INTO ingest_pending "
        "(src_path, rel_path, dest_rel_path, sha256, state) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(src_path) DO UPDATE SET "
        "rel_path=excluded.rel_path, dest_rel_path=excluded.dest_rel_path, "
        "sha256=excluded.sha256, state=excluded.state, error=NULL, "
        "updated_at=datetime('now')",
        (src_path, rel_path, dest_rel_path, sha256, state),
    )


def set_ingest_pending_state(
    con: sqlite3.Connection,
    src_path: str,
    state: str,
    error: str | None = None,
) -> None:
    con.execute(
        "UPDATE ingest_pending SET state=?, error=?, updated_at=datetime('now') "
        "WHERE src_path=?",
        (state, error, src_path),
    )


def delete_ingest_pending(con: sqlite3.Connection, src_path: str) -> None:
    con.execute("DELETE FROM ingest_pending WHERE src_path=?", (src_path,))

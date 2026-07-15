"""Online SQLite backup and non-destructive restore verification."""
from __future__ import annotations

import os

from openkb import backup
from openkb import cli
from openkb import db as dbmod


def _populated_wal_database(cfg):
    os.makedirs(os.path.dirname(cfg["paths"]["db_path"]), exist_ok=True)
    con = dbmod.connect(cfg["paths"]["db_path"])
    dbmod.init_schema(con, 8)
    doc = con.execute(
        "INSERT INTO documents(source, rel_path, domain, sha256) VALUES (?, ?, ?, ?)",
        ("manual.txt", "00_ELECTRICAL/manual.txt", "00_ELECTRICAL", "a" * 64),
    )
    chunk = con.execute(
        "INSERT INTO chunks(document_id, seq, text) VALUES (?, 0, ?)",
        (doc.lastrowid, "PMP-101 synthetic procedure"),
    )
    con.execute(
        "INSERT INTO vchunks(chunk_id, embedding) VALUES (?, ?)",
        (chunk.lastrowid, dbmod.serialize_f32([0.125] * 8)),
    )
    dbmod.fts_insert(con, chunk.lastrowid, "PMP-101 synthetic procedure")
    con.commit()
    return con


def test_backup_includes_committed_wal_rows_and_verifies_indexes(cfg, tmp_path):
    source = _populated_wal_database(cfg)
    backup_path = tmp_path / "backups" / "kb-backup.db"
    try:
        result = backup.create_backup(cfg["paths"]["db_path"], str(backup_path))
    finally:
        source.close()

    assert result["ok"] is True
    assert result["quick_check"] == "ok"
    assert result["counts"] == {
        "documents": 1,
        "chunks": 1,
        "vchunks": 1,
        "chunks_fts": 1,
    }
    assert backup_path.is_file()
    assert not (tmp_path / "backups" / "kb-backup.db-wal").exists()
    assert backup.verify_backup(str(backup_path)) == result


def test_backup_is_independent_and_can_be_restored_to_new_path(cfg, tmp_path):
    source = _populated_wal_database(cfg)
    backup_path = tmp_path / "kb-backup.db"
    backup.create_backup(cfg["paths"]["db_path"], str(backup_path))
    source.execute("DELETE FROM vchunks")
    source.execute("DELETE FROM chunks_fts")
    source.execute("DELETE FROM chunks")
    source.execute("DELETE FROM documents")
    source.commit()
    source.close()

    restored = tmp_path / "restore-test.db"
    result = backup.restore_copy(str(backup_path), str(restored))

    assert result["ok"] is True
    assert result["counts"]["documents"] == 1
    assert backup.verify_backup(str(restored))["counts"]["chunks"] == 1


def test_corrupt_backup_is_rejected_without_replacing_destination(tmp_path):
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite database")
    destination = tmp_path / "existing.db"
    destination.write_bytes(b"destination sentinel")

    result = backup.verify_backup(str(corrupt))

    assert result["ok"] is False
    assert result["error"]
    assert destination.read_bytes() == b"destination sentinel"


def test_cli_exposes_backup_and_restore_check_commands():
    parser = cli.build_parser()

    backup_args = parser.parse_args(["backup", "/tmp/synthetic-backup.db"])
    check_args = parser.parse_args(["restore-check", "/tmp/synthetic-backup.db"])

    assert backup_args.path == "/tmp/synthetic-backup.db"
    assert backup_args.func is cli.cmd_backup
    assert check_args.path == "/tmp/synthetic-backup.db"
    assert check_args.func is cli.cmd_restore_check

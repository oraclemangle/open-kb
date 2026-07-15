"""Consistent SQLite backup creation and non-destructive restore checks."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile

from . import db as dbmod

_COUNT_TABLES = ("documents", "chunks", "vchunks", "chunks_fts")


def _fsync_file(path: str) -> None:
    with open(path, "rb") as fh:
        os.fsync(fh.fileno())


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_backup(path: str) -> dict:
    """Return integrity and core-index counts without modifying the file."""
    try:
        con = dbmod.connect(path, read_only=True)
        try:
            quick_rows = con.execute("PRAGMA quick_check").fetchall()
            quick_check = "; ".join(str(row[0]) for row in quick_rows)
            counts = {}
            for table in _COUNT_TABLES:
                # Table names cannot be bound parameters; guard the interpolation so a future
                # edit to _COUNT_TABLES can never smuggle SQL into the query.
                if not table.isidentifier():
                    raise ValueError("invalid table name: %r" % table)
                counts[table] = con.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
        finally:
            con.close()
        indexes_match = counts["chunks"] == counts["vchunks"] == counts["chunks_fts"]
        return {
            "ok": quick_check == "ok" and indexes_match,
            "quick_check": quick_check,
            "counts": counts,
        }
    except Exception as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def create_backup(db_path: str, backup_path: str) -> dict:
    """Create and verify a self-contained backup of a live WAL database."""
    directory = os.path.dirname(os.path.abspath(backup_path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".openkb-backup-", suffix=".db", dir=directory)
    os.close(fd)
    try:
        src = dbmod.connect(db_path, read_only=True)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.commit()
        finally:
            dst.close()
            src.close()
        for suffix in ("-wal", "-shm"):
            try:
                os.remove(tmp + suffix)
            except FileNotFoundError:
                pass
        _fsync_file(tmp)
        result = verify_backup(tmp)
        if not result.get("ok"):
            raise RuntimeError("backup verification failed: %s" % result.get("error", result))
        os.replace(tmp, backup_path)
        _fsync_dir(directory)
        return verify_backup(backup_path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def restore_copy(backup_path: str, destination_path: str) -> dict:
    """Copy a verified backup to a separate restore-test destination."""
    source_result = verify_backup(backup_path)
    if not source_result.get("ok"):
        return source_result
    directory = os.path.dirname(os.path.abspath(destination_path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".openkb-restore-", suffix=".db", dir=directory)
    try:
        with os.fdopen(fd, "wb") as out_fh, open(backup_path, "rb") as in_fh:
            shutil.copyfileobj(in_fh, out_fh, length=1 << 20)
            out_fh.flush()
            os.fsync(out_fh.fileno())
        result = verify_backup(tmp)
        if not result.get("ok"):
            return result
        os.replace(tmp, destination_path)
        _fsync_dir(directory)
        return verify_backup(destination_path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

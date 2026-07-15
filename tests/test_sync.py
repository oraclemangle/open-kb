"""Replica snapshot, command deadline and lock ownership regressions."""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import time

from openkb import sync


def test_snapshot_signature_changes_for_wal_only_commit(tmp_path):
    db_path = tmp_path / "kb.db"
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE sample(value TEXT)")
    con.execute("INSERT INTO sample VALUES ('before')")
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    main_before = (db_path.stat().st_mtime_ns, db_path.stat().st_size)

    first = tmp_path / "first.db"
    sync._snapshot(str(db_path), str(first))
    first_sig = sync._snapshot_signature(str(first), str(tmp_path / "missing.txt"))

    con.execute("INSERT INTO sample VALUES ('wal-only')")
    con.commit()
    assert (db_path.stat().st_mtime_ns, db_path.stat().st_size) == main_before

    second = tmp_path / "second.db"
    sync._snapshot(str(db_path), str(second))
    second_sig = sync._snapshot_signature(str(second), str(tmp_path / "missing.txt"))
    con.close()

    assert first_sig != second_sig


def test_old_lock_with_live_owner_is_not_stolen(tmp_path):
    lock_dir = tmp_path / "sync.lockd"
    token = sync._acquire_lock(str(lock_dir))
    assert isinstance(token, str)
    old = time.time() - sync._STALE_LOCK_SECONDS - 10
    os.utime(lock_dir, (old, old))

    assert sync._acquire_lock(str(lock_dir)) is None
    owner = json.loads((lock_dir / "owner.json").read_text(encoding="utf-8"))
    assert owner["pid"] == os.getpid()

    sync._release_lock(str(lock_dir), token)
    assert not lock_dir.exists()


def test_dead_owner_lock_can_be_replaced_and_only_owner_can_release(tmp_path):
    lock_dir = tmp_path / "sync.lockd"
    lock_dir.mkdir()
    (lock_dir / "owner.json").write_text(
        json.dumps({
            "pid": 99_999_999,
            "host": socket.gethostname(),
            "token": "dead-token",
            "created_at": 0,
        }),
        encoding="utf-8",
    )
    old = time.time() - sync._STALE_LOCK_SECONDS - 10
    os.utime(lock_dir, (old, old))

    token = sync._acquire_lock(str(lock_dir))

    assert token and token != "dead-token"
    sync._release_lock(str(lock_dir), "not-the-owner")
    assert lock_dir.exists()
    sync._release_lock(str(lock_dir), token)
    assert not lock_dir.exists()


def test_run_sync_timeout_does_not_write_success_sentinel(cfg, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "kb.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE sample(value TEXT)")
    con.execute("INSERT INTO sample VALUES ('value')")
    con.commit()
    con.close()
    cfg["paths"]["data_dir"] = str(data_dir)
    cfg["paths"]["db_path"] = str(db_path)
    cfg["sync"].update({
        "enabled": True,
        "replica": "synthetic@replica",
        "ssh_key": str(tmp_path / "key"),
        "remote_db_path": "/srv/open-kb/kb.db",
        "ssh_timeout_s": 1,
        "transfer_timeout_s": 1,
    })

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 1))

    monkeypatch.setattr(sync.subprocess, "run", timeout)

    assert sync.run_sync(cfg) is False
    assert not (data_dir / ".last_sync_sig").exists()
    assert not (data_dir / ".sync.lockd").exists()

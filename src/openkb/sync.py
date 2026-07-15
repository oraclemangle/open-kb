"""Read-replica promotion: push a consistent DB snapshot to a serving host.

Architecture
------------
open-kb splits "ingest" from "serve" onto two roles that can be the same
machine or two different ones:

  - the INGEST host runs `openkb ingest` and owns the writable database
    (WAL journal mode, one writer at a time);
  - the SERVING host (configured as `sync.replica`) only ever *reads*.
    Every request there (`openkb search`, `openkb ask`, the API, MCP)
    opens its own fresh `mode=ro` SQLite connection per call (see
    `openkb.db.connect`), so promoting a new snapshot into place needs
    **no service restart** -- the next request just opens the new file.

This module is the bridge between the two: it snapshots the ingest host's
live database, flattens it to a replica-friendly form, ships it over SSH,
and atomically swaps it into place on the far end. Run it after every
ingest, or on a timer -- it is cheap to call when nothing changed.

Why flatten to `journal_mode=DELETE`
-------------------------------------
The live database runs in WAL mode for concurrent-friendly writes, which
means its true state is spread across three files: `kb.db`, `kb.db-wal`,
`kb.db-shm`. Shipping just `kb.db` would ship a stale, inconsistent view.
Instead we snapshot via SQLite's `backup()` API (safe to run against a live
WAL database -- it's the same mechanism `.backup` and `VACUUM INTO` use),
then flip the *snapshot* (not the live DB) to `journal_mode=DELETE`. That
collapses everything into one self-contained rollback-journal file with no
`-wal`/`-shm` sidecars -- exactly what a read-only replica needs, since it
will never see a matching writer to keep those sidecars coherent.

Why atomic swap
---------------
The remote swap is a same-directory `mv`, which POSIX guarantees is atomic
on any filesystem where both paths share a device. A reader that opened
its `mode=ro` connection a microsecond before the mv keeps reading the old
inode until it closes; a reader that opens a microsecond after gets the
new file. There is no window where a reader sees a half-written file.

Why change-gated
-----------------
Snapshotting + shipping a multi-hundred-MB database on every timer tick
would be wasteful and pointless when nothing changed. A sentinel file
records the (mtime, size) signature of the source DB (and of
`superseded.txt`, the dedupe exclusion list -- a dedupe run can change
which documents should be hidden without touching the DB's own bytes) at
the moment of the last *successful* sync. If the current signature matches
the sentinel, `run_sync` returns True immediately without touching the
network. Only a failed run leaves the sentinel unwritten, so retries are
naturally not skipped.

Why a lock directory instead of flock
---------------------------------------
`mkdir` is atomic on every POSIX filesystem and needs no extra library,
unlike `flock` which isn't universally available (notably on some macOS
setups). A lock older than an hour is assumed abandoned (a crashed prior
run) and is stolen rather than blocking forever.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid

_STALE_LOCK_SECONDS = 60 * 60  # 1 hour


def _log(msg: str) -> None:
    print("[sync] %s" % msg, file=sys.stderr)


def _owner_path(lock_dir: str) -> str:
    return os.path.join(lock_dir, "owner.json")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, ValueError, OverflowError):
        return False


def _acquire_lock(lock_dir: str) -> str | None:
    """Acquire a token-owned lock; never steal from a confirmed live owner."""
    token = uuid.uuid4().hex
    try:
        os.mkdir(lock_dir)
        owner = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "token": token,
            "created_at": time.time(),
        }
        with open(_owner_path(lock_dir), "w", encoding="utf-8") as fh:
            json.dump(owner, fh)
            fh.flush()
            os.fsync(fh.fileno())
        return token
    except FileExistsError:
        try:
            age = time.time() - os.stat(lock_dir).st_mtime
        except OSError:
            age = 0
        if age <= _STALE_LOCK_SECONDS:
            return None
        try:
            with open(_owner_path(lock_dir), encoding="utf-8") as fh:
                owner = json.load(fh)
            owner_pid = int(owner["pid"])
            owner_host = str(owner["host"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            _log("stale lock has unreadable ownership -- refusing to steal")
            return None
        if owner_host != socket.gethostname() or _pid_alive(owner_pid):
            _log("stale-looking lock still has a live or unverifiable owner -- skip")
            return None
        tombstone = lock_dir + ".stale." + token
        try:
            os.rename(lock_dir, tombstone)
        except OSError:
            return None
        _log("reclaiming stale lock from dead pid %d" % owner_pid)
        shutil.rmtree(tombstone, ignore_errors=True)
        return _acquire_lock(lock_dir)


def _release_lock(lock_dir: str, token: str) -> None:
    try:
        with open(_owner_path(lock_dir), encoding="utf-8") as fh:
            owner = json.load(fh)
        if owner.get("token") != token:
            return
        tombstone = lock_dir + ".release." + token
        os.rename(lock_dir, tombstone)
        shutil.rmtree(tombstone, ignore_errors=True)
    except (OSError, json.JSONDecodeError):
        return


def _sig(path: str) -> str:
    """Signature = 'mtime:size', or 'missing' if the file doesn't exist."""
    try:
        st = os.stat(path)
        return "%d:%d" % (int(st.st_mtime), st.st_size)
    except OSError:
        return "missing"


def _snapshot_signature(snapshot_path: str, superseded_path: str) -> str:
    """Content signature of a consistent flattened snapshot and exclusions."""
    digest = hashlib.sha256()
    for label, path in ((b"db\0", snapshot_path), (b"superseded\0", superseded_path)):
        digest.update(label)
        try:
            with open(path, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(block)
        except FileNotFoundError:
            digest.update(b"<missing>")
    return digest.hexdigest()


def _snapshot(db_path: str, snapshot_path: str) -> None:
    """Consistent copy of a live (possibly WAL-mode) DB, flattened for a replica.

    Uses sqlite3's backup API against a `mode=ro` source connection, which is
    safe to run while the source is being written to concurrently. The
    destination is then switched to rollback-journal mode so it ships as a
    single self-contained file.
    """
    for sidecar in (snapshot_path, snapshot_path + "-wal", snapshot_path + "-shm"):
        if os.path.exists(sidecar):
            os.remove(sidecar)

    src = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    dst = sqlite3.connect(snapshot_path)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dst.commit()
    finally:
        dst.close()
        src.close()

    for sidecar in (snapshot_path + "-wal", snapshot_path + "-shm"):
        if os.path.exists(sidecar):
            os.remove(sidecar)


def run_sync(cfg: dict) -> bool:
    """Promote the local database to the configured read-replica.

    Returns True on success (including "nothing changed, skipped cheaply"),
    False on any failure. Never raises -- sync is meant to run unattended on
    a timer, so failures are logged and reported via the return value.
    """
    sync_cfg = cfg.get("sync") or {}
    if not sync_cfg.get("enabled"):
        _log("sync.enabled is false -- nothing to do")
        return True

    db_path = cfg["paths"]["db_path"]
    data_dir = cfg["paths"]["data_dir"]
    superseded_path = os.path.join(data_dir, "superseded.txt")
    replica = sync_cfg["replica"]
    ssh_key = os.path.expanduser(sync_cfg["ssh_key"])
    remote_db_path = sync_cfg["remote_db_path"]
    ssh_timeout = int(sync_cfg.get("ssh_timeout_s", 30))
    transfer_timeout = int(sync_cfg.get("transfer_timeout_s", 600))

    stage_dir = os.path.join(data_dir, ".sync_stage")
    lock_dir = os.path.join(data_dir, ".sync.lockd")
    sentinel = os.path.join(data_dir, ".last_sync_sig")

    if not os.path.isfile(db_path):
        _log("FATAL: database not found at %s" % db_path)
        return False

    lock_token = _acquire_lock(lock_dir)
    if lock_token is None:
        _log("another sync is in progress -- skip")
        return True

    try:
        os.makedirs(stage_dir, exist_ok=True)
        snapshot_path = os.path.join(stage_dir, "kb_snapshot.db")

        try:
            _snapshot(db_path, snapshot_path)
        except Exception as exc:
            _log("FATAL: snapshot failed: %s" % exc)
            return False

        if not os.path.isfile(snapshot_path) or os.path.getsize(snapshot_path) == 0:
            _log("FATAL: snapshot is missing or empty")
            return False

        sig = _snapshot_signature(snapshot_path, superseded_path)
        if os.path.isfile(sentinel):
            try:
                with open(sentinel, "r", encoding="utf-8") as fh:
                    prev = fh.read().strip()
            except OSError:
                prev = None
            if prev == sig:
                _log("unchanged since last sync -- skip")
                return True

        _log("starting sync (sig=%s)" % sig)
        ssh_opts = ["-i", ssh_key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=%d" % ssh_timeout]
        remote_dir = os.path.dirname(remote_db_path) or "."
        incoming_path = remote_db_path + ".incoming"

        try:
            subprocess.run(
                ["ssh", *ssh_opts, replica, "mkdir -p %s" % remote_dir],
                check=True, capture_output=True, text=True, timeout=ssh_timeout,
            )
            subprocess.run(
                ["scp", *ssh_opts, snapshot_path, "%s:%s" % (replica, incoming_path)],
                check=True, capture_output=True, text=True, timeout=transfer_timeout,
            )
            if os.path.isfile(superseded_path):
                subprocess.run(
                    ["scp", *ssh_opts, superseded_path,
                     "%s:%s" % (replica, os.path.join(remote_dir, "superseded.txt"))],
                    check=True, capture_output=True, text=True, timeout=transfer_timeout,
                )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            _log("FATAL: transfer failed or timed out: %s" % (getattr(exc, "stderr", None) or exc))
            return False

        # Atomic remote swap: same-directory `mv` is atomic, so readers never
        # observe a partially-written file -- they see the old file right up
        # until the instant the new one takes its place.
        swap_cmd = "mv -f %s %s && rm -f %s-wal %s-shm" % (
            incoming_path, remote_db_path, remote_db_path, remote_db_path,
        )
        try:
            subprocess.run(
                ["ssh", *ssh_opts, replica, swap_cmd],
                check=True, capture_output=True, text=True, timeout=ssh_timeout,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            _log("FATAL: remote swap failed or timed out: %s" % (getattr(exc, "stderr", None) or exc))
            return False

        # Sentinel is only written on success, so a failed run is retried
        # (rather than silently skipped) the next time run_sync is called.
        with open(sentinel, "w", encoding="utf-8") as fh:
            fh.write(sig + "\n")
        _log("sync complete")
        return True
    finally:
        for sidecar in ("", "-wal", "-shm"):
            path = os.path.join(stage_dir, "kb_snapshot.db" + sidecar)
            if os.path.exists(path):
                os.remove(path)
        _release_lock(lock_dir, lock_token)

"""Read-only corpus/index audits and explicit dead-letter retry requests."""
from __future__ import annotations

import hashlib
import json
import os
import time

from . import db as dbmod


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_path(cfg: dict) -> str:
    return os.path.join(cfg["paths"]["curated"], "_MANIFEST.jsonl")


def _reextract_path(cfg: dict) -> str:
    return os.path.join(cfg["paths"]["curated"], "_REEXTRACT.jsonl")


def dead_letter_report(cfg: dict) -> list[dict]:
    latest: dict[str, dict] = {}
    try:
        with open(_manifest_path(cfg), encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("src"):
                    latest[record["src"]] = record
    except FileNotFoundError:
        return []
    return [
        {**record, "retryable": os.path.isfile(src) and not os.path.islink(src)}
        for src, record in sorted(latest.items())
        if record.get("action") == "dead_letter"
    ]


def request_retry(cfg: dict, src: str, commit: bool = False) -> dict:
    src = os.path.abspath(src)
    known = {os.path.abspath(row["src"]): row for row in dead_letter_report(cfg)}
    if src not in known:
        raise ValueError("source is not an active dead letter")
    if not known[src]["retryable"]:
        raise ValueError("dead-letter source is not a safe regular file")
    result = {"src": src, "would_retry": True, "committed": bool(commit)}
    if commit:
        record = {"src": src, "action": "retry_requested", "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        with open(_manifest_path(cfg), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    return result


def request_reextract(cfg: dict, rel_path: str, commit: bool = False) -> dict:
    """Queue re-extraction while retaining the current index until replacement commits."""
    con = dbmod.connect(cfg["paths"]["db_path"], read_only=True)
    try:
        row = con.execute(
            "SELECT id, sha256 FROM documents WHERE rel_path=?", (rel_path,)
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise ValueError("document rel_path not found")
    document_id, expected_sha = row
    curated = os.path.join(cfg["paths"]["curated"], rel_path)
    if not os.path.isfile(curated) or os.path.islink(curated) or _sha256(curated) != expected_sha:
        raise ValueError("curated original is missing or hash-mismatched")
    inbox_dir = os.path.join(cfg["paths"]["inbox"], ".reextract")
    src = os.path.join(inbox_dir, "%s__%s%s" % (
        os.path.splitext(os.path.basename(rel_path))[0], expected_sha[:8], os.path.splitext(rel_path)[1]
    ))
    result = {"rel_path": rel_path, "src": src, "would_reextract": True, "committed": bool(commit)}
    if commit:
        from .ingest.worker import _promote_copy
        os.makedirs(inbox_dir, exist_ok=True)
        if not os.path.exists(src):
            _promote_copy(curated, inbox_dir, src)
        record = {**result, "document_id": document_id, "sha256": expected_sha, "action": "requested"}
        with open(_reextract_path(cfg), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    return result


def check_consistency(cfg: dict) -> dict:
    con = dbmod.connect(cfg["paths"]["db_path"], read_only=True)
    try:
        documents = con.execute("SELECT id, rel_path, sha256, n_chunks FROM documents").fetchall()
        missing, hash_mismatches = [], []
        chunk_mismatches = 0
        for document_id, rel_path, expected_sha, expected_chunks in documents:
            path = os.path.join(cfg["paths"]["curated"], rel_path)
            if not os.path.isfile(path) or os.path.islink(path):
                missing.append(rel_path)
            elif _sha256(path) != expected_sha:
                hash_mismatches.append(rel_path)
            actual = con.execute("SELECT count(*) FROM chunks WHERE document_id=?", (document_id,)).fetchone()[0]
            if actual != expected_chunks:
                chunk_mismatches += 1
        counts = {
            "documents": len(documents),
            "chunks": con.execute("SELECT count(*) FROM chunks").fetchone()[0],
            "vectors": con.execute("SELECT count(*) FROM vchunks").fetchone()[0],
            "fts": con.execute("SELECT count(*) FROM chunks_fts").fetchone()[0],
            "pending": con.execute("SELECT count(*) FROM ingest_pending WHERE state != 'dead_letter'").fetchone()[0],
            "dead_letter_pending": con.execute("SELECT count(*) FROM ingest_pending WHERE state = 'dead_letter'").fetchone()[0],
        }
    finally:
        con.close()
    index_drift = counts["chunks"] != counts["vectors"] or counts["chunks"] != counts["fts"]
    return {
        "ok": not (missing or hash_mismatches or chunk_mismatches or index_drift or counts["pending"]),
        "missing_curated": missing,
        "hash_mismatches": hash_mismatches,
        "document_chunk_mismatches": chunk_mismatches,
        "index_count_mismatch": index_drift,
        "counts": counts,
        "dead_letters": dead_letter_report(cfg),
    }

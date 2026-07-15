from __future__ import annotations

import os

from openkb import db as dbmod
from openkb.maintenance import check_consistency, dead_letter_report, request_reextract, request_retry


def test_consistency_reports_missing_curated_and_index_drift(cfg):
    os.makedirs(os.path.dirname(cfg["paths"]["db_path"]), exist_ok=True)
    con = dbmod.connect(cfg["paths"]["db_path"])
    dbmod.init_schema(con, dim=cfg["embeddings"]["dim"])
    con.execute(
        "INSERT INTO documents(source, rel_path, domain, sha256, summary, n_chunks, extractor) VALUES (?,?,?,?,?,?,?)",
        ("missing.md", "00_ELECTRICAL/missing.md", "00_ELECTRICAL", "a" * 64, "s", 2, "text"),
    )
    con.commit()
    con.close()
    report = check_consistency(cfg)
    assert report["ok"] is False
    assert report["missing_curated"] == ["00_ELECTRICAL/missing.md"]
    assert report["document_chunk_mismatches"] == 1


def test_dead_letter_retry_is_explicit_and_resets_manifest_attempts(cfg, tmp_path):
    src = tmp_path / "data" / "inbox" / "broken.pdf"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"%PDF-broken")
    curated = tmp_path / "data" / "curated"
    curated.mkdir(parents=True, exist_ok=True)
    manifest = curated / "_MANIFEST.jsonl"
    manifest.write_text(
        '{"src":"%s","action":"dead_letter","attempt":3,"error":"bad PDF"}\n' % src,
        encoding="utf-8",
    )
    assert dead_letter_report(cfg)[0]["retryable"] is True
    assert request_retry(cfg, str(src), commit=False)["would_retry"] is True
    request_retry(cfg, str(src), commit=True)
    from openkb.ingest.worker import _resume_state
    done, attempts = _resume_state(cfg)
    assert str(src) not in done
    assert attempts[str(src)] == 0


def test_reextract_request_requires_verified_curated_original(cfg, tmp_path):
    curated = tmp_path / "data" / "curated" / "00_ELECTRICAL"
    curated.mkdir(parents=True)
    source = curated / "manual.md"
    source.write_text("verified source content", encoding="utf-8")
    import hashlib
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    con = dbmod.connect(cfg["paths"]["db_path"])
    dbmod.init_schema(con, dim=cfg["embeddings"]["dim"])
    con.execute(
        "INSERT INTO documents(source, rel_path, domain, sha256, summary, n_chunks, extractor) VALUES (?,?,?,?,?,?,?)",
        ("manual.md", "00_ELECTRICAL/manual.md", "00_ELECTRICAL", sha, "s", 0, "text"),
    )
    con.commit()
    con.close()
    preview = request_reextract(cfg, "00_ELECTRICAL/manual.md", commit=False)
    assert preview["would_reextract"] is True
    assert not os.path.exists(preview["src"])
    committed = request_reextract(cfg, "00_ELECTRICAL/manual.md", commit=True)
    assert os.path.isfile(committed["src"])

"""Tests for the ingest package: chunk_text, secrets.scan_file,
extract_register (against the real generated cable-list.xlsx), and the
zip-bomb guard in extract_office_xml."""
from __future__ import annotations

import os
import zipfile

import pytest

from openkb.ingest import extract as extractmod
from openkb.ingest import secrets as secretsmod
from openkb.ingest import worker as workermod
from openkb.ingest.worker import chunk_text

CORPUS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "corpus")


# ---------------------------------------------------------------------------
# chunk_text
# ---------------------------------------------------------------------------

def test_chunk_text_keeps_short_paragraph_whole():
    text = "A short paragraph that easily fits under the chunk limit."
    chunks = chunk_text(text, chunk_chars=1800, overlap=200)
    assert chunks == [text]


def test_chunk_text_falls_back_to_sliding_window_for_huge_paragraph():
    # one giant "paragraph" (no blank-line breaks) well over chunk_chars
    huge = "word " * 2000  # ~10000 chars, no paragraph breaks
    chunk_chars = 500
    overlap = 100
    chunks = chunk_text(huge, chunk_chars=chunk_chars, overlap=overlap)

    assert len(chunks) > 1
    for c in chunks[:-1]:
        assert len(c) == chunk_chars
    step = chunk_chars - overlap
    # verify the sliding window actually overlaps: the tail of chunk N
    # should reappear at the head of chunk N+1 by `overlap` characters
    assert chunks[0][step:] == chunks[1][:overlap]


# ---------------------------------------------------------------------------
# secrets.scan_file
# ---------------------------------------------------------------------------

def test_scan_file_flags_private_key_block(tmp_path):
    text = (
        "some preamble\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA1234567890abcdef\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    clean, reasons = secretsmod.scan_file(str(tmp_path / "notes.txt"), text=text)
    assert clean is False
    assert any("credential-shaped" in r for r in reasons)


def test_scan_file_flags_password_assignment(tmp_path):
    text = "config dump\npassword=SuperSecret123\nother=stuff\n"
    clean, reasons = secretsmod.scan_file(str(tmp_path / "config-dump.txt"), text=text)
    assert clean is False
    assert any("credential-shaped" in r for r in reasons)


def test_scan_content_checks_the_full_extracted_stream():
    text = ("safe maintenance prose " * 12_000) + "\napi_key=abcdefghijklmno"
    assert len(text) > 200_000
    assert secretsmod.scan_content(text) == "content contains a credential-shaped string"


def test_scan_file_passes_clean_prose():
    text = (
        "Diesel Generator DG1 — Operation & Maintenance Manual.\n"
        "Rated output: 500 kVA / 400 kW at 0.8 power factor.\n"
    )
    clean, reasons = secretsmod.scan_file("dg1-diesel-generator-manual.md", text=text)
    assert clean is True
    assert reasons == []


# ---------------------------------------------------------------------------
# extract_register against the real generated cable-list.xlsx
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def cable_list_path():
    path = os.path.join(CORPUS_DIR, "cable-list.xlsx")
    if not os.path.exists(path):
        pytest.skip("cable-list.xlsx not generated -- run examples/make_corpus.py first")
    return path


def test_extract_register_finds_header_and_aligns_rows(cable_list_path):
    header, rows = extractmod.extract_register(cable_list_path, header_keyword="cable number", sheet=None)

    assert header == ["Cable Number", "From", "To", "Type", "Cores", "Route"]
    assert len(rows) == 80

    # spot-check known tag values line up in the right columns
    from_col = header.index("From")
    to_col = header.index("To")
    cable_col = header.index("Cable Number")

    assert rows[0][cable_col] == "CAB-001"
    known_tags = {"DG1", "DG2", "MSB-1", "FP-101", "CWP-01", "AHU-3"}
    assert rows[0][from_col] in known_tags
    assert rows[0][to_col] in known_tags


def test_extract_register_ignores_junk_sheet_when_using_sheet0(cable_list_path):
    # sheet=None resolves to the FIRST sheet in the workbook, which is the
    # real "Cable Register" sheet (sheet 0), not the junk "Notes (ignore)"
    # sheet -- confirm the header row actually found belongs to the real
    # register, not the junk sheet.
    header, rows = extractmod.extract_register(cable_list_path, header_keyword="cable number", sheet=None)
    assert header  # header WAS found
    assert "Cable Number" in header

    # And explicitly confirm the junk sheet does NOT satisfy the same
    # header-keyword search (no header row with the keyword present there).
    junk_header, junk_rows = extractmod.extract_register(
        cable_list_path, header_keyword="cable number", sheet="Notes (ignore)"
    )
    assert junk_header == []
    assert junk_rows == []


# ---------------------------------------------------------------------------
# zip-bomb guard
# ---------------------------------------------------------------------------

def test_zip_guard_trips_on_oversized_entry(tmp_path):
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        # word/document.xml required so extract_office_xml would otherwise
        # try to read it -- but the guard must reject before that.
        info = zipfile.ZipInfo("word/document.xml")
        # Craft an entry whose declared uncompressed size exceeds the guard's
        # limit without actually writing that much data, by writing via
        # writestr with a real (smaller) payload then patching file_size --
        # zipfile recomputes file_size from the actual data on close, so
        # instead we write a real (if repetitive) blob exceeding the limit
        # is impractical in a fast test; instead directly exercise the
        # guard function with a crafted infolist substitute.
        zf.writestr(info, "<xml>hello</xml>")

    # Directly test _zip_guard's oversized-entry branch with an in-memory
    # fake ZipFile-like structure using the real infolist mutated post-hoc.
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        infos[0].file_size = extractmod.ZIP_MAX_XML_BYTES + 1
        with pytest.raises(ValueError, match="too large uncompressed"):
            extractmod._zip_guard(_FakeZipFile(infos))


def test_zip_guard_trips_on_absurd_entry_count():
    infos = []
    for i in range(extractmod.ZIP_MAX_ENTRIES + 1):
        info = zipfile.ZipInfo("part%d.xml" % i)
        info.file_size = 10
        info.compress_size = 10
        infos.append(info)
    with pytest.raises(ValueError, match="entries"):
        extractmod._zip_guard(_FakeZipFile(infos))


def test_zip_guard_trips_on_suspicious_compression_ratio():
    info = zipfile.ZipInfo("word/document.xml")
    info.file_size = 10_000_000
    info.compress_size = 100  # ratio 100000x, way over ZIP_MAX_RATIO
    with pytest.raises(ValueError, match="compression ratio"):
        extractmod._zip_guard(_FakeZipFile([info]))


class _FakeZipFile:
    """Minimal stand-in exposing only what _zip_guard needs (.infolist())."""

    def __init__(self, infos):
        self._infos = infos

    def infolist(self):
        return self._infos


# ---------------------------------------------------------------------------
# filesystem safety primitives
# ---------------------------------------------------------------------------

def test_iter_inbox_accepts_nested_regular_file_and_rejects_symlink(tmp_path):
    inbox = tmp_path / "inbox"
    nested = inbox / "nested"
    nested.mkdir(parents=True)
    regular = nested / "manual.txt"
    regular.write_text("regular document", encoding="utf-8")
    external = tmp_path / "outside.txt"
    external.write_text("outside document", encoding="utf-8")
    (nested / "escape.txt").symlink_to(external)

    assert list(workermod._iter_inbox_files(str(inbox))) == [str(regular)]
    assert workermod._is_safe_inbox_file(str(regular), str(inbox)) is True
    assert workermod._is_safe_inbox_file(str(nested / "escape.txt"), str(inbox)) is False


def test_is_safe_inbox_file_rejects_resolved_escape(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    external = tmp_path / "outside.txt"
    external.write_text("outside document", encoding="utf-8")

    assert workermod._is_safe_inbox_file(str(inbox / ".." / "outside.txt"), str(inbox)) is False


def test_collision_safe_dest_preserves_same_name_files(tmp_path):
    root = tmp_path / "quarantine"
    first_dir, first_path = workermod._collision_safe_dest(
        str(root), "manual.txt", "a" * 64
    )
    os.makedirs(first_dir)
    with open(first_path, "wb") as fh:
        fh.write(b"first")

    second_dir, second_path = workermod._collision_safe_dest(
        str(root), "manual.txt", "b" * 64
    )

    assert second_dir == first_dir
    assert second_path != first_path
    assert second_path.endswith("manual__bbbbbbbb.txt")


def test_promote_copy_keeps_source_and_writes_matching_destination(tmp_path):
    src = tmp_path / "inbox" / "manual.txt"
    src.parent.mkdir()
    src.write_bytes(b"durable source")
    dest_dir = tmp_path / "curated"
    dest = dest_dir / "manual.txt"

    workermod._promote_copy(str(src), str(dest_dir), str(dest))

    assert src.read_bytes() == b"durable source"
    assert dest.read_bytes() == b"durable source"
    sha = workermod._sha256_file(str(src))
    assert workermod._verified_hash(str(dest), sha) is True


def test_promote_copy_failure_keeps_source(tmp_path, monkeypatch):
    src = tmp_path / "inbox" / "manual.txt"
    src.parent.mkdir()
    src.write_bytes(b"only valid copy")
    dest_dir = tmp_path / "curated"
    dest = dest_dir / "manual.txt"

    def fail_replace(_src, _dest):
        raise OSError("injected rename failure")

    monkeypatch.setattr(workermod.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected rename failure"):
        workermod._promote_copy(str(src), str(dest_dir), str(dest))

    assert src.read_bytes() == b"only valid copy"
    assert not dest.exists()


# ---------------------------------------------------------------------------
# recoverable ingest transaction
# ---------------------------------------------------------------------------

def _deterministic_ingest(cfg, tmp_path, monkeypatch):
    inbox = tmp_path / "data" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    src = inbox / "manual.txt"
    src.write_text(
        "PMP-101 operating procedure.\n\nCheck breaker CB-PMP-09 before restart.",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        workermod,
        "_extract_with_ocr_fallback",
        lambda path, config: (src.read_text(encoding="utf-8"), "text", 0),
    )
    monkeypatch.setattr(workermod, "classify_document", lambda *args: "00_ELECTRICAL")
    monkeypatch.setattr(workermod, "summarize_document", lambda *args: "Synthetic pump procedure.")
    monkeypatch.setattr(workermod, "embed_chunk", lambda *args: [0.125] * 8)
    cfg["ingest"]["chunk_chars"] = 40
    cfg["ingest"]["chunk_overlap"] = 5
    return src


def _db_counts(cfg):
    con = workermod.dbmod.connect(cfg["paths"]["db_path"])
    try:
        return {
            "documents": con.execute("SELECT count(*) FROM documents").fetchone()[0],
            "chunks": con.execute("SELECT count(*) FROM chunks").fetchone()[0],
            "vchunks": con.execute("SELECT count(*) FROM vchunks").fetchone()[0],
            "fts": con.execute("SELECT count(*) FROM chunks_fts").fetchone()[0],
            "pending": con.execute("SELECT count(*) FROM ingest_pending").fetchone()[0],
        }
    finally:
        con.close()


def test_ingest_completes_with_one_curated_original_and_consistent_indexes(cfg, tmp_path, monkeypatch):
    src = _deterministic_ingest(cfg, tmp_path, monkeypatch)

    results = workermod.run_ingest(cfg)

    assert [row["action"] for row in results] == ["processed"]
    assert not src.exists()
    curated = tmp_path / "data" / "curated" / results[0]["dest"]
    assert curated.is_file()
    counts = _db_counts(cfg)
    assert counts["documents"] == 1
    assert counts["chunks"] >= 2
    assert counts["chunks"] == counts["vchunks"] == counts["fts"]
    assert counts["pending"] == 0


def test_extraction_warnings_are_recorded_in_manifest_result(cfg, tmp_path, monkeypatch):
    src = _deterministic_ingest(cfg, tmp_path, monkeypatch)
    monkeypatch.setattr(
        workermod,
        "_extract_with_ocr_fallback",
        lambda path, config: ("usable text\n[NOTE: OCR limited to first 2 of 9 pages]", "ocr", 2),
    )
    result = workermod.run_ingest(cfg)[0]
    assert result["action"] == "processed"
    assert result["extraction_warnings"] == ["ocr_page_limit"]


def test_failure_during_curated_promotion_retains_inbox_and_staged_state(cfg, tmp_path, monkeypatch):
    src = _deterministic_ingest(cfg, tmp_path, monkeypatch)
    original_promote = workermod._promote_copy
    monkeypatch.setattr(
        workermod,
        "_promote_copy",
        lambda *args: (_ for _ in ()).throw(OSError("injected promotion failure")),
    )

    first = workermod.run_ingest(cfg)

    assert first[0]["action"] == "error"
    assert src.is_file()
    assert _db_counts(cfg) == {"documents": 0, "chunks": 0, "vchunks": 0, "fts": 0, "pending": 1}

    monkeypatch.setattr(workermod, "_promote_copy", original_promote)
    second = workermod.run_ingest(cfg)
    assert second[0]["action"] == "processed"
    assert _db_counts(cfg)["pending"] == 0


def test_failure_during_index_transaction_rolls_back_and_reconciles(cfg, tmp_path, monkeypatch):
    src = _deterministic_ingest(cfg, tmp_path, monkeypatch)
    original_fts_insert = workermod.dbmod.fts_insert
    calls = {"count": 0, "fail": True}

    def fail_second_fts(con, chunk_id, text):
        calls["count"] += 1
        if calls["fail"] and calls["count"] == 2:
            raise RuntimeError("injected FTS failure")
        return original_fts_insert(con, chunk_id, text)

    monkeypatch.setattr(workermod.dbmod, "fts_insert", fail_second_fts)
    first = workermod.run_ingest(cfg)

    assert first[0]["action"] == "error"
    assert src.is_file()
    counts = _db_counts(cfg)
    assert counts["documents"] == counts["chunks"] == counts["vchunks"] == counts["fts"] == 0
    assert counts["pending"] == 1

    calls["fail"] = False
    calls["count"] = 0
    second = workermod.run_ingest(cfg)
    assert second[0]["action"] == "processed"
    counts = _db_counts(cfg)
    assert counts["documents"] == 1
    assert counts["chunks"] == counts["vchunks"] == counts["fts"]
    assert counts["pending"] == 0


def test_failure_after_commit_is_completed_by_startup_reconciliation(cfg, tmp_path, monkeypatch):
    src = _deterministic_ingest(cfg, tmp_path, monkeypatch)
    original_remove = workermod.os.remove
    fail_cleanup = {"enabled": True}

    def fail_inbox_cleanup(path):
        if fail_cleanup["enabled"] and os.path.abspath(path) == os.path.abspath(src):
            raise OSError("injected inbox cleanup failure")
        return original_remove(path)

    monkeypatch.setattr(workermod.os, "remove", fail_inbox_cleanup)
    first = workermod.run_ingest(cfg)

    assert first[0]["action"] == "error"
    assert src.is_file()
    counts = _db_counts(cfg)
    assert counts["documents"] == 1
    assert counts["pending"] == 1

    fail_cleanup["enabled"] = False
    second = workermod.run_ingest(cfg)
    assert any(row["action"] == "reconciled" for row in second)
    assert not src.exists()
    assert _db_counts(cfg)["pending"] == 0


def test_reset_waits_for_ingest_lock_before_touching_database(cfg, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    db_path = data_dir / "kb.db"
    wal_path = data_dir / "kb.db-wal"
    shm_path = data_dir / "kb.db-shm"
    db_path.write_bytes(b"database sentinel")
    wal_path.write_bytes(b"wal sentinel")
    shm_path.write_bytes(b"shm sentinel")
    lock = workermod._acquire_lock(cfg)
    assert lock is not None
    connect_called = {"value": False}

    def unexpected_connect(*args, **kwargs):
        connect_called["value"] = True
        raise AssertionError("database connection must not occur while locked")

    monkeypatch.setattr(workermod.dbmod, "connect", unexpected_connect)
    try:
        result = workermod.run_ingest(cfg, reset_db=True, confirm=True)
    finally:
        lock.close()

    assert result == [{"action": "locked", "reason": "another ingest run holds the lock"}]
    assert connect_called["value"] is False
    assert db_path.read_bytes() == b"database sentinel"
    assert wal_path.read_bytes() == b"wal sentinel"
    assert shm_path.read_bytes() == b"shm sentinel"


def test_duplicate_is_removed_only_when_curated_hash_matches(cfg, tmp_path, monkeypatch):
    src = _deterministic_ingest(cfg, tmp_path, monkeypatch)
    sha = workermod._sha256_file(str(src))
    curated = tmp_path / "data" / "curated" / "00_ELECTRICAL" / "manual.txt"
    curated.parent.mkdir(parents=True)
    curated.write_bytes(src.read_bytes())
    con = workermod.dbmod.connect(cfg["paths"]["db_path"])
    try:
        workermod.dbmod.init_schema(con, 8)
        con.execute(
            "INSERT INTO documents(source, rel_path, domain, sha256) VALUES (?, ?, ?, ?)",
            ("manual.txt", "00_ELECTRICAL/manual.txt", "00_ELECTRICAL", sha),
        )
        con.commit()
    finally:
        con.close()

    result = workermod.run_ingest(cfg)

    assert result[0]["action"] == "skip_duplicate"
    assert not src.exists()
    assert workermod._verified_hash(str(curated), sha)


def test_missing_curated_duplicate_row_is_repaired_without_source_loss(cfg, tmp_path, monkeypatch):
    src = _deterministic_ingest(cfg, tmp_path, monkeypatch)
    sha = workermod._sha256_file(str(src))
    con = workermod.dbmod.connect(cfg["paths"]["db_path"])
    try:
        workermod.dbmod.init_schema(con, 8)
        con.execute(
            "INSERT INTO documents(source, rel_path, domain, sha256) VALUES (?, ?, ?, ?)",
            ("manual.txt", "00_ELECTRICAL/missing.txt", "00_ELECTRICAL", sha),
        )
        con.commit()
    finally:
        con.close()
    monkeypatch.setattr(
        workermod,
        "_promote_copy",
        lambda *args: (_ for _ in ()).throw(OSError("injected promotion failure")),
    )

    result = workermod.run_ingest(cfg)

    assert result[0]["action"] == "error"
    assert src.is_file()
    counts = _db_counts(cfg)
    assert counts["documents"] == 0
    assert counts["pending"] == 1


def test_reconciliation_can_index_from_curated_when_inbox_copy_is_missing(cfg, tmp_path, monkeypatch):
    data = tmp_path / "data"
    curated = data / "curated" / "00_ELECTRICAL" / "manual.txt"
    curated.parent.mkdir(parents=True)
    curated.write_text(
        "PMP-101 operating procedure.\n\nCheck breaker CB-PMP-09 before restart.",
        encoding="utf-8",
    )
    missing_src = data / "inbox" / "manual.txt"
    sha = workermod._sha256_file(str(curated))
    con = workermod.dbmod.connect(cfg["paths"]["db_path"])
    try:
        workermod.dbmod.init_schema(con, 8)
        workermod.dbmod.upsert_ingest_pending(
            con,
            src_path=str(missing_src),
            rel_path="manual.txt",
            dest_rel_path="00_ELECTRICAL/manual.txt",
            sha256=sha,
            state="curated",
        )
        con.commit()
    finally:
        con.close()
    monkeypatch.setattr(
        workermod,
        "_extract_with_ocr_fallback",
        lambda path, config: (open(path, encoding="utf-8").read(), "text", 0),
    )
    monkeypatch.setattr(workermod, "classify_document", lambda *args: "00_ELECTRICAL")
    monkeypatch.setattr(workermod, "summarize_document", lambda *args: "Synthetic pump procedure.")
    monkeypatch.setattr(workermod, "embed_chunk", lambda *args: [0.125] * 8)
    cfg["ingest"]["chunk_chars"] = 40
    cfg["ingest"]["chunk_overlap"] = 5

    result = workermod.run_ingest(cfg)

    assert any(row["action"] == "processed" for row in result)
    counts = _db_counts(cfg)
    assert counts["documents"] == 1
    assert counts["pending"] == 0

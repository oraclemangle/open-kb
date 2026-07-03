"""Tests for the ingest package: chunk_text, secrets.scan_file,
extract_register (against the real generated cable-list.xlsx), and the
zip-bomb guard in extract_office_xml."""
from __future__ import annotations

import os
import zipfile

import pytest

from openkb.ingest import extract as extractmod
from openkb.ingest import secrets as secretsmod
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

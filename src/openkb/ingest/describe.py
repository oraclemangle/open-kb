"""Vision-DESCRIBE — make graphic-only documents searchable when OCR can't.

OCR (``ingest.ocr``) transcribes text that is *present* in a page image.
But a large share of engineering documents are not text hiding in a
picture — they're pictures: a wiring schematic, a general-arrangement
drawing, a nameplate photo, a scanned equipment panel. Point a
transcription prompt at a schematic and you get almost nothing back,
correctly — there is no paragraph of prose drawn on it to transcribe.
Re-running OCR harder does not fix this; the problem isn't a weak OCR
pass, it's that the document's information lives in symbols, layout, and
connections rather than words. Re-OCR of a pure-graphic drawing yields
the same near-empty output every time, however high the DPI.

The fix is a different task: ask the vision model to *describe* the
drawing — read the title block, list the equipment/components and their
labels, summarise how they connect — and index that description text
instead. It's not a transcript (nothing was "written" on the page in that
form) and it's explicitly factual/observational rather than inferential,
which is why the prompt insists on "only what is visibly drawn or
labelled. Do not invent." The stored chunk text is prefixed
``[VISION DESCRIPTION]`` unconditionally so a reader (or a downstream
LLM) never mistakes a model's description for the document's own words.

This only runs on documents that are already "text-thin" — total chunk
text under ``vision_describe.min_chars`` — and only swaps in the
description when it adds more than ``vision_describe.gain_min`` characters
over what's already stored, so a document that already has decent text
(e.g. a schematic with substantial OCR'd notes) is left alone.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.request

from .. import db as dbmod

__all__ = ["describe_thin_documents"]

_DESCRIBE_PROMPT = (
    "Describe this engineering drawing or schematic for a searchable technical "
    "index. State the title and system (read the title block), list the "
    "equipment and components shown with any tags or labels, and summarise "
    "the main connections or flow between them. Be factual and specific — "
    "only what is visibly drawn or labelled. Do not invent. Plain text, no "
    "preamble."
)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
VISION_PREFIX = "[VISION DESCRIPTION]"


def _post(url: str, payload: dict, timeout: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _require_fitz():
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "PyMuPDF is required for vision-describe page rendering. Install "
            "the 'pdf' extra: pip install 'open-kb[pdf]'"
        ) from exc
    return fitz


def _describe_png(png: bytes, cfg: dict) -> str:
    vd_cfg = cfg.get("vision_describe", {})
    ocr_cfg = cfg.get("ocr", {})
    url = ocr_cfg.get("url", "http://127.0.0.1:11434/api/chat")
    model = vd_cfg.get("model", "your-vision-model")
    timeout = cfg.get("llm", {}).get("timeout_s", 240)
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "user", "content": _DESCRIBE_PROMPT, "images": [base64.b64encode(png).decode("ascii")]}
        ],
        "options": {"temperature": 0.2, "top_p": 0.8, "num_predict": 700},
    }
    result = _post(url, payload, timeout=timeout)
    return (result.get("message", {}).get("content", "") or "").strip()


def _page_dpi_for_cap(rect, dpi: int, max_pixels: int) -> int:
    est_pixels = int((rect.width / 72 * dpi) * (rect.height / 72 * dpi))
    if est_pixels <= max_pixels:
        return dpi
    scale = (max_pixels / est_pixels) ** 0.5
    return max(50, int(dpi * scale * 0.95))


def describe_document(path: str, cfg: dict) -> str:
    """Produce a vision description for one document (image or PDF).

    For a PDF, describes up to ``vision_describe.max_pages`` pages and
    joins them with ``[Page N]`` markers; for a standalone image, describes
    the single image. Returns raw description text (no ``[VISION
    DESCRIPTION]`` prefix — that's applied by the caller once it has
    decided the description is actually going to be used, keeping this
    function a pure "what does the model see" primitive).
    """
    ext = os.path.splitext(path)[1].lower()
    ocr_cfg = cfg.get("ocr", {})
    dpi = ocr_cfg.get("dpi", 150)
    max_pixels = cfg.get("ingest", {}).get("max_pixels", 8_000_000)

    if ext in IMAGE_EXTENSIONS:
        with open(path, "rb") as fh:
            return _describe_png(fh.read(), cfg)

    fitz = _require_fitz()
    vd_cfg = cfg.get("vision_describe", {})
    max_pages = vd_cfg.get("max_pages", 4)
    doc = fitz.open(path)
    try:
        n = min(doc.page_count, max_pages)
        parts: list[str] = []
        for page_index in range(n):
            page = doc[page_index]
            page_dpi = _page_dpi_for_cap(page.rect, dpi, max_pixels)
            try:
                png = page.get_pixmap(dpi=page_dpi).tobytes("png")
                desc = _describe_png(png, cfg)
            except Exception:
                desc = ""
            if desc:
                parts.append("[Page %d] %s" % (page_index + 1, desc))
        if doc.page_count > max_pages:
            parts.append("[NOTE: described first %d of %d pages]" % (max_pages, doc.page_count))
        return "\n\n".join(parts)
    finally:
        doc.close()


def _doc_char_total(con, document_id: int) -> int:
    row = con.execute(
        "SELECT COALESCE(SUM(LENGTH(text)),0) FROM chunks WHERE document_id=?", (document_id,)
    ).fetchone()
    return row[0] or 0


def _swap_document_chunks(con, document_id: int, chunks: list[str], embed_fn, chunk_chars: int) -> None:
    """Replace all chunks/vchunks/FTS rows for one document with new content.

    This is the same delete-then-reinsert pattern used by the main ingest
    worker's document-level rewrite path: FTS5's external-content mode
    requires an explicit ``('delete', rowid, text)`` command using the
    *old* text before the corresponding row disappears, so old rows must
    be read back before they're deleted, not just dropped.
    """
    old_rows = con.execute(
        "SELECT id, text FROM chunks WHERE document_id=?", (document_id,)
    ).fetchall()
    embeddings = [embed_fn(c) for c in chunks]  # embed BEFORE the transaction (never hold a txn open across HTTP calls)
    cur = con.cursor()
    cur.execute("BEGIN")
    try:
        for chunk_id, old_text in old_rows:
            dbmod.fts_delete(con, chunk_id, old_text)
        cur.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
        for seq, (text, vector) in enumerate(zip(chunks, embeddings)):
            cur.execute(
                "INSERT INTO chunks(document_id, seq, text) VALUES (?, ?, ?)",
                (document_id, seq, text),
            )
            chunk_id = cur.lastrowid
            cur.execute(
                "INSERT INTO vchunks(chunk_id, embedding) VALUES (?, ?)",
                (chunk_id, dbmod.serialize_f32(vector)),
            )
            dbmod.fts_insert(con, chunk_id, text)
        cur.execute(
            "UPDATE documents SET n_chunks=?, extractor='vision-describe', ingested_at=? WHERE id=?",
            (len(chunks), time.strftime("%Y-%m-%d %H:%M:%S"), document_id),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise


def _chunk_text(text: str, chunk_chars: int, overlap: int) -> list[str]:
    """Paragraph-boundary chunking (shared shape with the main worker's chunker)."""
    import re

    parts: list[str] = []
    buf = ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(buf) + len(para) + 2 <= chunk_chars:
            buf = (buf + "\n\n" + para).strip()
        else:
            if buf:
                parts.append(buf)
            if len(para) > chunk_chars:
                step = max(1, chunk_chars - overlap)
                for i in range(0, len(para), step):
                    parts.append(para[i : i + chunk_chars])
                buf = ""
            else:
                buf = para
    if buf:
        parts.append(buf)
    return parts


def describe_thin_documents(cfg: dict, embed_fn, limit: int | None = None, commit: bool = False) -> list[dict]:
    """Find text-thin documents and vision-describe them.

    Dry-run by default (``commit=False``): computes what *would* change and
    returns a report without touching the database. Pass ``commit=True`` to
    actually delete the old chunks/vchunks/FTS rows for a qualifying
    document and insert the new description-derived chunks + embeddings,
    updating ``documents.extractor`` to ``'vision-describe'``.

    ``embed_fn`` is injected (a ``str -> list[float]`` callable) rather than
    imported here, so this module has no direct dependency on the
    embeddings HTTP client or its retry policy — the worker owns that.

    A document qualifies when:
      1. its current total chunk text is under ``vision_describe.min_chars``
         (it's "thin" — a candidate for a graphic-only document), and
      2. its file still exists at ``rel_path`` under ``curated/`` and is a
         PDF or image (only formats we can render/describe), and
      3. the generated description is more than ``vision_describe.gain_min``
         characters longer than what's already stored (never regress a
         document that already has more text than the description would
         add — e.g. a schematic that already carries substantial OCR'd
         annotation text).

    Returns one dict per candidate examined, with an ``"action"`` of
    ``"would_describe"``/``"described"``/``"no_gain"``/``"describe_error"``.
    """
    vd_cfg = cfg.get("vision_describe", {})
    min_chars = vd_cfg.get("min_chars", 800)
    gain_min = vd_cfg.get("gain_min", 200)
    chunk_chars = cfg.get("ingest", {}).get("chunk_chars", 1800)
    chunk_overlap = cfg.get("ingest", {}).get("chunk_overlap", 200)
    curated_root = cfg.get("paths", {}).get("curated", "./data/curated")

    con = dbmod.connect(cfg["paths"]["db_path"])
    try:
        rows = con.execute(
            """
            SELECT d.id, d.rel_path,
                   (SELECT COALESCE(SUM(LENGTH(text)),0) FROM chunks WHERE document_id=d.id) AS total_chars
            FROM documents d
            """
        ).fetchall()

        candidates = []
        for doc_id, rel_path, total_chars in rows:
            if total_chars >= min_chars:
                continue
            full_path = os.path.join(curated_root, rel_path)
            ext = os.path.splitext(rel_path)[1].lower()
            if ext not in ({".pdf"} | IMAGE_EXTENSIONS):
                continue
            if not os.path.exists(full_path):
                continue
            candidates.append((doc_id, rel_path, full_path, total_chars))

        candidates.sort(key=lambda c: c[3])  # thinnest first
        if limit:
            candidates = candidates[:limit]

        report: list[dict] = []
        for doc_id, rel_path, full_path, total_chars in candidates:
            try:
                raw_description = describe_document(full_path, cfg)
            except Exception as exc:
                report.append({"document_id": doc_id, "rel_path": rel_path, "action": "describe_error", "error": str(exc)})
                continue

            description = (VISION_PREFIX + "\n" + raw_description).strip() if raw_description else ""
            new_chars = len(description)
            if new_chars <= total_chars + gain_min:
                report.append({"document_id": doc_id, "rel_path": rel_path, "action": "no_gain", "old_chars": total_chars, "new_chars": new_chars})
                continue

            if not commit:
                report.append({"document_id": doc_id, "rel_path": rel_path, "action": "would_describe", "old_chars": total_chars, "new_chars": new_chars})
                continue

            chunks = _chunk_text(description, chunk_chars, chunk_overlap)
            if not chunks:
                report.append({"document_id": doc_id, "rel_path": rel_path, "action": "no_gain", "old_chars": total_chars, "new_chars": new_chars})
                continue
            try:
                _swap_document_chunks(con, doc_id, chunks, embed_fn, chunk_chars)
            except Exception as exc:
                report.append({"document_id": doc_id, "rel_path": rel_path, "action": "swap_error", "error": str(exc)})
                continue
            report.append({"document_id": doc_id, "rel_path": rel_path, "action": "described", "old_chars": total_chars, "new_chars": new_chars, "chunks": len(chunks)})

        return report
    finally:
        con.close()

"""The ingest pipeline — walk the inbox, and turn each document into a row
set in the KB.

For every file under ``paths.inbox`` this does, in order:

  1. **dedupe** — hash the file (sha256); if that hash is already in
     ``documents``, skip it (and remove the inbox copy — it's a repeat
     drop of something already ingested).
  2. **secrets gate** (:mod:`openkb.ingest.secrets`) — filename check
     before opening the file, content check after extraction. Any hit
     quarantines the original to ``paths.quarantine`` instead of ingesting
     it, and processing moves on to the next file.
  3. **extract** (:mod:`openkb.ingest.extract`) — get text out of the
     file. For a PDF whose text layer is thin relative to its page count,
     fall back to OCR (:mod:`openkb.ingest.ocr`) so scanned pages aren't
     silently ingested as near-empty documents.
  4. **classify** — ask the general LLM to choose exactly one bucket from
     ``taxonomy`` in ``config.yaml``. This is deliberately LLM-driven
     against a domain list *you* define, not a hard-coded keyword map: a
     keyword map baked into the library would only make sense for the
     equipment/vocabulary of whoever wrote it. If you want faster/cheaper
     classification, add an optional keyword map to your own config
     (outside the scope of what ships here) and check it before falling
     through to the LLM call — the taxonomy list itself is the only
     contract this module relies on. If the model's answer doesn't match
     any taxonomy entry, the *last* entry is used as a catch-all bucket
     (by convention, name it something like your project's "99_MISC").
  5. **summarise** — a few factual sentences for ``documents.summary``.
  6. **chunk** — paragraph-boundary chunking at ``ingest.chunk_chars``
     with ``ingest.chunk_overlap`` overlap on any chunk too big to split
     on a paragraph boundary.
  7. **embed** — one embeddings call per chunk against ``embeddings.url``
     (OpenAI-compatible), with retry/backoff on transient HTTP errors.
  8. **stage** — write a durable ``ingest_pending`` row, copy the original
     into ``paths.curated/<domain>/`` using fsync + atomic rename, and verify
     that the curated SHA-256 matches before publishing database rows.
  9. **commit + complete** — insert the document and all chunk/vector/FTS
     rows in one transaction, then remove the inbox copy only after another
     curated hash check and clear the pending row.

Resumability: ``ingest_pending`` is the durable interruption ledger and is
reconciled at locked startup. The JSONL manifest remains the operator-facing
attempt history and bounded retry counter. A duplicate hash is only treated
as complete when its recorded curated regular file exists and has the same
SHA-256; otherwise the inconsistent DB rows are removed and the intact inbox
source is processed again.

Concurrency: a single lock file under ``paths.data_dir`` stops two worker
invocations from racing on the same inbox/DB. It is advisory (``flock``),
not a database-level lock — if you need multiple concurrent ingesters,
partition the inbox yourself.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.request

from .. import db as dbmod
from . import extract as extractmod
from . import ocr as ocrmod
from . import secrets as secretsmod

__all__ = ["run_ingest"]

DOC_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".txt", ".md", ".csv",
    ".png", ".jpg", ".jpeg",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
OCR_TEXT_THIN_CHARS_PER_PAGE = 40  # below this, a PDF page is treated as "no usable text layer"
MAX_ATTEMPTS = 3
_LOCK_NAME = ".ingest.lock"
_MANIFEST_NAME = "_MANIFEST.jsonl"


# --------------------------------------------------------------------------
# small stdlib-only helpers
# --------------------------------------------------------------------------

def _post_json(url: str, payload: dict, timeout: int, retries: int = 3) -> dict:
    """POST JSON with retry/backoff. Raises the last error after exhausting retries.

    Transient failures (a local model server still loading weights, a
    momentary connection refusal) are common enough with local inference
    servers that a bare single-attempt call would make ingestion flaky for
    no good reason — but we do not retry forever, so a genuinely dead
    endpoint still surfaces as a real error rather than hanging the loop.
    """
    data = json.dumps(payload).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = exc
            time.sleep(2 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _atomic_write_bytes(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _manifest_path(cfg: dict) -> str:
    return os.path.join(cfg["paths"]["curated"], _MANIFEST_NAME)


def _manifest_append(cfg: dict, record: dict) -> None:
    path = _manifest_path(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _acquire_lock(cfg: dict):
    """Advisory single-instance lock under paths.data_dir. Returns an open
    file handle to hold (keep a reference!) or None if another run holds it."""
    import fcntl

    os.makedirs(cfg["paths"]["data_dir"], exist_ok=True)
    lock_path = os.path.join(cfg["paths"]["data_dir"], _LOCK_NAME)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def _is_ignorable_filename(name: str) -> bool:
    """Office lock files (~$foo.docx) and dotfiles are never real documents."""
    return name.startswith("~$") or name.startswith(".")


def _is_safe_inbox_file(path: str, inbox: str) -> bool:
    """Return true only for a regular, non-symlink file contained in inbox."""
    try:
        if os.path.islink(path):
            return False
        mode = os.lstat(path).st_mode
        if not stat.S_ISREG(mode):
            return False
        resolved_inbox = os.path.realpath(inbox)
        resolved_path = os.path.realpath(path)
        return os.path.commonpath([resolved_inbox, resolved_path]) == resolved_inbox
    except (OSError, ValueError):
        return False


def _iter_inbox_files(inbox: str):
    for path in sorted(glob.glob(os.path.join(inbox, "**", "*"), recursive=True)):
        if _is_safe_inbox_file(path, inbox):
            yield path


def _collision_safe_dest(root: str, rel: str, sha: str) -> tuple[str, str]:
    """Return a non-existing destination below root without clobbering names."""
    dest_dir = os.path.join(root, os.path.dirname(rel))
    base = os.path.basename(rel)
    stem, ext = os.path.splitext(base)
    candidate = os.path.join(dest_dir, base)
    if not os.path.lexists(candidate):
        return dest_dir, candidate
    suffix = "__%s" % sha[:8]
    candidate = os.path.join(dest_dir, stem + suffix + ext)
    serial = 2
    while os.path.lexists(candidate):
        candidate = os.path.join(dest_dir, "%s%s_%d%s" % (stem, suffix, serial, ext))
        serial += 1
    return dest_dir, candidate


def _safe_dest(curated_root: str, domain: str, rel: str, sha: str) -> tuple[str, str, str]:
    """Compute a collision-safe destination path under curated/<domain>/.

    Returns ``(dest_dir, dest_path, stem)``. If a file of the same name
    already exists at the destination, the sha prefix is appended to the
    stem so two different documents that happen to share a filename never
    clobber one another.
    """
    dest_dir, candidate = _collision_safe_dest(curated_root, os.path.join(domain, rel), sha)
    stem = os.path.splitext(os.path.basename(candidate))[0]
    return dest_dir, candidate, stem


def _promote_copy(src: str, dest_dir: str, dest_path: str) -> None:
    """Durably copy src to dest without removing the recoverable source."""
    os.makedirs(dest_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % os.path.basename(dest_path), dir=dest_dir)
    try:
        with os.fdopen(fd, "wb") as out_fh, open(src, "rb") as in_fh:
            shutil.copyfileobj(in_fh, out_fh, length=1 << 20)
            out_fh.flush()
            os.fsync(out_fh.fileno())
        os.replace(tmp, dest_path)
        dir_fd = os.open(dest_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _verified_hash(path: str, expected_sha: str) -> bool:
    if os.path.islink(path):
        return False
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return False
        return _sha256_file(path) == expected_sha
    except OSError:
        return False


def _remove_document(con, document_id: int) -> None:
    """Remove a document and all external-content index rows transactionally."""
    chunks = con.execute(
        "SELECT id, text FROM chunks WHERE document_id=?", (document_id,)
    ).fetchall()
    for chunk_id, text in chunks:
        con.execute("DELETE FROM vchunks WHERE chunk_id=?", (chunk_id,))
        dbmod.fts_delete(con, chunk_id, text)
    con.execute("DELETE FROM documents WHERE id=?", (document_id,))


def _quarantine(path: str, quarantine_root: str, sha: str) -> str:
    dest_dir, dest = _collision_safe_dest(quarantine_root, os.path.basename(path), sha)
    _promote_copy(path, dest_dir, dest)
    if not _verified_hash(dest, sha):
        raise OSError("quarantine copy failed hash verification")
    if not _is_safe_inbox_file(path, os.path.dirname(path)):
        raise OSError("quarantine source changed before cleanup")
    os.remove(path)
    return dest


# --------------------------------------------------------------------------
# LLM-backed steps: classify / summarise / embed
# --------------------------------------------------------------------------

def _is_degenerate_summary(text: str) -> bool:
    """Reject obviously-broken model output (near-empty, repetitive garbage)."""
    stripped = text.strip()
    if len(stripped) < 10:
        return True
    words = re.findall(r"\w+", stripped.lower())
    if len(words) >= 20 and len(set(words)) / len(words) < 0.12:
        return True
    if re.search(r"(.)\1{20,}", stripped):
        return True
    return False


def _strip_reasoning(text: str) -> str:
    """Some instruct models leak chain-of-thought before a </think> marker;
    keep only the final answer if that marker is present."""
    if "</think>" in text:
        return text.rsplit("</think>", 1)[-1].strip()
    return text.strip()


def classify_document(rel_path: str, text: str, cfg: dict) -> str:
    """Choose one taxonomy bucket for a document via the general LLM.

    Strict-choice prompting: the model is given the exact taxonomy list
    and told to reply with only a matching code. If nothing in the
    response matches a taxonomy entry (model error, malformed reply,
    endpoint down), the *last* taxonomy entry is used as a catch-all — by
    convention that should be a bucket like "99_MISC" in your own
    taxonomy, so an unclassifiable document is still found, just under an
    honestly-labelled bucket rather than lost.
    """
    taxonomy = cfg.get("taxonomy") or ["99_MISC"]
    catch_all = taxonomy[-1]
    sample = text[:1500]
    if len(text) > 1600:
        mid = len(text) // 2
        sample = text[:800] + "\n...\n" + text[mid : mid + 400] + "\n...\n" + text[-400:]

    llm_cfg = cfg.get("llm", {})
    messages = [
        {
            "role": "system",
            "content": "Classify the document into exactly ONE domain code from the list. Reply ONLY the code.",
        },
        {
            "role": "user",
            "content": "Domains: %s\n\nPath: %s\nExcerpt:\n%s\n\nDomain code:"
            % (", ".join(taxonomy), rel_path, sample[:1200]),
        },
    ]
    payload = {
        "model": llm_cfg.get("gen_model", "your-general-model"),
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.1, "top_p": 0.8, "top_k": 20, "num_predict": 20},
    }
    try:
        result = _post_json(llm_cfg.get("gen_url", "http://127.0.0.1:11434/api/chat"), payload, timeout=llm_cfg.get("timeout_s", 240))
        reply = (result.get("message", {}).get("content", "") or "").strip()
        for candidate in taxonomy:
            if candidate in reply:
                return candidate
    except Exception:
        pass
    return catch_all


def summarize_document(text: str, cfg: dict) -> str:
    """A few factual sentences describing what the document covers.

    Two attempts: instruct models occasionally return degenerate output
    (near-empty, or a repeated token loop) rather than a real error, so a
    single bad generation shouldn't leave a document with no summary at
    all when a second try would likely succeed.
    """
    llm_cfg = cfg.get("llm", {})
    messages = [
        {
            "role": "system",
            "content": (
                "Summarize this document in 2-3 sentences for an engineering "
                "knowledge base. State what equipment/system it covers. "
                "Factual, plain prose only."
            ),
        },
        {"role": "user", "content": text[:6000]},
    ]
    payload = {
        "model": llm_cfg.get("gen_model", "your-general-model"),
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.3, "top_p": 0.8, "top_k": 20, "num_predict": 800},
    }
    for _attempt in range(2):
        try:
            result = _post_json(llm_cfg.get("gen_url", "http://127.0.0.1:11434/api/chat"), payload, timeout=llm_cfg.get("timeout_s", 240))
            reply = _strip_reasoning(result.get("message", {}).get("content", "") or "")
            if not _is_degenerate_summary(reply):
                return reply
        except Exception:
            return ""
    return ""


def chunk_text(text: str, chunk_chars: int, overlap: int) -> list[str]:
    """Paragraph-boundary chunking with a hard character-count fallback.

    Splitting on blank-line paragraph boundaries keeps each chunk a
    coherent unit of prose rather than an arbitrary character slice. Only
    a single paragraph that is itself larger than ``chunk_chars`` falls
    back to a plain sliding window (with ``overlap`` characters of
    context repeated at each boundary) — that happens with dense tabular
    text blocks more than ordinary prose.
    """
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


def embed_chunk(text: str, cfg: dict) -> list[float]:
    """One embeddings call for one chunk, OpenAI-compatible request shape."""
    emb_cfg = cfg.get("embeddings", {})
    payload = {"model": emb_cfg.get("model", "your-embedding-model"), "input": text}
    result = _post_json(emb_cfg.get("url", "http://127.0.0.1:1234/v1/embeddings"), payload, timeout=cfg.get("llm", {}).get("timeout_s", 240))
    return result["data"][0]["embedding"]


# --------------------------------------------------------------------------
# extraction with OCR fallback
# --------------------------------------------------------------------------

def _extract_with_ocr_fallback(path: str, cfg: dict) -> tuple[str, str, int]:
    """Extract text, falling back to OCR for text-thin PDFs and all images.

    Returns ``(text, extractor_label, ocr_pages)``. A PDF is considered
    "text-thin" when its average characters-per-page falls under
    ``OCR_TEXT_THIN_CHARS_PER_PAGE`` — the same signal a human would use:
    a page with a real text layer has way more than 40 characters on it,
    so a PDF averaging less than that per page is almost certainly a scan.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTENSIONS:
        text, pages = ocrmod.transcribe_image(path, cfg)
        return text, "ocr", pages

    text, method = extractmod.extract_text(path, cfg)
    if ext == ".pdf":
        _full_text, chars_per_page = extractmod.extract_pdf_text(path)
        if chars_per_page and (sum(chars_per_page) / len(chars_per_page)) < OCR_TEXT_THIN_CHARS_PER_PAGE:
            ocr_text, pages = ocrmod.transcribe_pdf(path, cfg)
            if len(ocr_text) > len(text):
                return ocr_text, "ocr", pages
        return text, "text", 0
    return text, method, 0


# --------------------------------------------------------------------------
# per-document processing
# --------------------------------------------------------------------------

def _process_one(path: str, cfg: dict, con, dry_run: bool) -> dict:
    inbox = cfg["paths"]["inbox"]
    curated_root = cfg["paths"]["curated"]
    quarantine_root = cfg["paths"]["quarantine"]
    path = os.path.abspath(path)
    rel_path = os.path.relpath(path, inbox)
    basename = os.path.basename(path)

    pending = con.execute(
        "SELECT rel_path, dest_rel_path, sha256, state FROM ingest_pending WHERE src_path=?",
        (path,),
    ).fetchone()
    if pending:
        rel_path, pending_dest_rel, sha, _pending_state = pending
        curated_path = os.path.join(curated_root, pending_dest_rel)
        source_valid = _is_safe_inbox_file(path, inbox) and _verified_hash(path, sha)
        curated_valid = _verified_hash(curated_path, sha)
        if not source_valid and not curated_valid:
            raise RuntimeError("pending ingest has no valid source or curated copy")
        content_path = path if source_valid else curated_path
    else:
        pending_dest_rel = None
        if not _is_safe_inbox_file(path, inbox):
            return {"src": path, "action": "skip_unsafe", "reason": "not a regular file within inbox"}
        content_path = path

    if _is_ignorable_filename(basename):
        return {"src": path, "action": "skip_nondoc", "reason": "office-temp-or-dotfile"}

    ext = os.path.splitext(path)[1].lower()
    if ext not in DOC_EXTENSIONS:
        return {"src": path, "action": "skip_nondoc", "ext": ext}

    clean, reasons = secretsmod.scan_file(content_path)
    if not clean:
        if dry_run:
            return {"src": path, "action": "would_quarantine", "reasons": reasons}
        sha = _sha256_file(content_path)
        dest = _quarantine(path, quarantine_root, sha)
        return {"src": path, "action": "quarantine_secret", "reasons": reasons}

    sha = sha if pending else _sha256_file(content_path)
    existing = con.execute("SELECT id, rel_path FROM documents WHERE sha256=?", (sha,)).fetchone()
    if existing and not pending:
        existing_id, existing_rel = existing
        existing_curated = os.path.join(curated_root, existing_rel)
        if not _verified_hash(existing_curated, sha):
            cur = con.cursor()
            cur.execute("BEGIN")
            try:
                _remove_document(con, existing_id)
                con.commit()
            except Exception:
                con.rollback()
                raise
            existing = None
        else:
            if not _is_safe_inbox_file(path, inbox) or not _verified_hash(path, sha):
                return {"src": path, "action": "skip_unsafe", "reason": "duplicate source changed"}
            if not dry_run:
                os.remove(path)
            return {"src": path, "action": "skip_duplicate", "sha256": sha}
    if existing:
        if not dry_run:
            if _verified_hash(os.path.join(curated_root, existing[1]), sha):
                os.remove(path)
        return {"src": path, "action": "skip_duplicate", "sha256": sha}

    try:
        text, extractor, ocr_pages = _extract_with_ocr_fallback(content_path, cfg)
    except Exception as exc:
        raise RuntimeError("extraction failed for %s: %s" % (path, exc)) from exc

    if not text or len(text.strip()) < 20:
        return {"src": path, "action": "skip_empty", "extractor": extractor}

    clean, reasons = secretsmod.scan_file(content_path, text=text)
    if not clean:
        if dry_run:
            return {"src": path, "action": "would_quarantine", "reasons": reasons}
        dest = _quarantine(path, quarantine_root, sha)
        return {"src": path, "action": "quarantine_secret", "reasons": reasons}

    domain = classify_document(rel_path, text, cfg)
    summary = summarize_document(text, cfg)
    ingest_cfg = cfg.get("ingest", {})
    chunks = chunk_text(text, ingest_cfg.get("chunk_chars", 1800), ingest_cfg.get("chunk_overlap", 200))

    record = {
        "src": path,
        "rel_path": rel_path,
        "sha256": sha,
        "domain": domain,
        "extractor": extractor,
        "ocr_pages": ocr_pages,
        "chunks": len(chunks),
        "summary": summary,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if dry_run:
        record["action"] = "would_ingest"
        return record

    # Embed before opening the transaction -- never hold a DB transaction
    # open across a slow network call.
    embeddings = [embed_chunk(c, cfg) for c in chunks]

    if pending_dest_rel:
        dest_path = os.path.join(curated_root, pending_dest_rel)
        dest_dir = os.path.dirname(dest_path)
    else:
        dest_dir, dest_path, _stem = _safe_dest(curated_root, domain, rel_path, sha)

    dest_rel_path = os.path.relpath(dest_path, curated_root)
    dbmod.upsert_ingest_pending(
        con,
        src_path=path,
        rel_path=rel_path,
        dest_rel_path=dest_rel_path,
        sha256=sha,
        state="staged",
    )
    con.commit()

    if not _verified_hash(dest_path, sha):
        _promote_copy(content_path, dest_dir, dest_path)
    if not _verified_hash(dest_path, sha):
        raise OSError("curated copy failed hash verification")
    dbmod.set_ingest_pending_state(con, path, "curated")
    con.commit()

    cur = con.cursor()
    cur.execute("BEGIN")
    try:
        cur.execute(
            "INSERT INTO documents(source, rel_path, domain, sha256, summary, n_chunks, extractor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (basename, os.path.relpath(dest_path, curated_root), domain, sha, summary, len(chunks), extractor),
        )
        document_id = cur.lastrowid
        for seq, (chunk_body, vector) in enumerate(zip(chunks, embeddings)):
            cur.execute(
                "INSERT INTO chunks(document_id, seq, text) VALUES (?, ?, ?)",
                (document_id, seq, chunk_body),
            )
            chunk_id = cur.lastrowid
            cur.execute(
                "INSERT INTO vchunks(chunk_id, embedding) VALUES (?, ?)",
                (chunk_id, dbmod.serialize_f32(vector)),
            )
            dbmod.fts_insert(con, chunk_id, chunk_body)
        dbmod.set_ingest_pending_state(con, path, "indexed")
        con.commit()
    except Exception:
        con.rollback()
        raise

    if os.path.lexists(path):
        if not _is_safe_inbox_file(path, inbox) or not _verified_hash(path, sha):
            raise OSError("inbox source changed before verified cleanup")
        os.remove(path)
    dbmod.delete_ingest_pending(con, path)
    con.commit()
    record["action"] = "processed"
    record["dest"] = os.path.relpath(dest_path, curated_root)
    return record


def _reconcile_pending(cfg: dict, con) -> tuple[list[dict], list[str]]:
    """Repair durable file transitions before new inbox work begins."""
    inbox = cfg["paths"]["inbox"]
    curated_root = cfg["paths"]["curated"]
    records: list[dict] = []
    retry_paths: list[str] = []
    rows = con.execute(
        "SELECT src_path, rel_path, dest_rel_path, sha256, state FROM ingest_pending "
        "WHERE state != 'dead_letter' ORDER BY id"
    ).fetchall()
    for src_path, _rel_path, dest_rel_path, sha, state in rows:
        curated_path = os.path.join(curated_root, dest_rel_path)
        source_valid = _is_safe_inbox_file(src_path, inbox) and _verified_hash(src_path, sha)
        curated_valid = _verified_hash(curated_path, sha)
        document = con.execute(
            "SELECT id, rel_path FROM documents WHERE sha256=?", (sha,)
        ).fetchone()

        if document and curated_valid:
            if source_valid:
                try:
                    os.remove(src_path)
                except OSError as exc:
                    dbmod.set_ingest_pending_state(con, src_path, "indexed", str(exc)[:500])
                    con.commit()
                    continue
            dbmod.delete_ingest_pending(con, src_path)
            con.commit()
            records.append({"src": src_path, "action": "reconciled", "state": state})
            continue

        if document and not curated_valid:
            cur = con.cursor()
            cur.execute("BEGIN")
            try:
                _remove_document(con, document[0])
                dbmod.set_ingest_pending_state(
                    con, src_path, "staged" if source_valid else "dead_letter",
                    None if source_valid else "indexed rows exist but no valid source copy",
                )
                con.commit()
            except Exception:
                con.rollback()
                raise

        if curated_valid or source_valid:
            dbmod.set_ingest_pending_state(
                con, src_path, "curated" if curated_valid else "staged"
            )
            con.commit()
            retry_paths.append(src_path)
        else:
            dbmod.set_ingest_pending_state(
                con, src_path, "dead_letter", "no valid inbox or curated source copy"
            )
            con.commit()
    return records, retry_paths


# --------------------------------------------------------------------------
# resume state + main loop
# --------------------------------------------------------------------------

_TERMINAL_ACTIONS = {
    "processed", "quarantine_secret", "skip_nondoc", "skip_empty",
    "skip_duplicate", "skip_gone",
}


def _resume_state(cfg: dict) -> tuple[set[str], dict[str, int]]:
    """Read the manifest to find files already dealt with, and per-path
    attempt counts for files that previously errored (so a persistently
    broken file gets retried a bounded number of times, then skipped)."""
    done: set[str] = set()
    attempts: dict[str, int] = {}
    manifest_path = _manifest_path(cfg)
    if not os.path.exists(manifest_path):
        return done, attempts
    with open(manifest_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            src = record.get("src")
            action = record.get("action")
            if not src:
                continue
            if action in _TERMINAL_ACTIONS:
                done.add(src)
            else:
                attempts[src] = attempts.get(src, 0) + 1
    done |= {src for src, count in attempts.items() if count >= MAX_ATTEMPTS}
    return done, attempts


def _reset_database(cfg: dict) -> None:
    """Delete kb.db (and its WAL/SHM sidecar files). Only ever called when
    the caller passed reset_db=True AND confirm=True (see run_ingest) --
    a maintenance flag once wiped a live index by being left on in a
    script's default arguments, so this codebase requires it to be
    impossible to trigger by accident: two separate booleans, both explicit,
    both required, no environment-variable shortcut."""
    db_path = cfg["paths"]["db_path"]
    for suffix in ("", "-wal", "-shm"):
        candidate = db_path + suffix
        if os.path.exists(candidate):
            os.remove(candidate)


def run_ingest(
    cfg: dict,
    limit: int | None = None,
    dry_run: bool = False,
    reset_db: bool = False,
    confirm: bool = False,
) -> list[dict]:
    """Run the ingest pipeline over ``paths.inbox``.

    ``reset_db=True`` deletes the existing database before ingesting —
    this is destructive and irreversible, so it additionally requires
    ``confirm=True`` to actually happen. Passing ``reset_db=True`` alone
    raises ``ValueError`` rather than silently doing nothing or silently
    wiping the database; a maintenance flag that wipes a live index on a
    single accidental flag is a known way to lose a corpus, and the two
    -flags-required shape makes that require an unambiguous, deliberate
    call site rather than a stray CLI arg.

    Returns the list of per-document result records (the same records
    appended to the manifest when not in dry-run mode).
    """
    if reset_db:
        if not confirm:
            raise ValueError(
                "reset_db=True requires confirm=True — this deletes kb.db "
                "(and its -wal/-shm files). This guard exists because a "
                "maintenance flag once wiped a live index; make the call "
                "site say so explicitly."
            )

    lock_handle = None
    if not dry_run:
        lock_handle = _acquire_lock(cfg)
        if lock_handle is None:
            return [{"action": "locked", "reason": "another ingest run holds the lock"}]

    try:
        if reset_db and not dry_run:
            _reset_database(cfg)

        os.makedirs(cfg["paths"]["curated"], exist_ok=True)
        os.makedirs(cfg["paths"]["quarantine"], exist_ok=True)

        con = dbmod.connect(cfg["paths"]["db_path"])
        try:
            dbmod.init_schema(con, cfg["embeddings"]["dim"])

            reconciliation, pending_retries = _reconcile_pending(cfg, con)
            done, attempts = _resume_state(cfg)
            inbox_candidates = [
                p for p in _iter_inbox_files(cfg["paths"]["inbox"])
                if p not in done and os.path.splitext(p)[1].lower() in DOC_EXTENSIONS
                and not _is_ignorable_filename(os.path.basename(p))
            ]
            candidates = list(dict.fromkeys(pending_retries + inbox_candidates))

            results: list[dict] = list(reconciliation)
            processed = 0
            for path in sorted(candidates):
                try:
                    record = _process_one(path, cfg, con, dry_run)
                except Exception as exc:
                    attempt_count = attempts.get(path, 0) + 1
                    action = "dead_letter" if attempt_count >= MAX_ATTEMPTS else "error"
                    record = {"src": path, "action": action, "attempt": attempt_count, "error": str(exc)[:500]}

                results.append(record)
                if not dry_run:
                    _manifest_append(cfg, record)

                processed += 1
                if limit and processed >= limit:
                    break

            return results
        finally:
            con.close()
    finally:
        if lock_handle is not None:
            lock_handle.close()

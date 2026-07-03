"""Entity extraction — phase A of the three-phase equipment registry pipeline.

The pipeline (phases live in this package + ../dedupe.py):

  A. extract.py   RAW MENTIONS.  For every document, ask the gen LLM "what
                  equipment/systems does this text describe?" and store the
                  answer VERBATIM in `doc_entities`. This is a per-document,
                  per-model opinion -- it is not yet cross-referenced against
                  any other document.
  B. registry.py  DETERMINISTIC CANON.  Rebuild `equipment` + `doc_equipment`
                  from doc_entities using only high-confidence keys (identical
                  make+model, or an identical equipment/location tag). No LLM
                  judgement is trusted here -- just exact-match grouping.
  C. merge.py     LLM-ADJUDICATED PROPOSALS.  Registry entries that are
                  probably the same physical item under different labels
                  (e.g. a tag vs. its make/model) are proposed as merge
                  candidates for a human (or a high-confidence auto-apply
                  threshold) to confirm. Nothing is merged silently.

Why split this way: each phase has a different trust level, and separating
them means a bad LLM call in phase A can't silently corrupt the registry --
phase B only ever groups by keys it can verify itself, and phase C never
touches the registry without writing an auditable proposal row first.

Safety posture of THIS module:
  - non-destructive: doc_entities is populated once per document and never
    mutates documents/chunks/vchunks;
  - resumable: documents that already have a doc_entities row are skipped,
    so a batch can be stopped and restarted freely;
  - dry-run by default: `commit=False` prints what would be extracted without
    writing anything -- pass `commit=True` to persist;
  - the prompt explicitly instructs the model to use ONLY what the text
    states and invent nothing, and the JSON parser treats any output it
    cannot confidently parse as an extraction failure (skipped, not guessed).
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Any

from ..db import connect

__all__ = ["extract_entities"]

_MAXCHARS_DEFAULT = 2600
_MIN_INPUT_CHARS = 40

_SHAPE = {"equipment": [], "systems": [], "make": None, "model": None, "tags": [], "synonyms": []}

_PROMPT = """You are cataloguing technical documents for a knowledge base about physical \
assets and equipment. From the DOCUMENT below, extract the physical EQUIPMENT and SYSTEMS \
it describes, plus any manufacturer make/model and identifier codes. Use ONLY information \
explicitly present in the text. If something is unknown, use null or an empty list. Do not \
invent values.

Return STRICT JSON with exactly this shape:
{"equipment": [], "systems": [], "make": null, "model": null, "tags": [], "synonyms": []}

- equipment: specific items, e.g. "diesel generator", "circulation pump", "UPS", "chiller".
- systems: parent systems, e.g. "power generation", "HVAC", "fire detection".
- make / model: manufacturer and model designation if stated, e.g. "Aurora Power Systems" / "APG-500".
- tags: equipment or location codes present in the text, e.g. "DG1", "MSB-1", "FP-101", "=A1=PS01".
- synonyms: other names used for the same kit in this document.

DOCUMENT (source: %s):
%s
"""

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _post_json(url: str, payload: dict, timeout: int) -> dict:
    """Tiny stdlib-only JSON POST helper (no third-party HTTP client required)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_llm_json(text: str) -> dict[str, Any] | None:
    """Best-effort strict-JSON extraction from a chat model's free-form reply.

    Tries a plain parse first (the common case when `format: json` is
    honoured), then strips a fenced code block, then falls back to grabbing
    the first {...} span. Returns None (never a partial/guessed dict) if
    nothing parses -- callers must treat that as an extraction failure.
    """
    text = (text or "").strip()
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1].strip()
    for candidate in (text, *(m.group(1).strip() for m in _FENCE_RE.finditer(text))):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    m = _OBJECT_RE.search(text)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _normalise_shape(obj: dict[str, Any]) -> dict[str, Any]:
    """Coerce a parsed dict onto the exact expected shape, dropping junk keys."""
    out = dict(_SHAPE)
    for key in ("equipment", "systems", "tags", "synonyms"):
        v = obj.get(key)
        out[key] = [str(x) for x in v if isinstance(x, (str, int, float))] if isinstance(v, list) else []
    for key in ("make", "model"):
        v = obj.get(key)
        out[key] = str(v) if isinstance(v, (str, int, float)) and str(v).strip() else None
    return out


def _doc_input(con, document_id: int, summary: str | None, max_chars: int) -> str:
    rows = con.execute(
        "SELECT text FROM chunks WHERE document_id=? ORDER BY seq LIMIT 3", (document_id,)
    ).fetchall()
    body = "\n".join(r[0] or "" for r in rows)
    text = ((summary or "") + "\n\n" + body).strip()
    return text[:max_chars]


def _call_llm(cfg: dict, source: str, text: str) -> dict[str, Any] | None:
    llm = cfg.get("llm", {})
    gen_url = llm.get("gen_url", "http://127.0.0.1:11434/api/chat")
    model = llm.get("gen_model", "your-general-model")
    timeout = int(llm.get("timeout_s", 240))
    prompt = _PROMPT % (source[:80], text)

    is_ollama_native = gen_url.rstrip("/").endswith("/api/chat")
    if is_ollama_native:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1, "top_p": 0.8, "num_predict": 500},
        }
    else:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 500,
        }

    resp = _post_json(gen_url, payload, timeout=min(timeout, 180))
    if is_ollama_native:
        content = (resp.get("message", {}) or {}).get("content", "") or ""
    else:
        choices = resp.get("choices", [])
        content = (choices[0].get("message", {}).get("content", "") if choices else "") or ""

    obj = _parse_llm_json(content)
    if obj is None:
        return None
    return _normalise_shape(obj)


def extract_entities(cfg: dict, limit: int | None = None, commit: bool = False) -> dict[str, int]:
    """Run phase A over every document missing a `doc_entities` row.

    Dry-run by default (`commit=False`): prints each document's extraction
    without writing. Pass `commit=True` to persist rows. Resumable: already
    -extracted documents (present in doc_entities) are never revisited, so
    interrupting and re-running a batch is always safe.

    Returns {"candidates": N, "extracted": N, "failed": N}.
    """
    db_path = cfg["paths"]["db_path"]
    max_chars = int(cfg.get("ingest", {}).get("entity_maxchars", _MAXCHARS_DEFAULT))
    model = cfg.get("llm", {}).get("gen_model", "your-general-model")

    con = connect(db_path, read_only=False)
    try:
        rows = con.execute(
            """
            SELECT d.id, d.rel_path, d.summary
            FROM documents d
            WHERE d.id NOT IN (SELECT document_id FROM doc_entities)
              AND EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id)
            ORDER BY d.id
            """
        ).fetchall()
        if limit:
            rows = rows[:limit]

        ok = fail = 0
        for document_id, rel_path, summary in rows:
            text = _doc_input(con, document_id, summary, max_chars)
            if len(text) < _MIN_INPUT_CHARS:
                fail += 1
                continue
            try:
                obj = _call_llm(cfg, rel_path or "", text)
            except Exception as exc:  # noqa: BLE001 - one bad doc must never kill the batch
                fail += 1
                if not commit:
                    print("  [err] id=%s %s: %s" % (document_id, (rel_path or "")[:60], type(exc).__name__))
                continue
            if obj is None:
                fail += 1
                if not commit:
                    print("  [parse-fail] id=%s %s" % (document_id, (rel_path or "")[:60]))
                continue

            ok += 1
            if not commit:
                print("  id=%s %s" % (document_id, (rel_path or "")[:60]))
                print(
                    "     equipment=%s make=%s model=%s tags=%s"
                    % (obj["equipment"], obj["make"], obj["model"], obj["tags"])
                )
            else:
                con.execute(
                    "INSERT OR REPLACE INTO doc_entities (document_id, payload, model) VALUES (?, ?, ?)",
                    (document_id, json.dumps(obj, ensure_ascii=False), model),
                )
                con.commit()

        return {"candidates": len(rows), "extracted": ok, "failed": fail}
    finally:
        con.close()

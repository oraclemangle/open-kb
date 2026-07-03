"""LLM-adjudicated merge proposals — phase C of the three-phase pipeline.

See extract.py for the full three-phase overview. Phase B (registry.py)
groups by exact keys only, so the SAME physical item can end up as two
separate registry rows when it is described under two different naming
schemes -- e.g. a location tag ("DG1") in one document and a make+model
("Aurora Power Systems APG-500") in another, with no document stating both
at once. This phase finds those splits and proposes merging them.

Signal: DOCUMENT CO-OCCURRENCE. Two registry entries that are linked to
mostly the same set of documents are likely one physical kit described two
ways. Candidates must share at least `min_shared` documents AND a
containment ratio (shared / smaller side) of at least `min_ratio` --
config-with-defaults via function parameters, not hardcoded.

Precision-first guards, applied BEFORE ever asking the model:
  - same alphabetic prefix but a DIFFERENT trailing number (e.g. tag "DG1"
    vs tag "DG2") are different physical units by convention -- never
    proposed, regardless of co-occurrence;
  - two bare tag-only entries sharing documents are usually distinct kit
    referenced in the same procedure, not one item -- never proposed.

The LLM then adjudicates each surviving candidate to strict JSON
{"same": bool, "confidence": 0-1, "reason": "<=8 words"}, and DEFAULTS TO
NOT-SAME whenever it is unsure or its output fails to parse -- a missed
merge just means one extra registry row; a wrong merge silently blends two
pieces of equipment's document history, which is much worse.

Applying merges is intentionally a separate, explicit step (`apply_merges`):
proposals are written with status 'proposed' and NEVER touch the registry
themselves. A merge is only carried out for a proposal that is either
LLM-confirmed same at or above `min_conf`, or has been hand-set to
'approved' by an operator. This keeps the registry change-controlled and
auditable -- every merge traces back to a proposal row you can inspect.
"""
from __future__ import annotations

import json
import re
import urllib.request
from collections import Counter, defaultdict
from itertools import combinations
from typing import Any

from ..db import connect

__all__ = ["propose_merges", "apply_merges"]

_DEFAULT_MIN_SHARED = 3
_DEFAULT_MIN_RATIO = 0.5
_DEFAULT_MIN_CONF = 0.9

_NUMTAIL_RE = re.compile(r"^(.*?)(\d+)\s*$")

_PROMPT = """Two equipment registry entries from a technical knowledge base may or may not be \
the SAME physical equipment.
A: "%s"   (other names: %s)
B: "%s"   (other names: %s)
They are referenced together in %d documents.
SAME means one physical item under two labels -- e.g. a location tag and its make+model \
("DG1" and "Aurora Power Systems APG-500"), or two spellings of one name. DIFFERENT means \
separate units (e.g. "DG1" vs "DG2"), a part vs. the whole, or unrelated equipment.
Return strict JSON: {"same": true or false, "confidence": 0.0-1.0, "reason": "<=8 words"}.
If unsure, answer "same": false."""


def _post_json(url: str, payload: dict, timeout: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_llm_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1].strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else {}
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _different_unit(name_a: str, name_b: str) -> bool:
    """True if a,b are the same family but a different unit number (DG1 vs DG2)
    -- these must never be proposed as a merge."""
    ma, mb = _NUMTAIL_RE.match(name_a.strip()), _NUMTAIL_RE.match(name_b.strip())
    if ma and mb and ma.group(1).lower() == mb.group(1).lower() and ma.group(2) != mb.group(2):
        return True
    return False


def _is_tag_only(row: dict) -> bool:
    """A registry row is "tag-only" when it has no make/model -- i.e. it was
    keyed purely off an equipment/location tag in phase B."""
    return not row["make"] and not row["model"]


def _adjudicate(cfg: dict, a_name: str, a_aliases: list[str], b_name: str, b_aliases: list[str], shared: int) -> dict:
    llm = cfg.get("llm", {})
    gen_url = llm.get("gen_url", "http://127.0.0.1:11434/api/chat")
    model = llm.get("gen_model", "your-general-model")
    timeout = int(llm.get("timeout_s", 240))
    prompt = _PROMPT % (a_name, ", ".join(a_aliases[:6]), b_name, ", ".join(b_aliases[:6]), shared)

    is_ollama_native = gen_url.rstrip("/").endswith("/api/chat")
    if is_ollama_native:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1, "num_predict": 120},
        }
    else:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 120,
        }

    resp = _post_json(gen_url, payload, timeout=min(timeout, 120))
    if is_ollama_native:
        content = (resp.get("message", {}) or {}).get("content", "") or ""
    else:
        choices = resp.get("choices", [])
        content = (choices[0].get("message", {}).get("content", "") if choices else "") or ""
    return _parse_llm_json(content)


def _load_registry(con) -> dict[int, dict]:
    out = {}
    for row in con.execute("SELECT id, canonical_name, make, model, aliases_json, tags_json FROM equipment"):
        eid, canonical_name, make, model, aliases_json, tags_json = row
        try:
            aliases = json.loads(aliases_json or "[]")
        except (json.JSONDecodeError, ValueError):
            aliases = []
        out[eid] = {"canonical_name": canonical_name, "make": make, "model": model, "aliases": aliases}
    return out


def propose_merges(
    cfg: dict,
    limit: int | None = None,
    adjudicate: bool = True,
    min_shared: int = _DEFAULT_MIN_SHARED,
    min_ratio: float = _DEFAULT_MIN_RATIO,
) -> dict[str, int]:
    """Find co-occurrence candidates and (optionally) have the LLM adjudicate them.

    Writes one `equipment_merge_proposal` row per surviving candidate pair
    (status 'proposed'). Does NOT touch `equipment` / `doc_equipment` --
    call `apply_merges` separately to act on confirmed proposals.

    Returns {"candidates": N, "proposed": N, "llm_same": N}.
    """
    db_path = cfg["paths"]["db_path"]
    con = connect(db_path, read_only=False)
    try:
        registry = _load_registry(con)

        eq_docs: dict[int, set] = defaultdict(set)
        doc_eqs: dict[int, set] = defaultdict(set)
        for equipment_id, document_id in con.execute("SELECT equipment_id, document_id FROM doc_equipment"):
            eq_docs[equipment_id].add(document_id)
            doc_eqs[document_id].add(equipment_id)

        pair_shared: Counter = Counter()
        for document_id, eids in doc_eqs.items():
            for a, b in combinations(sorted(eids), 2):
                pair_shared[(a, b)] += 1

        candidates = []
        for (a, b), shared in pair_shared.items():
            if shared < min_shared:
                continue
            ratio = shared / max(1, min(len(eq_docs[a]), len(eq_docs[b])))
            if ratio < min_ratio:
                continue
            a_row, b_row = registry.get(a), registry.get(b)
            if not a_row or not b_row:
                continue
            if _different_unit(a_row["canonical_name"], b_row["canonical_name"]):
                continue
            if _is_tag_only(a_row) and _is_tag_only(b_row):
                continue  # two bare tags sharing docs are usually distinct kit, not one item
            candidates.append((a, b, shared, round(ratio, 2)))
        candidates.sort(key=lambda c: -c[2])
        if limit:
            candidates = candidates[:limit]

        n_same = 0
        for a, b, shared, ratio in candidates:
            same = conf = reason = None
            if adjudicate:
                try:
                    verdict = _adjudicate(
                        cfg, registry[a]["canonical_name"], registry[a]["aliases"],
                        registry[b]["canonical_name"], registry[b]["aliases"], shared,
                    )
                    same = 1 if verdict.get("same") else 0
                    conf = float(verdict.get("confidence") or 0)
                    reason = str(verdict.get("reason") or "")[:80]
                except Exception as exc:  # noqa: BLE001 - default to not-same on any failure
                    same, conf, reason = 0, 0.0, "err:" + type(exc).__name__
            con.execute(
                "INSERT INTO equipment_merge_proposal "
                "(a_id, b_id, shared_docs, llm_same, llm_conf, llm_reason, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'proposed')",
                (a, b, shared, same, conf, reason),
            )
            con.commit()
            if same == 1:
                n_same += 1

        return {"candidates": len(candidates), "proposed": len(candidates), "llm_same": n_same}
    finally:
        con.close()


def apply_merges(cfg: dict, min_conf: float = _DEFAULT_MIN_CONF) -> int:
    """Collapse confirmed merge proposals into the registry.

    A proposal is acted on when EITHER:
      - llm_same == 1 AND llm_conf >= min_conf, OR
      - status == 'approved' (a human confirmed it regardless of the LLM verdict).

    For each, the entry with the richer identity (make/model over tag-only,
    then more linked documents) is kept as canonical; the other's aliases,
    tags and document links are folded into the keeper and it is deleted.
    Proposal rows are marked 'applied'. Idempotent: re-running only ever
    processes rows still in 'proposed'/'approved' state.

    Returns the number of merges applied.
    """
    db_path = cfg["paths"]["db_path"]
    con = connect(db_path, read_only=False)
    try:
        rows = con.execute(
            "SELECT id, a_id, b_id FROM equipment_merge_proposal "
            "WHERE status IN ('proposed', 'approved') "
            "AND ((llm_same = 1 AND llm_conf >= ?) OR status = 'approved')",
            (min_conf,),
        ).fetchall()

        applied = 0
        for proposal_id, a_id, b_id in rows:
            a = con.execute(
                "SELECT make, model, canonical_name, aliases_json, tags_json FROM equipment WHERE id=?", (a_id,)
            ).fetchone()
            b = con.execute(
                "SELECT make, model, canonical_name, aliases_json, tags_json FROM equipment WHERE id=?", (b_id,)
            ).fetchone()
            if not a or not b:
                con.execute("UPDATE equipment_merge_proposal SET status='applied' WHERE id=?", (proposal_id,))
                con.commit()
                continue

            a_doc_count = con.execute(
                "SELECT COUNT(*) FROM doc_equipment WHERE equipment_id=?", (a_id,)
            ).fetchone()[0]
            b_doc_count = con.execute(
                "SELECT COUNT(*) FROM doc_equipment WHERE equipment_id=?", (b_id,)
            ).fetchone()[0]

            a_is_tag_only = not a[0] and not a[1]
            b_is_tag_only = not b[0] and not b[1]
            if a_is_tag_only and not b_is_tag_only:
                keep_id, drop_id, keep, drop = b_id, a_id, b, a
            elif b_is_tag_only and not a_is_tag_only:
                keep_id, drop_id, keep, drop = a_id, b_id, a, b
            elif a_doc_count >= b_doc_count:
                keep_id, drop_id, keep, drop = a_id, b_id, a, b
            else:
                keep_id, drop_id, keep, drop = b_id, a_id, b, a

            try:
                keep_aliases = set(json.loads(keep[3] or "[]"))
                drop_aliases = set(json.loads(drop[3] or "[]"))
            except (json.JSONDecodeError, ValueError):
                keep_aliases, drop_aliases = set(), set()
            try:
                keep_tags = set(json.loads(keep[4] or "[]"))
                drop_tags = set(json.loads(drop[4] or "[]"))
            except (json.JSONDecodeError, ValueError):
                keep_tags, drop_tags = set(), set()

            merged_aliases = sorted(x for x in (keep_aliases | drop_aliases | {drop[2]}) if x)[:60]
            merged_tags = sorted(x for x in (keep_tags | drop_tags) if x)[:60]
            con.execute(
                "UPDATE equipment SET aliases_json=?, tags_json=? WHERE id=?",
                (json.dumps(merged_aliases, ensure_ascii=False), json.dumps(merged_tags, ensure_ascii=False), keep_id),
            )

            for (document_id,) in con.execute("SELECT document_id FROM doc_equipment WHERE equipment_id=?", (drop_id,)):
                con.execute(
                    "INSERT OR IGNORE INTO doc_equipment (equipment_id, document_id) VALUES (?, ?)",
                    (keep_id, document_id),
                )
            con.execute("DELETE FROM doc_equipment WHERE equipment_id=?", (drop_id,))
            con.execute("DELETE FROM equipment WHERE id=?", (drop_id,))
            con.execute("UPDATE equipment_merge_proposal SET status='applied' WHERE id=?", (proposal_id,))
            con.commit()
            applied += 1

        return applied
    finally:
        con.close()

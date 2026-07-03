"""Deterministic equipment registry — phase B of the three-phase pipeline.

See extract.py for the full three-phase overview. This phase turns the raw,
per-document LLM opinions in `doc_entities` (phase A) into a canonical
`equipment` registry, using ONLY keys it can verify by exact string match:

  - identical normalised make+model  (e.g. "Aurora Power Systems" + "APG-500"
    said by three different documents is clearly one entity), or
  - identical equipment/location tag matching a generic tag grammar (letter-
    prefix+number codes like `DG1` / `MSB-1` / `FP-101`, or structured codes
    like `=A1=PS01`).

No LLM judgement is trusted in this phase -- that is deliberate. Grouping by
exact match is auditable and idempotent; grouping by "the model thinks these
are the same thing" belongs in phase C (merge.py), where it produces a
proposal row rather than a silent registry mutation.

Non-destructive to documents/chunks/vchunks. IDEMPOTENT: this rebuilds
`equipment` + `doc_equipment` from scratch every run, so it is always safe
to re-run after a fresh extraction batch -- there is no drift between runs
beyond what doc_entities itself contains.
"""
from __future__ import annotations

import json
import re
from collections import Counter

from ..db import connect

__all__ = ["build_registry"]

# Generic location/equipment tag grammar: letter-prefix + number (DG1, MSB-1,
# FP-101), or a structured "=<char><digit>=<letters><digits>" code such as
# building-services RDS-style tags (=A1=PS01). Deliberately generic -- this
# is a grammar, not a list of real-world tag values.
_TAG_RE = re.compile(r"^(=[A-Za-z]\d=[A-Za-z]{2}\d{2}|[A-Za-z]{2,4}[-_]?\d{1,3})$", re.IGNORECASE)

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(b\.?\s*v\.?|ltd\.?|gmbh|inc\.?|s\.?a\.?|n\.?v\.?|corp\.?|co\.?|limited|holding|group)\b",
    re.IGNORECASE,
)


def _clean(s: object) -> str | None:
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s or s[0] in "{[" or s.lower() in ("null", "none", "unknown", "n/a"):
        return None
    return re.sub(r"\s+", " ", s)


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _norm_make(make: str | None) -> str | None:
    """Collapse legal-suffix / spacing variants so e.g. 'Aurora Power Systems, Inc.'
    and 'Aurora Power Systems' are treated as the same make."""
    if not make:
        return make
    s = _LEGAL_SUFFIX_RE.sub("", make)
    s = re.sub(r"\s+", " ", s).strip(" .,-")
    return s or make


def _as_str_list(payload: object) -> list[str]:
    return [x for x in payload if isinstance(x, str)] if isinstance(payload, list) else []


def build_registry(cfg: dict) -> dict[str, int]:
    """Rebuild `equipment` + `doc_equipment` from `doc_entities` (idempotent).

    Returns a summary dict: {"equipment": N, "links": N, "multi_doc": N}.
    """
    db_path = cfg["paths"]["db_path"]
    con = connect(db_path, read_only=False)
    try:
        rows = con.execute("SELECT document_id, payload FROM doc_entities").fetchall()

        # key -> {"canonical_name","make","model","tags":set,"aliases":set,"docs":dict[doc_id]->basis}
        groups: dict[tuple, dict] = {}

        for document_id, payload in rows:
            try:
                obj = json.loads(payload) if payload else {}
            except (json.JSONDecodeError, ValueError):
                obj = {}
            equipment = _as_str_list(obj.get("equipment"))
            synonyms = _as_str_list(obj.get("synonyms"))
            tags = _as_str_list(obj.get("tags"))
            make = _norm_make(_clean(obj.get("make")))
            model = _clean(obj.get("model"))

            keys: list[tuple] = []
            if make and model:
                key = ("make-model", _norm(make + " " + model))
                keys.append((key, "%s %s" % (make, model), "make-model", make, model))
            elif make and equipment:
                key = ("make-equipment", _norm(make + " " + equipment[0]))
                keys.append((key, "%s %s" % (make, equipment[0]), "make-equipment", make, None))
            for tag in tags:
                tag = tag.strip()
                if _TAG_RE.match(tag):
                    key = ("tag", _norm(tag))
                    keys.append((key, tag, "tag", None, None))

            for key, canonical_name, basis, mk, md in keys:
                g = groups.setdefault(
                    key,
                    {
                        "canonical_name": canonical_name,
                        "basis": basis,
                        "make": mk,
                        "model": md,
                        "tag": canonical_name if basis == "tag" else None,
                        "aliases": set(),
                        "tags": set(),
                        "docs": {},
                    },
                )
                for alias in equipment + synonyms:
                    if alias and len(alias) < 60:
                        g["aliases"].add(alias)
                for tag in tags:
                    if tag:
                        g["tags"].add(tag)
                g["docs"][document_id] = basis

        con.execute("DELETE FROM doc_equipment")
        con.execute("DELETE FROM equipment")

        ordered = sorted(groups.values(), key=lambda g: -len(g["docs"]))
        for g in ordered:
            cur = con.execute(
                "INSERT INTO equipment (canonical_name, make, model, aliases_json, tags_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    g["canonical_name"],
                    g["make"],
                    g["model"],
                    json.dumps(sorted(g["aliases"])[:40], ensure_ascii=False),
                    json.dumps(sorted(g["tags"])[:40], ensure_ascii=False),
                ),
            )
            equipment_id = cur.lastrowid
            for document_id in g["docs"]:
                con.execute(
                    "INSERT OR IGNORE INTO doc_equipment (equipment_id, document_id) VALUES (?, ?)",
                    (equipment_id, document_id),
                )
        con.commit()

        n_equipment = con.execute("SELECT COUNT(*) FROM equipment").fetchone()[0]
        n_links = con.execute("SELECT COUNT(*) FROM doc_equipment").fetchone()[0]
        n_multi = sum(1 for g in groups.values() if len(g["docs"]) > 1)
        return {"equipment": n_equipment, "links": n_links, "multi_doc": n_multi}
    finally:
        con.close()

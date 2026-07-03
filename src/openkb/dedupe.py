"""Near-duplicate detection and revision supersession.

Two related but distinct problems, both solved without ever deleting a row
from the database:

  1. NEAR-DUPLICATES -- two documents whose CONTENT overlaps heavily (a
     renamed re-export, a byte-different copy, a near-identical revision)
     produce competing chunks for the same fact. That is bad for retrieval:
     the reranker and RRF fusion both have to arbitrate between near-
     identical passages instead of one authoritative one, and if the older
     copy happens to rank first the answer can be stale or contradictory.
     `find_near_dups` reports clusters using pure-stdlib shingle Jaccard
     similarity over normalised chunk text -- no embedding model required,
     so this works even on a KB with no configured embedding endpoint.

  2. REVISION FAMILIES -- many source trees keep every historical revision
     of a document (`...rev1.pdf`, `...rev2.pdf`, `...v1.3.pdf`, dated
     re-issues). All of them get ingested (ingest should never silently
     drop a document), but retrieval should prefer the LATEST revision.
     `find_revision_families` groups documents whose normalised filename
     differs only by a revision/version/date marker and identifies the
     newest member of each family.

WHY EXCLUSION-LIST, NOT DELETION: `supersede()` does not touch the
`documents` table at all. It appends the affected `rel_path` values to a
text file, `superseded.txt`, living next to the database file (same
directory as `paths.db_path`). The retrieval engine is expected to load
this file (`load_superseded`) and exclude any listed path from search
results. This is deliberately reversible and low-risk:

  - reversible: deleting a line from superseded.txt immediately restores
    that document to retrieval -- no re-ingest, no schema change, no risk
    of having lost the original row;
  - resumable / idempotent: re-running supersede() with paths already in
    the file is a no-op for those paths (duplicates are not appended
    twice);
  - safe under a live read-replica: replica sync processes are expected to
    gate a sync on this file's mtime alongside the database's, so a
    superseded-list update propagates atomically with (or after) the data
    it depends on -- a replica can never see the exclusion without also
    having the documents it excludes;
  - auditable: every line names WHY the document was excluded and, for
    revision supersession, what replaced it -- a plain grep answers "why
    isn't X showing up in search?".

Everything here is read-mostly: detection opens the database read-only,
and `commit=False` (the default) never writes to superseded.txt either --
it only returns the plan so a caller can review before applying it.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict

from .db import connect

__all__ = [
    "load_superseded",
    "supersede",
    "find_near_dups",
    "find_revision_families",
]

_WORD_RE = re.compile(r"[a-z0-9]+")
_SHINGLE_K = 5
_SHINGLE_CAP = 800          # bound cost on long documents; a high-overlap dup still shares plenty
_MIN_SIG_CHARS = 300        # very short documents (e.g. photo OCR stubs) cluster spuriously — skip

_N_HASH = 64
_MASK = (1 << 32) - 1
_SEEDS = [(i * 2654435761) & _MASK for i in range(_N_HASH)]

# Filename revision/version markers, in priority order. Each captures enough
# to (a) strip it from the stem so revisions of one document collapse
# together, and (b) build a sortable "which one is newer" key.
_VER_DOT_RE = re.compile(r"(?<![A-Za-z])v\.?\s*(\d+)\.(\d+)\b", re.IGNORECASE)
_REV_LETTER_RE = re.compile(r"(?<![A-Za-z])rev[\s._-]*([A-Za-z])(\d*)\b", re.IGNORECASE)
_REV_NUM_RE = re.compile(r"(?<![A-Za-z])rev[\s._-]*(\d+)\b", re.IGNORECASE)
_DATE8_RE = re.compile(r"\b(20\d{6})\b")
_DATED_RE = re.compile(r"\b(\d{2})[-_.](\d{2})[-_.](20\d{2})\b")
_COPY_RE = re.compile(r"\(\d+\)")


def superseded_path(db_path: str) -> str:
    """Path to the exclusion-list file: `superseded.txt` next to the DB file."""
    return os.path.join(os.path.dirname(os.path.abspath(os.path.expanduser(db_path))), "superseded.txt")


def load_superseded(db_path: str) -> set[str]:
    """Return the set of excluded rel_paths (empty set if the file is absent).

    File format: one rel_path per line; everything after '#' is a comment.
    """
    path = superseded_path(db_path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            out = set()
            for line in fh:
                rel = line.split("#", 1)[0].strip()
                if rel:
                    out.add(rel)
            return out
    except OSError:
        return set()


def supersede(cfg: dict, rel_paths: list[str], reason: str = "", commit: bool = False) -> list[str]:
    """Append `rel_paths` to superseded.txt (dry-run by default).

    Paths already listed are skipped (idempotent). Returns the list of
    rel_paths newly appended (or that WOULD be appended, when
    `commit=False`) so callers can report what changed either way.
    """
    db_path = cfg["paths"]["db_path"]
    path = superseded_path(db_path)
    existing = load_superseded(db_path)
    new = [rel for rel in rel_paths if rel not in existing]
    if commit and new:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for rel in new:
                suffix = ("  # %s" % reason) if reason else ""
                fh.write("%s%s\n" % (rel, suffix))
    return new


# --------------------------------------------------------------------------
# 1. Near-duplicate content detection (MinHash + LSH over shingled chunk text)
# --------------------------------------------------------------------------

def _normalise_text(text: str) -> str:
    return " ".join(_WORD_RE.findall((text or "").lower()))


def _shingles(tokens: list[str], k: int = _SHINGLE_K) -> set[str]:
    if len(tokens) < k:
        return {" ".join(tokens)} if tokens else set()
    shingle_set = {" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1)}
    if len(shingle_set) > _SHINGLE_CAP:
        # deterministic bottom-k sample so cost is bounded but the result is stable across runs
        shingle_set = set(
            sorted(shingle_set, key=lambda s: hashlib.blake2b(s.encode(), digest_size=4).digest())[:_SHINGLE_CAP]
        )
    return shingle_set


def _minhash(shingle_set: set[str]) -> tuple[int, ...] | None:
    if not shingle_set:
        return None
    sig = [_MASK] * _N_HASH
    for shingle in shingle_set:
        h = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=4).digest(), "big")
        for i in range(_N_HASH):
            v = (h ^ _SEEDS[i]) & _MASK
            if v < sig[i]:
                sig[i] = v
    return tuple(sig)


def _jaccard_estimate(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def find_near_dups(cfg: dict, threshold: float = 0.80) -> list[list[dict]]:
    """Report clusters of exact or near-duplicate documents (read-only, no writes).

    Two independent signals, fused with union-find:
      - EXACT: identical normalised chunk text (a byte-different re-export).
      - NEAR: MinHash + LSH text similarity >= `threshold` (renamed-but-
        similar content).

    Returns a list of clusters (each a list of document dicts: id, rel_path,
    domain, chars), largest first. Does not modify anything -- combine with
    `supersede()` if you decide to act on a cluster.
    """
    db_path = cfg["paths"]["db_path"]
    con = connect(db_path, read_only=True)
    try:
        docs: dict[int, dict] = {
            row[0]: {"rel_path": row[1], "domain": row[2], "text": []}
            for row in con.execute("SELECT id, rel_path, domain FROM documents")
        }
        for document_id, text in con.execute("SELECT document_id, text FROM chunks ORDER BY document_id, seq"):
            d = docs.get(document_id)
            if d is not None:
                d["text"].append(text or "")
    finally:
        con.close()

    for d in docs.values():
        normalised = _normalise_text(" ".join(d["text"]))
        d["chars"] = len(normalised)
        d["exact_hash"] = hashlib.sha1(normalised.encode()).hexdigest() if normalised else None
        d["sig"] = _minhash(_shingles(normalised.split())) if len(normalised) >= _MIN_SIG_CHARS else None
        del d["text"]

    uf = _UnionFind()

    by_exact: dict[str, list[int]] = defaultdict(list)
    for document_id, d in docs.items():
        if d["exact_hash"] and d["chars"] > 40:
            by_exact[d["exact_hash"]].append(document_id)
    for group in by_exact.values():
        for other in group[1:]:
            uf.union(group[0], other)

    bands, rows_per_band = 16, _N_HASH // 16
    buckets: dict[tuple, list[int]] = defaultdict(list)
    for document_id, d in docs.items():
        if not d["sig"]:
            continue
        for b in range(bands):
            key = (b,) + d["sig"][b * rows_per_band : (b + 1) * rows_per_band]
            buckets[key].append(document_id)
    checked: set[tuple[int, int]] = set()
    for group in buckets.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                key = (group[i], group[j])
                if key in checked:
                    continue
                checked.add(key)
                if _jaccard_estimate(docs[group[i]]["sig"], docs[group[j]]["sig"]) >= threshold:
                    uf.union(group[i], group[j])

    clustered: dict[int, list[int]] = defaultdict(list)
    for document_id in docs:
        clustered[uf.find(document_id)].append(document_id)

    clusters = []
    for members in clustered.values():
        if len(members) < 2:
            continue
        clusters.append(
            sorted(
                (
                    {"id": m, "rel_path": docs[m]["rel_path"], "domain": docs[m]["domain"], "chars": docs[m]["chars"]}
                    for m in members
                ),
                key=lambda d: -d["chars"],
            )
        )
    clusters.sort(key=len, reverse=True)
    return clusters


# --------------------------------------------------------------------------
# 2. Revision families (supersede all but the latest revision)
# --------------------------------------------------------------------------

def _parse_revision(name: str) -> tuple[str, tuple, str] | None:
    """Return (kind, sortkey, label) for the first EXPLICIT revision marker
    found in `name`, or None. Sortkeys are only ever compared WITHIN one
    kind, so mixing conventions never produces a bogus ordering."""
    m = _REV_LETTER_RE.search(name)
    if m:
        return ("rev-letter", (ord(m.group(1).upper()), int(m.group(2) or 0)), "rev%s%s" % (m.group(1), m.group(2)))
    m = _REV_NUM_RE.search(name)
    if m:
        return ("rev-num", (int(m.group(1)),), "rev%s" % m.group(1))
    m = _VER_DOT_RE.search(name)
    if m:
        return ("version", (int(m.group(1)), int(m.group(2))), "v%s.%s" % m.groups())
    m = _DATED_RE.search(name)
    if m:
        return ("date", (int(m.group(3) + m.group(2) + m.group(1)),), "%s-%s-%s" % m.groups())
    m = _DATE8_RE.search(name)
    if m:
        return ("date", (int(m.group(1)),), m.group(1))
    m = _COPY_RE.search(name)
    if m:
        return ("copy", (int(re.sub(r"\D", "", m.group(0)) or 0),), m.group(0))
    return None


def _stem(name: str, kind: str) -> str:
    """Normalised stem with the revision token (and dates/copy-markers)
    stripped, so every revision of one document collapses to one key."""
    s = os.path.splitext(name)[0]
    for rx in (_REV_LETTER_RE, _REV_NUM_RE, _VER_DOT_RE, _DATED_RE, _DATE8_RE, _COPY_RE):
        s = rx.sub(" ", s)
    return re.sub(r"[ _.\-]+", " ", s).strip().lower()


def find_revision_families(cfg: dict) -> list[dict]:
    """Group documents whose filename differs only by a revision/version/date
    marker (read-only, no writes).

    A family is only reported when it has 2+ members sharing a normalised
    stem AND file extension AND revision convention (a .pdf and .xlsx never
    group; a "rev" marker never groups with a "v1.2" marker). Returns a list
    of {"stem", "kind", "keep": {...}, "supersede": [{...}, ...]} -- "keep"
    is the highest-revision member; "supersede" lists the rest, each with
    its own rel_path/sha256, ready to hand to `supersede()`.

    Guards (never act without these holding):
      - stems shorter than 6 chars are ignored (avoids trivial collisions);
      - a member whose revision key EQUALS the kept one is held out, not
        superseded (same revision as the kept doc -- likely a duplicate
        copy or an unrelated document the stem happened to catch);
      - a member carrying more than double the kept latest's chunk count is
        held out too (the "latest" may be a stub/cover page -- do not hide
        the substantive older document).
    """
    db_path = cfg["paths"]["db_path"]
    con = connect(db_path, read_only=True)
    try:
        rows = con.execute("SELECT rel_path, sha256, n_chunks FROM documents").fetchall()
    finally:
        con.close()

    families: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for rel_path, sha256, n_chunks in rows:
        base = os.path.basename(rel_path or "")
        ext = os.path.splitext(base)[1].lower()
        parsed = _parse_revision(base)
        if not parsed:
            continue
        kind, sortkey, label = parsed
        stem = _stem(base, kind)
        if len(stem) < 6:
            continue
        families[(stem, ext, kind)].append(
            {"rel_path": rel_path, "sha256": sha256, "n_chunks": n_chunks or 0, "sortkey": sortkey, "label": label}
        )

    out = []
    for (stem, ext, kind), members in families.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: m["sortkey"], reverse=True)
        keep = members[0]
        held, supersede_list = [], []
        for m in members[1:]:
            if m["sortkey"] == keep["sortkey"]:
                held.append({**m, "why": "same revision as the kept document"})
                continue
            if m["n_chunks"] > 2 * max(1, keep["n_chunks"]):
                held.append({**m, "why": "older revision has far more text than the kept latest — possible stub"})
                continue
            supersede_list.append(m)
        out.append({"stem": stem, "ext": ext, "kind": kind, "keep": keep, "supersede": supersede_list, "held": held})

    out.sort(key=lambda f: -len(f["supersede"]))
    return out

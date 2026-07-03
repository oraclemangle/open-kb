"""Retrieval engine -- the single source of truth for turning a query into
ranked chunks (search) or a cited answer (ask).

APPROACH: hybrid retrieval, reciprocal-rank fusion, optional rerank.

  1. VECTOR arm: embed the query (any OpenAI-compatible embeddings endpoint)
     and run a KNN search over `vchunks` (sqlite-vec). Good at "meaning"
     matches -- synonyms, paraphrase, cross-lingual-ish recall.
  2. LEXICAL arm: FTS5 MATCH over `chunks_fts`. Good at exact terms --
     part numbers, error codes, tag names -- that an embedding can blur.
  3. FUSION: reciprocal-rank fusion (RRF). Instead of trying to make vector
     distances and BM25 scores commensurate (they aren't), RRF only looks at
     each arm's *rank order* and sums 1/(rrf_k + rank) across arms. This is
     simple, has no tunable except rrf_k, and is a well-known strong
     baseline precisely because it sidesteps the score-normalisation
     problem entirely.
  4. Optional ENTITY BOOST: if the corpus has an equipment/alias registry,
     a query naming a specific piece of equipment gets a small additive
     nudge for chunks belonging to that equipment's linked documents. This
     is NOT query expansion (the query text is never rewritten -- expansion
     tends to add noise words that hurt lexical precision); it only
     re-orders what hybrid retrieval already found. Off by default --
     MEASURE it against your own gold set before flipping it on. In our own
     evaluation this measured a small but real MRR gain with no regressions;
     that is a property of one corpus and one query mix, not a universal law.
  5. SUPERSEDED-REVISION EXCLUSION: a knowledge base of real documents
     accumulates revisions (v1, v2, "final", "final-FINAL"...). Chunks
     belonging to a document listed in `superseded.txt` (next to the DB
     file) never surface, so an old drawing doesn't shadow the current one.
  6. Optional RERANK (see rerank.py): re-orders the fused pool using a model
     that actually reads the candidate text, then the result is cut to k.

Honesty note: every "optional" step above is a lever, not a law. Enabling
it changes measured recall@k/MRR on YOUR corpus in ways that don't
necessarily transfer from anyone else's writeup (including this one). The
canonical way to decide is `openkb eval` (see evaluate.py) -- run it before
and after flipping a flag, keep the change only if the numbers improve.

ask() builds on search(): it retrieves a fused, reranked pool, formats it as
numbered context blocks, and asks the configured generation model to answer
using ONLY that context with inline [n] citations. Retrieval failures still
return no answer; generation failures degrade to a message that surfaces the
citations anyway -- a slow or down LLM should never look like "nothing was
found" when the KB in fact has hits for the question.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from .config import load_config
from .db import connect, serialize_f32
from .rerank import rerank as _rerank

__all__ = ["search", "ask"]

# Alias tokens shorter than this, or in this stopword set, are too generic to
# safely trigger an entity boost (e.g. "power" would match almost anything).
_ENTITY_STOPWORDS = {
    "system", "systems", "cable", "panel", "control", "manual", "drawing",
    "unit", "general", "battery", "power", "supply", "module", "sensor",
    "device", "equipment", "switchboard", "facility", "asset", "electrical",
    "installation", "monitor", "monitoring",
}

_ALIAS_MAP_CACHE: dict[str, set[int]] | None = None  # alias(lower) -> {document_id}

_SUPERSEDED_CACHE: dict[str, Any] = {"mtime": -1.0, "set": set()}


# ---------------------------------------------------------------------------
# low-level HTTP helpers (stdlib only -- no `requests` dependency)
# ---------------------------------------------------------------------------

def _post_json(url: str, payload: dict, timeout: int = 60) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _embed(query: str, cfg: dict) -> list[float]:
    """Embed a query via an OpenAI-compatible /v1/embeddings endpoint."""
    emb_cfg = cfg.get("embeddings", {})
    url = emb_cfg.get("url", "http://127.0.0.1:1234/v1/embeddings")
    model = emb_cfg.get("model", "your-embedding-model")
    resp = _post_json(url, {"model": model, "input": query}, timeout=90)
    return resp["data"][0]["embedding"]


def _fts_escape(query: str) -> str:
    """Build a safe FTS5 MATCH expression from free text.

    FTS5's query syntax treats many punctuation characters specially, so we
    never hand raw user text to MATCH. Instead we tokenize to alphanumerics
    (plus a few identifier-friendly characters), quote each token, and OR
    them together -- this survives arbitrary punctuation in the query
    without ever raising a syntax error from FTS5 itself.
    """
    terms = re.findall(r"[A-Za-z0-9_./-]+", query or "")
    if not terms:
        return '""'
    quoted = [t.replace('"', '""') for t in terms]
    return " OR ".join('"%s"' % t for t in quoted)


# ---------------------------------------------------------------------------
# superseded-revision exclusion
# ---------------------------------------------------------------------------

def _superseded_path(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "superseded.txt")


def _load_superseded_file(db_path: str) -> set[str]:
    path = _superseded_path(db_path)
    if not os.path.isfile(path):
        return set()
    out: set[str] = set()
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                out.add(line)
    return out


def _superseded_rel_paths(db_path: str) -> set[str]:
    """rel_path set of documents.rel_path listed in <db dir>/superseded.txt.

    mtime-cached so a normal query pays roughly one stat() call, not a file
    re-read. A missing file is simply "nothing superseded" (empty set); a
    file that exists but can't be read is a louder problem -- it means the
    exclusion list is present but broken, so we complain to stderr and fail
    open (retrieval keeps working, just without the exclusion) rather than
    crash the whole search.
    """
    path = _superseded_path(db_path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _SUPERSEDED_CACHE["mtime"] = -1.0
        _SUPERSEDED_CACHE["set"] = set()
        return _SUPERSEDED_CACHE["set"]
    if _SUPERSEDED_CACHE["mtime"] != mtime:
        try:
            _SUPERSEDED_CACHE["set"] = _load_superseded_file(db_path)
            _SUPERSEDED_CACHE["mtime"] = mtime
        except OSError as e:
            print(
                "ERROR: could not read %s (%s) -- superseded-revision exclusion "
                "DISABLED for this call" % (path, e),
                file=sys.stderr,
            )
            return set()
    return _SUPERSEDED_CACHE["set"]


# ---------------------------------------------------------------------------
# entity-aware retrieval boost
# ---------------------------------------------------------------------------

def _alias_map(con) -> dict[str, set[int]]:
    """Build (and cache) alias(lower) -> {document_id} from the equipment
    registry. Cached per process: the registry changes rarely relative to
    query volume, and a stale cache only costs a slightly-out-of-date boost,
    never a wrong answer (the boost is additive, not a filter)."""
    global _ALIAS_MAP_CACHE
    if _ALIAS_MAP_CACHE is not None:
        return _ALIAS_MAP_CACHE
    mapping: dict[str, set[int]] = {}
    try:
        eq_docs: dict[int, set[int]] = {}
        for eid, did in con.execute("SELECT equipment_id, document_id FROM doc_equipment"):
            eq_docs.setdefault(eid, set()).add(did)
        for eid, canon, aliases_json in con.execute(
            "SELECT id, canonical_name, aliases_json FROM equipment"
        ):
            docs = eq_docs.get(eid)
            if not docs:
                continue
            keys = {canon or ""}
            try:
                keys |= set(json.loads(aliases_json or "[]"))
            except (json.JSONDecodeError, TypeError):
                pass
            for key in keys:
                kl = (key or "").strip().lower()
                if len(kl) >= 6 and kl not in _ENTITY_STOPWORDS:
                    mapping.setdefault(kl, set()).update(docs)
    except Exception as e:  # noqa: BLE001 - boost is best-effort
        print(
            "WARNING: entity alias map build failed (%s: %s) -- entity boost "
            "disabled for this process" % (type(e).__name__, e),
            file=sys.stderr,
        )
        mapping = {}
    _ALIAS_MAP_CACHE = mapping
    return mapping


def _entity_document_ids(con, query: str) -> set[int]:
    """document_id set for any equipment alias literally present in `query`.

    Word-boundary substring match on a normalised (alnum + space) copy of
    the query -- never mutates the query itself, so this only ever changes
    ranking, never what gets searched for.
    """
    alias_map = _alias_map(con)
    if not alias_map:
        return set()
    normalised = " " + re.sub(r"[^a-z0-9 ]+", " ", (query or "").lower()) + " "
    hit: set[int] = set()
    for alias, doc_ids in alias_map.items():
        if (" " + alias + " ") in normalised:
            hit |= doc_ids
    return hit


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def search(
    query: str,
    cfg: dict | None = None,
    domains: list[str] | None = None,
    k: int | None = None,
    mode: str = "hybrid",
) -> list[dict]:
    """Return ranked chunks for `query`.

    mode: "vector" (embedding KNN only), "fts" (lexical only), or "hybrid"
    (both arms, fused with RRF -- the default and the only mode eligible for
    the entity boost and rerank, since both need a fused pool to work with).

    Each result dict: {chunk_id, document_id, source, rel_path, domain,
    text, score, rank}.
    """
    cfg = cfg or load_config()
    retrieval_cfg = cfg.get("retrieval", {})
    k = k if k is not None else int(retrieval_cfg.get("k", 8))
    rrf_k = int(retrieval_cfg.get("rrf_k", 60))
    db_path = cfg["paths"]["db_path"]

    con = connect(db_path, read_only=True)
    con.row_factory = sqlite3.Row
    try:
        pool = max(k * 4, 20)
        dom_filter = set(domains) if domains else None

        # When domain-filtering, resolve the allowed document ids up front so
        # vector KNN (which can't filter inside sqlite-vec) can overfetch and
        # then be restricted in Python, rather than under-returning because
        # off-domain chunks outranked in-domain ones before any cap applied.
        dom_doc_ids: set[int] | None = None
        if dom_filter:
            qmarks = ",".join("?" * len(dom_filter))
            dom_doc_ids = {
                row[0]
                for row in con.execute(
                    "SELECT id FROM documents WHERE domain IN (%s)" % qmarks,
                    list(dom_filter),
                ).fetchall()
            }
            if not dom_doc_ids:
                return []

        vec_chunk_ids: list[int] = []
        fts_chunk_ids: list[int] = []

        if mode in ("vector", "hybrid"):
            query_vec = _embed(query, cfg)
            vec_pool = min(max(pool, k * 8), 500) if dom_filter else pool
            rows = con.execute(
                "SELECT chunk_id FROM vchunks WHERE embedding MATCH ? AND k = ? "
                "ORDER BY distance",
                [serialize_f32(query_vec), vec_pool],
            ).fetchall()
            if dom_doc_ids is not None:
                cand_ids = [r[0] for r in rows]
                if cand_ids:
                    qmarks = ",".join("?" * len(cand_ids))
                    ok_ids = {
                        r[0]
                        for r in con.execute(
                            "SELECT id FROM chunks WHERE id IN (%s) AND document_id IN (%s)"
                            % (qmarks, ",".join("?" * len(dom_doc_ids))),
                            cand_ids + list(dom_doc_ids),
                        ).fetchall()
                    }
                    vec_chunk_ids = [cid for cid in cand_ids if cid in ok_ids]
            else:
                vec_chunk_ids = [r[0] for r in rows]

        if mode in ("fts", "hybrid"):
            try:
                if dom_filter:
                    qmarks = ",".join("?" * len(dom_doc_ids))
                    rows = con.execute(
                        "SELECT f.rowid FROM chunks_fts f "
                        "JOIN chunks c ON c.id = f.rowid "
                        "WHERE chunks_fts MATCH ? AND c.document_id IN (%s) "
                        "ORDER BY rank LIMIT ?" % qmarks,
                        [_fts_escape(query), *list(dom_doc_ids), pool],
                    ).fetchall()
                else:
                    rows = con.execute(
                        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
                        "ORDER BY rank LIMIT ?",
                        [_fts_escape(query), pool],
                    ).fetchall()
                fts_chunk_ids = [r[0] for r in rows]
            except Exception as e:  # noqa: BLE001 - malformed MATCH must not crash search
                print(
                    "WARNING: FTS query failed (%s: %s) -- continuing with vector-only results"
                    % (type(e).__name__, e),
                    file=sys.stderr,
                )
                fts_chunk_ids = []

        # reciprocal-rank fusion
        score: dict[int, float] = {}
        for rank_pos, cid in enumerate(vec_chunk_ids):
            score[cid] = score.get(cid, 0.0) + 1.0 / (rrf_k + rank_pos)
        for rank_pos, cid in enumerate(fts_chunk_ids):
            score[cid] = score.get(cid, 0.0) + 1.0 / (rrf_k + rank_pos)

        entity_cfg = retrieval_cfg.get("entity_boost", {})
        if entity_cfg.get("enabled", False) and score and mode == "hybrid":
            try:
                boosted_docs = _entity_document_ids(con, query)
                if boosted_docs:
                    weight = float(entity_cfg.get("weight", 0.012))
                    ids = list(score)
                    qmarks = ",".join("?" * len(ids))
                    for cid, did in con.execute(
                        "SELECT id, document_id FROM chunks WHERE id IN (%s)" % qmarks,
                        ids,
                    ):
                        if did in boosted_docs:
                            score[cid] += weight
            except Exception as e:  # noqa: BLE001 - boost is best-effort
                print(
                    "WARNING: entity boost failed (%s: %s) -- continuing without it"
                    % (type(e).__name__, e),
                    file=sys.stderr,
                )

        ordered_ids = sorted(score, key=score.get, reverse=True)
        superseded = _superseded_rel_paths(db_path)

        rerank_cfg = cfg.get("rerank", {})
        do_rerank = mode == "hybrid" and rerank_cfg.get("enabled", True) and rerank_cfg.get("backend", "none") != "none"
        target = max(k, int(rerank_cfg.get("pool", 15))) if do_rerank else k

        out: list[dict] = []
        for cid in ordered_ids:
            row = con.execute(
                """SELECT c.id, c.document_id, c.seq, c.text, d.source, d.rel_path, d.domain
                   FROM chunks c JOIN documents d ON d.id = c.document_id
                   WHERE c.id = ?""",
                [cid],
            ).fetchone()
            if not row:
                continue
            if row["rel_path"] in superseded:
                continue
            if dom_filter and row["domain"] not in dom_filter:
                continue
            out.append(
                {
                    "chunk_id": row["id"],
                    "document_id": row["document_id"],
                    "source": row["source"],
                    "rel_path": row["rel_path"],
                    "domain": row["domain"],
                    "text": row["text"],
                    "score": round(score[cid], 6),
                }
            )
            if len(out) >= target:
                break

        if do_rerank and len(out) > 1:
            out = _rerank(query, out, cfg)

        out = out[:k]
        for i, h in enumerate(out, 1):
            h["rank"] = i
        return out
    finally:
        con.close()


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------

def _chat_completion(url: str, model: str, messages: list[dict], timeout: int, temperature: float) -> str:
    """Call either Ollama's native /api/chat or an OpenAI-compatible
    /v1/chat/completions endpoint, detected by URL path, and return the
    assistant's text content."""
    is_ollama_native = "/api/chat" in url
    if is_ollama_native:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {"temperature": temperature, "num_predict": 800},
        }
        resp = _post_json(url, payload, timeout=timeout)
        text = (resp.get("message", {}) or {}).get("content", "") or ""
    else:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 800,
        }
        resp = _post_json(url, payload, timeout=timeout)
        choices = resp.get("choices", [])
        text = (choices[0].get("message", {}).get("content", "") if choices else "") or ""
    # Some locally-hosted "thinking" models leak their reasoning trace into
    # the content field ahead of a </think> marker -- strip it so citations
    # aren't buried in scratch reasoning.
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1]
    return text.strip()


def ask(
    query: str,
    cfg: dict | None = None,
    domains: list[str] | None = None,
    k: int | None = None,
) -> dict:
    """Retrieve + generate a cited answer.

    Returns {"answer": str, "sources": [...], "hits": [...]}. Retrieval and
    generation fail independently: if retrieval finds nothing, the answer
    says so plainly with no sources. If retrieval succeeds but generation
    fails (model down, timeout, empty output), the answer explains that
    plainly and STILL returns the sources/hits -- a caller can fall back to
    reading the raw chunks even when the LLM step is unavailable.
    """
    cfg = cfg or load_config()
    hits = search(query, cfg=cfg, domains=domains, k=k, mode="hybrid")
    if not hits:
        return {
            "answer": "No relevant information found in the knowledge base.",
            "sources": [],
            "hits": [],
        }

    context = "\n\n".join(
        "[%d] (source: %s, domain %s)\n%s" % (i, h["source"], h["domain"], h["text"])
        for i, h in enumerate(hits, 1)
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You are a knowledge assistant for a technical asset/facility "
                "knowledge base. Answer ONLY from the numbered context below. "
                "Cite sources inline like [1], [2]. If the answer is not in the "
                "context, say so plainly. Be concise and exact with model numbers, "
                "tags, and specifications."
            ),
        },
        {"role": "user", "content": "Context:\n%s\n\nQuestion: %s" % (context, query)},
    ]
    sources = [
        {"n": i, "source": h["source"], "domain": h["domain"], "chunk_id": h["chunk_id"]}
        for i, h in enumerate(hits, 1)
    ]

    llm_cfg = cfg.get("llm", {})
    gen_url = llm_cfg.get("gen_url", "http://127.0.0.1:11434/api/chat")
    gen_model = llm_cfg.get("gen_model", "your-general-model")
    timeout = int(llm_cfg.get("timeout_s", 240))

    try:
        answer = _chat_completion(gen_url, gen_model, messages, timeout, temperature=0.2)
        if not answer:
            return {
                "answer": (
                    "[Generation produced no answer. Relevant sources were found -- "
                    "see sources and inspect the hits directly.]"
                ),
                "sources": sources,
                "hits": hits,
            }
        return {"answer": answer, "sources": sources, "hits": hits}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as e:
        return {
            "answer": (
                "[Retrieval succeeded but generation is unavailable (%s). See "
                "sources and inspect the hits directly.]" % type(e).__name__
            ),
            "sources": sources,
            "hits": hits,
        }


if __name__ == "__main__":  # pragma: no cover - manual smoke-test entrypoint
    import sys as _sys

    t0 = time.time()
    q = " ".join(_sys.argv[1:]) or "status"
    result = ask(q)
    print(result["answer"])
    print("\nSources:")
    for s in result["sources"]:
        print("  [%d] %s (%s)" % (s["n"], s["source"], s["domain"]))
    print("\n(%.1fs)" % (time.time() - t0))

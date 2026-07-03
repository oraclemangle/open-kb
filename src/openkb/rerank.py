"""Pluggable reranker for the fused (vector + FTS) retrieval pool.

RRF gives you a decent *first cut*, but it fuses two rankers that know
nothing about each other's notion of relevance. A reranker looks at the
actual candidate text (not just its rank) and re-orders the pool with a
model that can read. Three backends, chosen by `rerank.backend`:

  "llm"     listwise rerank (RankGPT-style): number the candidates, ask an
            instruct model to return the numbers best-first as a JSON array,
            reorder accordingly. Works with any chat-capable local model --
            no extra service to run. Slower (one generation call) but in our
            own measurements this was consistently the strongest option on a
            technical/tabular corpus.
  "service" cross-encoder rerank via a small HTTP microservice (e.g.
            sentence-transformers CrossEncoder). POST {"query","texts"} ->
            {"scores":[...]}, one score per candidate, reorder by score
            descending. Fast (~100ms warm) but needs the extra process, and
            in our own measurements a general-purpose cross-encoder actually
            scored *worse* than no rerank at all on this kind of corpus --
            it's wired up so you can re-test it against your own eval set as
            the model landscape moves, not because it's a safe default.
  "none"    passthrough -- returns hits unchanged. Useful as a control arm
            when measuring whether reranking helps at all.

Whichever backend you pick, THIS IS FAIL-OPEN BY DESIGN: any exception
(model down, malformed response, network error) must degrade to the
original fused order rather than raise. A missing reranker should never be
the reason retrieval breaks -- it should just be slightly less good.

Measure, don't assume: enable one backend, run `openkb eval` (see
evaluate.py) before and after, and only keep the change if it actually
improves recall@k / MRR on your own gold set. What wins on someone else's
corpus may not win on yours.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any

__all__ = ["rerank"]


def _post(url: str, payload: dict, timeout: int = 60) -> dict:
    """Tiny stdlib-only JSON POST helper (mirrors the one in engine.py)."""
    import urllib.request

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_json_array(text: str) -> list[int] | None:
    """Best-effort extraction of a JSON array of ints from model output.

    Instruct models are asked to return ONLY a JSON array, but they
    sometimes wrap it in prose, code fences, or partial reasoning. Try a
    strict parse first, then fall back to a regex over the raw numbers so a
    slightly-mangled response still yields a usable order rather than
    forcing a full fail-open.
    """
    text = text.strip()
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1].strip()
    m = re.search(r"\[[\d,\s]*\]", text)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list) and all(isinstance(x, int) for x in arr):
                return arr
        except (json.JSONDecodeError, ValueError):
            pass
    nums = [int(n) for n in re.findall(r"\d+", text)]
    return nums or None


def _rerank_llm(query: str, hits: list[dict], cfg: dict) -> list[dict]:
    """Listwise LLM rerank of the candidate pool (RankGPT-style).

    Numbers the candidates 1..N, asks the model to return the full
    permutation best-first, applies it. Any index missing from the model's
    answer is appended in its original relative order, so a partial or
    slightly-malformed response still produces a safe, complete ordering.
    """
    rr = cfg.get("rerank", {})
    pool = int(rr.get("pool", 15))
    model = rr.get("model", "your-instruct-model")
    gen_url = cfg.get("llm", {}).get("gen_url", "http://127.0.0.1:11434/api/chat")
    timeout = int(cfg.get("llm", {}).get("timeout_s", 240))

    cands = hits[:pool]
    if len(cands) < 2:
        return hits

    lines = []
    for i, h in enumerate(cands, 1):
        text = (h.get("text") or "")[:300].replace("\n", " ")
        lines.append("[%d] (%s) %s" % (i, h.get("source", ""), text))
    prompt = (
        "You are a search reranker for a technical knowledge base about physical "
        "assets and equipment. Given the QUERY and %d numbered PASSAGES, order ALL "
        "the passage numbers from MOST to LEAST relevant for answering the query. "
        "Output ONLY a JSON array of the numbers, e.g. [3,1,2]. No prose.\n\n"
        "QUERY: %s\n\nPASSAGES:\n%s\n\nJSON array:"
    ) % (len(cands), query, "\n".join(lines))

    payload: dict[str, Any]
    is_ollama_native = gen_url.rstrip("/").endswith("/api/chat")
    if is_ollama_native:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "options": {"temperature": 0, "num_predict": 200},
        }
    else:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 200,
        }

    resp = _post(gen_url, payload, timeout=min(timeout, 60))
    if is_ollama_native:
        content = (resp.get("message", {}) or {}).get("content", "") or ""
    else:
        choices = resp.get("choices", [])
        content = (choices[0].get("message", {}).get("content", "") if choices else "") or ""

    order = _extract_json_array(content)
    if not order:
        return hits

    seen: set[int] = set()
    final_order: list[int] = []
    for x in order:
        if 1 <= x <= len(cands) and x not in seen:
            seen.add(x)
            final_order.append(x)
    for x in range(1, len(cands) + 1):
        if x not in seen:
            final_order.append(x)
    return [cands[x - 1] for x in final_order] + hits[pool:]


def _rerank_service(query: str, hits: list[dict], cfg: dict) -> list[dict]:
    """Cross-encoder rerank via an external HTTP microservice.

    POST {"query", "texts"} -> {"scores": [...]}, one score per candidate,
    reorder by score descending. Any shape mismatch or transport error is
    treated the same as "service unavailable" -- fail open.
    """
    rr = cfg.get("rerank", {})
    pool = int(rr.get("pool", 15))
    url = rr.get("url", "http://127.0.0.1:8000/rerank")

    cands = hits[:pool]
    if len(cands) < 2:
        return hits

    texts = [(h.get("text") or "")[:2000] for h in cands]
    resp = _post(url, {"query": query, "texts": texts}, timeout=20)
    scores = resp.get("scores")
    if not scores or len(scores) != len(cands):
        return hits
    order = sorted(range(len(cands)), key=lambda i: scores[i], reverse=True)
    return [cands[i] for i in order] + hits[pool:]


def rerank(query: str, hits: list[dict], cfg: dict) -> list[dict]:
    """Reorder `hits` (fused retrieval results) by relevance to `query`.

    Dispatches on cfg["rerank"]["backend"]: "llm" | "service" | "none".
    FAIL-OPEN: any exception anywhere in this path returns `hits` unchanged
    (logged to stderr) -- a broken reranker degrades quality, it must never
    take retrieval down.
    """
    if not hits:
        return hits
    rr = cfg.get("rerank", {}) if cfg else {}
    if not rr.get("enabled", True):
        return hits
    backend = (rr.get("backend") or "none").lower()
    if backend == "none":
        return hits
    try:
        if backend == "llm":
            return _rerank_llm(query, hits, cfg)
        if backend == "service":
            return _rerank_service(query, hits, cfg)
        print("WARNING: unknown rerank backend %r -- passthrough" % backend, file=sys.stderr)
        return hits
    except Exception as e:  # noqa: BLE001 - deliberate fail-open boundary
        print(
            "WARNING: rerank backend %r failed (%s: %s) -- falling back to fused order"
            % (backend, type(e).__name__, str(e)[:200]),
            file=sys.stderr,
        )
        return hits

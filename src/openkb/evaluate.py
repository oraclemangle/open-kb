"""Canonical eval harness -- "measure, don't assume."

Every retrieval/rerank lever in engine.py and rerank.py (entity boost,
rerank backend choice, RRF constant, k...) is a hypothesis, not a fact.
The only honest way to know whether a change helps *your* corpus is to run
it against a fixed, hand-authored gold set before and after, and compare
numbers. This module is that fixed measurement: same gold set, same
metrics, every time, so a change either earns its keep or it doesn't.

Subjective "is this a good answer?" grading is theatre without a human
in the loop, so this harness deliberately only measures things that are
objectively checkable:

  METRIC 1 -- retrieval accuracy: for each gold question with a known
    `expect_source` (a substring of the true source filename), does
    engine.search() return that source in the top k? Reported as
    recall@k and MRR (mean reciprocal rank, 0 if missed entirely).

  METRIC 2 -- citation quality: does the answer cite a valid retrieved
    passage, and does that cited passage contain the expected fact? This
    separates citation presence/validity from actual evidential support.

  METRIC 3 -- gold-answer substring match (optional, strongest signal):
    if a gold item has `expect_substr`, does the generated answer contain
    it (case-insensitive, unicode dashes folded to ASCII first so "P‑101"
    and "P-101" compare equal)? This is the only metric backed by a human
    who actually knows the right answer, not just the right source -- treat
    it as authoritative where it exists.

Gold format: JSONL, one object per line, '#'-prefixed lines and blank lines
ignored. Recognised keys, all optional except "q":
  {"q": "...", "expect_source": "partial-filename.pdf",
   "domains": ["00_ELECTRICAL"], "expect_substr": "APG-500"}

Build the gold set from real questions asked of your own knowledge base --
it is only as good as the ground truth in it, and it should grow over time
as new failure modes are discovered.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from typing import Any

from .config import load_config
from .engine import ask, search

__all__ = ["load_gold", "validate_gold_item", "run_eval", "main"]

_CONTRACT_KEYS = {"id", "category", "expected_behaviour", "expect_citations"}
_FAILURE_CATEGORIES = {"kb_failure", "model_failure", "embedding_failure"}
_REFUSAL_MARKERS = (
    "cannot find", "could not find", "not found", "no relevant",
    "insufficient information", "retrieval failed", "generation failed",
    "embedding failed", "unavailable", "offline",
)


def validate_gold_item(item: dict) -> None:
    """Validate the richer vessel-style gold contract.

    The historical example format remains accepted by :func:`load_gold`;
    records opting into any rich-contract key must satisfy the whole contract.
    """
    required = {"id", "category", "q", "expected_behaviour", "expect_citations"}
    missing = sorted(required - set(item))
    if missing:
        raise ValueError("missing required keys: %s" % ", ".join(missing))
    if not all(isinstance(item[key], str) and item[key].strip() for key in ("id", "category", "q")):
        raise ValueError("id, category and q must be non-empty strings")
    if not isinstance(item["expect_citations"], bool):
        raise ValueError("expect_citations must be boolean")

    category = item["category"]
    behaviour = item["expected_behaviour"]
    failure_or_missing = category in _FAILURE_CATEGORIES or category == "missing_information"
    if failure_or_missing:
        if behaviour not in {"refuse", "degraded"}:
            raise ValueError("failure/missing gold item must expect refuse or degraded behaviour")
        return

    if behaviour != "answer" or not item.get("expect_source") or not item.get("expected_facts"):
        raise ValueError("answerable gold item requires answer behaviour, expect_source and expected_facts")
    if not isinstance(item["expected_facts"], list) or not all(
        isinstance(fact, str) and fact.strip() for fact in item["expected_facts"]
    ):
        raise ValueError("answerable gold item expected_facts must be non-empty strings")


def _fold_unicode(text: str) -> str:
    """Normalise unicode dashes/quotes to their plain ASCII equivalents so a
    gold `expect_substr` written with a plain hyphen still matches an answer
    that came back with an en-dash or similar, and vice versa."""
    if not text:
        return text
    replacements = {
        "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
        "‘": "'", "’": "'", "“": '"', "”": '"',
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return unicodedata.normalize("NFKC", text)


def load_gold(gold_path: str) -> list[dict]:
    """Load gold JSONL, skipping blank lines and '#' comments. Malformed
    lines are skipped rather than raising -- one bad hand-edited line
    shouldn't block the whole eval run."""
    out: list[dict] = []
    try:
        with open(gold_path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    item = json.loads(line)
                    if _CONTRACT_KEYS & set(item):
                        validate_gold_item(item)
                    out.append(item)
                except json.JSONDecodeError as e:
                    print(
                        "WARNING: %s:%d: skipping malformed gold line (%s)"
                        % (gold_path, lineno, e),
                        file=sys.stderr,
                    )
    except FileNotFoundError:
        print("WARNING: gold set not found at %s" % gold_path, file=sys.stderr)
    return out


def _metric(hits: int, total: int) -> dict:
    return {"hits": hits, "total": total, "rate": (hits / total) if total else None}


def _citation_numbers(answer: str) -> list[int]:
    return [int(value) for value in re.findall(r"\[(\d+)\]", answer or "")]


def _is_refusal(answer: str, sources: list[dict]) -> bool:
    lowered = (answer or "").lower()
    return not sources and any(marker in lowered for marker in _REFUSAL_MARKERS)


def _citations_support_facts(citations: list[int], hits: list[dict], expected_facts: list[str]) -> bool:
    """True when every expected fact appears in at least one cited passage."""
    if not citations or not expected_facts:
        return False
    cited_texts = [
        _fold_unicode(hits[number - 1].get("text") or "").lower()
        for number in citations
        if 1 <= number <= len(hits)
    ]
    return bool(cited_texts) and all(
        any(_fold_unicode(fact).lower() in text for text in cited_texts)
        for fact in expected_facts
    )


def run_eval(
    cfg: dict | None = None,
    gold_path: str | None = None,
    k: int | None = None,
    retrieval_only: bool = False,
) -> dict:
    """Run the full eval and return a results dict (also used by main()).

    retrieval_only=True skips ask() entirely -- useful for a fast iteration
    loop while tuning retrieval-only knobs (RRF constant, entity boost)
    without paying for generation on every question.
    """
    cfg = cfg or load_config()
    eval_cfg = cfg.get("eval", {})
    gold_path = gold_path or eval_cfg.get("gold_path", "./examples/gold.example.jsonl")
    k = k if k is not None else int(eval_cfg.get("k", 8))

    gold = load_gold(gold_path)
    per_question: list[dict] = []

    src_total = 0
    src_hits = 0
    mrr_sum = 0.0
    substr_total = 0
    substr_hits = 0
    citation_presence_total = citation_presence_hits = 0
    citation_validity_total = citation_validity_hits = 0
    citation_support_total = citation_support_hits = 0
    correctness_total = correctness_hits = 0
    refusal_total = refusal_hits = 0
    failure_total = failure_hits = 0

    for g in gold:
        q = g["q"]
        expect_source = g.get("expect_source")
        domains = g.get("domains")
        expect_substr = g.get("expect_substr")

        hits = search(q, cfg=cfg, domains=domains, k=k, mode="hybrid")

        rank = None
        if expect_source:
            src_total += 1
            for pos, h in enumerate(hits, 1):
                if expect_source.lower() in (h.get("source") or "").lower():
                    rank = pos
                    break
            if rank:
                src_hits += 1
                mrr_sum += 1.0 / rank

        row: dict[str, Any] = {"q": q, "expect_source": expect_source, "rank": rank}

        if not retrieval_only:
            result = ask(q, cfg=cfg, domains=domains, k=k)
            answer = result.get("answer", "")
            sources = result.get("sources", [])
            answer_hits = result.get("hits", [])
            row["state"] = result.get("state", "unknown")

            behaviour = g.get("expected_behaviour")
            citations = _citation_numbers(answer)
            if g.get("expect_citations") is True:
                citation_presence_total += 1
                citation_validity_total += 1
                if citations:
                    citation_presence_hits += 1
                if citations and all(1 <= number <= len(sources) for number in citations):
                    citation_validity_hits += 1

            expected_facts = g.get("expected_facts") or []
            if expected_facts:
                correctness_total += 1
                citation_support_total += 1
                folded_answer = _fold_unicode(answer).lower()
                if all(_fold_unicode(fact).lower() in folded_answer for fact in expected_facts):
                    correctness_hits += 1
                if _citations_support_facts(citations, answer_hits, expected_facts):
                    citation_support_hits += 1

            if behaviour == "refuse":
                refusal_total += 1
                if _is_refusal(answer, sources):
                    refusal_hits += 1
            if g.get("category") in _FAILURE_CATEGORIES:
                failure_total += 1
                if behaviour == "degraded" and _is_refusal(answer, sources):
                    failure_hits += 1

            if expect_substr:
                substr_total += 1
                ok = _fold_unicode(expect_substr).lower() in _fold_unicode(answer).lower()
                if ok:
                    substr_hits += 1
                row["substr_ok"] = ok

        per_question.append(row)

    results: dict[str, Any] = {
        "n": len(gold),
        "k": k,
        "gold_path": gold_path,
        "retrieval_only": retrieval_only,
        "per_question": per_question,
        "retrieval": {
            "questions_with_expect_source": src_total,
            "recall_at_k": (src_hits / src_total) if src_total else None,
            "mrr": (mrr_sum / src_total) if src_total else None,
        },
    }
    if not retrieval_only:
        results["substring"] = {
            "questions_with_expect_substr": substr_total,
            "hits": substr_hits,
            "rate": (substr_hits / substr_total) if substr_total else None,
        }
        results["citation_presence"] = _metric(citation_presence_hits, citation_presence_total)
        results["citation_validity"] = _metric(citation_validity_hits, citation_validity_total)
        results["citation_support"] = _metric(citation_support_hits, citation_support_total)
        results["known_answer_correctness"] = _metric(correctness_hits, correctness_total)
        results["refusal_quality"] = _metric(refusal_hits, refusal_total)
        results["failure_behaviour"] = _metric(failure_hits, failure_total)
    return results


def _print_table(results: dict) -> None:
    n = results["n"]
    k = results["k"]
    print("=== open-kb eval: %d gold questions, k=%d ===\n" % (n, k))
    for i, row in enumerate(results["per_question"], 1):
        rank = row.get("rank")
        tag = "src@%d" % rank if rank else ("src:MISS" if row.get("expect_source") else "src:-")
        parts = ["[%2d]" % i, "%-9s" % tag]
        if "state" in row:
            parts.append("state:%s" % row["state"])
        if "substr_ok" in row:
            parts.append("sub:OK" if row["substr_ok"] else "sub:MISS")
        parts.append(row["q"][:54])
        print(" ".join(parts))

    print("\n=== RESULTS ===")
    r = results["retrieval"]
    if r["questions_with_expect_source"]:
        print(
            "Retrieval recall@%d: %d/%d = %.0f%%   MRR: %.3f"
            % (
                k,
                round(r["recall_at_k"] * r["questions_with_expect_source"]),
                r["questions_with_expect_source"],
                100 * r["recall_at_k"],
                r["mrr"],
            )
        )
    else:
        print("Retrieval recall@%d: no gold questions had expect_source" % k)

    if not results.get("retrieval_only"):
        support = results["citation_support"]
        if support["total"]:
            print(
                "Citation support: %d/%d = %.0f%%"
                % (support["hits"], support["total"], 100 * (support["rate"] or 0))
            )
        s = results["substring"]
        if s["questions_with_expect_substr"]:
            print(
                "Gold-answer substring hits: %d/%d = %.0f%%"
                % (s["hits"], s["questions_with_expect_substr"], 100 * (s["rate"] or 0))
            )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the open-kb canonical eval harness.")
    parser.add_argument("--gold", dest="gold_path", default=None, help="Path to gold JSONL (overrides config)")
    parser.add_argument("--k", type=int, default=None, help="Top-k for retrieval (overrides config)")
    parser.add_argument("--retrieval-only", action="store_true", help="Skip generation and citation-quality checks")
    parser.add_argument("--json", dest="json_out", default=None, help="Write full results as JSON to this path")
    args = parser.parse_args(argv)

    cfg = load_config()
    results = run_eval(cfg, gold_path=args.gold_path, k=args.k, retrieval_only=args.retrieval_only)

    if not results["per_question"]:
        print("No gold questions loaded from %s -- nothing to evaluate." % results["gold_path"])
        return

    _print_table(results)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
        print("\nFull results written to %s" % args.json_out)


if __name__ == "__main__":
    main()

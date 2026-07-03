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

  METRIC 2 -- answer faithfulness: does engine.ask() produce a non-empty,
    non-degenerate answer that cites at least one source, whenever
    retrieval actually found something? This catches silent failures
    (empty generations, "no answer" collapses) that recall@k alone can't
    see, since it only measures the retrieval step.

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
import sys
import unicodedata
from typing import Any

from .config import load_config
from .engine import ask, search

__all__ = ["load_gold", "run_eval", "main"]

_REFUSAL_PREFIXES = ("[generation", "[retrieval")


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
                    out.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(
                        "WARNING: %s:%d: skipping malformed gold line (%s)"
                        % (gold_path, lineno, e),
                        file=sys.stderr,
                    )
    except FileNotFoundError:
        print("WARNING: gold set not found at %s" % gold_path, file=sys.stderr)
    return out


def _is_faithful(answer: str, sources: list[dict]) -> bool:
    """Non-empty, cites >=1 source, and isn't one of the engine's own
    degraded-generation messages (those are honest failures, not answers)."""
    if not sources:
        return False
    if len(answer) <= 30:
        return False
    return not answer.lower().startswith(_REFUSAL_PREFIXES)


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
    faithful = 0
    ask_failures = 0
    substr_total = 0
    substr_hits = 0

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
            is_faithful = _is_faithful(answer, sources)
            if is_faithful:
                faithful += 1
            else:
                ask_failures += 1
            row["faithful"] = is_faithful

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
        results["faithfulness"] = {
            "faithful": faithful,
            "total": len(gold),
            "rate": (faithful / len(gold)) if gold else None,
            "failures": ask_failures,
        }
        results["substring"] = {
            "questions_with_expect_substr": substr_total,
            "hits": substr_hits,
            "rate": (substr_hits / substr_total) if substr_total else None,
        }
    return results


def _print_table(results: dict) -> None:
    n = results["n"]
    k = results["k"]
    print("=== open-kb eval: %d gold questions, k=%d ===\n" % (n, k))
    for i, row in enumerate(results["per_question"], 1):
        rank = row.get("rank")
        tag = "src@%d" % rank if rank else ("src:MISS" if row.get("expect_source") else "src:-")
        parts = ["[%2d]" % i, "%-9s" % tag]
        if "faithful" in row:
            parts.append("faith:OK" if row["faithful"] else "faith:XX")
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
        f = results["faithfulness"]
        print(
            "Answer faithfulness: %d/%d = %.0f%%  (failures: %d)"
            % (f["faithful"], f["total"], 100 * (f["rate"] or 0), f["failures"])
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
    parser.add_argument("--retrieval-only", action="store_true", help="Skip ask()/faithfulness checks")
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

"""Tests for openkb.evaluate -- recall@k / MRR arithmetic against a
stubbed search(), with a small hand-built gold list and known hit
positions. No live LLM/embeddings/DB required."""
from __future__ import annotations

from openkb import evaluate as evalmod


def test_run_eval_recall_and_mrr_arithmetic(tmp_path, monkeypatch):
    gold_path = tmp_path / "gold.jsonl"
    gold_path.write_text(
        '{"q": "question one", "expect_source": "doc-a.md"}\n'
        '{"q": "question two", "expect_source": "doc-b.md"}\n',
        encoding="utf-8",
    )

    def fake_search(query, cfg=None, domains=None, k=None, mode="hybrid"):
        if query == "question one":
            # hit at rank 1
            return [{"source": "doc-a.md"}, {"source": "doc-x.md"}]
        if query == "question two":
            # hit at rank 3
            return [{"source": "doc-y.md"}, {"source": "doc-z.md"}, {"source": "doc-b.md"}]
        return []

    monkeypatch.setattr(evalmod, "search", fake_search)

    cfg = {"eval": {"gold_path": str(gold_path), "k": 8}}
    results = evalmod.run_eval(cfg, gold_path=str(gold_path), k=8, retrieval_only=True)

    r = results["retrieval"]
    assert r["questions_with_expect_source"] == 2
    assert r["recall_at_k"] == 1.0
    # MRR = (1/1 + 1/3) / 2
    assert abs(r["mrr"] - ((1.0 / 1 + 1.0 / 3) / 2)) < 1e-9

    ranks = [row["rank"] for row in results["per_question"]]
    assert ranks == [1, 3]


def test_run_eval_missed_question_scores_zero_mrr_contribution(tmp_path, monkeypatch):
    gold_path = tmp_path / "gold.jsonl"
    gold_path.write_text(
        '{"q": "found", "expect_source": "doc-a.md"}\n'
        '{"q": "missed", "expect_source": "doc-missing.md"}\n',
        encoding="utf-8",
    )

    def fake_search(query, cfg=None, domains=None, k=None, mode="hybrid"):
        if query == "found":
            return [{"source": "doc-a.md"}]
        return [{"source": "doc-unrelated.md"}]

    monkeypatch.setattr(evalmod, "search", fake_search)

    cfg = {"eval": {"gold_path": str(gold_path), "k": 8}}
    results = evalmod.run_eval(cfg, gold_path=str(gold_path), k=8, retrieval_only=True)

    r = results["retrieval"]
    assert r["questions_with_expect_source"] == 2
    # 1 hit out of 2 -> recall 0.5; MRR = (1/1 + 0) / 2 = 0.5
    assert r["recall_at_k"] == 0.5
    assert abs(r["mrr"] - 0.5) < 1e-9


def test_load_gold_skips_comments_blank_and_malformed_lines(tmp_path):
    gold_path = tmp_path / "gold.jsonl"
    gold_path.write_text(
        "# a comment line\n"
        "\n"
        '{"q": "valid one"}\n'
        "{not valid json\n"
        '{"q": "valid two"}\n',
        encoding="utf-8",
    )
    gold = evalmod.load_gold(str(gold_path))
    assert [g["q"] for g in gold] == ["valid one", "valid two"]

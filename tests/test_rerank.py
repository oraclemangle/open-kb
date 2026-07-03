"""Tests for openkb.rerank -- LLM backend JSON parsing (clean + messy),
partial-order safety, and fail-open passthrough on unknown/none backends."""
from __future__ import annotations

import json

from openkb import rerank as rerank_mod


def _hits(n):
    return [{"chunk_id": i, "source": "doc-%d.md" % i, "text": "text body %d" % i} for i in range(1, n + 1)]


def _cfg(pool=15):
    return {
        "rerank": {"enabled": True, "backend": "llm", "model": "test-model", "pool": pool},
        "llm": {"gen_url": "http://127.0.0.1:11434/api/chat", "timeout_s": 60},
    }


def test_rerank_llm_clean_json_array(monkeypatch):
    hits = _hits(3)  # ids 1,2,3 in original order

    def fake_post(url, payload, timeout=60):
        return {"message": {"content": "[3,1,2]"}}

    monkeypatch.setattr(rerank_mod, "_post", fake_post)

    out = rerank_mod.rerank("query", hits, _cfg())
    assert [h["chunk_id"] for h in out] == [3, 1, 2]


def test_rerank_llm_messy_prose_wrapped_json(monkeypatch):
    hits = _hits(3)

    def fake_post(url, payload, timeout=60):
        return {
            "message": {
                "content": "Sure! Here is the ranking you asked for:\n```json\n[2, 3, 1]\n```\nHope that helps."
            }
        }

    monkeypatch.setattr(rerank_mod, "_post", fake_post)

    out = rerank_mod.rerank("query", hits, _cfg())
    assert [h["chunk_id"] for h in out] == [2, 3, 1]


def test_rerank_llm_partial_order_appends_remainder_in_original_order(monkeypatch):
    hits = _hits(5)  # ids 1..5

    def fake_post(url, payload, timeout=60):
        # model only confidently ranked 2 of the 5 candidates
        return {"message": {"content": "[4, 1]"}}

    monkeypatch.setattr(rerank_mod, "_post", fake_post)

    out = rerank_mod.rerank("query", hits, _cfg())
    ids = [h["chunk_id"] for h in out]
    # 4 and 1 first (model's stated order), then the remainder (2,3,5) in
    # their original relative order
    assert ids == [4, 1, 2, 3, 5]


def test_rerank_llm_out_of_range_and_duplicate_indices_dropped(monkeypatch):
    hits = _hits(3)

    def fake_post(url, payload, timeout=60):
        return {"message": {"content": "[3, 3, 99, 1]"}}

    monkeypatch.setattr(rerank_mod, "_post", fake_post)

    out = rerank_mod.rerank("query", hits, _cfg())
    ids = [h["chunk_id"] for h in out]
    assert ids == [3, 1, 2]


def test_rerank_backend_none_is_passthrough():
    hits = _hits(3)
    cfg = {"rerank": {"enabled": True, "backend": "none"}}
    out = rerank_mod.rerank("query", hits, cfg)
    assert out == hits


def test_rerank_disabled_is_passthrough():
    hits = _hits(3)
    cfg = {"rerank": {"enabled": False, "backend": "llm"}}
    out = rerank_mod.rerank("query", hits, cfg)
    assert out == hits


def test_rerank_service_backend_shape_mismatch_fails_open(monkeypatch):
    hits = _hits(3)
    cfg = {
        "rerank": {"enabled": True, "backend": "service", "url": "http://127.0.0.1:8000/rerank", "pool": 15},
    }

    def fake_post(url, payload, timeout=60):
        return {"scores": [0.9]}  # wrong length -- shape mismatch

    monkeypatch.setattr(rerank_mod, "_post", fake_post)

    out = rerank_mod.rerank("query", hits, cfg)
    assert out == hits

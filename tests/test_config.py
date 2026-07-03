"""Tests for openkb.config -- defaults, env-var coercion, YAML merge."""
from __future__ import annotations

import os

import pytest

from openkb.config import load_config


def test_defaults_load_with_no_yaml_file(tmp_path, monkeypatch):
    """With no config.yaml anywhere in the search path, load_config() should
    still return the full DEFAULTS shape (paths expanded, sync ssh_key
    expanded)."""
    monkeypatch.delenv("OPENKB_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)  # no ./config.yaml here
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.config/open-kb/config.yaml either

    cfg = load_config()

    assert cfg["retrieval"]["k"] == 8
    assert cfg["rerank"]["backend"] == "llm"
    assert cfg["embeddings"]["dim"] == 768
    assert cfg["taxonomy"][0] == "00_ELECTRICAL"


def test_env_var_bool_override_false(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENKB_CONFIG", raising=False)
    monkeypatch.setenv("OPENKB_RERANK_ENABLED", "0")

    cfg = load_config()

    assert cfg["rerank"]["enabled"] is False


def test_env_var_bool_override_truthy(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENKB_CONFIG", raising=False)
    monkeypatch.setenv("OPENKB_RERANK_ENABLED", "yes-please")

    cfg = load_config()

    assert cfg["rerank"]["enabled"] is True


def test_env_var_int_override(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENKB_CONFIG", raising=False)
    monkeypatch.setenv("OPENKB_RETRIEVAL_K", "42")

    cfg = load_config()

    assert cfg["retrieval"]["k"] == 42
    assert isinstance(cfg["retrieval"]["k"], int)


def test_env_var_nested_double_underscore_override(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENKB_CONFIG", raising=False)
    monkeypatch.setenv("OPENKB_RETRIEVAL_ENTITY_BOOST__ENABLED", "1")

    cfg = load_config()

    assert cfg["retrieval"]["entity_boost"]["enabled"] is True
    # sibling key must survive untouched
    assert cfg["retrieval"]["entity_boost"]["weight"] == 0.012


def test_yaml_merge_overrides_nested_key_without_clobbering_siblings(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENKB_CONFIG", raising=False)
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "retrieval:\n"
        "  k: 3\n"
        "llm:\n"
        "  gen_model: my-custom-model\n",
        encoding="utf-8",
    )

    cfg = load_config(str(yaml_path))

    assert cfg["retrieval"]["k"] == 3
    # sibling key rrf_k must be untouched by the partial YAML override
    assert cfg["retrieval"]["rrf_k"] == 60
    assert cfg["llm"]["gen_model"] == "my-custom-model"
    # sibling llm key untouched
    assert cfg["llm"]["gen_url"] == "http://127.0.0.1:11434/api/chat"

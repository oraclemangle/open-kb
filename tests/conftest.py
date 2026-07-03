"""Shared pytest fixtures for the open-kb test suite.

Nothing here ever touches a real ./data directory or a real config.yaml --
every test config is pointed at a pytest tmp_path, and the embeddings
dimension is kept tiny (8) so vector tests run fast without a real model.
"""
from __future__ import annotations

import copy
import os

import pytest

from openkb import db as dbmod
from openkb.config import DEFAULTS


TEST_DIM = 8


def make_test_config(tmp_path, dim: int = TEST_DIM) -> dict:
    """Build a throwaway config dict identical in shape to load_config()'s
    output, but with every path rooted under tmp_path and a small
    embeddings dimension for fast vector tests."""
    cfg = copy.deepcopy(DEFAULTS)
    data_dir = str(tmp_path / "data")
    cfg["paths"] = {
        "data_dir": data_dir,
        "db_path": os.path.join(data_dir, "kb.db"),
        "inbox": os.path.join(data_dir, "inbox"),
        "curated": os.path.join(data_dir, "curated"),
        "quarantine": os.path.join(data_dir, "quarantine"),
    }
    cfg["embeddings"]["dim"] = dim
    # Rerank/entity-boost off by default in tests -- individual tests that
    # want to exercise them turn them on explicitly.
    cfg["rerank"]["enabled"] = False
    cfg["retrieval"]["entity_boost"]["enabled"] = False
    return cfg


@pytest.fixture
def cfg(tmp_path):
    return make_test_config(tmp_path)


@pytest.fixture
def empty_db(cfg):
    """A freshly-initialised (schema-only) database at cfg['paths']['db_path']."""
    os.makedirs(os.path.dirname(cfg["paths"]["db_path"]), exist_ok=True)
    con = dbmod.connect(cfg["paths"]["db_path"])
    dbmod.init_schema(con, dim=cfg["embeddings"]["dim"])
    yield con
    con.close()

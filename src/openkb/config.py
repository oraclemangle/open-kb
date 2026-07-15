"""Configuration loader.

Precedence (later wins): built-in defaults -> YAML file -> OPENKB_* env vars.
The YAML file is found via $OPENKB_CONFIG, else ./config.yaml, else
~/.config/open-kb/config.yaml. Everything has a sane localhost default so a
single-machine demo runs with no config file at all.

Env override naming: OPENKB_<SECTION>_<KEY>, e.g. OPENKB_LLM_GEN_URL,
OPENKB_RERANK_ENABLED=0, OPENKB_RETRIEVAL_K=12. Nested keys under a section use
double underscores: OPENKB_RETRIEVAL_ENTITY_BOOST__ENABLED=1.
"""
from __future__ import annotations

import copy
import os
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a hard dep, but degrade politely
    yaml = None

DEFAULTS: dict[str, Any] = {
    "paths": {
        "data_dir": "./data",
        "db_path": "./data/kb.db",
        "inbox": "./data/inbox",
        "curated": "./data/curated",
        "quarantine": "./data/quarantine",
    },
    "llm": {
        "gen_url": "http://127.0.0.1:11434/api/chat",
        "gen_model": "your-general-model",
        "timeout_s": 240,
    },
    "embeddings": {
        "url": "http://127.0.0.1:1234/v1/embeddings",
        "model": "your-embedding-model",
        "dim": 768,
    },
    "ocr": {
        "url": "http://127.0.0.1:11434/api/chat",
        "model": "your-vision-model",
        "max_pages": 25,
        "dpi": 150,
    },
    "vision_describe": {
        "model": "your-vision-model",
        "max_pages": 4,
        "min_chars": 800,
        "gain_min": 200,
    },
    "rerank": {
        "enabled": True,
        "backend": "llm",          # llm | service | none
        "model": "your-instruct-model",
        "url": "http://127.0.0.1:8000/rerank",
        "pool": 15,
    },
    "retrieval": {
        "k": 8,
        "rrf_k": 60,
        "entity_boost": {"enabled": False, "weight": 0.012},
    },
    "taxonomy": [
        "00_ELECTRICAL", "01_MECHANICAL", "02_CONTROLS", "03_NETWORK_IT",
        "04_SAFETY", "05_ADMIN_SOP", "06_DRAWINGS", "99_MISC",
    ],
    "ingest": {
        "chunk_chars": 1800,
        "chunk_overlap": 200,
        "max_pixels": 8_000_000,
        "registers": [],
    },
    "sync": {
        "enabled": False,
        "replica": "user@replica-host",
        "ssh_key": "~/.ssh/id_ed25519_openkb_replica",
        "remote_db_path": "/srv/open-kb/kb.db",
        "ssh_timeout_s": 30,
        "transfer_timeout_s": 600,
    },
    "api": {"host": "127.0.0.1", "port": 8080, "token": ""},
    "eval": {"gold_path": "./examples/gold.example.jsonl", "k": 8},
}

_CONFIG_SEARCH = (
    os.environ.get("OPENKB_CONFIG", ""),
    "./config.yaml",
    os.path.expanduser("~/.config/open-kb/config.yaml"),
)


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _coerce(current: Any, raw: str) -> Any:
    """Coerce an env-var string to the type of the value it replaces."""
    if isinstance(current, bool):
        return raw.strip().lower() not in ("0", "false", "no", "off", "")
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(raw)
        except ValueError:
            return current
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError:
            return current
    return raw


def _apply_env(cfg: dict) -> dict:
    for name, raw in os.environ.items():
        if not name.startswith("OPENKB_") or name == "OPENKB_CONFIG":
            continue
        path = name[len("OPENKB_"):].lower()
        section, _, rest = path.partition("_")
        if section not in cfg or not rest:
            continue
        node = cfg[section]
        if not isinstance(node, dict):
            continue
        keys = rest.split("__")          # double underscore = nesting
        for key in keys[:-1]:
            node = node.setdefault(key, {})
            if not isinstance(node, dict):
                break
        else:
            leaf = keys[-1]
            node[leaf] = _coerce(node.get(leaf), raw)
    return cfg


def load_config(path: str | None = None) -> dict:
    """Load the effective configuration (defaults <- YAML <- env)."""
    cfg = copy.deepcopy(DEFAULTS)
    candidates = (path,) if path else _CONFIG_SEARCH
    for cand in candidates:
        if cand and os.path.isfile(cand):
            if yaml is None:
                raise RuntimeError("PyYAML is required to read %s" % cand)
            with open(cand, "r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
            cfg = _deep_merge(cfg, loaded)
            break
    cfg = _apply_env(cfg)
    # normalise ~ in paths
    for key, val in list(cfg["paths"].items()):
        cfg["paths"][key] = os.path.expanduser(str(val))
    cfg["sync"]["ssh_key"] = os.path.expanduser(str(cfg["sync"]["ssh_key"]))
    return cfg

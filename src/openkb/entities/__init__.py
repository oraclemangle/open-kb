"""Equipment/entity registry — three-phase pipeline.

Phase A (extract.extract_entities): raw per-document LLM extraction into
`doc_entities`. Phase B (registry.build_registry): deterministic, exact-key
grouping into the canonical `equipment` + `doc_equipment` tables. Phase C
(merge.propose_merges / merge.apply_merges): co-occurrence candidates
adjudicated by the LLM into `equipment_merge_proposal` rows, applied only
above a confidence threshold or on human approval.

See extract.py for the full design rationale and safety posture.
"""
from __future__ import annotations

from .extract import extract_entities
from .merge import apply_merges, propose_merges
from .registry import build_registry

__all__ = ["extract_entities", "build_registry", "propose_merges", "apply_merges"]

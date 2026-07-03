"""Document ingest pipeline: extract, OCR, vision-describe, secrets gate, worker.

Public entry points:
  run_ingest(cfg, limit=None, dry_run=False, reset_db=False, confirm=False)
      Walk paths.inbox and ingest every new document (see worker.py).
  describe_thin_documents(cfg, embed_fn, limit=None, commit=False)
      Vision-describe text-thin graphic-only documents already in the KB
      (see describe.py).
"""
from __future__ import annotations

from .describe import describe_thin_documents
from .worker import run_ingest

__all__ = ["run_ingest", "describe_thin_documents"]

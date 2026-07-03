# CLAUDE.md

Guidance for Claude Code (or any AI coding agent) working in this repo.

## Repo map

```
src/openkb/
  cli.py              CLI entry point (openkb <subcommand>) — lazy-imports each subcommand's module
  config.py           config loader: defaults <- config.yaml <- OPENKB_* env vars
  db.py                SQLite schema + connection helpers (sqlite-vec + FTS5)
  engine.py            search() and ask() — hybrid retrieval + RRF fusion + cited answers
  rerank.py            pluggable reranker (llm / service / none), fail-open
  evaluate.py          canonical eval harness — recall@k, MRR, answer faithfulness
  dedupe.py            near-duplicate + revision-family detection -> superseded.txt
  sync.py              read-replica promotion (snapshot, ship over SSH, atomic swap)
  api.py               stdlib REST API + bundled chat UI (static/index.html)
  mcp.py               stdio MCP server (kb_search, kb_ask, kb_status)
  ingest/              extract.py, ocr.py, describe.py, secrets.py, worker.py (the ingest pipeline)
  entities/            extract.py, registry.py, merge.py (three-phase equipment registry)
docs/                  full documentation set — start at docs/ai-operator-guide.md for deployment tasks
examples/               examples/corpus (synthetic demo documents) + examples/gold.example.jsonl (eval set)
config.example.yaml     copy to config.yaml and edit — every key documented in docs/configuration.md
```

## Key commands

```bash
.venv/bin/pytest                          # run the test suite
openkb init                               # create data dirs + SQLite schema
openkb ingest [--dry-run] [--limit N]     # process paths.inbox
openkb search "<query>" [--json]          # hybrid search, no generation
openkb ask "<query>" [--json]             # search + cited LLM answer
openkb eval [--retrieval-only] [--json out.json]   # canonical quality measurement
openkb entities extract|registry|merge|apply       # equipment registry pipeline
openkb dedupe near|revisions [--commit]   # near-dup / revision detection
openkb sync                               # promote local DB to configured replica
openkb serve                              # REST API + web UI
openkb mcp                                # stdio MCP server
openkb status [--json]                    # doc/chunk/equipment counts, config summary
```

## Conventions

- **Config-driven, not hardcoded.** Taxonomy, model endpoints, retrieval
  constants, rerank backend — all in `config.yaml`, all overridable by
  `OPENKB_<SECTION>_<KEY>` env vars. Don't hardcode a value that belongs in
  config; don't add a new knob without a `config.example.yaml` entry and a
  `docs/configuration.md` row.
- **Fail-open.** Every optional retrieval/rerank/boost step must catch its
  own exceptions and degrade to the next-simplest-thing-that-still-works —
  never let an optional lever take core search or generation down.
- **Proposals-only maintenance.** Dedupe, revision supersession, and entity
  merges never delete rows or mutate the registry directly — they write to
  `superseded.txt` or an `equipment_merge_proposal` row, with a separate
  explicit `--commit`/`apply` step. Preserve this shape in any change to
  these modules.
- **Measure, don't assume.** Any change to a retrieval lever (rerank
  backend, entity boost, RRF constant, chunk size) should be run through
  `openkb eval` before and after. See `docs/lessons-learned.md` for what
  happens when this rule is skipped.
- **`examples/corpus` is synthetic.** It is built to exercise the ingest
  pipeline's code paths (text, OCR fallback, classification), not to
  resemble any real deployment. Treat it as fixture data, not documentation
  of a real facility.

## Deployment tasks

For anything involving standing up local LLMs, ingesting a real corpus, or
splitting ingest/serving across two hosts, read
[`docs/ai-operator-guide.md`](docs/ai-operator-guide.md) first — it's a
phased runbook written specifically for an AI agent operating this repo on
a user's behalf, including known CLI gaps to watch for.

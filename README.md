# open-kb

**A private, offline-capable RAG knowledge base for complex physical assets** —
a plant, a building, a ship, a factory floor, any equipment-heavy facility
with a pile of PDFs, drawings, manuals and registers that nobody can search.

open-kb ingests that pile into a single SQLite file, retrieves from it with
hybrid vector + keyword search, reranks with a listwise LLM pass, and answers
questions with inline citations back to the source document. Everything runs
against local model endpoints (Ollama, LM Studio, llama.cpp, or any
OpenAI-compatible server) — no document, chunk, or query ever has to leave
your network.

Maritime is one good example of the problem shape (a vessel's electrical,
mechanical, controls and safety documentation is exactly this kind of
corpus), but there is nothing maritime-specific in the code. Swap "ship" for
"substation," "hospital wing," or "production line" and it's the same tool.

---

## Why offline / private-first

Technical documentation for a physical asset is usually some mix of
commercially sensitive, safety-critical, and simply too large and too
specific to hand to a general-purpose cloud chatbot. open-kb's answer to that
is architectural, not a policy promise:

- every model call (embedding, generation, OCR, vision-describe, rerank) is
  an HTTP request to a URL **you** configure — point it at `127.0.0.1` and
  nothing leaves the box;
- the knowledge base is one SQLite file. Back it up with `cp`. Move it with
  `scp`. Diff two versions with `sqlite3`. No vector database service, no
  message queue, no managed index to provision or pay for;
- ingest, search, and serving are separate commands you can run on separate
  machines, or all on one laptop for a five-minute demo.

## Feature overview

| Capability | How |
|---|---|
| Hybrid retrieval | vector KNN (`sqlite-vec`) + FTS5 lexical search, fused with reciprocal-rank fusion (RRF) |
| Listwise LLM rerank | numbers the fused pool, asks an instruct model to reorder it, degrades to the fused order on any failure |
| Entity-aware boost | optional, off by default — nudges ranking toward documents that mention an equipment alias literally present in the query |
| OCR fallback | scanned/thin-text PDFs and standalone images are transcribed by a local vision-language model |
| Vision-describe | graphic-only drawings (schematics, GA drawings) that OCR can't transcribe are instead *described* by a vision model |
| Register-aware XLSX | large tabular workbooks (cable schedules, asset registers) get header-aligned TSV extraction instead of a naive cell dump |
| Equipment/entity registry | three-phase pipeline: raw LLM extraction → deterministic exact-match canon → LLM-adjudicated merge proposals |
| Secret-detection gate | filename + content scan quarantines anything credential-shaped before it is ever indexed |
| Non-destructive maintenance | near-duplicate and revision detection only ever *propose* an exclusion list; nothing is deleted from the database |
| Read-replica sync | atomic, change-gated snapshot promotion over SSH from an ingest host to a read-only serving host |
| Canonical eval harness | a fixed gold-question set scores recall@k, MRR, and answer faithfulness — the only way any of the above "levers" earns a place in your config |
| REST API + MCP server | `openkb serve` (stdlib HTTP, bundled chat UI) and `openkb mcp` (stdio JSON-RPC) front the same retrieval engine |

## Architecture

```
                    ┌────────────────────────────────────────────┐
                    │                  INGEST                     │
                    │  inbox/ → secrets gate → extract (+OCR/     │
                    │  vision-describe) → classify → summarise →  │
                    │  chunk → embed → curated/<domain>/          │
                    └───────────────────┬──────────────────────────┘
                                        │ writes
                                        ▼
                    ┌────────────────────────────────────────────┐
                    │            SQLite  (kb.db, one file)         │
                    │  documents · chunks · vchunks (sqlite-vec)   │
                    │  chunks_fts (FTS5) · equipment · doc_equipment│
                    │  equipment_merge_proposal                    │
                    └───────────────────┬──────────────────────────┘
                                        │ reads (mode=ro)
                                        ▼
                    ┌────────────────────────────────────────────┐
                    │                RETRIEVAL                     │
                    │  vector KNN ─┐                                │
                    │  FTS5 match ─┼─► RRF fuse ─► entity boost?    │
                    │              │              ─► rerank? ─► top k│
                    └───────────────────┬──────────────────────────┘
                                        │
                                        ▼
                    ┌────────────────────────────────────────────┐
                    │        ANSWER (cited, LLM-generated)         │
                    │  CLI (`openkb ask`) · REST API (`serve`) ·   │
                    │  MCP stdio server (`mcp`)                    │
                    └────────────────────────────────────────────┘

     side rails: eval harness (measure any lever before keeping it) ·
     dedupe/supersede (non-destructive exclusion list) ·
     sync (read-replica promotion to a second host)
```

See [`docs/architecture.md`](docs/architecture.md) for the full component
walk-through and the reasoning behind each design choice.

## 5-minute quickstart

```bash
git clone <this-repo> open-kb && cd open-kb
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[pdf,dev]'

cp config.example.yaml config.yaml
# edit config.yaml: point llm.gen_url / embeddings.url at your local model server

openkb init
cp -r examples/corpus/* data/inbox/          # or drop in your own documents
openkb ingest
openkb ask "what is the standby generator's rated output?"
```

That's the whole loop: one database file, one config file, one CLI. For the
full walkthrough — installing Ollama, pulling models, running the web UI,
running the eval harness — see [`docs/quickstart.md`](docs/quickstart.md).

## Documentation map

| Doc | What it covers |
|---|---|
| [`docs/quickstart.md`](docs/quickstart.md) | Single-machine setup, model install, ingest, search, web UI, eval, MCP hookup for Claude Code / Codex |
| [`docs/architecture.md`](docs/architecture.md) | Component walk-through, mermaid diagram, design rationale |
| [`docs/configuration.md`](docs/configuration.md) | Every config key, env-var override, default, and guidance |
| [`docs/deployment.md`](docs/deployment.md) | Two-host ingest/serve pattern, SSH sync, scheduling, backups |
| [`docs/ai-operator-guide.md`](docs/ai-operator-guide.md) | Phased runbook for an AI coding agent operating this repo |
| [`docs/lessons-learned.md`](docs/lessons-learned.md) | Honest engineering notes — what worked, what didn't, and why |

## Philosophy

**Measure, don't assume.** Every retrieval "lever" in this codebase —
reranker backend, entity boost, RRF constant, chunk size — is a hypothesis
about your corpus, not a universal fact. `openkb eval` runs a fixed
gold-question set through retrieval and generation and reports recall@k,
MRR, and answer faithfulness. The rule this project holds itself to: run the
eval before and after any change to a retrieval lever, and only keep the
change if the numbers improve. What wins on someone else's corpus (including
the corpus this project happened to be built against) is not guaranteed to
win on yours.

**Fail-open, always.** A reranker that's down, an entity-boost alias map
that fails to build, a superseded-list file that can't be read — none of
these should ever take retrieval down. Every optional step in the pipeline
catches its own exceptions and degrades to the next-simplest thing that
still works (usually: the plain fused RRF order). A slow or unavailable
generation model degrades to "here are the sources, read them yourself,"
never to a wrong or empty response presented as confident.

**Maintenance is proposals-only.** Nothing in the dedupe, entity-merge, or
supersession pipeline deletes a row from the database. Near-duplicate
detection and revision-family detection both return a *plan*; the operator
(or an explicit `--commit`) decides whether to act on it, and acting on it
means appending to an exclusion list, never a `DELETE`. Equipment-entity
merges are the same shape: proposals are written, and only applied
explicitly, above a confidence threshold or with human approval. A knowledge
base you can't safely un-break is a knowledge base people stop trusting.

## License

MIT — see [`LICENSE`](LICENSE).

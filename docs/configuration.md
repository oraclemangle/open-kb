# Configuration reference

open-kb is configured entirely through `config.yaml` (copy it from
`config.example.yaml`) with every key overridable by an environment
variable. Precedence, later wins: **built-in defaults → `config.yaml` →
`OPENKB_*` environment variables.**

## Finding the config file

`load_config()` looks in this order and stops at the first hit:

1. `$OPENKB_CONFIG` (an explicit path), or
2. `./config.yaml` (current working directory), or
3. `~/.config/open-kb/config.yaml`

If none of these exist, every value falls back to its built-in default —
enough for a fully local demo with no config file at all (as long as your
model endpoints happen to be at the default URLs). `--config <path>` on the
CLI overrides all of the above for that invocation.

## Environment-variable override syntax

`OPENKB_<SECTION>_<KEY>`, upper-case, matching the YAML section and key
names. Nested keys under a section use a double underscore:

```bash
export OPENKB_LLM_GEN_URL=http://127.0.0.1:11434/api/chat
export OPENKB_RERANK_ENABLED=0
export OPENKB_RETRIEVAL_K=12
export OPENKB_RETRIEVAL_ENTITY_BOOST__ENABLED=1
```

Booleans accept `0`/`false`/`no`/`off` (case-insensitive) as false, anything
else (including empty) as true when the underlying default is boolean.
Numeric env values are coerced to match the type of the default they
replace; a value that fails to coerce is silently ignored (the previous
value is kept) rather than crashing config load.

---

## `paths`

| Key | Default | Env var | Notes |
|---|---|---|---|
| `data_dir` | `./data` | `OPENKB_PATHS_DATA_DIR` | Root for everything else under `paths.*` unless overridden individually |
| `db_path` | `./data/kb.db` | `OPENKB_PATHS_DB_PATH` | The single SQLite file — documents, chunks, vectors, FTS, registry |
| `inbox` | `./data/inbox` | `OPENKB_PATHS_INBOX` | Drop source documents here; `openkb ingest` walks it recursively |
| `curated` | `./data/curated` | `OPENKB_PATHS_CURATED` | Ingest moves processed originals here, under `<domain>/`; also holds `_MANIFEST.jsonl` |
| `quarantine` | `./data/quarantine` | `OPENKB_PATHS_QUARANTINE` | Documents that tripped the secret detector land here, never ingested |

`~` is expanded in every path value at load time.

## `llm`

| Key | Default | Env var | Notes |
|---|---|---|---|
| `gen_url` | `http://127.0.0.1:11434/api/chat` | `OPENKB_LLM_GEN_URL` | Chat endpoint used for classification, summarisation, answer generation, LLM rerank, and entity extraction/adjudication. Ollama's native `/api/chat` shape is detected by URL; anything else is treated as OpenAI-compatible `/v1/chat/completions` |
| `gen_model` | `your-general-model` | `OPENKB_LLM_GEN_MODEL` | Any modern instruct model — e.g. a Qwen or Llama instruct variant |
| `timeout_s` | `240` | `OPENKB_LLM_TIMEOUT_S` | Per-request timeout; generation on a CPU-bound local model can be slow, especially for longer answers |

## `embeddings`

| Key | Default | Env var | Notes |
|---|---|---|---|
| `url` | `http://127.0.0.1:1234/v1/embeddings` | `OPENKB_EMBEDDINGS_URL` | Must speak OpenAI-compatible `/v1/embeddings` (request: `{"model","input"}`; response: `{"data":[{"embedding":[...]}]}`) |
| `model` | `your-embedding-model` | `OPENKB_EMBEDDINGS_MODEL` | e.g. `nomic-embed-text`, a `bge-*` model, or any embeddings model your server exposes |
| `dim` | `768` | `OPENKB_EMBEDDINGS_DIM` | **MUST exactly match the model's real output vector length.** The `vec0` virtual table is created with this fixed dimension at `openkb init` time and cannot be resized — changing embedding models requires deleting `kb.db` and re-ingesting from scratch |

## `ocr`

Used for transcribing scanned/text-thin PDF pages and standalone images.
See `docs/lessons-learned.md` (f) for why this is a different job from
`vision_describe` below.

| Key | Default | Env var | Notes |
|---|---|---|---|
| `url` | `http://127.0.0.1:11434/api/chat` | `OPENKB_OCR_URL` | A vision-capable chat endpoint. This module assumes Ollama's native `/api/chat` image-attachment shape (`images: [base64...]` on the message) |
| `model` | `your-vision-model` | `OPENKB_OCR_MODEL` | Any vision-capable chat model, e.g. a Qwen-VL variant |
| `max_pages` | `25` | `OPENKB_OCR_MAX_PAGES` | Cap on pages OCR'd per PDF; pages beyond this are noted, not silently dropped |
| `dpi` | `150` | `OPENKB_OCR_DPI` | Render resolution before sending to the vision model; auto-downscaled per-page if it would exceed `ingest.max_pixels` |

## `vision_describe`

Used for graphic-only documents (schematics, drawings) that OCR can't
usefully transcribe — see `docs/lessons-learned.md` (f).

| Key | Default | Env var | Notes |
|---|---|---|---|
| `model` | `your-vision-model` | `OPENKB_VISION_DESCRIBE_MODEL` | Vision-capable chat model; can be the same as `ocr.model` |
| `max_pages` | `4` | `OPENKB_VISION_DESCRIBE_MAX_PAGES` | Pages described per document (kept low — description is meant for drawings/title pages, not long text runs) |
| `min_chars` | `800` | `OPENKB_VISION_DESCRIBE_MIN_CHARS` | A document's existing total chunk text must be under this to be considered "text-thin" and eligible for description |
| `gain_min` | `200` | `OPENKB_VISION_DESCRIBE_GAIN_MIN` | The generated description must add more than this many characters over what's already stored, or it's discarded (`no_gain`) — protects a document that already has decent OCR'd text from being needlessly replaced |

## `rerank`

See `docs/architecture.md` and `docs/lessons-learned.md` (b) for the
reasoning behind the default backend choice.

| Key | Default | Env var | Notes |
|---|---|---|---|
| `enabled` | `true` | `OPENKB_RERANK_ENABLED` | Master on/off switch |
| `backend` | `llm` | `OPENKB_RERANK_BACKEND` | `llm` (listwise, RankGPT-style — default) \| `service` (external cross-encoder HTTP microservice) \| `none` (passthrough, useful as a control arm) |
| `model` | `your-instruct-model` | `OPENKB_RERANK_MODEL` | Only used by `backend: llm`; any instruct model reachable via `llm.gen_url` |
| `url` | `http://127.0.0.1:8000/rerank` | `OPENKB_RERANK_URL` | Only used by `backend: service`; expects `POST {"query","texts"} -> {"scores":[...]}` |
| `pool` | `15` | `OPENKB_RERANK_POOL` | How many fused candidates are handed to the reranker before cutting to `retrieval.k` |

## `retrieval`

| Key | Default | Env var | Notes |
|---|---|---|---|
| `k` | `8` | `OPENKB_RETRIEVAL_K` | Results returned by `search`/`ask` |
| `rrf_k` | `60` | `OPENKB_RETRIEVAL_RRF_K` | Reciprocal-rank-fusion constant — higher values flatten the influence of rank position; 60 is RRF's commonly-cited default and a reasonable starting point |
| `entity_boost.enabled` | `false` | `OPENKB_RETRIEVAL_ENTITY_BOOST__ENABLED` | **Measure on your own eval set before enabling** — see `docs/lessons-learned.md` (c) |
| `entity_boost.weight` | `0.012` | `OPENKB_RETRIEVAL_ENTITY_BOOST__WEIGHT` | Additive nudge to a chunk's fused score when its document is linked to an equipment alias present in the query — roughly a rank-20 RRF contribution at the default `rrf_k` |

## `taxonomy`

A YAML list, fully yours to define — not overridable via a single env var
(it's a list, not a scalar). Every ingested document is classified by the
general LLM into exactly one entry. Keep entries filesystem-safe (they
become subdirectory names under `paths.curated`); list order is display
order. **The last entry is used as the catch-all bucket** when the model's
classification doesn't match anything in the list — name it something like
`99_MISC` by convention so an unclassifiable document is still findable,
just honestly labelled.

```yaml
taxonomy:
  - 00_ELECTRICAL
  - 01_MECHANICAL
  - 02_CONTROLS
  - 03_NETWORK_IT
  - 04_SAFETY
  - 05_ADMIN_SOP
  - 06_DRAWINGS
  - 99_MISC          # catch-all — keep this last
```

## `ingest`

| Key | Default | Env var | Notes |
|---|---|---|---|
| `chunk_chars` | `1800` | `OPENKB_INGEST_CHUNK_CHARS` | Target chunk size in characters, paragraph-boundary-aware |
| `chunk_overlap` | `200` | `OPENKB_INGEST_CHUNK_OVERLAP` | Overlap applied only when a single paragraph exceeds `chunk_chars` and must be sliding-window split |
| `max_pixels` | `8000000` | `OPENKB_INGEST_MAX_PIXELS` | Cap on rendered page area (width × height in pixels) for OCR/vision-describe; large-format drawings are auto-downscaled to fit |
| `registers` | `[]` | *(list — not env-overridable)* | XLSX registers needing header-aligned TSV extraction instead of the generic per-sheet dump — see below |

### `ingest.registers` entries

Each entry describes one register-shaped workbook pattern:

```yaml
ingest:
  registers:
    - name: cable schedule            # label only, for your own reference
      glob: "**/*cable*schedule*.xlsx" # matched against the file's path (and basename)
      header_keyword: "cable number"   # case-insensitive substring the header row must contain
      sheet: null                      # null = first sheet in the workbook; or a sheet name string
```

Any `.xlsx`/`.xlsm` file matching a `glob` is extracted via
`extract_register` (header row located by the keyword, every data row
aligned and tab-separated) instead of the generic sheet dump. If the
keyword is never found in the matched file, extraction falls back to the
generic dump automatically — this is a "try harder first" mechanism, not a
strict requirement.

## `sync`

Read-replica promotion — see [`docs/deployment.md`](deployment.md) for the
full two-host walkthrough. Configure this section **only on the ingest
host**; the serving host needs no `sync.*` config at all.

| Key | Default | Env var | Notes |
|---|---|---|---|
| `enabled` | `false` | `OPENKB_SYNC_ENABLED` | `openkb sync` is a safe no-op when `false` |
| `replica` | `user@replica-host` | `OPENKB_SYNC_REPLICA` | SSH destination, `user@host` form |
| `ssh_key` | `~/.ssh/id_ed25519_openkb_replica` | `OPENKB_SYNC_SSH_KEY` | Use a **dedicated** key generated for this purpose, not a personal one |
| `remote_db_path` | `/srv/open-kb/kb.db` | `OPENKB_SYNC_REMOTE_DB_PATH` | Must match `paths.db_path` in the serving host's own `config.yaml` |

## `api`

| Key | Default | Env var | Notes |
|---|---|---|---|
| `host` | `127.0.0.1` | `OPENKB_API_HOST` | Loopback-only by default — see `docs/deployment.md` before binding elsewhere |
| `port` | `8080` | `OPENKB_API_PORT` | |
| `token` | `""` (empty) | `OPENKB_API_TOKEN` | When set, every `POST` route requires `Authorization: Bearer <token>`. **Set this before exposing the API beyond localhost.** `GET` routes (`/`, `/health`, `/stats`) stay open regardless, by design |

## `eval`

| Key | Default | Env var | Notes |
|---|---|---|---|
| `gold_path` | `./examples/gold.example.jsonl` | `OPENKB_EVAL_GOLD_PATH` | Point this at your own hand-authored gold set once you have one — see `docs/lessons-learned.md` (d) on why size matters |
| `k` | `8` | `OPENKB_EVAL_K` | Top-k used when scoring recall/MRR during eval; independent of `retrieval.k` so you can eval at a different cutoff than production serves |

### Gold JSONL format

One JSON object per line; blank lines and `#`-prefixed comment lines are
skipped:

```jsonl
{"q": "what is the standby generator's rated output?", "expect_source": "generator-manual.pdf", "expect_substr": "500 kW"}
{"q": "which valve isolates the fuel supply?", "domains": ["01_MECHANICAL"]}
```

All keys except `q` are optional: `expect_source` (substring of the true
source filename, scores recall@k/MRR), `domains` (restricts the search),
`expect_substr` (the strongest signal — checks the generated answer
actually contains the known-correct text, unicode-dash/quote-folded before
comparison).

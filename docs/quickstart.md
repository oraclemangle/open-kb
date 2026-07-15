# Quickstart — single machine

This walks through everything on one machine: local models, config, ingest,
search, the web UI, and the eval harness. No prior RAG knowledge assumed.
Commands are copy-pasteable; expected output is shown so you know what
"working" looks like.

Estimated time: 20-30 minutes, most of it spent waiting for model downloads.

---

## 1. Prerequisites

- Python 3.10+
- ~10GB free disk for local models (varies by model choice)
- macOS, Linux, or WSL2 (anything Ollama/LM Studio support)

Check your Python version:

```bash
python3 --version
# Python 3.10.x or later
```

## 2. Install a local LLM runtime

Any OpenAI-compatible or Ollama-native chat/embeddings server works. Ollama
is the easiest path to a working demo because it serves chat, vision, and
embeddings from one process.

### 2a. Install Ollama

```bash
# macOS
brew install ollama
# or download from https://ollama.com/download

ollama serve
```

Leave `ollama serve` running in its own terminal (or let the menu-bar app
manage it). It listens on `http://127.0.0.1:11434` by default.

### 2b. Pull an instruct model (generation, classification, rerank)

```bash
ollama pull qwen2.5:14b-instruct
```

Any modern instruct model works — smaller (7-8B) is fine for a first test,
larger models generally improve summarisation, classification, and rerank
quality. Qwen, Llama, and Mistral instruct families have all been used
successfully with open-kb's prompt shapes.

### 2c. Pull a vision-capable model (OCR + vision-describe)

```bash
ollama pull qwen2.5vl:7b
```

Used for transcribing scanned pages (`ocr.*`) and describing graphic-only
drawings (`vision_describe.*`). Any vision-capable chat model Ollama serves
will work.

### 2d. Get an embeddings model

Two options:

**Option A — Ollama for embeddings too** (simplest, one server for
everything):

```bash
ollama pull nomic-embed-text
```

Ollama's native `/api/embeddings` is a different shape from
`/v1/embeddings`; if you go this route, front it with a small
OpenAI-compatible proxy, or use Option B.

**Option B — LM Studio** (native OpenAI-compatible `/v1/embeddings`):

1. Download [LM Studio](https://lmstudio.ai/).
2. In LM Studio's model search, download an embedding model — `nomic-embed-text`
   or a `bge-*` model both work well for technical/tabular text.
3. Start LM Studio's local server (Developer tab → Start Server). By default
   it listens on `http://127.0.0.1:1234` and serves `/v1/embeddings`.

This guide assumes Option B (LM Studio on `:1234` for embeddings, Ollama on
`:11434` for chat/vision) since that matches `config.example.yaml`'s
defaults.

## 3. Install open-kb

```bash
git clone <this-repo> open-kb
cd open-kb
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[pdf,dev]'
```

The `pdf` extra pulls in PyMuPDF (needed for PDF text extraction, OCR page
rendering, and vision-describe page rendering). The `dev` extra pulls in
pytest for running the test suite.

Verify the CLI is on your `PATH`:

```bash
openkb --help
```

Expected output: a usage line and the list of subcommands (`init`, `ingest`,
`search`, `ask`, `describe`, `entities`, `dedupe`, `eval`, `sync`, `serve`,
`mcp`, `status`).

## 4. Configure

```bash
cp config.example.yaml config.yaml
```

Open `config.yaml` and set:

```yaml
llm:
  gen_url: http://127.0.0.1:11434/api/chat
  gen_model: qwen2.5:14b-instruct

embeddings:
  url: http://127.0.0.1:1234/v1/embeddings
  model: text-embedding-nomic-embed-text-v1.5   # whatever LM Studio calls it
  dim: 768                                       # MUST match the model's output dimension

ocr:
  url: http://127.0.0.1:11434/api/chat
  model: qwen2.5vl:7b

vision_describe:
  model: qwen2.5vl:7b
```

**The embedding dimension matters.** `embeddings.dim` must exactly match the
model's actual output vector length — the SQLite `vec0` virtual table is
created with a fixed dimension at `openkb init` time. If you change
embedding models later, you need a fresh `openkb ingest --reset-db` (see
[`docs/configuration.md`](configuration.md)) — the table can't be resized.

Every key in `config.yaml` can also be set via an environment variable
(`OPENKB_<SECTION>_<KEY>`) — see [`docs/configuration.md`](configuration.md)
for the full reference and precedence rules.

## 5. Initialise

```bash
openkb init
```

Expected output:

```
Initialised open-kb.
  data dir : ./data
  database : ./data/kb.db

Next steps:
  1. Copy config.example.yaml to config.yaml and point it at your
     local LLM / embedding endpoints.
  2. Drop source documents into: ./data/inbox
  3. Run: openkb ingest
  4. Run: openkb ask "<question>"
```

This creates `./data/{inbox,curated,quarantine}` and the SQLite schema at
`./data/kb.db` with a vector table sized to `embeddings.dim`.

## 6. Ingest the example corpus

```bash
cp -r examples/corpus/* data/inbox/
openkb ingest
```

`examples/corpus/` is a small synthetic set of documents shaped like a real
technical corpus — a mix of prose manuals, a spec sheet, and a drawing —
purpose-built to exercise the ingest pipeline (text extraction, OCR
fallback, classification) without depending on any real facility's
documents.

Expected output: a JSON array, one object per document, each with an
`"action"` of `processed`, `skip_duplicate`, `quarantine_secret`, or similar:

```json
[
  {
    "src": "data/inbox/generator-manual.pdf",
    "rel_path": "generator-manual.pdf",
    "sha256": "…",
    "domain": "01_MECHANICAL",
    "extractor": "pdf",
    "chunks": 14,
    "summary": "…",
    "action": "processed",
    "dest": "01_MECHANICAL/generator-manual.pdf"
  }
]
```

Ingest is resumable — re-running `openkb ingest` skips anything already in
the manifest (`data/curated/_MANIFEST.jsonl`) or already hashed into the
database. A `--dry-run` flag previews what would happen without writing
anything; `--limit N` caps how many documents are processed in one run.

Check the result:

```bash
openkb status
```

```
open-kb status
  database   : ./data/kb.db
  documents  : 4
  chunks     : 52
  equipment  : 0
  retrieval.k        : 8
  rerank.enabled     : True
  entity_boost       : False
```

(`equipment` is 0 until you run `openkb entities` — see
[`docs/ai-operator-guide.md`](ai-operator-guide.md) Phase 3.)

## 7. Search and ask

```bash
openkb search "generator rated output"
```

```
[1] 01_MECHANICAL/generator-manual.pdf                       score=0.0325
    Rated output is 500 kW continuous at 0.8 power factor, 400V 3-phase...
[2] ...
```

```bash
openkb ask "what is the standby generator's rated output?"
```

```
The standby generator is rated at 500 kW continuous output at 0.8 power
factor, 400V three-phase [1].

Sources:
  [1] generator-manual.pdf
```

Add `--json` to either command for machine-readable output; `--domains
00_ELECTRICAL,01_MECHANICAL` filters to specific taxonomy buckets; `-k 5`
changes how many results come back.

## 8. Run the web UI

```bash
openkb serve
```

```
open-kb API listening on http://127.0.0.1:8080  (open (no token configured))
```

Open `http://127.0.0.1:8080` in a browser. The bundled single-page chat UI
(no build step, no JS framework) lets you type a question and see the
answer plus its cited sources. If you've set `api.token` in `config.yaml`,
the UI's token bar (top-right toggle) lets you paste it in before asking.

The same process also serves:

- `GET /health` — `{"ok": true, "documents": N, "chunks": N}`
- `GET /stats` — per-domain document/chunk counts, equipment count, DB file size
- `POST /search`, `POST /ask` — the same JSON shapes as the CLI's `--json` output

## 9. Run the eval harness

```bash
openkb eval
```

Runs every question in `examples/gold.example.jsonl` through retrieval (and,
by default, generation) and reports recall@k, MRR, and citation support:

```
=== open-kb eval: 12 gold questions, k=8 ===

[ 1] src@1     state:grounded  what is the standby generator's rated output?
[ 2] src@2     state:grounded  which pump feeds the...
...

=== RESULTS ===
Retrieval recall@8: 11/12 = 92%   MRR: 0.714
Citation support: 11/12 = 92%
```

Use `--retrieval-only` to skip generation entirely (much faster, useful
while tuning retrieval-only settings), and `--json report.json` to write the
full per-question breakdown for later comparison. See
[`docs/lessons-learned.md`](lessons-learned.md) for why this harness — not
ad-hoc spot-checking — is the only way any retrieval "lever" earns a place
in your `config.yaml`.

## 10. Connect an AI assistant via MCP (optional)

`openkb mcp` exposes the knowledge base as an MCP stdio server (tools:
`kb_search`, `kb_ask`, `kb_status`), so a frontier-model client — Claude
Code, Claude Desktop, Codex, or any MCP-capable agent — can search and cite
your corpus while the corpus itself stays local. Only the retrieved chunks
the assistant requests ever leave your machine; mind that boundary if the
corpus is sensitive.

With Claude Code:

```bash
claude mcp add open-kb -- openkb mcp
```

Or by hand in the client's `mcpServers` config (Claude Desktop, Codex, etc.):

```json
"open-kb": {
  "command": "/path/to/open-kb/.venv/bin/openkb",
  "args": ["--config", "/path/to/open-kb/config.yaml", "mcp"]
}
```

Use absolute paths for both the executable and `--config` — MCP clients
don't inherit your shell's venv or working directory. Then ask the
assistant something like *"using the open-kb tools, what is the standby
generator's rated output?"* and it will call `kb_search`/`kb_ask` and
answer with citations.

## Next steps

- Point `paths.inbox` at your own document tree and re-run `openkb ingest`.
- Run `openkb entities extract --commit`, `openkb entities registry`, then
  inspect merge candidates with `openkb entities merge` — see
  [`docs/ai-operator-guide.md`](ai-operator-guide.md) Phase 3.
- Ready to split ingest and serving onto two machines? See
  [`docs/deployment.md`](deployment.md).

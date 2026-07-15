# AI operator guide

You are an AI coding agent (Claude Code, Codex, or similar) helping a user
deploy and operate open-kb. This is a phased runbook: work through the
phases in order, verify each one before moving to the next, and stop at the
listed checkpoints if something doesn't verify. Don't skip ahead — a corpus
ingested against the wrong `embeddings.dim`, or an entity boost enabled
without an eval win, is much more annoying to unwind than to avoid.

## What NOT to do

- **Never delete rows from the database by hand.** Every maintenance
  operation in this codebase (dedupe, revision supersession, entity merges)
  is designed to be reversible through `superseded.txt` or a proposal-review
  step. A manual `DELETE FROM documents` throws that away — if a document
  genuinely needs to disappear from search, use `openkb dedupe revisions
  --commit` (adds it to `superseded.txt`, reversible) rather than editing
  the database directly.
- **Never enable `retrieval.entity_boost.enabled: true` without an eval
  win.** It defaults to `false` for a reason — see
  [`docs/lessons-learned.md`](lessons-learned.md) (c). Run `openkb eval`
  before and after flipping it; only keep the change if recall@k or MRR
  improves on the user's own gold set.
- **Never expose `openkb serve`'s API without `api.token` set**, if it will
  be reachable from anything beyond `127.0.0.1`. See
  [`docs/deployment.md`](deployment.md) section 4.
- **Never run `--reset-db`-equivalent destructive operations without
  explicit user confirmation of what will be lost.** See the CLI note in
  Phase 2 below — the underlying `run_ingest(reset_db=True)` requires a
  second `confirm=True` flag that the current CLI does not yet expose (see
  "Known CLI gaps").

## Known CLI gaps (read before Phase 1)

Two mismatches exist between `src/openkb/cli.py` and the modules it calls,
as of this writing. Neither is a reason to avoid the tool — both have a
straightforward workaround — but know about them before you rely on the
affected subcommands:

1. **`openkb ingest --reset-db` will raise, not reset.**
   `run_ingest()` (in `src/openkb/ingest/worker.py`) requires **both**
   `reset_db=True` **and** `confirm=True` before it deletes `kb.db`; the CLI
   only wires up `--reset-db` and never passes `confirm`. Calling `openkb
   ingest --reset-db` will raise `ValueError` with a message explaining the
   guard, rather than silently doing nothing or silently wiping data —
   which is the safe failure mode, just not the convenient one. If a reset
   is genuinely needed, do it explicitly instead:
   ```bash
   rm -f data/kb.db data/kb.db-wal data/kb.db-shm
   openkb init
   openkb ingest
   ```
2. **`openkb describe` will raise a `TypeError`.**
   `describe_thin_documents()` (in `src/openkb/ingest/describe.py`) requires
   a positional `embed_fn` callable argument; the CLI's `cmd_describe` calls
   it without one. Until this is fixed upstream, treat `openkb describe` as
   not yet wired up via the CLI — vision-describe is otherwise fully
   implemented and reachable by calling `describe_thin_documents` directly
   from a short Python snippet with an `embed_fn` that POSTs to
   `embeddings.url` (see the shape of `embed_chunk` in
   `src/openkb/ingest/worker.py` for the expected request/response).

Report these to the user if you hit them; don't silently work around them
in a way that hides the underlying bug.

---

## Phase 0 — Preflight

**Goal:** confirm the environment can run open-kb before touching anything.

```bash
python3 --version                 # need >=3.10
df -h .                           # confirm disk headroom for models + corpus
which ollama || echo "ollama not found"
```

**Decide host layout now:** single machine for a first pass, or the
two-host ingest/serve split from the start? If the user hasn't said, default
to single-machine — it's strictly less to get wrong, and migrating to
two-host later is just adding `sync.*` config and a second `openkb init`.

**Verify:** Python version prints 3.10+; there's meaningfully more than a
few GB free (model weights alone can be several GB each).

**Common failures:**
- Python too old → install 3.10+ via the platform's usual method (`brew
  install python@3.12`, `pyenv install`, etc.) before proceeding.
- No local LLM runtime installed → that's Phase 1's job, not a blocker here.

## Phase 1 — Single-box bring-up

**Goal:** `openkb ask` returns a cited answer against the example corpus.

Follow [`docs/quickstart.md`](quickstart.md) steps 2 through 8 verbatim:
install Ollama (+ an instruct model + a vision model), install LM Studio (or
an Ollama-compatible embeddings proxy), `pip install -e '.[pdf,dev]'`,
`cp config.example.yaml config.yaml` and edit endpoints, `openkb init`,
ingest `examples/corpus/`, `openkb ask`.

**Verify:**
```bash
openkb status --json
```
`documents` and `chunks` are both > 0, `error` is `null`.

**Common failures + fixes:**
- `openkb init` fails with a PyYAML import error → `pip install -e
  '.[pdf,dev]'` wasn't run in the active venv; re-run it.
- `openkb ingest` reports every document as an error with a connection
  refused message → the model server (Ollama/LM Studio) isn't actually
  running, or the URL in `config.yaml` doesn't match its actual port.
- Embedding call fails with a dimension mismatch downstream → `embeddings.dim`
  in config doesn't match the model's real output size. Check the model's
  documented dimension, fix the config, delete `data/kb.db*`, re-run
  `openkb init` and `openkb ingest` from scratch (dimension is fixed at
  schema-creation time — see [`docs/configuration.md`](configuration.md)).

## Phase 2 — Corpus ingest (real documents)

**Goal:** the user's actual document tree is ingested, classified, and
searchable.

1. Point `paths.inbox` at (or copy documents into) the real inbox.
2. **Inbox hygiene first** — before a large ingest run, sanity check what's
   about to be processed:
   ```bash
   openkb ingest --dry-run --limit 20
   ```
   Read the `action` field of each result: `would_ingest` is normal;
   `would_quarantine` means the secret-detector fired — inspect `reasons`
   before deciding whether that's a true positive (a real credential — leave
   it quarantined) or a false positive (a filename/content pattern that
   happened to match — the user can move it back into the inbox manually
   after review, or accept the quarantine).
3. Run the real ingest:
   ```bash
   openkb ingest
   ```
4. **Watch the manifest** for anything not cleanly `processed`:
   ```bash
   grep -v '"action": "processed"' data/curated/_MANIFEST.jsonl | tail -50
   ```
   `skip_duplicate` and `skip_nondoc` are routine. `error`/`dead_letter`
   need a look — usually a corrupt file or an extraction edge case; `error`
   entries are retried automatically on the next `openkb ingest` run (up to
   3 attempts) before becoming `dead_letter`.
   Also inspect durable interruption-recovery state:
   ```bash
   sqlite3 data/kb.db \
     "SELECT state, rel_path, error FROM ingest_pending ORDER BY updated_at;"
   ```
   `staged`, `curated`, and `indexed` entries are reconciled automatically
   under the ingest lock on the next run. A `dead_letter` row means neither
   its recorded inbox source nor its curated copy passed SHA-256 validation;
   preserve the row and restore a known-good original before retrying.
5. **Quarantine review**: anything in `paths.quarantine` tripped the secrets
   gate. Walk through it with the user — do not silently delete or silently
   re-ingest; a human call on "is this actually sensitive" is exactly what
   the quarantine step exists to force.

The worker rejects inbox symlinks and paths that resolve outside the inbox.
Quarantine and curated promotions are collision-safe and durable. A duplicate
inbox file is removed only when the corresponding curated regular file exists
and has the same SHA-256 as the database row.

**Verify:** `openkb status` document count roughly matches the number of
ingestible files the user expected (accounting for genuine duplicates and
quarantined items).

**Common failures + fixes:**
- A huge XLSX register comes out as an unreadable wall of numbers → configure
  `ingest.registers` for it (see [`docs/configuration.md`](configuration.md))
  and re-ingest that file specifically (delete its curated copy + its
  `documents` row by re-running ingest after fixing config — duplicates by
  hash are otherwise skipped, so a genuine re-extraction needs the old
  document's hash to no longer match, which config changes don't achieve on
  their own; simplest path is a `--reset-db` cycle for a small corpus, or
  ask the user before doing anything more surgical on a large one).
- A scanned PDF ingests with almost no text → check `documents.extractor` in
  the DB for that document (`sqlite3 data/kb.db "SELECT extractor FROM
  documents WHERE rel_path LIKE '%name%'"`) — if it's `text` not `ocr`, the
  OCR-fallback threshold wasn't triggered; if it's `ocr` but still thin, the
  document is likely graphic-only and needs vision-describe (see the CLI gap
  note above for the current workaround).

## Phase 3 — Quality pass

**Goal:** a measured baseline, a deduplicated corpus, and a populated
equipment registry — in that rough order, because eval should run against a
corpus that isn't full of near-duplicate noise.

1. **Dedupe first:**
   ```bash
   openkb dedupe near            # report only — read it
   openkb dedupe revisions       # report only — read it
   openkb dedupe revisions --commit   # only after reviewing the "supersede" list
   ```
   Read every proposed cluster/family before committing. `dedupe near`
   never writes anything itself (there's no `--commit` for it in the CLI —
   pass its output to `supersede()` yourself if you decide to act on a
   cluster, or ask the user to confirm which member to keep, since ranking
   is ambiguous for pure content duplicates in a way it isn't for
   filename-based revisions).
2. **Entities:**
   ```bash
   openkb entities extract --commit    # phase A: raw per-doc LLM extraction
   openkb entities registry            # phase B: deterministic canon (idempotent, re-runnable)
   openkb entities merge               # phase C: propose merges (writes proposals, no registry change yet)
   openkb entities apply --min-conf 0.9   # only applies proposals >= min-conf, or hand-approved ones
   ```
   Do not lower `--min-conf` casually — a wrong merge silently blends two
   pieces of equipment's document history and is much harder to notice
   (let alone undo) than a missed one. If the user wants to review borderline
   proposals by hand, query `equipment_merge_proposal` directly and set
   `status='approved'` on the ones they confirm before running `apply`.
3. **Eval baseline:**
   ```bash
   openkb eval --json baseline.json
   ```
   This needs a gold set — either the user's own
   (`eval.gold_path` in config) or `examples/gold.example.jsonl` as a
   placeholder while a real one is built. Keep `baseline.json` — every
   later change to a retrieval lever gets compared against it.

**Verify:** `openkb status --json` shows `equipment > 0`; `baseline.json`
has non-null `recall_at_k` and `mrr`.

## Phase 4 — Optional two-host deploy

**Goal:** ingest and serving split across two hosts, with a working
sync loop.

Follow [`docs/deployment.md`](deployment.md) in full: dedicated SSH key,
`sync.*` config on the ingest host only, `openkb sync` run manually once to
confirm it works end-to-end before scheduling it, then launchd/systemd units
for both `ingest` and `sync` on the ingest host, and a long-running `openkb
serve` process (its own service unit, not a timer) on the serving host.

**Verify:**
```bash
# on the ingest host, after a manual `openkb sync`
ssh <replica-user>@<replica-host> "sqlite3 <remote_db_path> 'SELECT COUNT(*) FROM documents;'"
```
matches the ingest host's own count.

**Common failures + fixes:**
- `sync` fails with an SSH error → the dedicated key isn't authorized on the
  serving host, or `BatchMode=yes` is failing because the key needs a
  passphrase (it must not have one — see the `ssh-keygen -N ""` step in
  deployment.md).
- Serving host doesn't see new data after a sync → confirm `paths.db_path`
  on the serving host is exactly `sync.remote_db_path` on the ingest host;
  a path mismatch means sync is writing somewhere the server never reads.

## Phase 5 — Operations

**Goal:** the system stays healthy and measurably good over time, not just
on day one.

- **Re-ingest cadence**: scheduled (Phase 4) or manual, but always followed
  by a glance at the manifest for new errors — a corpus that grows should
  have its dedupe/entities passes re-run periodically too (Phase 3, steps 1
  and 2), not just once at initial setup.
- **Eval before/after any model change.** Swapping `llm.gen_model`,
  `embeddings.model`, `rerank.model`, or flipping any `retrieval.*`/
  `rerank.*` flag is exactly the situation `openkb eval` exists for. Run it,
  compare against the last saved baseline, and only keep the change if the
  numbers hold up or improve. Changing `embeddings.model` additionally
  requires `embeddings.dim` to match the new model and a full re-ingest from
  a fresh database (existing vectors were computed by the old model and
  are not compatible) — this is a bigger operation than a config edit,
  say so explicitly to the user before doing it.
- **Never let a config change to a retrieval lever ship without a
  before/after eval number attached to it**, even a quick
  `--retrieval-only` pass. This is the single habit that keeps this project
  honest — see [`docs/lessons-learned.md`](lessons-learned.md) (a) and (b)
  for what happens without it.

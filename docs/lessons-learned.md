# Lessons learned

Honest engineering notes from building and measuring this system. No asset
detail here — just what was tried, what the numbers said, and what changed
as a result. Every metric below came from `openkb eval` against a fixed
gold-question set, not a vibe check.

## (a) A query-expansion lever shipped on a flawed ad-hoc test — then failed the canonical eval

An early retrieval-quality experiment tried query expansion: rewriting the
user's query with related terms before searching, on the theory that it
would improve recall for paraphrased questions. A quick, informally-run
A/B comparison showed a +4-point improvement and the change nearly shipped
as the default.

Running it through the canonical eval harness instead told a different
story: recall@8 regressed from 94% to 84% on the fixed gold set. The ad-hoc
comparison had used a different, smaller, and less representative question
set, and happened to land on a subset where expansion looked good. The
change was reverted.

**Lesson: one canonical eval set, not an ad-hoc spot check, decides whether
a lever ships.** This is the whole reason `evaluate.py` exists as a fixed
harness rather than "try a few queries and see." It's also why query
expansion isn't in this codebase at all — the entity-boost mechanism that
replaced the idea (see (c) below) was designed specifically to avoid this
failure mode.

## (b) A cross-encoder reranker measured worse than no rerank at all

The intuitive choice for a fast reranker is a cross-encoder microservice
(e.g. `sentence-transformers` `CrossEncoder`) — reads query+passage pairs,
scores each, typically ~100-230ms warm. On this project's own
technical/tabular corpus, measured against the canonical eval:

| Rerank backend | recall@8 | MRR | latency |
|---|---|---|---|
| none (plain RRF) | baseline | baseline | ~0ms |
| cross-encoder service | **82** (worse than none) | **0.617** (worse than none) | ~230ms |
| listwise LLM rerank | **86** (+14 recall@5 vs none) | **0.679** (best) | ~5s |

The cross-encoder, despite being far faster, scored *worse than doing no
reranking at all* on this corpus. The slow listwise LLM rerank scored best
by a clear margin. This is very likely corpus-shape-dependent: a
general-purpose cross-encoder trained on web/QA-style pairs may simply not
have good priors for dense technical/tabular text (part numbers, spec
tables, tag codes), whereas the general instruct model doing listwise rerank
can reason about the passage content directly.

**The choice made:** ship `rerank.backend: llm` as the default, keep the
cross-encoder path fully wired up (`rerank.backend: service`) rather than
deleting it — quality over latency, but re-testable as the model landscape
moves. If you have a corpus where 5 seconds per query is unacceptable and
you have a cross-encoder you trust, `openkb eval` will tell you honestly
whether it beats `none` on your data before you commit to it.

## (c) Entity-boost replaced query expansion — and stays off by default

Given (a)'s failure, the actual fix for "a query naming a specific piece of
equipment should favour documents about that equipment" needed a mechanism
that couldn't inject noise into the lexical search arm. The answer: never
touch the query text at all. `retrieval.entity_boost` looks for an equipment
alias from the registry that appears literally in the query, and gives a
small additive nudge to the *already-fused* ranking for chunks belonging to
that alias's linked documents. It can re-order the fused pool; it can never
change what's in the pool, because it runs after RRF fusion, not before
search.

Measured on the canonical eval (see the design rationale in
`engine.py` and [`docs/architecture.md`](architecture.md)): a small but real
MRR gain with no recall regressions. That is still a property of one corpus
and one query mix, which is exactly why `retrieval.entity_boost.enabled`
defaults to `false` in `config.example.yaml` — it should only be switched on
after it beats your own eval set, not because it beat this project's.

## (d) Eval-set size matters — noisy deltas below ~100 questions

Early iterations ran the eval harness against a 35-question gold set. Deltas
of a few percentage points between configurations were common but often
weren't reproducible — re-running the same configuration against a
marginally-edited gold set could flip the sign of a "win." The gold-question
battery grew to 120 questions specifically to get past this noise floor.
**If you're building your own gold set and seeing a lever "win" by 2-3
points on a 20-30 question set, be suspicious of that result before trusting
it** — grow the set before making it load-bearing for a config change.

## (e) Every retrieval lever is env/config-gated and fails open

A direct consequence of (a) and (b): nothing in `engine.py` or `rerank.py`
assumes a lever should be on. Rerank backend, entity boost, and RRF's
constant are all read from config with a documented default, and every
optional step catches its own exceptions and degrades to the
next-simplest-thing-that-still-works — usually the plain RRF-fused order.
A broken reranker, a corrupt alias-map build, or an unreadable
`superseded.txt` should make search slightly worse, never make it crash.

## (f) OCR transcription cannot fix a graphic-only drawing — describe it instead

Pointing the OCR transcription prompt (`ingest/ocr.py`) at a wiring
schematic or a general-arrangement drawing reliably produced near-empty
output, correctly — there usually isn't a paragraph of prose drawn on the
page to transcribe. Raising OCR resolution or trying a different vision
model didn't change this; the problem wasn't OCR quality, it was that the
task itself was wrong for that kind of document. The document's information
lives in symbols, layout, and connections, not words.

The fix (`ingest/describe.py`) is a different prompt entirely: ask the
vision model to *describe* the drawing — read the title block, list
equipment/components and their labels, summarise the connections — and
index that description instead, prefixed `[VISION DESCRIPTION]` so it's
never mistaken for the document's own transcribed words. This only runs on
documents that are still text-thin after normal extraction, and only swaps
in the description when it adds meaningfully more searchable text than
what's already there.

## (g) Naive XLSX extraction garbles a large register

A generic sheet-to-text dump (walk every cell, join with tabs) works fine
for small reference workbooks, but falls apart on a genuinely large,
strictly tabular register — a cable schedule, an I/O list, an asset master.
With thousands of numeric-looking cells and no natural-language context per
cell, the naive dump degenerates into an undifferentiated wall of numbers:
nothing in the extracted text says "this number is a port," "this one is a
cable ID," "this one is a rack position." It can't be usefully grepped,
chunked, or explained out of context, and if extracted from raw
shared-strings XML rather than through a real workbook reader, cell values
can resolve to meaningless lookup-table indices rather than actual text.

The fix (`extract_register` in `ingest/extract.py`) uses `openpyxl` in
read-only/data-only mode (so formulas resolve to values and shared strings
resolve to real text), finds the header row by two independent signals (at
least 3 non-empty cells, and a case-insensitive match for a configured
`header_keyword`), and aligns every subsequent row under that header,
tab-separated. Configure it per-register in `ingest.registers` — see
[`docs/configuration.md`](configuration.md).

## (h) Maintenance is proposals-only, because an un-fixable KB stops being trusted

Near-duplicate detection, revision-family detection, and equipment-entity
merging all follow the same shape: detect, propose, and only ever act
through an explicit, reversible mechanism (`superseded.txt` append for
dedupe; a `equipment_merge_proposal` row plus a separate `apply` step for
entities). None of it deletes a row from `documents`, `chunks`, or
`equipment` directly. Quarantine (for secret-shaped content) moves a file
out of the inbox rather than discarding it.

This is a deliberate trade-off against a "just delete it" implementation
that would be simpler to write. A knowledge base where a bad automatic
decision can't be cheaply undone is a knowledge base people stop trusting —
and once trust goes, the corpus effectively becomes unusable even if the
underlying data is still fine. Reversibility was treated as a hard
requirement, not a nice-to-have, for every maintenance operation in this
codebase.

## (i) Guard destructive flags behind two explicit signals, not one

`run_ingest`'s `reset_db=True` deletes the entire database (and its WAL/SHM
sidecars) before ingesting. Early in development, a maintenance script left
a reset flag defaulted to "on" in one code path and wiped a live index by
accident. The fix: `reset_db=True` alone now raises `ValueError` — it
additionally requires `confirm=True`, a second, independent boolean, before
anything is actually deleted. Two flags that both have to be true, set at
the same call site, make an accidental reset require a genuinely deliberate
line of code rather than a stray default or a copy-pasted script argument.
(Note: this guard lives in the Python API; see
[`docs/ai-operator-guide.md`](ai-operator-guide.md) for the current gap
between this and the CLI's exposed flags.)

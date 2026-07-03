#!/usr/bin/env bash
# examples/demo.sh -- end-to-end walkthrough of open-kb using the synthetic
# demo corpus in examples/corpus/.
#
# What this script does, in order:
#   1. Checks the configured (or well-known default) LLM/embeddings
#      endpoints are reachable, and exits with setup guidance if not --
#      BEFORE touching any files, so a dead endpoint never leaves you with
#      a half-initialised demo directory.
#   2. Sets up a scratch demo directory (never touches a real ./config.yaml
#      or ./data if one already exists at the repo root).
#   3. Runs `openkb init`, copies the corpus into the inbox, runs
#      `openkb ingest`.
#   4. Runs a few `openkb search` / `openkb ask` examples inspired by the
#      gold question set.
#   5. Runs the entity pipeline: extract -> registry -> merge.
#   6. Runs `openkb eval` against examples/gold.example.jsonl.
#
# This script is NOT run automatically by the test suite -- it requires a
# live local LLM (e.g. Ollama) and embeddings endpoint. Sanity-check its
# bash syntax with `bash -n examples/demo.sh`.
set -euo pipefail

# --------------------------------------------------------------------------
# 0. locate repo root + demo scratch dir
# --------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEMO_DIR="${OPENKB_DEMO_DIR:-$REPO_ROOT/.demo}"

echo "=== open-kb demo ==="
echo "repo root : $REPO_ROOT"
echo "demo dir  : $DEMO_DIR"
echo

# --------------------------------------------------------------------------
# 1. banner: check the configured LLM / embeddings endpoints are reachable
#    BEFORE doing anything else. Read them out of an existing config.yaml
#    if present (repo root, or an already-initialised demo dir), else fall
#    back to the well-known defaults from config.example.yaml.
# --------------------------------------------------------------------------
DEFAULT_GEN_URL="http://127.0.0.1:11434/api/chat"
DEFAULT_EMB_URL="http://127.0.0.1:1234/v1/embeddings"

GEN_URL="$DEFAULT_GEN_URL"
EMB_URL="$DEFAULT_EMB_URL"

CONFIG_CANDIDATE=""
if [ -f "$DEMO_DIR/config.yaml" ]; then
    CONFIG_CANDIDATE="$DEMO_DIR/config.yaml"
elif [ -f "$REPO_ROOT/config.yaml" ]; then
    CONFIG_CANDIDATE="$REPO_ROOT/config.yaml"
fi

if [ -n "$CONFIG_CANDIDATE" ] && command -v python3 >/dev/null 2>&1; then
    # Best-effort YAML read; fall back to defaults on any error.
    READ_URLS="$(python3 - "$CONFIG_CANDIDATE" <<'PYEOF' || true
import sys
try:
    import yaml
except ImportError:
    sys.exit(0)
path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
except OSError:
    sys.exit(0)
gen = (cfg.get("llm") or {}).get("gen_url") or ""
emb = (cfg.get("embeddings") or {}).get("url") or ""
print(gen)
print(emb)
PYEOF
)"
    if [ -n "$READ_URLS" ]; then
        GEN_URL="$(echo "$READ_URLS" | sed -n '1p')"
        EMB_URL="$(echo "$READ_URLS" | sed -n '2p')"
        [ -z "$GEN_URL" ] && GEN_URL="$DEFAULT_GEN_URL"
        [ -z "$EMB_URL" ] && EMB_URL="$DEFAULT_EMB_URL"
    fi
fi

echo "--- Checking local model endpoints ---"
echo "  LLM (chat/generation) : $GEN_URL"
echo "  Embeddings             : $EMB_URL"
echo

check_endpoint() {
    local url="$1"
    local base
    # Strip the path down to scheme://host:port for a lightweight reachability probe.
    base="$(echo "$url" | sed -E 's#(https?://[^/]+).*#\1#')"
    curl -fsS --max-time 3 -o /dev/null "$base" 2>/dev/null && return 0
    # Some servers 404/405 a bare GET on '/' but are still up -- a curl
    # connection failure (exit 7/28) is the real "endpoint down" signal;
    # any HTTP response at all counts as "reachable".
    curl -sS --max-time 3 -o /dev/null -w '%{http_code}' "$base" 2>/dev/null | grep -qE '^[0-9]{3}$'
}

ENDPOINTS_OK=1
if ! check_endpoint "$GEN_URL"; then
    echo "  [FAIL] cannot reach LLM endpoint at $GEN_URL"
    ENDPOINTS_OK=0
fi
if ! check_endpoint "$EMB_URL"; then
    echo "  [FAIL] cannot reach embeddings endpoint at $EMB_URL"
    ENDPOINTS_OK=0
fi

if [ "$ENDPOINTS_OK" -ne 1 ]; then
    cat <<'EOF'

One or more local model endpoints are unreachable. This demo needs a real
chat-capable model and a real embeddings model running locally (e.g. via
Ollama and/or LM Studio) before it can ingest documents or answer questions.

Setup guidance:
  1. Install and start a local inference server, e.g.:
       - Ollama:    https://ollama.com  (serves /api/chat on :11434)
       - LM Studio: https://lmstudio.ai (serves /v1/embeddings on :1234)
  2. Pull/load a chat model and an embeddings model.
  3. Copy config.example.yaml to config.yaml (repo root or this demo dir)
     and point llm.gen_url / embeddings.url at your running server(s).
  4. Re-run this script.

Exiting politely -- nothing has been created or modified yet.
EOF
    exit 1
fi

echo "  [OK] both endpoints reachable -- proceeding."
echo

# --------------------------------------------------------------------------
# 2. scratch config + data dir (never clobber a real user config blindly)
# --------------------------------------------------------------------------
mkdir -p "$DEMO_DIR"

if [ -f "$DEMO_DIR/config.yaml" ]; then
    echo "--- Found existing $DEMO_DIR/config.yaml -- leaving it in place ---"
else
    echo "--- Copying config.example.yaml -> $DEMO_DIR/config.yaml ---"
    cp "$REPO_ROOT/config.example.yaml" "$DEMO_DIR/config.yaml"
    # Point paths at the scratch demo dir instead of the repo-root ./data default.
    python3 - "$DEMO_DIR/config.yaml" "$DEMO_DIR" <<'PYEOF'
import sys
import yaml

path, demo_dir = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as fh:
    cfg = yaml.safe_load(fh) or {}
cfg.setdefault("paths", {})
cfg["paths"]["data_dir"] = demo_dir + "/data"
cfg["paths"]["db_path"] = demo_dir + "/data/kb.db"
cfg["paths"]["inbox"] = demo_dir + "/data/inbox"
cfg["paths"]["curated"] = demo_dir + "/data/curated"
cfg["paths"]["quarantine"] = demo_dir + "/data/quarantine"
cfg.setdefault("eval", {})
cfg["eval"]["gold_path"] = sys.argv[1].rsplit("/", 1)[0] + "/../examples/gold.example.jsonl"
with open(path, "w", encoding="utf-8") as fh:
    yaml.safe_dump(cfg, fh, sort_keys=False)
PYEOF
fi

export OPENKB_CONFIG="$DEMO_DIR/config.yaml"

# --------------------------------------------------------------------------
# 3. init + ingest the synthetic corpus
# --------------------------------------------------------------------------
echo
echo "--- openkb init ---"
openkb --config "$OPENKB_CONFIG" init

echo
echo "--- Copying synthetic corpus into the inbox ---"
mkdir -p "$DEMO_DIR/data/inbox"
cp -v "$SCRIPT_DIR"/corpus/*.md "$DEMO_DIR/data/inbox/" 2>/dev/null || true
cp -v "$SCRIPT_DIR"/corpus/*.xlsx "$DEMO_DIR/data/inbox/" 2>/dev/null || true
cp -v "$SCRIPT_DIR"/corpus/*.pdf "$DEMO_DIR/data/inbox/" 2>/dev/null || true

echo
echo "--- openkb ingest ---"
openkb --config "$OPENKB_CONFIG" ingest

# --------------------------------------------------------------------------
# 4. a few search/ask examples, inspired by the gold question set
# --------------------------------------------------------------------------
echo
echo "--- Example search: 'What feeds MSB-1?' ---"
openkb --config "$OPENKB_CONFIG" search "What feeds MSB-1?"

echo
echo "--- Example search: 'DG1 fails to start on auto-start signal' ---"
openkb --config "$OPENKB_CONFIG" search "DG1 fails to start on auto-start signal"

echo
echo "--- Example ask: 'Which pump is shed from supply when FP-101 needs to start?' ---"
openkb --config "$OPENKB_CONFIG" ask "Which pump is shed from supply when FP-101 needs to start?"

echo
echo "--- Example ask: 'What is the rated output of generator DG1?' ---"
openkb --config "$OPENKB_CONFIG" ask "What is the rated output of generator DG1?"

# --------------------------------------------------------------------------
# 5. entity pipeline: extract -> registry -> merge
# --------------------------------------------------------------------------
echo
echo "--- openkb entities extract (dry-run preview) ---"
openkb --config "$OPENKB_CONFIG" entities extract

echo
echo "--- openkb entities extract --commit ---"
openkb --config "$OPENKB_CONFIG" entities extract --commit

echo
echo "--- openkb entities registry ---"
openkb --config "$OPENKB_CONFIG" entities registry

echo
echo "--- openkb entities merge (proposals only, nothing applied) ---"
openkb --config "$OPENKB_CONFIG" entities merge

# --------------------------------------------------------------------------
# 6. eval harness against the gold question set
# --------------------------------------------------------------------------
echo
echo "--- openkb eval ---"
openkb --config "$OPENKB_CONFIG" eval --gold "$REPO_ROOT/examples/gold.example.jsonl"

echo
echo "=== Demo complete. Scratch data lives under: $DEMO_DIR ==="
echo "    Re-run any 'openkb --config $OPENKB_CONFIG <command>' directly to explore further."

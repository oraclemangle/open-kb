# Deployment — two-host production pattern

Once single-machine ingest and search work (see
[`docs/quickstart.md`](quickstart.md)), the natural next step is splitting
"where the LLMs and ingest run" from "where people/apps query the knowledge
base." open-kb supports this as a first-class pattern: two hosts, one
read-only file copy between them, no shared database service.

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│  HOST A — ingest box          │        │  HOST B — serving host        │
│  (spare Mac mini / any        │  SSH   │  (small VM, cheap to run,     │
│   32GB+ machine)               │  push  │   no GPU/model needed)        │
│                                │──────▶ │                                │
│  Ollama + LM Studio (or any    │  kb.db │  openkb serve                 │
│  local LLM server)             │ replica│  (REST API + bundled chat UI) │
│  openkb ingest (cron/timer)   │        │  openkb mcp (stdio, per-client)│
│  owns the WRITABLE database    │        │  reads a READ-ONLY replica     │
└─────────────────────────────┘        └──────────────────────────────┘
```

HOST A does the expensive, occasional work: running local models, chewing
through the inbox, building the entity registry. HOST B does the cheap,
constant work: answering queries against a snapshot, with no model inference
of its own required (retrieval + a lightweight API only — HOST B doesn't
even need `llm.gen_url` to point anywhere reachable unless you also want
`ask`/`serve` to generate answers there rather than just serve raw search
results; if HOST B should also run `ask`, point its `llm.gen_url` /
`embeddings.url` at HOST A's model server over your private network).

## 1. SSH key setup between hosts

Generate a **dedicated** key for this purpose — don't reuse a personal SSH
key for an unattended sync job.

On HOST A (the ingest box):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_openkb_replica -N "" -C "openkb-sync"
```

Copy the public key to HOST B, under the account that will own the replica
file:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519_openkb_replica.pub <replica-user>@<replica-host>
```

Confirm it works non-interactively (this is exactly the call `openkb sync`
makes internally):

```bash
ssh -i ~/.ssh/id_ed25519_openkb_replica -o BatchMode=yes <replica-user>@<replica-host> "echo ok"
```

Then set the `sync` section in HOST A's `config.yaml`:

```yaml
sync:
  enabled: true
  replica: <replica-user>@<replica-host>
  ssh_key: ~/.ssh/id_ed25519_openkb_replica
  remote_db_path: /srv/open-kb/kb.db
  ssh_timeout_s: 30
  transfer_timeout_s: 600
```

On HOST B, `config.yaml` should point `paths.db_path` at that same path
(`/srv/open-kb/kb.db`) and does **not** need `sync.*` configured at all —
HOST B only ever reads the file that lands there; it never initiates a sync.

## 2. `openkb sync` semantics

```bash
openkb sync
```

What happens, in order (implemented in `src/openkb/sync.py`):

1. **Snapshot.** The live database (which runs in WAL mode for
   concurrent-friendly writes — its true state is spread across `kb.db`,
   `kb.db-wal`, `kb.db-shm`) is snapshotted via SQLite's `backup()` API
   against a `mode=ro` connection — safe to run while ingest is writing.
   The snapshot is then flattened to `journal_mode=DELETE`, collapsing
   everything into one self-contained file with no WAL sidecars.
2. **Content-gated.** SHA-256 over the consistent flattened snapshot and
   `superseded.txt` is compared with the last successful sentinel. This sees
   committed WAL-only changes; unchanged content skips network transfer.
3. **Ship with deadlines.** The snapshot and `superseded.txt` are `scp`'d to the replica
   host as `<remote_db_path>.incoming`.
4. **Atomic swap.** A same-directory `mv` on the remote host replaces the
   live replica file. POSIX guarantees this is atomic — a reader mid-request
   keeps its already-open file descriptor on the old inode; the next
   request opens the new file. **There is no restart needed on HOST B** —
   every request there opens a fresh `mode=ro` SQLite connection per call
   (see `src/openkb/db.py`), so the very next `search`/`ask`/API call simply
   sees the new data.
5. **Sentinel written only on success.** A failed sync leaves the sentinel
   untouched, so the next scheduled run retries automatically rather than
   silently skipping a failed attempt.

Locking: `data/.sync.lockd/owner.json` records PID, host and a random ownership
token. An old lock is never stolen while its local PID is alive (or its owner
cannot be verified). A confirmed-dead stale owner can be reclaimed, and only
the matching token can release the current lock.

## 3. Scheduling both ingest and sync

Run `openkb ingest` on a timer on HOST A (it's a no-op / fast pass over the
manifest when the inbox is empty), and `openkb sync` on a timer on HOST A
right after it (or on its own tighter schedule — it's cheap to call when
nothing changed).

### macOS — launchd

Create `~/Library/LaunchAgents/com.openkb.ingest.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.openkb.ingest</string>
    <key>ProgramArguments</key>
    <array>
        <string><PLACEHOLDER_VENV_PATH>/bin/openkb</string>
        <string>--config</string>
        <string><PLACEHOLDER_REPO_PATH>/config.yaml</string>
        <string>ingest</string>
    </array>
    <key>WorkingDirectory</key><string><PLACEHOLDER_REPO_PATH></string>
    <key>StartInterval</key><integer>900</integer>  <!-- every 15 minutes -->
    <key>StandardOutPath</key><string><PLACEHOLDER_REPO_PATH>/logs/ingest.log</string>
    <key>StandardErrorPath</key><string><PLACEHOLDER_REPO_PATH>/logs/ingest.err.log</string>
</dict>
</plist>
```

Create a second plist, `com.openkb.sync.plist`, identical shape but with
`sync` in place of `ingest` and its own log paths — run it a few minutes
after the ingest interval so it always has a fresh DB to ship.

Load and check both:

```bash
launchctl load ~/Library/LaunchAgents/com.openkb.ingest.plist
launchctl load ~/Library/LaunchAgents/com.openkb.sync.plist
launchctl list | grep com.openkb
```

### Linux — systemd service + timer

`/etc/systemd/system/openkb-ingest.service`:

```ini
[Unit]
Description=open-kb ingest

[Service]
Type=oneshot
WorkingDirectory=<PLACEHOLDER_REPO_PATH>
ExecStart=<PLACEHOLDER_VENV_PATH>/bin/openkb --config <PLACEHOLDER_REPO_PATH>/config.yaml ingest
User=<PLACEHOLDER_SERVICE_USER>
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=<PLACEHOLDER_REPO_PATH>/data
MemoryMax=4G
CPUQuota=200%
```

`/etc/systemd/system/openkb-ingest.timer`:

```ini
[Unit]
Description=Run open-kb ingest periodically

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
```

Mirror both for `openkb-sync.service` / `openkb-sync.timer` (swap `ingest`
for `sync`, offset the timer by a few minutes).

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now openkb-ingest.timer openkb-sync.timer
systemctl list-timers | grep openkb
```

On the serving host (HOST B), there is nothing to schedule for retrieval
itself — `openkb serve` runs as a long-lived process (its own
systemd/launchd service, no timer) and simply reads whatever file is at
`db_path` on each request.

## 4. Reverse proxy note

`openkb serve` binds to `api.host`/`api.port` — the default is
`127.0.0.1:8080`, i.e. loopback-only. That is the safe default: nothing
outside the host can reach it.

If you need the API reachable beyond localhost:

1. **Set `api.token`** in `config.yaml` first. With a token set, every
   `POST` route (`/search`, `/ask`) requires `Authorization: Bearer
   <token>`; `GET` routes (`/`, `/health`, `/stats`) stay open by design
   (health checks and the static UI shouldn't need a token to load — the UI
   itself has a token field it attaches to its own POST calls).
2. **Keep `api.host` at `127.0.0.1`** and put a reverse proxy (nginx, Caddy)
   in front, terminating TLS and forwarding to the loopback port. Example
   Caddy snippet:

   ```
   kb.example.internal {
       reverse_proxy 127.0.0.1:8080
   }
   ```

   Caddy handles TLS automatically; for nginx, terminate TLS with your usual
   certificate setup and `proxy_pass http://127.0.0.1:8080;`.

3. Do **not** bind `api.host` to `0.0.0.0` without both a token and a
   proxy/firewall in front — the API has no CORS restriction by design
   (same-origin only, since a wildcard CORS policy would let any website's
   JavaScript query a private knowledge base from a visitor's browser), and
   no built-in rate limiting.

## 5. Backup guidance

Back up two things together, but never archive a live WAL database file:

- a verified SQLite backup created with the online backup API
- the curated tree: `paths.curated` — this holds the actual source
  documents your KB was built from, organised by taxonomy domain, plus the
  manifest (`_MANIFEST.jsonl`) that makes ingest resumable

```bash
# HOST A: create and verify a self-contained DB snapshot first
openkb backup data/backups/kb-$(date +%Y%m%d).db
openkb restore-check data/backups/kb-$(date +%Y%m%d).db

# archive that verified snapshot with source documents and supersession state
tar czf backup-$(date +%Y%m%d).tar.gz \
  data/backups/kb-$(date +%Y%m%d).db data/curated data/superseded.txt
```

**Verify restores.** A backup you've never restored is a hypothesis, not a
backup. Periodically:

```bash
python -c 'from openkb.backup import restore_copy; import json; print(json.dumps(restore_copy("data/backups/kb-YYYYMMDD.db", "/tmp/openkb-restore-test.db"), indent=2))'
openkb restore-check /tmp/openkb-restore-test.db
```

and require `quick_check: ok` plus matching document/chunk/vector/FTS counts.
For the curated tree, spot-check
that a handful of `rel_path` values from `documents` actually resolve to
files on disk.

If you run the two-host pattern, HOST B's replica is itself a live,
disposable copy — the durable backup obligation lives on HOST A (the
writable original), not on the replica.

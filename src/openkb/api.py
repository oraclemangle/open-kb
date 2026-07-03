"""REST front-door for open-kb -- stdlib `http.server` only, no framework.

Thin wrapper over `openkb.engine` (the shared retrieval/answer truth layer).
Read-mostly: every route except the two POST routes is a GET, and even the
POST routes only *read* the database (search/ask never write).

Routes
------
  GET  /                 static/index.html (the bundled chat UI)
  GET  /health           {"ok": true, "documents": N, "chunks": N}
  GET  /stats            per-domain counts + equipment count + db file size
  POST /search           {"query", "domains"?, "k"?}  -> list of hits
  POST /ask              {"query", "domains"?, "k"?}  -> {"answer","sources"}

Auth
----
If `api.token` is set in config, every POST route requires
`Authorization: Bearer <token>`. GET routes stay open (health checks and the
static UI shouldn't need a token to load; the UI itself prompts for one and
attaches it to its own POST calls). With no token configured, POST routes
are open too -- the same "local trust" model as talking to Ollama on
localhost. Binding this server to a non-loopback address without a token is
your call to make, not this module's -- it does not enforce a bind-address
policy, unlike some deployments that refuse 0.0.0.0 without auth.

CORS
----
Deliberately NOT set. A private knowledge-base API has no business
inviting arbitrary browser origins to read from it -- a wildcard
`Access-Control-Allow-Origin: *` would let any website's JavaScript query
your KB from a visitor's browser if they ever hit this port. Same-origin
only. If you need a browser on a *different* origin to call this API
(e.g. a separate dashboard app), put a same-origin reverse proxy in front
of both rather than loosening CORS here.
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

MAX_QUERY_LEN = 2000
MAX_K = 50


def _make_handler(cfg: dict):
    token = str(cfg.get("api", {}).get("token") or "").strip()

    class Handler(BaseHTTPRequestHandler):
        server_version = "openkb/0.1"

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib override
            sys.stderr.write("[api] %s - %s\n" % (self.address_string(), fmt % args))

        # -- helpers ----------------------------------------------------
        def _send_json(self, code: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, code: int, message: str) -> None:
            self._send_json(code, {"error": message})

        def _authorized(self) -> bool:
            if not token:
                return True
            hdr = self.headers.get("Authorization", "")
            if not hdr.lower().startswith("bearer "):
                return False
            supplied = hdr[7:].strip()
            import hmac

            return bool(supplied) and hmac.compare_digest(supplied, token)

        def _read_json_body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return None
            if length <= 0 or length > 1_000_000:
                return None
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return data if isinstance(data, dict) else None

        def _clamp_k(self, raw, default: int) -> int | None:
            if raw is None:
                return default
            try:
                k = int(raw)
            except (TypeError, ValueError):
                return None
            return max(1, min(k, MAX_K))

        # -- routes -------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            path = urlparse(self.path).path
            try:
                if path == "/":
                    self._serve_index()
                elif path == "/health":
                    self._handle_health()
                elif path == "/stats":
                    self._handle_stats()
                else:
                    self._send_error_json(404, "unknown route: %s" % path)
            except Exception as exc:  # pragma: no cover - defensive
                print("[api] 500 on %s: %s" % (path, exc), file=sys.stderr)
                self._send_error_json(500, "internal error")

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            path = urlparse(self.path).path
            try:
                if not self._authorized():
                    self._send_error_json(401, "unauthorized")
                    return
                if path == "/search":
                    self._handle_search()
                elif path == "/ask":
                    self._handle_ask()
                else:
                    self._send_error_json(404, "unknown route: %s" % path)
            except Exception as exc:  # pragma: no cover - defensive
                print("[api] 500 on %s: %s" % (path, exc), file=sys.stderr)
                self._send_error_json(500, "internal error")

        # -- handlers -------------------------------------------------------
        def _serve_index(self) -> None:
            import os

            index_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
            try:
                with open(index_path, "rb") as fh:
                    body = fh.read()
            except OSError:
                self._send_error_json(404, "static/index.html not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_health(self) -> None:
            from . import db

            try:
                con = db.connect(cfg["paths"]["db_path"], read_only=True)
                try:
                    docs = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
                    chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                finally:
                    con.close()
                self._send_json(200, {"ok": True, "documents": docs, "chunks": chunks})
            except Exception as exc:
                self._send_json(200, {"ok": False, "error": str(exc)})

        def _handle_stats(self) -> None:
            import os

            from . import db

            con = db.connect(cfg["paths"]["db_path"], read_only=True)
            try:
                by_domain = con.execute(
                    "SELECT domain, COUNT(*), COALESCE(SUM(n_chunks), 0) "
                    "FROM documents GROUP BY domain ORDER BY domain"
                ).fetchall()
                equipment_count = con.execute("SELECT COUNT(*) FROM equipment").fetchone()[0]
            finally:
                con.close()
            db_path = cfg["paths"]["db_path"]
            db_size = os.path.getsize(db_path) if os.path.isfile(db_path) else 0
            self._send_json(
                200,
                {
                    "domains": [
                        {"domain": d, "documents": n, "chunks": c} for d, n, c in by_domain
                    ],
                    "equipment": equipment_count,
                    "db_size_bytes": db_size,
                },
            )

        def _handle_search(self) -> None:
            from .engine import search

            body = self._read_json_body() or {}
            query = str(body.get("query") or "").strip()
            if not query:
                self._send_error_json(400, "missing 'query'")
                return
            if len(query) > MAX_QUERY_LEN:
                self._send_error_json(400, "query too long (max %d)" % MAX_QUERY_LEN)
                return
            k = self._clamp_k(body.get("k"), cfg["retrieval"]["k"])
            if k is None:
                self._send_error_json(400, "'k' must be an integer")
                return
            domains = body.get("domains") or None
            if domains is not None and not isinstance(domains, list):
                self._send_error_json(400, "'domains' must be a list of strings")
                return
            mode = body.get("mode", "hybrid")
            if mode not in ("hybrid", "vector", "fts"):
                self._send_error_json(400, "'mode' must be hybrid|vector|fts")
                return
            hits = search(query, cfg=cfg, domains=domains, k=k, mode=mode)
            self._send_json(200, {"hits": hits})

        def _handle_ask(self) -> None:
            from .engine import ask

            body = self._read_json_body() or {}
            query = str(body.get("query") or "").strip()
            if not query:
                self._send_error_json(400, "missing 'query'")
                return
            if len(query) > MAX_QUERY_LEN:
                self._send_error_json(400, "query too long (max %d)" % MAX_QUERY_LEN)
                return
            k = self._clamp_k(body.get("k"), cfg["retrieval"]["k"])
            if k is None:
                self._send_error_json(400, "'k' must be an integer")
                return
            domains = body.get("domains") or None
            if domains is not None and not isinstance(domains, list):
                self._send_error_json(400, "'domains' must be a list of strings")
                return
            result = ask(query, cfg=cfg, domains=domains, k=k)
            self._send_json(200, result)

    return Handler


def serve(cfg: dict) -> None:
    """Run the REST API forever (blocking). Ctrl-C to stop."""
    host = cfg["api"]["host"]
    port = int(cfg["api"]["port"])
    handler = _make_handler(cfg)
    httpd = ThreadingHTTPServer((host, port), handler)
    auth_state = "token required" if cfg["api"].get("token") else "open (no token configured)"
    print("open-kb API listening on http://%s:%d  (%s)" % (host, port, auth_state))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main() -> None:
    from .config import load_config

    serve(load_config())


if __name__ == "__main__":
    main()

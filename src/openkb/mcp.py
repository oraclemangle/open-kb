"""Minimal MCP (Model Context Protocol) stdio server for open-kb.

Hand-rolled JSON-RPC 2.0 over stdin/stdout -- stdlib only, no `mcp` SDK
dependency. One JSON object per line in, one JSON object per line out.
This keeps open-kb installable with zero extra packages just to expose it
to an MCP-capable client (Claude Code, Codex, etc).

Methods implemented
--------------------
  initialize              -> protocol version + server info + capabilities
  notifications/initialized  (no response; client->server notification)
  tools/list               -> the 3 tools below, with JSON-schema input specs
  tools/call               -> dispatches to openkb.engine / a status query
  ping                      -> {} (liveness)

Tools exposed
-------------
  kb_search  {query, domains?, k?}  -> ranked chunks with citations
  kb_ask     {query, domains?, k?}  -> {"answer","sources"} grounded answer
  kb_status  {}                     -> document/chunk/equipment counts

Registering with an MCP client
-------------------------------
Generic one-liner (adjust for your client's CLI):

    claude mcp add open-kb -- openkb mcp

Or by hand in the client's `mcpServers` config:

    "open-kb": {
      "command": "openkb",
      "args": ["mcp"]
    }

If you need a specific config file, point at it explicitly since MCP
clients don't inherit your shell's cwd assumptions:

    "open-kb": {
      "command": "openkb",
      "args": ["--config", "/path/to/config.yaml", "mcp"]
    }
"""
from __future__ import annotations

import json
import sys
import traceback
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "open-kb", "version": "0.1.0"}

TOOLS = [
    {
        "name": "kb_search",
        "description": (
            "Search the knowledge base. Returns ranked text chunks with source "
            "citations and domain. Hybrid vector+keyword search over ingested "
            "documents."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language or keyword query"},
                "k": {"type": "integer", "description": "Number of results (default from config)"},
                "domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional domain filter, e.g. ['00_ELECTRICAL']",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "kb_ask",
        "description": (
            "Ask a question; retrieves relevant chunks and returns a generated "
            "answer grounded ONLY in the retrieved context, with inline citations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "description": "Chunks to retrieve (default from config)"},
                "domains": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["query"],
        },
    },
    {
        "name": "kb_status",
        "description": "Knowledge-base health: document, chunk, and equipment counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _kb_status(cfg: dict) -> dict[str, Any]:
    from . import db

    con = db.connect(cfg["paths"]["db_path"], read_only=True)
    try:
        documents = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        equipment = con.execute("SELECT COUNT(*) FROM equipment").fetchone()[0]
    finally:
        con.close()
    return {"documents": documents, "chunks": chunks, "equipment": equipment}


def _dispatch(cfg: dict, name: str, args: dict) -> Any:
    if name == "kb_search":
        from .engine import search

        return search(
            args["query"],
            cfg=cfg,
            domains=args.get("domains"),
            k=args.get("k"),
        )
    if name == "kb_ask":
        from .engine import ask

        return ask(
            args["query"],
            cfg=cfg,
            domains=args.get("domains"),
            k=args.get("k"),
        )
    if name == "kb_status":
        return _kb_status(cfg)
    raise ValueError("unknown tool: %s" % name)


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(msg_id, result) -> None:
    _send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _error(msg_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def main(cfg: dict | None = None) -> None:
    """Run the MCP stdio loop until stdin closes. Blocking."""
    if cfg is None:
        from .config import load_config

        cfg = load_config()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        try:
            if method == "initialize":
                _result(
                    msg_id,
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": SERVER_INFO,
                    },
                )
            elif method == "notifications/initialized":
                pass  # notification: no response expected
            elif method == "tools/list":
                _result(msg_id, {"tools": TOOLS})
            elif method == "tools/call":
                name = params.get("name")
                args = params.get("arguments") or {}
                out = _dispatch(cfg, name, args)
                _result(msg_id, {"content": [{"type": "text", "text": json.dumps(out, indent=2, ensure_ascii=False)}]})
            elif method == "ping":
                _result(msg_id, {})
            elif msg_id is not None:
                _error(msg_id, -32601, "method not found: %s" % method)
        except Exception as exc:
            if msg_id is not None:
                tb = traceback.format_exc()[-500:]
                _error(msg_id, -32603, "%s\n%s" % (exc, tb))


if __name__ == "__main__":
    main()

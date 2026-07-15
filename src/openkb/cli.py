"""Command-line entry point for open-kb.

One binary, many subcommands. Each subcommand's implementation lives in a
sibling module (engine, evaluate, ingest.worker, entities.*, dedupe, sync,
api, mcp) and is imported *lazily*, inside the handler function that needs
it -- not at module load time. That means:

  - `openkb --help` and argument parsing work even if optional deps for one
    subcommand (e.g. PyMuPDF for ingest, or a not-yet-built sibling module)
    are missing or broken.
  - A broken/missing module only breaks the one subcommand that uses it,
    not the whole CLI.

Output is human-friendly by default; commands that benefit from machine
consumption (`eval`, `status`) accept `--json`.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .config import load_config


def _print_json(obj: Any, path: str | None = None) -> None:
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)


def cmd_init(args: argparse.Namespace) -> int:
    """Create the data directories and initialise the SQLite schema."""
    from . import db

    cfg = load_config(args.config)
    import os

    for key in ("data_dir", "inbox", "curated", "quarantine"):
        os.makedirs(cfg["paths"][key], exist_ok=True)
    os.makedirs(os.path.dirname(cfg["paths"]["db_path"]) or ".", exist_ok=True)

    con = db.connect(cfg["paths"]["db_path"])
    try:
        db.init_schema(con, dim=int(cfg["embeddings"]["dim"]))
    finally:
        con.close()

    print("Initialised open-kb.")
    print("  data dir : %s" % cfg["paths"]["data_dir"])
    print("  database : %s" % cfg["paths"]["db_path"])
    print()
    print("Next steps:")
    print("  1. Copy config.example.yaml to config.yaml and point it at your")
    print("     local LLM / embedding endpoints.")
    print("  2. Drop source documents into: %s" % cfg["paths"]["inbox"])
    print("  3. Run: openkb ingest")
    print("  4. Run: openkb ask \"<question>\"")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from .ingest.worker import run_ingest

    cfg = load_config(args.config)
    result = run_ingest(cfg, limit=args.limit, dry_run=args.dry_run, reset_db=args.reset_db)
    _print_json(result)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from .engine import search

    cfg = load_config(args.config)
    domains = args.domains.split(",") if args.domains else None
    hits = search(args.query, cfg=cfg, domains=domains, k=args.k, mode=args.mode)
    if args.json:
        _print_json(hits)
        return 0
    for i, h in enumerate(hits, 1):
        src = h.get("source") or h.get("rel_path") or "?"
        score = h.get("score")
        print("[%d] %-60s  score=%s" % (i, src, ("%.4f" % score) if score is not None else "?"))
        text = (h.get("text") or "").strip().replace("\n", " ")
        print("    %s" % (text[:160] + ("..." if len(text) > 160 else "")))
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    from .engine import ask

    cfg = load_config(args.config)
    domains = args.domains.split(",") if args.domains else None
    result = ask(args.query, cfg=cfg, domains=domains, k=args.k)
    if args.json:
        _print_json(result)
        return 0
    print(result.get("answer", "").strip())
    sources = result.get("sources") or []
    if sources:
        print("\nSources:")
        for i, s in enumerate(sources, 1):
            label = s.get("source") or s.get("rel_path") or s.get("path") or "?"
            print("  [%d] %s" % (i, label))
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    from .ingest.describe import describe_thin_documents
    from .ingest.worker import embed_chunk

    cfg = load_config(args.config)
    result = describe_thin_documents(
        cfg, embed_fn=lambda text: embed_chunk(text, cfg), limit=args.limit, commit=args.commit
    )
    _print_json(result)
    return 0


def cmd_maintenance(args: argparse.Namespace) -> int:
    from .maintenance import check_consistency, dead_letter_report, request_reextract, request_retry

    cfg = load_config(args.config)
    if args.action == "check":
        result = check_consistency(cfg, stale_days=args.stale_days)
    elif args.action == "dead-letters":
        result = dead_letter_report(cfg)
    elif args.action == "retry":
        if not args.src:
            raise ValueError("retry requires --src")
        result = request_retry(cfg, args.src, commit=args.commit)
    else:
        if not args.rel_path:
            raise ValueError("reextract requires --rel-path")
        result = request_reextract(cfg, args.rel_path, commit=args.commit)
    _print_json(result)
    return 0 if not isinstance(result, dict) or result.get("ok", True) else 2


def cmd_entities(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    action = args.action

    if action == "extract":
        from .entities.extract import extract_entities

        result = extract_entities(cfg, limit=args.limit, commit=args.commit)
    elif action == "registry":
        from .entities.registry import build_registry

        result = build_registry(cfg)
    elif action == "merge":
        from .entities.merge import propose_merges

        result = propose_merges(cfg, limit=args.limit, adjudicate=True)
    elif action == "apply":
        from .entities.merge import apply_merges

        result = apply_merges(cfg, min_conf=args.min_conf)
    else:  # pragma: no cover - argparse choices already restrict this
        raise ValueError("unknown entities action: %s" % action)

    _print_json(result)
    return 0


def cmd_dedupe(args: argparse.Namespace) -> int:
    from . import dedupe

    cfg = load_config(args.config)
    if args.action == "near":
        result = dedupe.find_near_dups(cfg)
    else:  # revisions
        families = dedupe.find_revision_families(cfg)
        if args.commit:
            rel_paths = [m["rel_path"] for fam in families for m in fam["supersede"]]
            dedupe.supersede(cfg, rel_paths, reason="revision superseded", commit=True)
        result = families
    _print_json(result)
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from .evaluate import run_eval

    cfg = load_config(args.config)
    result = run_eval(cfg, gold_path=args.gold, k=args.k, retrieval_only=args.retrieval_only)
    _print_json(result, path=args.json)
    if args.json:
        print("Wrote eval report -> %s" % args.json)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from .sync import run_sync

    cfg = load_config(args.config)
    ok = run_sync(cfg)
    print("sync: %s" % ("OK" if ok else "FAILED / SKIPPED"))
    return 0 if ok else 1


def cmd_backup(args: argparse.Namespace) -> int:
    from .backup import create_backup

    cfg = load_config(args.config)
    result = create_backup(cfg["paths"]["db_path"], args.path)
    _print_json(result)
    return 0 if result.get("ok") else 1


def cmd_restore_check(args: argparse.Namespace) -> int:
    from .backup import verify_backup

    result = verify_backup(args.path)
    _print_json(result)
    return 0 if result.get("ok") else 1


def cmd_serve(args: argparse.Namespace) -> int:
    from .api import serve

    cfg = load_config(args.config)
    if args.host:
        cfg["api"]["host"] = args.host
    if args.port:
        cfg["api"]["port"] = args.port
    serve(cfg)
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp import main as mcp_main

    mcp_main(load_config(args.config))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    from . import db

    counts: dict[str, Any] = {"documents": 0, "chunks": 0, "equipment": 0}
    error = None
    try:
        con = db.connect(cfg["paths"]["db_path"], read_only=True)
        try:
            counts["documents"] = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            counts["chunks"] = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            counts["equipment"] = con.execute("SELECT COUNT(*) FROM equipment").fetchone()[0]
        finally:
            con.close()
    except Exception as exc:  # database missing / not initialised yet
        error = str(exc)

    summary = {
        "db_path": cfg["paths"]["db_path"],
        "counts": counts,
        "rerank_enabled": cfg["rerank"]["enabled"],
        "entity_boost_enabled": cfg["retrieval"]["entity_boost"]["enabled"],
        "retrieval_k": cfg["retrieval"]["k"],
        "error": error,
    }

    if args.json:
        _print_json(summary)
        return 0

    print("open-kb status")
    print("  database   : %s" % summary["db_path"])
    if error:
        print("  (not initialised yet -- run `openkb init`: %s)" % error)
    else:
        print("  documents  : %d" % counts["documents"])
        print("  chunks     : %d" % counts["chunks"])
        print("  equipment  : %d" % counts["equipment"])
    print("  retrieval.k        : %s" % summary["retrieval_k"])
    print("  rerank.enabled     : %s" % summary["rerank_enabled"])
    print("  entity_boost       : %s" % summary["entity_boost_enabled"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openkb", description="Local-first RAG knowledge base.")
    p.add_argument("--config", default=None, help="Path to config.yaml (default: auto-discover)")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="Create data directories and initialise the database")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("ingest", help="Ingest documents from the inbox")
    sp.add_argument("--limit", type=int, default=None, help="Max documents to process")
    sp.add_argument("--dry-run", action="store_true", help="Do not write to the database")
    sp.add_argument("--reset-db", action="store_true", help="Drop and recreate the schema first")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("search", help="Hybrid search over the knowledge base")
    sp.add_argument("query")
    sp.add_argument("--domains", default=None, help="Comma-separated domain filter")
    sp.add_argument("-k", type=int, default=None, help="Number of results")
    sp.add_argument("--mode", default="hybrid", choices=("hybrid", "vector", "fts"))
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("ask", help="Ask a question, grounded in retrieved chunks")
    sp.add_argument("query")
    sp.add_argument("--domains", default=None, help="Comma-separated domain filter")
    sp.add_argument("-k", type=int, default=None, help="Chunks to retrieve")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("describe", help="Vision-describe text-thin documents (scans, drawings)")
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--commit", action="store_true", help="Write descriptions back (default: preview)")
    sp.set_defaults(func=cmd_describe)

    sp = sub.add_parser("maintenance", help="Audit corpus/index consistency or manage dead letters")
    sp.add_argument("action", choices=("check", "dead-letters", "retry", "reextract"))
    sp.add_argument("--src", default=None, help="Exact dead-letter source path for retry")
    sp.add_argument("--rel-path", default=None, help="Exact curated rel_path for re-extraction")
    sp.add_argument("--commit", action="store_true", help="Append a durable retry request (default: preview)")
    sp.add_argument("--stale-days", type=int, default=365, help="Age threshold reported by maintenance check")
    sp.set_defaults(func=cmd_maintenance)

    sp = sub.add_parser("entities", help="Entity/equipment extraction and registry pipeline")
    sp.add_argument("action", choices=("extract", "registry", "merge", "apply"))
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--commit", action="store_true", help="Write results back (default: preview)")
    sp.add_argument("--min-conf", type=float, default=0.9, help="Confidence threshold for `apply`")
    sp.set_defaults(func=cmd_entities)

    sp = sub.add_parser("dedupe", help="Find near-duplicates or revision families")
    sp.add_argument("action", choices=("near", "revisions"))
    sp.add_argument("--commit", action="store_true", help="Mark superseded revisions (revisions only)")
    sp.set_defaults(func=cmd_dedupe)

    sp = sub.add_parser("eval", help="Run the retrieval/answer quality eval harness")
    sp.add_argument("--gold", default=None, help="Path to gold JSONL (default: config eval.gold_path)")
    sp.add_argument("--retrieval-only", action="store_true", help="Skip answer generation, score retrieval only")
    sp.add_argument("--json", default=None, help="Write full JSON report to this path")
    sp.set_defaults(func=cmd_eval)

    sp = sub.add_parser("sync", help="Promote the local database to the read-replica")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("backup", help="Create and verify a consistent SQLite backup")
    sp.add_argument("path", help="Destination backup database path")
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("restore-check", help="Verify a backup without replacing the live database")
    sp.add_argument("path", help="Backup database path to verify")
    sp.set_defaults(func=cmd_restore_check)

    sp = sub.add_parser("serve", help="Run the REST API")
    sp.add_argument("--host", default=None)
    sp.add_argument("--port", type=int, default=None)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("mcp", help="Run the MCP stdio server")
    sp.set_defaults(func=cmd_mcp)

    sp = sub.add_parser("status", help="Show document/chunk/equipment counts and config summary")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

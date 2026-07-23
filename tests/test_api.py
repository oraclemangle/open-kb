"""Tests for openkb.api -- GET /domains and GET /source, plus a regression
guard that /domains and /stats stay unauthenticated even when a token is
configured. Runs a real ThreadingHTTPServer on an ephemeral loopback port so
requests go through the actual do_GET/do_POST dispatch (including the
_authorized() bearer-token check), no mocking of the HTTP layer itself.
Nothing here calls a live LLM or embeddings endpoint."""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from openkb import api
from openkb import db as dbmod

from .conftest import TEST_DIM


def _seed_document(con, rel_path, domain, texts, source=None, extractor="text", summary="a summary"):
    cur = con.execute(
        "INSERT INTO documents(source, rel_path, domain, sha256, summary, n_chunks, extractor) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source or os.path.basename(rel_path), rel_path, domain, "sha-" + rel_path, summary, len(texts), extractor),
    )
    document_id = cur.lastrowid
    for seq, text in enumerate(texts):
        con.execute(
            "INSERT INTO chunks(document_id, seq, text) VALUES (?, ?, ?)", (document_id, seq, text)
        )
    con.commit()
    return document_id


@pytest.fixture
def running_server(cfg):
    """Start the real API server on an ephemeral port and yield (base_url, cfg)."""
    os.makedirs(os.path.dirname(cfg["paths"]["db_path"]), exist_ok=True)
    con = dbmod.connect(cfg["paths"]["db_path"])
    dbmod.init_schema(con, dim=cfg["embeddings"]["dim"])

    handler = api._make_handler(cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        yield "http://127.0.0.1:%d" % port, cfg, con
    finally:
        httpd.shutdown()
        httpd.server_close()
        con.close()


def _get(base_url, path, token=None):
    req = urllib.request.Request(base_url + path)
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_domains_counts_and_zero_fill(running_server):
    base_url, cfg, con = running_server
    taxonomy = cfg["taxonomy"]
    domain_with_docs = taxonomy[0]
    domain_without_docs = taxonomy[1]

    _seed_document(con, "a/doc-a.md", domain_with_docs, ["hello world"])
    _seed_document(con, "b/doc-b.md", domain_with_docs, ["another doc"])

    status, body = _get(base_url, "/domains")
    assert status == 200
    by_name = {d["name"]: d["documents"] for d in body["domains"]}

    assert by_name[domain_with_docs] == 2
    assert by_name[domain_without_docs] == 0
    # every configured taxonomy domain must be present
    for name in taxonomy:
        assert name in by_name


def test_source_found_returns_document_and_joined_text_in_seq_order(running_server):
    base_url, cfg, con = running_server
    doc_id = _seed_document(
        con,
        "docs/manual.md",
        cfg["taxonomy"][0],
        ["first chunk", "second chunk", "third chunk"],
        source="manual.md",
        extractor="text",
        summary="a test manual",
    )

    status, body = _get(base_url, "/source?document_id=%d" % doc_id)
    assert status == 200
    assert body["document"] == {
        "id": doc_id,
        "source": "manual.md",
        "rel_path": "docs/manual.md",
        "domain": cfg["taxonomy"][0],
        "summary": "a test manual",
        "extractor": "text",
        "n_chunks": 3,
    }
    assert body["text"] == "first chunk\n\nsecond chunk\n\nthird chunk"

    # alternative lookup key
    status2, body2 = _get(base_url, "/source?rel_path=docs/manual.md")
    assert status2 == 200
    assert body2 == body


def test_source_404_for_unknown_id_and_unknown_rel_path(running_server):
    base_url, _cfg, _con = running_server

    status, body = _get(base_url, "/source?document_id=999999")
    assert status == 404
    assert "error" in body

    status2, body2 = _get(base_url, "/source?rel_path=does/not/exist.md")
    assert status2 == 404
    assert "error" in body2


def test_source_requires_token_when_configured(cfg):
    cfg["api"]["token"] = "s3cret-test-token"
    os.makedirs(os.path.dirname(cfg["paths"]["db_path"]), exist_ok=True)
    con = dbmod.connect(cfg["paths"]["db_path"])
    dbmod.init_schema(con, dim=cfg["embeddings"]["dim"])
    doc_id = _seed_document(con, "docs/manual.md", cfg["taxonomy"][0], ["some text"])

    handler = api._make_handler(cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    base_url = "http://127.0.0.1:%d" % port
    try:
        # No token supplied -> rejected, same shape as POST route rejection.
        status, body = _get(base_url, "/source?document_id=%d" % doc_id)
        assert status == 401
        assert body == {"error": "unauthorized"}

        # Wrong token -> still rejected.
        status_wrong, _ = _get(base_url, "/source?document_id=%d" % doc_id, token="wrong-token")
        assert status_wrong == 401

        # Correct token -> succeeds.
        status_ok, body_ok = _get(base_url, "/source?document_id=%d" % doc_id, token="s3cret-test-token")
        assert status_ok == 200
        assert body_ok["document"]["id"] == doc_id

        # /domains and /stats must remain unauthenticated even with a token configured.
        status_domains, _ = _get(base_url, "/domains")
        assert status_domains == 200
        status_stats, _ = _get(base_url, "/stats")
        assert status_stats == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        con.close()

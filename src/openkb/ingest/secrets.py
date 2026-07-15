"""Pre-ingest secret gate — refuse to index credential material.

A knowledge base built from a folder of dumped documents will, sooner or
later, be handed a file someone saved with a live password in it: a
".env" backup, an exported private key, a config dump with an API token
still in it. Those must never become a chunk of retrievable text sitting
in the database (and, downstream, in an LLM's context window). This
module is the gate that runs *before* extraction commits anything: given
a file path and (once extracted) its text, it decides whether the
document is clean or must be quarantined instead of ingested.

Two independent detectors, because a secret can announce itself two ways:

  * filename detector — the file is *named* like it holds a secret
    (``id_rsa``, ``server.pem``, ``.env``, ``backup-passwords.xlsx``).
    This runs before extraction even happens, so a bad file never gets as
    far as being opened and parsed.
  * content detector — the extracted text *contains* a secret shape: a
    PEM private key block, a ``password=``/``api_key=``/``client_secret=``
    assignment, a bearer token, or a recognisable cloud-provider key ID
    (AWS access keys are the one vendor-specific shape kept, since
    ``AKIA...`` is a generic, well-published pattern with no vendor
    account/network details attached — not a secret itself, just a shape
    that means "there is probably a real AWS secret near here").

Both are deliberately broad/generic regexes (label-anchored: they look for
the *label* ``password =`` etc, not just any high-entropy string) — false
positives are cheap here (a human reviews the quarantine folder) while
false negatives (a real credential slipping into the KB) are not.
"""
from __future__ import annotations

import os
import re

__all__ = ["scan_file", "scan_filename", "scan_content"]

_FILENAME_PATTERN = re.compile(
    r"(passwo?r?d|passwd|secret|credential|private[-_]?key|id_rsa|\.pem$|\.key$|\.p12$|\.pfx$|\.env$|api[-_]?key)",
    re.IGNORECASE,
)

_CONTENT_PATTERN = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|password\s*[:=]\s*\S{4,}"
    r"|passwd\s*[:=]\s*\S{4,}"
    r"|api[_-]?key\s*[:=]\s*\S{8,}"
    r"|client[_-]?secret\s*[:=]\s*\S{8,}"
    r"|secret\s*[:=]\s*\S{8,}"
    r"|bearer\s+[A-Za-z0-9._\-]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|aws_secret_access_key\s*[:=])",
    re.IGNORECASE,
)

def scan_filename(path: str) -> str | None:
    """Return a reason string if the filename itself looks secret-shaped."""
    if _FILENAME_PATTERN.search(os.path.basename(path)):
        return "filename matches secret-like pattern"
    return None


def scan_content(text: str) -> str | None:
    """Return a reason string if extracted text contains a secret shape."""
    if not text:
        return None
    if _CONTENT_PATTERN.search(text):
        return "content contains a credential-shaped string"
    return None


def scan_file(path: str, text: str | None = None) -> tuple[bool, list[str]]:
    """Run both detectors. Returns ``(clean, reasons)``.

    ``clean`` is ``True`` only if neither detector fires. ``text`` is
    optional so the filename check can run standalone before extraction —
    the ingest worker calls this once with just ``path`` (cheap, pre-open),
    and again with ``text`` filled in once extraction has produced
    content, so a secret buried in a plausibly-named file is still caught.
    """
    reasons: list[str] = []
    name_hit = scan_filename(path)
    if name_hit:
        reasons.append(name_hit)
    content_hit = scan_content(text) if text is not None else None
    if content_hit:
        reasons.append(content_hit)
    return (len(reasons) == 0, reasons)

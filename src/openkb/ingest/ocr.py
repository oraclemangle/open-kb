"""OCR via a local vision-language model (VLM) chat endpoint.

Some PDFs (scans, exported drawings with a raster title block) have a
text layer that is empty or nearly so — PyMuPDF's ``page.get_text()``
returns almost nothing even though a human can clearly read text in the
page image. There's no OCR "engine" in the traditional Tesseract sense
here: we render the page to an image and ask a vision-capable chat model
to transcribe it, the same way you'd hand a person a photo and ask them to
type out what it says. This is deliberately literal transcription — see
``ingest.describe`` for the different job of *describing* a page that has
no transcribable text at all (a pure schematic/photo).

Transport: Ollama's native ``/api/chat`` endpoint, because it accepts an
``images`` field of base64-encoded page images directly on the message —
the OpenAI-compatible chat-completions shape does not have a first-class
image field in the same way across every local server. If your VLM only
speaks the OpenAI vision format, point ``ocr.url``/``ocr.model`` at a
compatible proxy; this module's HTTP shape assumes Ollama's chat API.

Low temperature throughout: transcription is a copying task, not a
creative one — sampling temperature should be near zero so the model
reproduces what's on the page rather than paraphrasing or hallucinating.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

__all__ = ["transcribe_pdf", "transcribe_image"]

_TRANSCRIBE_PROMPT = (
    "Transcribe ALL text in this document image verbatim. Preserve tables as "
    "markdown. Output only the text."
)


def _post(url: str, payload: dict, timeout: int) -> dict:
    """Tiny stdlib-only JSON POST helper (mirrors rerank.py's ``_post``)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ocr_png_bytes(png: bytes, cfg: dict) -> str:
    ocr_cfg = cfg.get("ocr", {})
    url = ocr_cfg.get("url", "http://127.0.0.1:11434/api/chat")
    model = ocr_cfg.get("model", "your-vision-model")
    timeout = cfg.get("llm", {}).get("timeout_s", 240)
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": _TRANSCRIBE_PROMPT,
                "images": [base64.b64encode(png).decode("ascii")],
            }
        ],
        "options": {
            "temperature": 0.1,
            "top_p": 0.8,
            "top_k": 20,
        },
    }
    result = _post(url, payload, timeout=timeout)
    return (result.get("message", {}).get("content", "") or "").strip()


def _require_fitz():
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "PyMuPDF is required for OCR page rendering. Install the 'pdf' extra: "
            "pip install 'open-kb[pdf]'"
        ) from exc
    return fitz


def _page_dpi_for_cap(rect, dpi: int, max_pixels: int) -> int:
    """Downscale DPI so a rendered page stays under ``max_pixels``.

    A drawing sized for a large-format plotter can be many times the area
    of a normal page at the same DPI; rendering it at full ``ocr.dpi``
    could produce an image too large for the VLM's context/memory budget.
    Rather than skip the page outright, scale the DPI down by the square
    root of the overage ratio (area scales with the square of linear DPI)
    so the page is still legible at a size that fits the cap.
    """
    est_pixels = int((rect.width / 72 * dpi) * (rect.height / 72 * dpi))
    if est_pixels <= max_pixels:
        return dpi
    scale = (max_pixels / est_pixels) ** 0.5
    return max(50, int(dpi * scale * 0.95))


def transcribe_pdf(path: str, cfg: dict) -> tuple[str, int]:
    """OCR-transcribe up to ``ocr.max_pages`` pages of a PDF via the VLM.

    Returns ``(text, pages_ocred)`` with pages joined by ``[Page N]``
    markers so retrieval hits can be traced back to a page number. Pages
    beyond the cap are noted, not silently dropped, so the resulting text
    is honest about its own coverage.
    """
    fitz = _require_fitz()
    ocr_cfg = cfg.get("ocr", {})
    max_pages = ocr_cfg.get("max_pages", 25)
    dpi = ocr_cfg.get("dpi", 150)
    max_pixels = cfg.get("ingest", {}).get("max_pixels", 8_000_000)

    doc = fitz.open(path)
    try:
        total_pages = doc.page_count
        n = min(total_pages, max_pages)
        parts: list[str] = []
        pages_ocred = 0
        for page_index in range(n):
            page = doc[page_index]
            page_dpi = _page_dpi_for_cap(page.rect, dpi, max_pixels)
            try:
                png = page.get_pixmap(dpi=page_dpi).tobytes("png")
                text = _ocr_png_bytes(png, cfg)
                pages_ocred += 1
            except (urllib.error.URLError, OSError, ValueError) as exc:
                text = "[OCR error on page %d: %s]" % (page_index + 1, exc)
            if text:
                parts.append("[Page %d] %s" % (page_index + 1, text))
        if total_pages > max_pages:
            parts.append("[NOTE: OCR limited to first %d of %d pages]" % (max_pages, total_pages))
        return "\n\n".join(parts), pages_ocred
    finally:
        doc.close()


def transcribe_image(path: str, cfg: dict) -> tuple[str, int]:
    """OCR-transcribe a single standalone image file (.png/.jpg/.jpeg)."""
    with open(path, "rb") as fh:
        png = fh.read()
    text = _ocr_png_bytes(png, cfg)
    return text, (1 if text else 0)

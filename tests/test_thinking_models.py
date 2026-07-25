"""Regression tests: reasoning ("thinking") models must not silently break ingest.

Observed 2026-07-25 with gemma4:26b-a4b-it-qat on Ollama: the model returns
reasoning in `message.thinking` and leaves `message.content` empty. With the
original 20-token classification budget every document in the specimen corpus
was silently filed under the catch-all bucket, and roughly half the summaries
came back blank — with no error anywhere.
"""
import openkb.ingest.worker as worker

TAXONOMY = ["00_ELECTRICAL", "04_SAFETY", "99_MISC"]


def test_message_text_prefers_content():
    result = {"message": {"content": "04_SAFETY", "thinking": "some reasoning"}}
    assert worker._message_text(result) == "04_SAFETY"


def test_message_text_falls_back_to_thinking_when_content_empty():
    """The exact shape a cut-off reasoning model returns."""
    result = {"message": {"content": "", "thinking": "the path says safety, so 04_SAFETY"}}
    assert "04_SAFETY" in worker._message_text(result)


def test_message_text_handles_missing_fields():
    assert worker._message_text({}) == ""
    assert worker._message_text({"message": {}}) == ""


def test_classification_recovers_from_thinking_only_reply(monkeypatch):
    """End-to-end: content empty, answer only in thinking -> still classified."""
    def fake_chat(llm_cfg, messages, options, think=False):
        return {"message": {"content": "",
                            "thinking": "Fire dampers are safety equipment, so 04_SAFETY"}}

    monkeypatch.setattr(worker, "_chat", fake_chat)
    got = worker.classify_document("safety/x.md", "fire damper schedule",
                                   {"taxonomy": TAXONOMY, "llm": {}})
    assert got == "04_SAFETY"


def test_classification_falls_back_to_catch_all_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(worker, "_chat", boom)
    got = worker.classify_document("x.md", "text", {"taxonomy": TAXONOMY, "llm": {}})
    assert got == "99_MISC"


def test_chat_sends_think_false_by_default(monkeypatch):
    seen = {}

    def fake_post(url, payload, timeout, retries=3):
        seen.update(payload)
        return {"message": {"content": "ok"}}

    monkeypatch.setattr(worker, "_post_json", fake_post)
    worker._chat({}, [{"role": "user", "content": "hi"}], {"num_predict": 10})
    assert seen.get("think") is False


def test_chat_retries_without_think_when_endpoint_rejects_it(monkeypatch):
    """A non-Ollama OpenAI-compatible server may reject the unknown field."""
    calls = []

    def fake_post(url, payload, timeout, retries=3):
        calls.append(dict(payload))
        if "think" in payload:
            raise RuntimeError("400 unknown field 'think'")
        return {"message": {"content": "recovered"}}

    monkeypatch.setattr(worker, "_post_json", fake_post)
    out = worker._chat({}, [{"role": "user", "content": "hi"}], {"num_predict": 10})
    assert out["message"]["content"] == "recovered"
    assert len(calls) == 2
    assert "think" in calls[0] and "think" not in calls[1]


def test_config_can_omit_think_entirely(monkeypatch):
    """llm.think: null -> never send the field (for endpoints that dislike it)."""
    seen = {}

    def fake_post(url, payload, timeout, retries=3):
        seen.update(payload)
        return {"message": {"content": "ok"}}

    monkeypatch.setattr(worker, "_post_json", fake_post)
    worker._chat({"think": None}, [{"role": "user", "content": "hi"}], {"num_predict": 10})
    assert "think" not in seen

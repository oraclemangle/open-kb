"""Negative-control scoring: what the system does with an unanswerable question.

These encode two defects found by running the harness against a real corpus:

  1. `_is_refusal` required `not sources`, which made the metric essentially
     unmeasurable — hybrid retrieval always returns its top-k, so a
     cited-answer system practically never has zero sources.
  2. The two-way refuse/not-refuse split scored a *grounded non-answer* as a
     failure, punishing the better behaviour.
"""
from openkb.evaluate import (
    NEG_GROUNDED_NON_ANSWER, NEG_REFUSED, NEG_UNSUPPORTED,
    _classify_negative, _is_refusal,
)

DEGRADED = ("[Degraded answer: generation returned no valid source citations]\n\n"
            "Relevant source excerpts:\n[1] hvac.md — text\n[2] wm.md — text")


def test_refusal_is_detected_despite_retrieved_sources():
    """The old rule required zero sources and so never fired."""
    assert _is_refusal(DEGRADED, [{"a": 1}], "degraded") is True


def test_degraded_sentinel_beats_the_citation_scan():
    """The degraded answer lists its excerpts as [1], [2]... — a naive citation
    scan concludes the system cited sources when it explicitly declined."""
    assert _classify_negative(DEGRADED, [{"a": 1}], "") == NEG_REFUSED


def test_grounded_non_answer_is_a_success_not_a_failure():
    ans = ("The provided documentation does not specify the individual permitted flows "
           "for inter-VLAN routing; it only states that routing is default-deny and that "
           "permitted flows are documented in a firewall rule export held by the yard's "
           "integrator [1].")
    assert _classify_negative(ans, [{"a": 1}], "grounded") == NEG_GROUNDED_NON_ANSWER


def test_absence_phrasing_variants():
    for ans in (
        "Based on the provided documentation, there is no information regarding a "
        "replacement interval for the stabiliser fins [1].",
        "The manuals do not mention a service interval [2].",
        "That value is not documented in the supplied set [1].",
    ):
        assert _classify_negative(ans, [{"a": 1}], "grounded") == NEG_GROUNDED_NON_ANSWER


def test_deferral_to_another_document_is_grounded():
    """Naming where a fact would live is useful and corpus-supported, not invented."""
    ans = ("The generator incomer settings are recorded in the as-built single line "
           "diagram package (drawing set E-100), which is part of the handover "
           "documentation set [1].")
    assert _classify_negative(ans, [{"a": 1}], "grounded") == NEG_GROUNDED_NON_ANSWER


def test_unsupported_assertion_is_the_only_failure():
    ans = "The coffee machine in the crew mess is a Jura X8 [1]."
    assert _classify_negative(ans, [{"a": 1}], "grounded") == NEG_UNSUPPORTED


def test_grounded_answer_to_an_answerable_question_is_not_a_refusal():
    assert _is_refusal("Rated output is 350 kVA [1].", [{"a": 1}], "grounded") is False

"""Regression tests for taxonomy-code parsing out of a model reply.

These encode the failure that made every specimen document land in the
catch-all bucket: a model that restates the option list before committing to
an answer defeats a naive first-match scan.
"""
import pytest

from openkb.ingest.worker import _parse_domain_reply

TAXONOMY = [
    "00_ELECTRICAL", "01_MECHANICAL", "02_CONTROLS", "03_NETWORK_AVIT",
    "04_SAFETY", "05_COMMISSIONING", "06_DRAWINGS", "07_REGISTERS", "99_MISC",
]


def test_bare_code():
    assert _parse_domain_reply("04_SAFETY", TAXONOMY) == "04_SAFETY"


def test_code_in_a_sentence():
    reply = "This document belongs to 04_SAFETY."
    assert _parse_domain_reply(reply, TAXONOMY) == "04_SAFETY"


def test_model_restates_the_option_list_first():
    """The bug: a first-match scan returns 00_ELECTRICAL here."""
    reply = (
        "The available domains are: 00_ELECTRICAL, 01_MECHANICAL, 02_CONTROLS, "
        "03_NETWORK_AVIT, 04_SAFETY, 05_COMMISSIONING, 06_DRAWINGS, "
        "07_REGISTERS, 99_MISC.\n"
        "Given the fire damper content, the correct code is 04_SAFETY"
    )
    assert _parse_domain_reply(reply, TAXONOMY) == "04_SAFETY"


def test_final_line_wins_over_earlier_mentions():
    reply = (
        "It could be 00_ELECTRICAL because of the switchboard reference,\n"
        "but the document is really about ventilation dampers.\n"
        "04_SAFETY"
    )
    assert _parse_domain_reply(reply, TAXONOMY) == "04_SAFETY"


def test_reasoning_block_then_answer():
    reply = "<think>weighing electrical vs safety</think>\n04_SAFETY"
    # _strip_reasoning runs before this in the caller; parsing must still work
    # if the marker survives.
    assert _parse_domain_reply(reply, TAXONOMY) == "04_SAFETY"


def test_label_only_fallback():
    reply = "Domain: SAFETY"
    assert _parse_domain_reply(reply, TAXONOMY) == "04_SAFETY"


def test_network_avit_label_fallback():
    reply = "This is clearly NETWORK_AVIT material."
    assert _parse_domain_reply(reply, TAXONOMY) == "03_NETWORK_AVIT"


@pytest.mark.parametrize("reply", ["", "   ", "I don't know", "banana"])
def test_no_match_returns_none(reply):
    """Caller falls back to the catch-all bucket — parsing must not guess."""
    assert _parse_domain_reply(reply, TAXONOMY) is None


def test_short_labels_are_not_used_for_fuzzy_matching():
    """A 3-char label must not fire on an unrelated substring."""
    tax = ["00_ABC", "99_MISC"]
    assert _parse_domain_reply("the abcess was drained", tax) is None

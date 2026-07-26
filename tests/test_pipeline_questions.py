"""What the model is allowed to put in front of an applicant.

The RocketRide -> TokenRouter -> MiniMax lane rewrites derived field labels into
spoken questions. It is an improvement, not a dependency: the deterministic
labels are already usable, so the lane is allowed to do nothing.

What it is not allowed to do is hand back a damaged question, and it produced one
in practice. MiniMax closes its <think> block mid-sentence, so the tail of the
reasoning and the head of the first answer sit on the same side of </think>:

    ...as if speaking to someone with patience.What's
    </think>

    your last name?
    When were you born?

Stripping the block leaves "your last name?". The line count is still correct,
so a count check passes it, and the applicant is asked a question with its first
half missing.
"""

from __future__ import annotations

import pytest

from api.pipeline import _strip_reasoning, _well_formed_question

#: Captured verbatim from a live pipeline run, not invented for the test.
OBSERVED = (
    "<think>\nThe user wants me to rewrite field labels. Let me think about each one:\n"
    '1. "Family Name (Last Name)" - A patient person might ask...\n'
    "I'll keep them simple and natural-sounding, as if speaking to someone with "
    "patience.What's\n</think>\n\nyour last name?\nWhen were you born?"
)


def test_the_observed_truncation_is_rejected():
    """The exact failure this guard was written for."""
    lines = [ln.strip() for ln in _strip_reasoning(OBSERVED).splitlines() if ln.strip()]
    assert lines == ["your last name?", "When were you born?"]
    # The count is right, which is precisely why counting was not enough.
    assert len(lines) == 2
    assert _well_formed_question(lines[0]) is False
    assert _well_formed_question(lines[1]) is True


@pytest.mark.parametrize(
    "line",
    [
        "What's your last name?",
        "When were you born?",
        "What is your A-Number?",
        "2 or more previous names?",
    ],
)
def test_accepts_whole_spoken_questions(line):
    assert _well_formed_question(line) is True


@pytest.mark.parametrize(
    ("line", "why"),
    [
        ("your last name?", "lost its opening words to the reasoning block"),
        ("what is your name?", "lowercase opening is the signature of a severed sentence"),
        ("Tell me your name.", "not a question"),
        ("Family Name (Last Name)", "the raw label, unrewritten"),
        ("?", "too short to be a question"),
        ("A" * 300 + "?", "far longer than anything spoken aloud"),
        ("", "empty"),
        ("   ", "whitespace only"),
    ],
)
def test_refuses_anything_not_a_whole_question(line, why):
    assert _well_formed_question(line) is False, why


def test_unclosed_think_block_strips_to_nothing():
    """Truncation mid-reasoning must not leak reasoning into a question."""
    assert _strip_reasoning("<think>\nstill thinking and then the budget ran") == ""

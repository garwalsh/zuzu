"""What must never reach a filed form.

The dangerous failure in this product is not a crash. It is a form that reports
itself complete while a box the applicant answered is empty, because the
applicant signs and files it and finds out months later.

Every case here was reproduced against the real I-765 before it was fixed.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from api.form_registry import get_form
from api.i765_schema import get_i765_schema
from api.orchestrator import _as_date, run_pipeline
from api.pdf_engine import _normalize, fill_form

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ANSWERS = json.loads((REPO_ROOT / "data" / "demo_personas.json").read_text())["personas"]["maria"][
    "answers"
]


@pytest.fixture
def schema():
    return get_form("I-765")


@pytest.fixture
def out(tmp_path):
    return tmp_path / "filled.pdf"


def test_a_complete_application_still_fills(schema, out):
    """The guards must not block the ordinary case."""
    written, record = run_pipeline("s-ok", schema, dict(ANSWERS), {}, out)
    assert written is not None
    assert [f for f in record["findings"] if f["severity"] == "error"] == []


def test_a_state_the_box_will_not_take_blocks_the_form(schema, out):
    """ "Texas" is not an export code on the state combo; "TX" is.

    The viewer discards anything outside /Opt, so this used to produce a form
    reporting success with the state line blank.
    """
    values = dict(ANSWERS) | {"mailing_state": "Texas"}
    written, record = run_pipeline("s-tx", schema, values, {}, out)

    assert written is None, "a required value that cannot be written must block"
    errors = [f for f in record["findings"] if f["severity"] == "error"]
    assert any(f["field_id"] == "mailing_state" for f in errors)
    assert not out.exists(), "no half-filled file may be left behind"


def test_a_spoken_date_never_reaches_a_date_box(schema):
    """What a voice caller actually says, printed verbatim, is the failure."""
    field = get_i765_schema().get_field("date_of_birth")
    assert _normalize("April twelfth, nineteen ninety-eight", field, []) is None
    assert _normalize("banana", field, []) is None
    # And the formats that are readable still convert to what the box prints.
    assert _normalize("1998-04-12", field, []) == "04/12/1998"
    assert _normalize("April 12, 1998", field, []) == "04/12/1998"


def test_a_us_style_date_does_not_block_the_form(schema, out):
    """Dates were compared as strings.

    "09/03/2023" sorts before "1998-04-12" character by character, so an
    ordinary US-style arrival date raised "arrival before date of birth" -- a
    blocking error no answer could clear, with nothing explaining why.
    """
    values = dict(ANSWERS) | {"date_of_last_entry": "09/03/2023"}
    written, record = run_pipeline("s-date", schema, values, {}, out)

    assert written is not None
    assert [f for f in record["findings"] if f["severity"] == "error"] == []


def test_a_genuinely_impossible_date_order_still_blocks(schema, out):
    """The check has to keep working once it compares real dates."""
    values = dict(ANSWERS) | {"date_of_last_entry": "1990-01-01"}
    written, record = run_pipeline("s-impossible", schema, values, {}, out)

    assert written is None
    assert any(
        f["field_id"] == "date_of_last_entry" and f["severity"] == "error"
        for f in record["findings"]
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1998-04-12", (1998, 4, 12)),
        ("09/03/2023", (2023, 9, 3)),
        ("April 12, 1998", (1998, 4, 12)),
        ("12 April 1998", (1998, 4, 12)),
        ("nineteen ninety-eight", None),
        ("", None),
        (None, None),
    ],
)
def test_date_reading_is_explicit_about_what_it_cannot_read(text, expected):
    result = _as_date(text)
    assert (result is None) == (expected is None)
    if expected:
        assert (result.year, result.month, result.day) == expected


def test_the_fill_report_names_what_it_discarded(schema):
    """The engine is the only thing that knows a value did not survive."""
    with tempfile.TemporaryDirectory() as tmp:
        report = fill_form(
            dict(ANSWERS) | {"mailing_state": "Texas"}, pathlib.Path(tmp) / "r.pdf", schema
        )
    assert "mailing_state" in report.dropped
    assert "Texas" in report.dropped["mailing_state"]
    assert "mailing_state" not in report.filled

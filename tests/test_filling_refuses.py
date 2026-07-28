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

from api.band.tools import SessionTools
from api.form_registry import get_form
from api.i765_schema import get_i765_schema
from api.pdf_engine import _normalize, fill_form
from api.session_store import get_session_store, reset_session_store
from api.tenancy import Principal, Tenant
from api.validation import as_date

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


@pytest.fixture(autouse=True)
def _store(monkeypatch):
    monkeypatch.setenv("STORE_BACKEND", "memory")
    reset_session_store()
    yield
    reset_session_store()


async def _run(session_id: str, values: dict, out_dir) -> tuple[bool, list[dict]]:
    """Validate and fill exactly as the Validator and Filler agents do."""
    store = get_session_store()
    await store.create(session_id, "+14155550142", "I-765", tenant_id="t")
    for field_id, value in values.items():
        await store.save_field(session_id=session_id, field_id=field_id, value=value)
    tools = SessionTools(
        session_id, Principal(tenant=Tenant(id="t", name="T"), user_id="+14155550142"), out_dir
    )
    checked = await tools.cross_check()
    written = await tools.write_form()
    return bool(written.get("written")), checked["findings"]


@pytest.mark.asyncio
async def test_a_complete_application_still_fills(out):
    """The guards must not block the ordinary case."""
    written, findings = await _run("s-ok", dict(ANSWERS), out.parent)
    assert written is True
    assert [f for f in findings if f["severity"] == "error"] == []


@pytest.mark.asyncio
async def test_a_state_the_box_will_not_take_blocks_the_form(out):
    """ "Texas" is not an export code on the state combo; "TX" is.

    The viewer discards anything outside /Opt, so this used to produce a form
    reporting success with the state line blank.
    """
    written, _ = await _run("s-tx", dict(ANSWERS) | {"mailing_state": "Texas"}, out.parent)
    assert written is False, "a required value that cannot be written must block"
    assert not (out.parent / "s-tx.pdf").exists(), "no half-filled file may be left behind"


def test_a_spoken_date_never_reaches_a_date_box(schema):
    """What a voice caller actually says, printed verbatim, is the failure."""
    field = get_i765_schema().get_field("date_of_birth")
    assert _normalize("April twelfth, nineteen ninety-eight", field, []) is None
    assert _normalize("banana", field, []) is None
    # And the formats that are readable still convert to what the box prints.
    assert _normalize("1998-04-12", field, []) == "04/12/1998"
    assert _normalize("April 12, 1998", field, []) == "04/12/1998"


@pytest.mark.asyncio
async def test_a_us_style_date_does_not_block_the_form(out):
    """Dates were compared as strings.

    "09/03/2023" sorts before "1998-04-12" character by character, so an
    ordinary US-style arrival date raised "arrival before date of birth" -- a
    blocking error no answer could clear, with nothing explaining why.
    """
    written, findings = await _run(
        "s-date", dict(ANSWERS) | {"date_of_last_entry": "09/03/2023"}, out.parent
    )
    assert written is True
    assert [f for f in findings if f["severity"] == "error"] == []


@pytest.mark.asyncio
async def test_a_genuinely_impossible_date_order_still_blocks(out):
    """The check has to keep working once it compares real dates."""
    written, findings = await _run(
        "s-impossible", dict(ANSWERS) | {"date_of_last_entry": "1990-01-01"}, out.parent
    )
    assert written is False
    assert any(f["field_id"] == "date_of_last_entry" and f["severity"] == "error" for f in findings)


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
    result = as_date(text)
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

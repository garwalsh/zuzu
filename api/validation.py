"""The cross-checks that decide whether an application may be written.

These are the mistakes that get an application rejected months later, and none
of them are visible field by field -- they only appear when the answers are read
together. An arrival dated before a birth. A student category with no SEVIS
number. A value that will not fit the box it is printed in.

This used to live inside a five-stage pipeline object that also did provenance,
mapping, filling and auditing. Those jobs now belong to the Band agents, who do
them by talking to each other, so what is left here is the part that was never
orchestration in the first place: a set of rules over a set of answers.

Nothing here knows about agents, sessions or rooms. It takes a schema and some
values and returns findings, which is why both the voice path and the agents can
use it without either owning it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from api.i765_schema import SKIP_SENTINEL, FormSchema
from api.pdf_engine import missing_required

#: Worst first, so a caller can read the top of the list and stop.
SEVERITY_ORDER = {"error": 0, "warning": 1, "note": 2}

#: What people actually say, and what transcription produces. ISO first because
#: that is what the schema asks for and what the PDF engine writes.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%m-%d-%Y",
    "%Y/%m/%d",
    "%B %d, %Y",
    "%d %B %Y",
)


def as_date(value: str | None) -> date | None:
    """Read a date, or admit it cannot. Never guesses between orderings."""
    if not value:
        return None
    text = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class Finding:
    """Something a human should look at before signing."""

    severity: str
    field_id: str | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "field_id": self.field_id, "message": self.message}


def cross_check(schema: FormSchema, values: dict[str, str]) -> list[Finding]:
    """Every cross-field check, worst finding first.

    An `error` means the form must not be written. A `warning` means it may be,
    and somebody should look. A `note` is context.
    """
    findings: list[Finding] = []

    def flag(severity: str, message: str, field_id: str | None = None) -> None:
        findings.append(Finding(severity=severity, field_id=field_id, message=message))

    def real(field_id: str) -> str | None:
        value = values.get(field_id)
        return None if value is None or value == SKIP_SENTINEL else value

    # Dates must sit in a possible order. Compared as dates, not as strings:
    # "09/03/2023" sorts before "1998-04-12" character by character, so an
    # ordinary US-style date used to raise a blocking error no answer could
    # clear, and nothing said why.
    dob, entry = as_date(real("date_of_birth")), as_date(real("date_of_last_entry"))
    if dob and entry and entry < dob:
        flag(
            "error", "Arrival in the U.S. is dated before the date of birth.", "date_of_last_entry"
        )
    for field_id in ("date_of_birth", "date_of_last_entry", "passport_expiry"):
        raw = real(field_id)
        if raw and as_date(raw) is None:
            flag(
                "note", f"Could not read {raw!r} as a date, so it was not cross-checked.", field_id
            )
    expiry = as_date(real("passport_expiry"))
    if expiry and expiry < datetime.now(UTC).date():
        flag("warning", "The passport is already expired.", "passport_expiry")

    # A student category without a SEVIS number is the single most common
    # avoidable rejection on an I-765.
    category = (real("eligibility_category") or "").lower().replace(" ", "")
    if category.startswith(("(c)(3", "c3")) and not real("sevis_number"):
        flag(
            "warning",
            "A (c)(3) student category normally needs a SEVIS number and an endorsed I-20.",
            "sevis_number",
        )
    status = real("current_status")
    if status and "student" in status.lower():
        if category and not category.startswith(("(c)(3", "c3")):
            flag(
                "note",
                "Status says student but the eligibility category is not a (c)(3) category.",
                "eligibility_category",
            )

    # Required answers, and anything that would overflow its box on the page.
    for field_id in missing_required(values, schema):
        flag("error", "Required, and not answered.", field_id)
    for field_id, value in values.items():
        form_field = schema.get_field(field_id)
        if not form_field or not form_field.max_len or value == SKIP_SENTINEL:
            continue
        # Measure what will actually be written. The PDF engine strips
        # punctuation from these before filling, so "(415) 555-0142" is ten
        # characters by the time it reaches the box -- flagging the raw string
        # would be crying wolf, and a validator nobody trusts is worse than none.
        written = (
            re.sub(r"\D", "", value)
            if form_field.type in ("ssn", "a_number", "zip", "phone")
            else value
        )
        if len(written) > form_field.max_len:
            flag(
                "warning",
                f"Longer than the {form_field.max_len} characters the box holds.",
                field_id,
            )

    findings.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
    return findings


def blocking(findings: list[Finding]) -> list[Finding]:
    """The findings that mean the form must not be written."""
    return [f for f in findings if f.severity == "error"]

"""The agent pipeline that turns collected answers into a filed-ready form.

Spec: prompts/orchestrator_Python.prompt

Five stages, each with one job, run in order, every one of them recording what
it did:

    Extractor   normalise what the applicant actually said
    Mapper      place each value on this form's fields
    Validator   cross-check the application before anyone signs it
    Filler      write the PDF
    Auditor     record who set what, from where, at what confidence

The stages exist separately because in a legal-filing domain the audit trail is
the product. "Your date of birth is 1998-04-12" is worth little; "your date of
birth is 1998-04-12, you said it on the 26th, we read it back, and it was
normalised from 'April twelfth nineteen ninety-eight'" is worth a great deal
when a form comes back rejected and someone has to work out why.

On Band: these five roles are registered as agent identities on the Band
platform, and their ids are recorded on every audit entry below. The stages
themselves run in this process. Band's user API key is scoped to agent
registration only -- every other endpoint returns `insufficient_scope` -- so
dispatching the work to those agents over Band's own transport is not something
this key can do. The seam is here and named; only the transport is missing.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from api.i765_schema import SKIP_SENTINEL, FormSchema
from api.pdf_engine import fill_form, missing_required

logger = logging.getLogger(__name__)


def _band_agents() -> dict[str, str]:
    """The agent id for each stage, taken from the fleet that actually runs.

    These ids were hardcoded here, and after the fleet was re-registered they
    pointed at agents that no longer exist: /agents reported five of six as
    unregistered with empty names, and every audit entry attributed its work to
    a deleted identity. An id that cannot be resolved is worse than no id, since
    the whole reason it is on the trail is so a reviewer can look it up.

    There is now one source: the credentials the fleet connects with. If the
    fleet is not configured this is empty, and an audit entry carries no agent
    id rather than a wrong one.
    """
    from api.band.credentials import agent_ids

    try:
        return agent_ids()
    except Exception:  # pragma: no cover - credentials are optional
        return {}


#: Band agent identities for each stage, resolved once at import from the fleet.
BAND_AGENTS: dict[str, str] = _band_agents()

#: Questions chosen during the call, per session, before the batch pipeline runs.
#:
#: Intake was registered on Band and then attributed nothing, because choosing the
#: next question happens once per conversational turn while the other five stages
#: run once at the end. The audit trail was therefore silent about the part of the
#: process the applicant actually experienced: it could say which agent wrote a
#: value into the PDF, but not that anyone decided to ask for it.
_INTAKE_TRAIL: dict[str, list[AuditEntry]] = {}
#: Per-session cap. The longest form is under 400 questions; anything past this is
#: a loop, not an interview.
INTAKE_TRAIL_LIMIT = 600

SEVERITY_ORDER = {"error": 0, "warning": 1, "note": 2}

BAND_BASE_URL = "https://app.band.ai/api/v1"


async def registered_agents() -> list[dict[str, str]]:
    """The pipeline's agent identities, read live from Band.

    Returns an empty list rather than raising: this is for display, and a
    reachability problem at Band must never affect filling a form.
    """
    import httpx

    key = os.environ.get("BAND_USER_API_KEY", "")
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{BAND_BASE_URL}/me/agents", headers={"X-API-Key": key})
            resp.raise_for_status()
            agents = resp.json().get("data") or []
    except Exception as exc:
        logger.info("band agent listing unavailable: %s", type(exc).__name__)
        return []

    by_id = {a.get("id"): a for a in agents}
    return [
        {
            "stage": stage,
            "agent_id": agent_id,
            "name": (by_id.get(agent_id) or {}).get("name", ""),
            "registered": agent_id in by_id,
        }
        for stage, agent_id in BAND_AGENTS.items()
    ]


@dataclass
class AuditEntry:
    """One thing an agent did, and why."""

    stage: str
    agent_id: str
    action: str
    field_id: str | None = None
    detail: str = ""
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "agent_id": self.agent_id,
            "action": self.action,
            "field_id": self.field_id,
            "detail": self.detail,
            "at": self.at,
        }


#: What people actually say and what transcription produces. ISO first because
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


def _as_date(value: str | None) -> date | None:
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


def note_intake(session_id: str, action: str, field_id: str | None, detail: str = "") -> None:
    """Record that Intake chose what to ask next. Called once per turn.

    This runs on the live call path, so it does nothing but append to a list.
    """
    trail = _INTAKE_TRAIL.setdefault(session_id, [])
    # The agent re-asks for the same field when a value fails read-back, and
    # recording that twice is right -- it is what happened. Only a runaway loop
    # is trimmed.
    if len(trail) >= INTAKE_TRAIL_LIMIT:
        del trail[0]
    trail.append(
        AuditEntry(
            stage="intake",
            agent_id=BAND_AGENTS.get("intake", ""),
            action=action,
            field_id=field_id,
            detail=detail,
        )
    )


def intake_trail(session_id: str) -> list[AuditEntry]:
    """What Intake did on this call, oldest first."""
    return list(_INTAKE_TRAIL.get(session_id, ()))


def forget_intake(session_id: str) -> None:
    """Drop a finished call's turn-by-turn record."""
    _INTAKE_TRAIL.pop(session_id, None)


@dataclass
class Finding:
    """Something the Validator wants a human to look at."""

    severity: str
    field_id: str | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "field_id": self.field_id, "message": self.message}


class Run:
    """One pass of the pipeline over one session's answers."""

    def __init__(self, session_id: str, schema: FormSchema) -> None:
        self.session_id = session_id
        self.schema = schema
        self.audit: list[AuditEntry] = []
        self.findings: list[Finding] = []

    def record(
        self, stage: str, action: str, field_id: str | None = None, detail: str = ""
    ) -> None:
        self.audit.append(
            AuditEntry(
                stage=stage,
                agent_id=BAND_AGENTS.get(stage, ""),
                action=action,
                field_id=field_id,
                detail=detail,
            )
        )

    def flag(self, severity: str, message: str, field_id: str | None = None) -> None:
        self.findings.append(Finding(severity=severity, field_id=field_id, message=message))


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def extract(run: Run, values: dict[str, str], sources: dict[str, str]) -> dict[str, str]:
    """Take what the applicant said and note where each value came from.

    Normalisation of spoken values happens on the way in, at save time; this
    stage's job is provenance -- recording that a value arrived from voice, from
    memory, or from a document -- because that is what a reviewer needs later.
    """
    for field_id, value in values.items():
        origin = sources.get(field_id, "voice")
        run.record(
            "extractor",
            "skipped" if value == SKIP_SENTINEL else "captured",
            field_id,
            f"from {origin}",
        )
    return values


def map_to_form(run: Run, values: dict[str, str]) -> dict[str, str]:
    """Keep only what this form has somewhere to put.

    A value with no destination is not an error -- forms differ, and answers
    carry across them -- but it must not be counted as filled.
    """
    placed: dict[str, str] = {}
    for field_id, value in values.items():
        form_field = run.schema.get_field(field_id)
        if form_field is None:
            run.record("mapper", "no destination on this form", field_id)
            continue
        placed[field_id] = value
        if value != SKIP_SENTINEL:
            run.record("mapper", "mapped", field_id, form_field.pdf_field or "choice")
    return placed


def validate(run: Run, values: dict[str, str]) -> list[Finding]:
    """Cross-check the application before anyone signs it.

    These are the mistakes that get an application rejected months later, and
    none of them are visible field-by-field -- they only show up when you look
    at the answers together.
    """

    def real(field_id: str) -> str | None:
        v = values.get(field_id)
        return None if v is None or v == SKIP_SENTINEL else v

    # Dates must sit in a possible order. Compared as dates, not as strings:
    # "09/03/2023" sorts before "1998-04-12" character by character, so a
    # perfectly ordinary US-style date used to raise a blocking error that no
    # answer could clear -- the Filler refuses on any error, so the applicant
    # could never generate their form and nothing said why.
    dob, entry = _as_date(real("date_of_birth")), _as_date(real("date_of_last_entry"))
    if dob and entry and entry < dob:
        run.flag(
            "error", "Arrival in the U.S. is dated before the date of birth.", "date_of_last_entry"
        )
    for field_id in ("date_of_birth", "date_of_last_entry", "passport_expiry"):
        raw = real(field_id)
        if raw and _as_date(raw) is None:
            run.flag(
                "note", f"Could not read {raw!r} as a date, so it was not cross-checked.", field_id
            )
    expiry = _as_date(real("passport_expiry"))
    if expiry and expiry < datetime.now(UTC).date():
        run.flag("warning", "The passport is already expired.", "passport_expiry")

    # A student category without a SEVIS number is the single most common
    # avoidable rejection on an I-765.
    category = (real("eligibility_category") or "").lower().replace(" ", "")
    if category.startswith(("(c)(3", "c3")) and not real("sevis_number"):
        run.flag(
            "warning",
            "A (c)(3) student category normally needs a SEVIS number and an endorsed I-20.",
            "sevis_number",
        )
    if real("current_status") and "student" in real("current_status").lower():
        if category and not category.startswith(("(c)(3", "c3")):
            run.flag(
                "note",
                "Status says student but the eligibility category is not a (c)(3) category.",
                "eligibility_category",
            )

    # Required answers, and anything that would overflow its box on the page.
    for field_id in missing_required(values, run.schema):
        run.flag("error", "Required, and not answered.", field_id)
    for field_id, value in values.items():
        form_field = run.schema.get_field(field_id)
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
            run.flag(
                "warning",
                f"Longer than the {form_field.max_len} characters the box holds.",
                field_id,
            )

    run.findings.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
    for finding in run.findings:
        run.record("validator", finding.severity, finding.field_id, finding.message)
    if not run.findings:
        run.record("validator", "no findings", detail="every cross-check passed")
    return run.findings


def fill(run: Run, values: dict[str, str], out_path: Path) -> Path | None:
    """Write the PDF, unless something must be fixed first."""
    blocking = [f for f in run.findings if f.severity == "error"]
    if blocking:
        run.record("filler", "refused", detail=f"{len(blocking)} blocking finding(s)")
        return None
    report = fill_form(values, out_path, run.schema)

    # An answer the page would not take. Only the engine knows this: whether a
    # value survives depends on export codes read out of the document itself,
    # so "Texas" is discarded where "TX" is written. Reporting the form complete
    # while a required box sits empty is the failure this product exists to
    # prevent, so a dropped required value is an error and the file goes.
    for field_id, why in report.dropped.items():
        form_field = run.schema.get_field(field_id)
        required = bool(form_field and form_field.required)
        run.flag(
            "error" if required else "warning",
            f"Could not be written to the form: {why}.",
            field_id,
        )
        run.record("filler", "value discarded", field_id, why)
    if any(f.severity == "error" for f in run.findings):
        run.findings.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
        report.path.unlink(missing_ok=True)
        run.record("filler", "refused", detail="a required answer would not fit the form")
        return None

    run.record("filler", "wrote pdf", detail=report.path.name)
    return report.path


def audit(run: Run) -> dict[str, Any]:
    """Close the record for this run."""
    run.record(
        "auditor",
        "sealed",
        detail=f"{len(run.audit)} entries, {len(run.findings)} finding(s)",
    )
    return {
        "session_id": run.session_id,
        "form_id": run.schema.form_id,
        "agents": BAND_AGENTS,
        "findings": [f.as_dict() for f in run.findings],
        "trail": [e.as_dict() for e in run.audit],
    }


def run_pipeline(
    session_id: str,
    schema: FormSchema,
    values: dict[str, str],
    sources: dict[str, str],
    out_path: Path,
) -> tuple[Path | None, dict[str, Any]]:
    """Intake -> Extractor -> Mapper -> Validator -> Filler -> Auditor.

    Intake ran earlier, once per turn, while the applicant was still on the
    phone; its record is folded in here so the trail reads as one story from
    "we asked for this" through to "we wrote it here".
    """
    run = Run(session_id, schema)
    run.audit.extend(intake_trail(session_id))
    extracted = extract(run, values, sources)
    mapped = map_to_form(run, extracted)
    validate(run, mapped)
    written = fill(run, mapped, out_path)
    record = audit(run)
    logger.info(
        "pipeline session=%s form=%s findings=%d pdf=%s",
        session_id,
        schema.form_id,
        len(run.findings),
        bool(written),
    )
    return written, record

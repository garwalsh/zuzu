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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.i765_schema import SKIP_SENTINEL, FormSchema
from api.pdf_engine import fill_i765, missing_required

logger = logging.getLogger(__name__)

#: Band agent identities for each stage. Registered on the Band platform; the
#: ids are carried into the audit trail so a reviewer can attribute every value.
BAND_AGENTS: dict[str, str] = {
    "extractor": os.environ.get("BAND_AGENT_EXTRACTOR", "0098e4bd-74a3-4f14-b35a-a085a5368f15"),
    "mapper": os.environ.get("BAND_AGENT_MAPPER", "88b43172-7d4d-4256-85d9-6ff74d4a30ef"),
    "validator": os.environ.get("BAND_AGENT_VALIDATOR", "d71d3305-3967-42ab-94b7-660ff8d43975"),
    "filler": os.environ.get("BAND_AGENT_FILLER", "2bcd4cdf-9ffa-4ecd-9ab9-054ca13f2812"),
    "auditor": os.environ.get("BAND_AGENT_AUDITOR", "a9933561-4d8e-48eb-b201-fa7480bf5909"),
}

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

    # Dates must sit in a possible order.
    dob, entry = real("date_of_birth"), real("date_of_last_entry")
    if dob and entry and entry < dob:
        run.flag(
            "error", "Arrival in the U.S. is dated before the date of birth.", "date_of_last_entry"
        )
    expiry = real("passport_expiry")
    if expiry and expiry < datetime.now(UTC).date().isoformat():
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
    written = fill_i765(values, out_path, run.schema)
    run.record("filler", "wrote pdf", detail=written.name)
    return written


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
    """Extractor -> Mapper -> Validator -> Filler -> Auditor."""
    run = Run(session_id, schema)
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

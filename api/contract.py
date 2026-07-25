from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SESSION_STARTED = "session_started"
FIELD_SAVED = "field_saved"
FORM_READY = "form_ready"
SESSION_COMPLETED = "session_completed"

sensitive_fields: set[str] = {
    "ssn",
    "a_number",
    "passport_number",
    "i94_number",
    "uscis_online_account_number",
}


class StrictModel(BaseModel):
    """Base model that rejects unexpected fields at the API boundary."""

    model_config = ConfigDict(extra="forbid")


class SessionInitRequest(StrictModel):
    """Request payload used to initialize a new session."""

    caller_id: str
    conversation_id: str
    agent_id: str | None = None


class DynamicVariables(StrictModel):
    """Dynamic variables returned when a session starts."""

    applicant_name: str
    is_returning: bool
    preferred_language: str
    active_form: str
    known_summary: str


class SessionInitResponse(StrictModel):
    """Response payload for session initialization."""

    dynamic_variables: DynamicVariables


class GetMissingFieldsRequest(StrictModel):
    """Request payload for fetching the next missing field."""

    session_id: str
    form_id: str


class NextField(StrictModel):
    """Metadata for the next field that should be collected."""

    id: str
    question: str
    type: str
    sensitive: bool


class GetMissingFieldsResponse(StrictModel):
    """Response containing the next missing field and collection progress."""

    next_field: NextField | None
    remaining_count: int
    known_count: int


class SaveFieldRequest(StrictModel):
    """Request payload used to persist a collected field value."""

    session_id: str
    field_id: str
    value: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    language: str = "en"


class SaveFieldResponse(StrictModel):
    """Response payload after saving a field value."""

    ok: bool
    needs_confirmation: bool
    remaining_count: int


class GenerateFormRequest(StrictModel):
    """Request payload used to trigger form generation."""

    session_id: str


class GenerateFormResponse(StrictModel):
    """Response payload describing form generation status."""

    status: str
    pdf_url: str | None
    missing: list[str]


class SessionCompleteRequest(StrictModel):
    """Request payload used to finalize a session."""

    conversation_id: str
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    collected: dict[str, str] = Field(default_factory=dict)


class SessionCompleteResponse(StrictModel):
    """Response payload returned when a session is completed."""

    ok: bool
    fields_reconciled: int


class FieldValue(StrictModel):
    """Stored value metadata for a collected field."""

    value: str
    confidence: float = Field(ge=0.0, le=1.0)
    source: str
    language: str
    saved_at: datetime


class SessionEvent(StrictModel):
    """Event emitted during session processing."""

    type: str
    session_id: str
    at: datetime
    data: dict[str, Any] = Field(default_factory=dict)

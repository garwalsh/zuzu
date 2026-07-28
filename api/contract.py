from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SESSION_STARTED = "session_started"
FIELD_SAVED = "field_saved"
FORM_READY = "form_ready"
SESSION_COMPLETED = "session_completed"
FORM_CHANGED = "form_changed"

sensitive_fields: set[str] = {
    "ssn",
    "a_number",
    "passport_number",
    "i94_number",
    "uscis_online_account_number",
}


class StrictModel(BaseModel):
    """Base for values we produce. Rejects unexpected fields."""

    model_config = ConfigDict(extra="forbid")


class InboundModel(BaseModel):
    """Base for payloads ElevenLabs sends us. Tolerates unexpected fields.

    Be liberal in what you accept. The real conversation-initiation webhook
    carries `called_number`, `call_sid`, and `source` alongside the three keys
    the integration contract documents, and the post-call webhook carries far
    more. Rejecting those is a 422 at `/session/init` -- the call dies before
    the applicant says a word.

    This is deliberately not laxness about correctness: an unknown `field_id`
    is still rejected in the route, because a value with nowhere to go on the
    form must never look collected. Refusing an unknown *envelope key* protects
    nobody; refusing an unknown *form field* protects the applicant.
    """

    model_config = ConfigDict(extra="ignore")


class SessionInitRequest(InboundModel):
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
    """Response payload for session initialization.

    ElevenLabs' conversation-initiation webhook expects a `type` discriminator
    alongside the variables; the integration contract's example omits it, and it
    costs nothing to send. `conversation_config_override` is how a returning
    Spanish speaker gets greeted in Spanish without the agent having to detect
    it first -- we already know their language from memory.
    """

    type: str = "conversation_initiation_client_data"
    dynamic_variables: DynamicVariables
    conversation_config_override: dict[str, Any] | None = None


class GetMissingFieldsRequest(InboundModel):
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
    #: The form these counts and this question belong to. The session decides
    #: which form is being filled, so echoing it back lets the agent notice when
    #: what it thinks it is filling and what Zuzu is filling have diverged.
    form_id: str = ""


class SaveFieldRequest(InboundModel):
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
    #: The field this answer actually became. Usually the one that was sent; it
    #: differs when the agent guessed an id the form does not have and the
    #: orchestrator worked out which question was really being answered.
    field_id: str = ""


class IdentifyFormRequest(InboundModel):
    """Payload for the identify_form server tool."""

    text: str = ""
    url: str = ""
    session_id: str = ""


class SetFormRequest(InboundModel):
    """Payload for the set_form server tool."""

    form_id: str
    session_id: str = ""


class GenerateFormRequest(InboundModel):
    """Request payload used to trigger form generation."""

    session_id: str


class GenerateFormResponse(StrictModel):
    """Response payload describing form generation status."""

    status: str
    pdf_url: str | None
    missing: list[str]


class SessionCompleteRequest(InboundModel):
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

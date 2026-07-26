"""Session state: what this caller has told us, and what to ask next.

Spec: prompts/session_store_Python.prompt

Everything is async even where the in-memory implementation does not need to
be, so the Layer 2 Redis store is a drop-in and no call site changes.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from api.contract import FieldValue
from api.i765_schema import SKIP_SENTINEL, FormField, FormSchema


class SessionNotFoundError(KeyError):
    """Raised when a call arrives for a session the orchestrator does not have.

    This means the agent and the orchestrator have diverged; inventing the
    session here would hide that and silently start a second, empty interview.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"no such session: {session_id!r}")


@dataclass
class Session:
    session_id: str
    caller_id: str
    form_id: str
    values: dict[str, FieldValue] = field(default_factory=dict)
    preferred_language: str = "en"
    is_returning: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    pdf_path: str | None = None

    def answered(self, field_id: str) -> bool:
        """True once a field has been asked and resolved, skip included."""
        return field_id in self.values

    def usable_value(self, field_id: str) -> str | None:
        """The stored value, or None if absent or explicitly skipped."""
        stored = self.values.get(field_id)
        if stored is None or stored.value == SKIP_SENTINEL:
            return None
        return stored.value


class SessionStore(Protocol):
    async def create(
        self, session_id: str, caller_id: str, form_id: str, **kwargs: object
    ) -> Session: ...

    async def get(self, session_id: str) -> Session: ...

    async def save_field(
        self,
        session_id: str,
        field_id: str,
        value: str,
        confidence: float = 1.0,
        language: str = "en",
        source: str = "voice",
    ) -> Session: ...

    async def set_pdf_path(self, session_id: str, pdf_path: str) -> Session: ...

    async def delete(self, session_id: str) -> None: ...


class InMemorySessionStore:
    """Single-process session state, guarded against concurrent tool calls."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    async def create(
        self, session_id: str, caller_id: str, form_id: str, **kwargs: object
    ) -> Session:
        async with self._lock:
            session = Session(
                session_id=session_id,
                caller_id=caller_id,
                form_id=form_id,
                preferred_language=str(kwargs.get("preferred_language", "en")),
                is_returning=bool(kwargs.get("is_returning", False)),
            )
            self._sessions[session_id] = session
            return session

    async def get(self, session_id: str) -> Session:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            return session

    async def save_field(
        self,
        session_id: str,
        field_id: str,
        value: str,
        confidence: float = 1.0,
        language: str = "en",
        source: str = "voice",
    ) -> Session:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            session.values[field_id] = FieldValue(
                value=value,
                confidence=confidence,
                source=source,
                language=language,
                saved_at=datetime.now(UTC),
            )
            return session

    async def set_pdf_path(self, session_id: str, pdf_path: str) -> Session:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            session.pdf_path = pdf_path
            return session

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)


def next_missing_field(session: Session, schema: FormSchema) -> FormField | None:
    """The next unanswered field to ask about, in schema order.

    Optional fields are asked too. Passport, I-94, and SEVIS numbers are
    optional to submit but they have boxes on the form, and an applicant who
    has them should not find out later that Zuzu never asked. `required`
    governs what blocks generation, not what gets asked -- the applicant can
    always say skip.

    None means the interview is over -- that is the agent's cue to generate the
    form. A field gated by `depends_on` is skipped until its gate has a real
    answer, so we never ask for an apartment number after "no apartment".
    """
    for form_field in schema.fields:
        if session.answered(form_field.id):
            continue
        gate = form_field.depends_on
        if gate is not None and session.usable_value(gate) is None:
            continue
        return form_field
    return None


def counts(session: Session, schema: FormSchema) -> tuple[int, int]:
    """(remaining_count, known_count) for the agent's progress reporting.

    Counts every unanswered field, not just required ones, so it matches what
    `next_missing_field` will actually keep asking for. Counting only required
    fields reported 0 remaining on a schema derived from a PDF -- where nothing
    is marked required -- while 273 questions were still to come, which would
    invite the agent to generate the form early.
    """
    remaining = sum(1 for f in schema.fields if not session.answered(f.id))
    return remaining, len(session.values)


_store: SessionStore | None = None


def get_session_store() -> SessionStore:
    """The process-wide store, chosen by STORE_BACKEND."""
    global _store
    if _store is None:
        backend = os.environ.get("STORE_BACKEND", "memory").strip().lower()
        if backend == "redis":
            raise NotImplementedError(
                "STORE_BACKEND=redis is Milestone 6 (Layer 2). Refusing to run "
                "single-process while claiming to be distributed."
            )
        if backend not in ("", "memory"):
            raise ValueError(f"unknown STORE_BACKEND: {backend!r}")
        _store = InMemorySessionStore()
    return _store


def reset_session_store() -> None:
    """Drop the singleton. For tests only."""
    global _store
    _store = None

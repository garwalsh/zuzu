"""Zuzu orchestrator: the service the ElevenLabs voice agent calls.

Spec: prompts/main_Python.prompt

A human is mid-sentence while these endpoints run, so the read and write paths
do nothing but touch session state and publish an event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from api.contract import (
    FIELD_SAVED,
    FORM_READY,
    SESSION_COMPLETED,
    SESSION_STARTED,
    DynamicVariables,
    GenerateFormRequest,
    GenerateFormResponse,
    GetMissingFieldsRequest,
    GetMissingFieldsResponse,
    NextField,
    SaveFieldRequest,
    SaveFieldResponse,
    SessionCompleteRequest,
    SessionCompleteResponse,
    SessionEvent,
    SessionInitRequest,
    SessionInitResponse,
)
from api.event_bus import get_event_bus
from api.form_registry import DEFAULT_FORM_ID, UnknownFormError, get_form, list_forms
from api.i765_schema import REPO_ROOT, SKIP_SENTINEL
from api.memory import get_memory, summarize
from api.pdf_engine import fill_i765, missing_required
from api.security import require_shared_secret, verify_secret
from api.session_store import (
    Session,
    SessionNotFoundError,
    counts,
    get_session_store,
    next_missing_field,
)

CONFIDENCE_CONFIRM_THRESHOLD = 0.85
OUT_DIR = REPO_ROOT / "out"
PERSONA_PATH = REPO_ROOT / "data" / "demo_personas.json"


class JsonLogFormatter(logging.Formatter):
    """Structured logs. Field ids are loggable; field values never are."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        for key in ("session_id", "field_id", "duration_ms", "form_id"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload)


def _configure_logging() -> logging.Logger:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    if not any(isinstance(h.formatter, JsonLogFormatter) for h in root.handlers):
        root.handlers = [handler]
    root.setLevel(logging.INFO)
    return logging.getLogger("zuzu")


logger = _configure_logging()

app = FastAPI(title="Zuzu orchestrator", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _publish(event_type: str, session_id: str, data: dict[str, Any] | None = None) -> None:
    await get_event_bus().publish(
        SessionEvent(
            type=event_type,
            session_id=session_id,
            at=datetime.now(UTC),
            data=data or {},
        )
    )


#: Strong refs to in-flight background tasks; without these the event loop can
#: garbage-collect a task mid-write.
_background: set[asyncio.Task[Any]] = set()


def _spawn_background(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _load_session(session_id: str) -> Session:
    try:
        return await get_session_store().get(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"unknown session: {session_id}") from exc


def _resolve_form(form_id: str):
    try:
        return get_form(form_id or DEFAULT_FORM_ID)
    except UnknownFormError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "form_ids": list_forms()}


@app.post("/session/init", response_model=SessionInitResponse)
async def session_init(
    payload: SessionInitRequest,
    _: None = Depends(require_shared_secret),
) -> SessionInitResponse:
    """Create the session the whole call hangs off, and load what we know.

    A returning caller is greeted by name and never re-asked for anything we
    already have. If the memory lookup fails, we greet them as new rather than
    failing the call -- see api/memory.py.
    """
    session_id = payload.conversation_id
    schema = get_form(DEFAULT_FORM_ID)
    profile = await get_memory().load_profile(payload.caller_id, schema)

    session = await get_session_store().create(
        session_id=session_id,
        caller_id=payload.caller_id,
        form_id=DEFAULT_FORM_ID,
        preferred_language=profile.preferred_language,
        is_returning=profile.is_returning,
    )

    # Prefill from memory so the interview only covers what is genuinely new.
    for field_id, value in profile.known_values.items():
        await get_session_store().save_field(
            session_id=session_id,
            field_id=field_id,
            value=value,
            confidence=1.0,
            language=profile.preferred_language,
            source="memory",
        )

    remaining, known = counts(await get_session_store().get(session_id), schema)
    await _publish(
        SESSION_STARTED,
        session_id,
        {
            "is_returning": profile.is_returning,
            "prefilled_count": len(profile.known_values),
            "remaining_count": remaining,
            "known_count": known,
        },
    )
    logger.info(
        "session started",
        extra={"session_id": session_id, "form_id": schema.form_id},
    )
    del session

    # We already know a returning caller's language, so tell the agent to open
    # in it rather than making them speak first and hoping detection lands.
    override: dict[str, Any] | None = None
    if profile.is_returning and profile.preferred_language != "en":
        override = {"agent": {"language": profile.preferred_language}}

    return SessionInitResponse(
        dynamic_variables=DynamicVariables(
            applicant_name=profile.display_name,
            is_returning=profile.is_returning,
            preferred_language=profile.preferred_language,
            active_form=DEFAULT_FORM_ID,
            known_summary=summarize(profile, schema),
        ),
        conversation_config_override=override,
    )


@app.post("/tools/get_missing_fields", response_model=GetMissingFieldsResponse)
async def get_missing_fields(
    payload: GetMissingFieldsRequest,
    _: None = Depends(require_shared_secret),
) -> GetMissingFieldsResponse:
    started = time.perf_counter()
    session = await _load_session(payload.session_id)
    schema = _resolve_form(payload.form_id)

    field = next_missing_field(session, schema)
    remaining, known = counts(session, schema)
    next_field = (
        NextField(id=field.id, question=field.question, type=field.type, sensitive=field.sensitive)
        if field
        else None
    )
    logger.info(
        "next field resolved",
        extra={
            "session_id": payload.session_id,
            "field_id": field.id if field else None,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )
    return GetMissingFieldsResponse(
        next_field=next_field, remaining_count=remaining, known_count=known
    )


@app.post("/tools/save_field", response_model=SaveFieldResponse)
async def save_field(
    payload: SaveFieldRequest,
    _: None = Depends(require_shared_secret),
) -> SaveFieldResponse:
    """Store one answer. No LLM, no filesystem, no PDF work happens here."""
    started = time.perf_counter()
    session = await _load_session(payload.session_id)
    schema = _resolve_form(session.form_id)

    form_field = schema.get_field(payload.field_id)
    if form_field is None:
        # A value with nowhere to go on the form is a value we must not pretend
        # to have collected.
        raise HTTPException(
            status_code=422,
            detail=f"unknown field_id {payload.field_id!r} for form {schema.form_id}",
        )

    session = await get_session_store().save_field(
        session_id=payload.session_id,
        field_id=payload.field_id,
        value=payload.value,
        confidence=payload.confidence,
        language=payload.language,
    )
    remaining, _known = counts(session, schema)
    needs_confirmation = form_field.sensitive or payload.confidence < CONFIDENCE_CONFIRM_THRESHOLD

    # Persist to long-term memory off the critical path. This endpoint is on a
    # live human's latency budget; mem0 writes are queued server-side anyway, so
    # awaiting one would buy nothing but delay.
    _spawn_background(
        get_memory().save_field(
            caller_id=session.caller_id,
            field_id=payload.field_id,
            value=payload.value,
            schema=schema,
            language=payload.language,
        )
    )

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    await _publish(
        FIELD_SAVED,
        payload.session_id,
        {
            "field_id": payload.field_id,
            "group": form_field.group,
            "sensitive": form_field.sensitive,
            "confidence": payload.confidence,
            "remaining_count": remaining,
            # Measured, not asserted -- the dashboard shows this number.
            "duration_ms": duration_ms,
        },
    )
    logger.info(
        "field saved",
        extra={
            "session_id": payload.session_id,
            "field_id": payload.field_id,
            "duration_ms": duration_ms,
        },
    )
    return SaveFieldResponse(
        ok=True, needs_confirmation=needs_confirmation, remaining_count=remaining
    )


@app.post("/tools/generate_form", response_model=GenerateFormResponse)
async def generate_form(
    payload: GenerateFormRequest,
    _: None = Depends(require_shared_secret),
) -> GenerateFormResponse:
    session = await _load_session(payload.session_id)
    schema = _resolve_form(session.form_id)

    plain = {fid: fv.value for fid, fv in session.values.items()}
    missing = missing_required(plain, schema)
    if missing:
        logger.info(
            "generation refused: incomplete",
            extra={"session_id": payload.session_id, "form_id": schema.form_id},
        )
        return GenerateFormResponse(status="incomplete", pdf_url=None, missing=missing)

    out_path = OUT_DIR / f"{payload.session_id}.pdf"
    # Off the event loop: the call is still live and other tool calls must land.
    await asyncio.to_thread(fill_i765, plain, out_path, schema)
    await get_session_store().set_pdf_path(payload.session_id, str(out_path))

    pdf_url = f"{os.environ.get('PUBLIC_BASE_URL', '').rstrip('/')}/forms/{payload.session_id}.pdf"
    await _publish(FORM_READY, payload.session_id, {"pdf_url": pdf_url})
    logger.info("form ready", extra={"session_id": payload.session_id, "form_id": schema.form_id})
    return GenerateFormResponse(status="complete", pdf_url=pdf_url, missing=[])


@app.get("/forms/{session_id}.pdf")
async def download_form(session_id: str) -> FileResponse:
    session = await _load_session(session_id)
    if not session.pdf_path or not Path(session.pdf_path).exists():
        raise HTTPException(status_code=404, detail="no generated form for this session yet")
    return FileResponse(
        session.pdf_path,
        media_type="application/pdf",
        filename=f"I-765_{session_id}.pdf",
    )


@app.post("/session/complete", response_model=SessionCompleteResponse)
async def session_complete(
    payload: SessionCompleteRequest,
    _: None = Depends(require_shared_secret),
) -> SessionCompleteResponse:
    session = await _load_session(payload.conversation_id)
    schema = _resolve_form(session.form_id)

    reconciled = 0
    for field_id, value in payload.collected.items():
        if schema.get_field(field_id) is None or session.answered(field_id):
            continue
        await get_session_store().save_field(
            session_id=payload.conversation_id,
            field_id=field_id,
            value=value,
            source="transcript",
        )
        reconciled += 1

    await _publish(SESSION_COMPLETED, payload.conversation_id, {"reconciled": reconciled})
    logger.info(
        "session completed",
        extra={"session_id": payload.conversation_id, "form_id": schema.form_id},
    )
    return SessionCompleteResponse(ok=True, fields_reconciled=reconciled)


@app.websocket("/ws/{session_id}")
async def session_events(websocket: WebSocket, session_id: str, secret: str = Query(default="")):
    if not verify_secret(secret):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        async for event in get_event_bus().subscribe(session_id):
            await websocket.send_text(event.model_dump_json())
    except WebSocketDisconnect:
        # A closed dashboard tab is normal, not an error.
        pass


@app.post("/demo/run")
async def demo_run(
    persona: str = Query(default="maria"),
    _: None = Depends(require_shared_secret),
) -> dict[str, Any]:
    """Drive a full scripted call through this same contract.

    The Demo Mode fallback: no microphone, no widget, no network egress.
    """
    personas = json.loads(PERSONA_PATH.read_text(encoding="utf-8"))["personas"]
    if persona not in personas:
        raise HTTPException(status_code=404, detail=f"unknown persona {persona!r}")
    profile = personas[persona]
    answers: dict[str, str] = profile["answers"]

    session_id = f"demo_{persona}_{int(time.time())}"
    store = get_session_store()
    await store.create(session_id, profile["caller_id"], DEFAULT_FORM_ID)
    await _publish(SESSION_STARTED, session_id, {"demo": True})

    schema = get_form(DEFAULT_FORM_ID)
    asked: list[str] = []
    for _ in range(len(schema.fields) + 5):
        session = await store.get(session_id)
        field = next_missing_field(session, schema)
        if field is None:
            break
        # Only ever answer from the persona. Never synthesize.
        started = time.perf_counter()
        await store.save_field(
            session_id=session_id,
            field_id=field.id,
            value=answers.get(field.id, SKIP_SENTINEL),
            source="demo",
        )
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        asked.append(field.id)
        remaining, _k = counts(await store.get(session_id), schema)
        await _publish(
            FIELD_SAVED,
            session_id,
            {
                "field_id": field.id,
                "group": field.group,
                "sensitive": field.sensitive,
                "remaining_count": remaining,
                "duration_ms": duration_ms,
            },
        )
        await asyncio.sleep(0.05)  # let the dashboard animate

    result = await generate_form(GenerateFormRequest(session_id=session_id))
    return {
        "session_id": session_id,
        "persona": profile["display_name"],
        "fields_asked": asked,
        "status": result.status,
        "pdf_url": result.pdf_url,
        "missing": result.missing,
    }


def _display_value(field_id: str, value: str, schema: Any) -> str:
    """What the dashboard may show. Never the full sensitive value."""
    if value == SKIP_SENTINEL:
        return SKIP_SENTINEL
    form_field = schema.get_field(field_id)
    if form_field is not None and form_field.sensitive and len(value) > 2:
        return "*" * (len(value) - 2) + value[-2:]
    return value


@app.get("/forms/{form_id}/schema")
async def form_schema(form_id: str) -> dict[str, Any]:
    """The field list the dashboard renders. Public: it contains no applicant data."""
    schema = _resolve_form(form_id)
    return {
        "form_id": schema.form_id,
        "title": schema.title,
        "edition": schema.edition,
        "fields": [
            {
                "id": f.id,
                "question": f.question,
                "group": f.group,
                "sensitive": f.sensitive,
                "required": f.required,
            }
            for f in schema.fields
        ],
    }


@app.get("/sessions/recent")
async def sessions_recent(_: None = Depends(require_shared_secret)) -> dict[str, Any]:
    """Most recent sessions, so the dashboard can attach to a live voice call
    without anyone copying a conversation id by hand mid-demo."""
    store = get_session_store()
    sessions = getattr(store, "_sessions", {})
    ordered = sorted(sessions.values(), key=lambda s: s.created_at, reverse=True)
    return {
        "sessions": [
            {
                "session_id": s.session_id,
                "form_id": s.form_id,
                "is_returning": s.is_returning,
                "known_count": len(s.values),
                "created_at": s.created_at.isoformat(),
                "has_pdf": bool(s.pdf_path),
            }
            for s in ordered[:10]
        ]
    }


@app.get("/sessions/{session_id}/values")
async def session_values(
    session_id: str, _: None = Depends(require_shared_secret)
) -> dict[str, Any]:
    """Current values for a session, so a dashboard opened mid-call can paint
    the fields already collected instead of starting blank."""
    session = await _load_session(session_id)
    schema = _resolve_form(session.form_id)
    remaining, known = counts(session, schema)
    return {
        "session_id": session_id,
        "is_returning": session.is_returning,
        "remaining_count": remaining,
        "known_count": known,
        "has_pdf": bool(session.pdf_path),
        # Sensitive values are masked here too: this feeds a projected screen.
        # The skip sentinel is passed through untouched so the dashboard can
        # render "not provided" rather than a masked-looking fake value.
        "values": {
            fid: _display_value(fid, fv.value, schema) for fid, fv in session.values.items()
        },
        "sources": {fid: fv.source for fid, fv in session.values.items()},
    }


@app.get("/config")
async def client_config() -> dict[str, Any]:
    """Public front-end config. Contains no secret.

    The ElevenLabs agent id is public by design -- it is what the browser widget
    connects with. The shared secret is never served from here.
    """
    return {
        "elevenlabs_agent_id": os.environ.get("ELEVENLABS_AGENT_ID", ""),
        "default_form_id": DEFAULT_FORM_ID,
    }


@app.get("/dashboard")
async def dashboard() -> FileResponse:
    """Single self-contained page: no build step, so nothing can fail to compile
    on demo day."""
    page = REPO_ROOT / "dashboard" / "index.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="dashboard not built")
    return FileResponse(page, media_type="text/html")


@app.post("/session/forget")
async def session_forget(
    caller_id: str = Query(...),
    _: None = Depends(require_shared_secret),
) -> dict[str, Any]:
    """Delete everything remembered about a caller.

    The pitch promises an applicant can say "delete my data". This is that.
    """
    removed = await get_memory().forget(caller_id)
    return {"ok": True, "entries_removed": removed}


@app.get("/")
async def root() -> Response:
    return Response(
        content=json.dumps({"service": "zuzu", "docs": "/docs", "health": "/health"}),
        media_type="application/json",
    )

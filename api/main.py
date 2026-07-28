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
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from api.band import ledger
from api.band.credentials import agent_ids
from api.band.fleet import get_fleet
from api.band.roles import ROLES as BAND_ROLES
from api.band.tools import SessionTools
from api.contract import (
    FIELD_SAVED,
    FORM_CHANGED,
    FORM_READY,
    SESSION_COMPLETED,
    SESSION_STARTED,
    DynamicVariables,
    GenerateFormRequest,
    GenerateFormResponse,
    GetMissingFieldsRequest,
    GetMissingFieldsResponse,
    IdentifyFormRequest,
    NextField,
    SaveFieldRequest,
    SaveFieldResponse,
    SessionCompleteRequest,
    SessionCompleteResponse,
    SessionEvent,
    SessionInitRequest,
    SessionInitResponse,
    SetFormRequest,
)
from api.delivery import deliver_packet
from api.event_bus import get_event_bus
from api.form_finder import identify
from api.form_onboarding import OnboardingError, load_catalog, onboard
from api.form_registry import DEFAULT_FORM_ID, UnknownFormError, get_form, list_forms
from api.i765_schema import REPO_ROOT, SKIP_SENTINEL
from api.memory import DeletionUnverifiable, Tier, _user_key, get_memory, summarize
from api.pdf_engine import missing_required
from api.retrieval import applicable_items, fetch_document_checklist
from api.security import (
    download_token,
    require_shared_secret,
    verify_download,
    verify_secret,
)
from api.session_store import (
    InvalidSessionId,
    Session,
    SessionNotFoundError,
    counts,
    get_session_store,
    next_missing_field,
)
from api.tenancy import (
    DEFAULT_TENANT,
    TENANT_HEADER,
    Principal,
    TenancyError,
    Tenant,
    guard_session,
    require_tenant,
    resolve_tenant,
)

#: The organisation making this request. Annotated rather than a default value,
#: which is the current FastAPI idiom and keeps the dependency out of the
#: function signature's mutable-default territory.
TenantDep = Annotated[Tenant, Depends(require_tenant)]

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


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Bring the Band agent fleet up with the service, and down with it.

    The agents are long-lived WebSocket connections, so they belong to the
    process lifetime rather than to a request. Starting is deliberately
    best-effort: if Band or the model is unreachable the service still serves
    every endpoint and still fills forms, because orchestration is how the work
    is coordinated and audited, not what makes a filing possible.
    """
    fleet = get_fleet()
    try:
        started = await fleet.start()
    except Exception as exc:
        logger.warning("band fleet did not start: %s", type(exc).__name__)
        started = False
    from api.memory_store import check_backend

    memory_status = await check_backend()
    logger.info(
        "startup complete; band fleet %s, memory %s",
        "up" if started else "off",
        memory_status["backend"] if memory_status["reachable"] else "UNREACHABLE",
    )
    try:
        yield
    finally:
        await fleet.stop()


app = FastAPI(title="Zuzu orchestrator", version="0.1.0", lifespan=lifespan)


@app.exception_handler(InvalidSessionId)
async def _invalid_session_id(_: Any, exc: InvalidSessionId) -> Response:
    """A caller-supplied id that cannot be a filename is a bad request.

    It reached the caller as a 500 with a traceback, which reads as "we broke"
    rather than "you sent something we will not accept".
    """
    return Response(
        content=json.dumps({"detail": str(exc)}),
        status_code=400,
        media_type="application/json",
    )


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


async def _load_session(session_id: str, tenant: Tenant | None = None) -> Session:
    """Strict lookup, for read paths where inventing a session would be a lie.

    When a tenant is supplied the session must belong to it. Holding a valid key
    proves which organisation you are, not which sessions you may read, and
    session ids are not secret -- a demo one is `web_maria_<unix seconds>`.
    """
    try:
        session = await get_session_store().get(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"unknown session: {session_id}") from exc
    if tenant is not None:
        guard_session(session.tenant_id, tenant)
    return session


async def _load_or_open_session(
    session_id: str, caller_id: str = "", tenant: Tenant | None = None
) -> Session:
    """Lookup for the live tool path, creating the session if it is missing.

    The conversation-initiation webhook is an optimisation, not a precondition.
    It fires for telephony but not reliably for the embedded widget, and when it
    does not fire every tool call 404s and the agent tells the applicant it
    cannot get the next question -- which is exactly what happens on stage.

    A session opened here has no caller_id, so there is no memory lookup and the
    interview simply starts cold. Losing the returning-caller greeting is a far
    better failure than losing the call.
    """
    store = get_session_store()
    try:
        session = await store.get(session_id)
    except SessionNotFoundError:
        logger.warning(
            "session opened lazily -- conversation-init webhook did not fire",
            extra={"session_id": session_id},
        )
        session = await store.create(
            session_id=session_id,
            caller_id=caller_id,
            form_id=DEFAULT_FORM_ID,
            # Owned from the moment it exists. A session created without one
            # can never be checked against anybody afterwards, and the
            # deployment's own tenant reads all of them.
            tenant_id=tenant.id if tenant else "",
        )
        await _publish(SESSION_STARTED, session_id, {"is_returning": False, "lazy": True})
        return session
    if tenant is not None:
        guard_session(session.tenant_id, tenant)
    return session


def _resolve_form(form_id: str, tenant: Tenant | None = None):
    """The schema for a form, if this organisation is allowed to file it.

    Tenant.allowed_forms existed, was unit-tested, and was never consulted on a
    single request -- a restriction nobody enforces is not a restriction. An
    empty list still means all forms, which is what most tenants want.
    """
    try:
        schema = get_form(form_id or DEFAULT_FORM_ID)
    except UnknownFormError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if tenant is not None and not tenant.may_file(schema.form_id):
        raise HTTPException(
            status_code=403,
            detail=f"{tenant.name} is not configured to file {schema.form_id}",
        )
    return schema


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness, plus whether the things memory depends on are actually there."""
    from api.band import brain
    from api.memory_store import check_backend

    memory_status = await check_backend()
    fleet = get_fleet()
    return {
        "status": "ok" if memory_status["reachable"] else "degraded",
        "form_ids": list_forms(),
        "memory": memory_status,
        # A bare boolean could not tell "six agents connected" from "one did".
        # Both were reported as True, and the difference is whether a filing gets
        # validated before it is written.
        "fleet": {
            "running": fleet.is_running,
            "agents": len(fleet.agents),
            "expected": len(BAND_ROLES),
            "connected": sorted(fleet.agents),
            "live_rooms": len(fleet.by_room),
            # Which of the two is deciding. Without the model the agents still
            # run, on the fixed hand-off order -- worth being able to see from
            # outside rather than inferring it from an audit trail after a call.
            "reasoner": brain.REASONER_MODEL if brain.is_available() else brain.REASONER_FALLBACK,
        },
        "fleet_running": fleet.is_running,
    }


@app.post("/session/init", response_model=SessionInitResponse)
async def session_init(
    payload: SessionInitRequest,
    tenant: TenantDep,
    _: None = Depends(require_shared_secret),
) -> SessionInitResponse:
    """Create the session the whole call hangs off, and load what we know.

    A returning caller is greeted by name and never re-asked for anything we
    already have. If the memory lookup fails, we greet them as new rather than
    failing the call -- see api/memory.py.
    """
    session_id = payload.conversation_id

    # An id that is already in use must still belong to whoever is naming it.
    # Without this, /session/init on somebody else's conversation id simply
    # created over the top of it: the session's tenant_id became the caller's,
    # and with it every ownership check downstream -- the answers, the PDF, the
    # audit trail. A session id is not a secret; a demo one is a timestamp.
    try:
        existing = await get_session_store().get(session_id)
    except SessionNotFoundError:
        existing = None
    if existing is not None:
        guard_session(existing.tenant_id, tenant)

    schema = get_form(DEFAULT_FORM_ID)
    profile = await get_memory(tenant.id).load_profile(payload.caller_id, schema)

    session = await get_session_store().create(
        session_id=session_id,
        caller_id=payload.caller_id,
        form_id=DEFAULT_FORM_ID,
        tenant_id=tenant.id,
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
    tenant: TenantDep,
    _: None = Depends(require_shared_secret),
) -> GetMissingFieldsResponse:
    started = time.perf_counter()
    session = await _load_or_open_session(payload.session_id, tenant=tenant)
    # The session owns which form is being filled, not the caller of this
    # endpoint. The agent carries the form_id it confirmed earlier in the
    # conversation, so after a mid-call switch it asks for the previous form
    # while save_field and generate_form use the new one -- questions from one
    # form, answers filed against another, and no error to show for it.
    schema = _resolve_form(session.form_id)

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
        next_field=next_field,
        remaining_count=remaining,
        known_count=known,
        form_id=schema.form_id,
    )


@app.post("/tools/save_field", response_model=SaveFieldResponse)
async def save_field(
    payload: SaveFieldRequest,
    tenant: TenantDep,
    _: None = Depends(require_shared_secret),
) -> SaveFieldResponse:
    """Store one answer. No LLM, no filesystem, no PDF work happens here."""
    started = time.perf_counter()
    session = await _load_or_open_session(payload.session_id, tenant=tenant)
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
        get_memory(session.tenant_id).save_field(
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
    tenant: TenantDep,
    _: None = Depends(require_shared_secret),
) -> GenerateFormResponse:
    session = await _load_or_open_session(payload.session_id, tenant=tenant)
    schema = _resolve_form(session.form_id)

    plain = {fid: fv.value for fid, fv in session.values.items()}
    missing = missing_required(plain, schema)
    if missing:
        logger.info(
            "generation refused: incomplete",
            extra={"session_id": payload.session_id, "form_id": schema.form_id},
        )
        return GenerateFormResponse(status="incomplete", pdf_url=None, missing=missing)

    # The same two operations the Validator and Filler agents call, and the same
    # code -- there is no second implementation of what a valid filing is. The
    # difference is only who decided to run them: here the voice agent asked
    # directly, because the applicant is still on the phone and a full agent
    # collaboration takes a minute and a half.
    principal = Principal(tenant=tenant, user_id=session.caller_id)
    tools = SessionTools(payload.session_id, principal, OUT_DIR)
    checked = await tools.cross_check()
    if checked["blocking"]:
        return GenerateFormResponse(
            status="incomplete",
            pdf_url=None,
            missing=[f["field_id"] for f in checked["findings"] if f["severity"] == "error"],
        )
    result = await tools.write_form()
    if not result["written"]:
        return GenerateFormResponse(
            status="incomplete", pdf_url=None, missing=result.get("fields", [])
        )

    # The link is emailed to someone with no shared secret, so it carries a
    # signed, expiring token rather than relying on the session id.
    _base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    pdf_url = f"{_base}/forms/{payload.session_id}.pdf?t={download_token(payload.session_id)}"
    await _publish(FORM_READY, payload.session_id, {"pdf_url": pdf_url})
    logger.info("form ready", extra={"session_id": payload.session_id, "form_id": schema.form_id})
    return GenerateFormResponse(status="complete", pdf_url=pdf_url, missing=[])


@app.get("/forms/{session_id}.pdf")
async def download_form(
    session_id: str,
    t: str = Query(default="", description="download token issued with the form"),
    x_zuzu_secret: str | None = Header(default=None, alias="X-Zuzu-Secret"),
    x_zuzu_tenant_key: str | None = Header(default=None, alias=TENANT_HEADER),
) -> FileResponse:
    """The completed form, to whoever was given the link.

    This file holds the applicant's name, date of birth and usually their SSN,
    and it used to be served to anyone who could name the session -- which for a
    demo run is `web_maria_<unix seconds>`. The link still has to work for an
    applicant who has no shared secret, so it carries a signed token instead.
    """
    # The signed token is the only credential that opens this without naming an
    # organisation. The shared secret used to be accepted instead, and it is one
    # value for the whole deployment -- so any tenant holding it could fetch any
    # other tenant's completed form, SSN and all, by session id alone.
    if not verify_download(session_id, t):
        if not verify_secret(x_zuzu_secret):
            raise HTTPException(status_code=404, detail="no generated form for this session yet")
        try:
            tenant = resolve_tenant(x_zuzu_tenant_key)
        except TenancyError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        session = await _load_session(session_id, tenant)
    else:
        session = await _load_session(session_id)
    if not session.pdf_path or not Path(session.pdf_path).exists():
        raise HTTPException(status_code=404, detail="no generated form for this session yet")
    return FileResponse(
        session.pdf_path,
        media_type="application/pdf",
        filename=f"{session.form_id}_{session_id}.pdf",
    )


@app.post("/session/complete", response_model=SessionCompleteResponse)
async def session_complete(
    payload: SessionCompleteRequest,
    tenant: TenantDep,
    _: None = Depends(require_shared_secret),
) -> SessionCompleteResponse:
    session = await _load_or_open_session(payload.conversation_id, tenant=tenant)
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

    # EPISODIC + PROCEDURAL writes happen here, at the natural end of a call,
    # and off the response path -- the agent is hanging up, not waiting on us.
    plain = {fid: fv.value for fid, fv in session.values.items()}
    _spawn_background(
        get_memory(session.tenant_id).record_episode(
            caller_id=session.caller_id,
            session_id=payload.conversation_id,
            form_id=schema.form_id,
            fields_collected=len(session.values),
            completed=bool(session.pdf_path),
            language=session.preferred_language,
        )
    )
    _spawn_background(
        get_memory(session.tenant_id).learn_from_session(
            caller_id=session.caller_id,
            values=plain,
            schema=schema,
            language=session.preferred_language,
        )
    )

    await _publish(SESSION_COMPLETED, payload.conversation_id, {"reconciled": reconciled})
    logger.info(
        "session completed",
        extra={"session_id": payload.conversation_id, "form_id": schema.form_id},
    )
    return SessionCompleteResponse(ok=True, fields_reconciled=reconciled)


#: How a browser proves which organisation it is on a WebSocket. The browser
#: WebSocket API cannot set request headers, and the tenant key is a credential
#: that has no business in a URL -- it lands in proxy logs, browser history and
#: Referer. Sec-WebSocket-Protocol is a header the browser WILL send, so the key
#: travels there.
TENANT_SUBPROTOCOL = "zuzu-tenant"


@app.websocket("/ws/{session_id}")
async def session_events(websocket: WebSocket, session_id: str, secret: str = Query(default="")):
    """Live events for one session, for whoever owns that session.

    This used to take the deployment-wide shared secret and nothing else. Any
    holder of it could subscribe to any session id and receive that filing's
    events -- including form.ready, which carries a signed pdf_url that opens
    the completed application with no credential at all. Every careful ownership
    check on the HTTP routes was reachable around, over a socket.
    """
    if not verify_secret(secret):
        await websocket.close(code=1008)
        return

    # ["zuzu-tenant", "<key>"] -- the key rides as the second offered protocol.
    offered = list(websocket.scope.get("subprotocols") or [])
    tenant_key = offered[1] if len(offered) > 1 and offered[0] == TENANT_SUBPROTOCOL else ""
    try:
        tenant = resolve_tenant(tenant_key)
        session = await get_session_store().get(session_id)
        guard_session(session.tenant_id, tenant)
    except (TenancyError, HTTPException, SessionNotFoundError):
        # One code for all three. Which of "no key", "wrong key", "not yours"
        # and "no such session" applies is itself something an unauthorised
        # caller should not be able to learn by trying ids.
        await websocket.close(code=1008)
        return

    accepted = TENANT_SUBPROTOCOL if tenant_key else None
    await websocket.accept(subprotocol=accepted)
    try:
        async for event in get_event_bus().subscribe(session_id):
            await websocket.send_text(event.model_dump_json())
    except WebSocketDisconnect:
        # A closed dashboard tab is normal, not an error.
        pass


@app.post("/demo/run")
async def demo_run(
    tenant: TenantDep,
    persona: str = Query(default="maria"),
    _: None = Depends(require_shared_secret),
) -> dict[str, Any]:
    """Drive a full application through this same contract.

    Runs the real interview loop against a sample applicant, so the product can
    be shown end to end without a microphone or a working widget -- and so the
    contract itself stays exercised in CI.
    """
    personas = json.loads(PERSONA_PATH.read_text(encoding="utf-8"))["personas"]
    if persona not in personas:
        raise HTTPException(status_code=404, detail=f"unknown persona {persona!r}")
    profile = personas[persona]
    answers: dict[str, str] = profile["answers"]

    session_id = f"web_{persona}_{int(time.time())}"
    store = get_session_store()
    await store.create(session_id, profile["caller_id"], DEFAULT_FORM_ID, tenant_id=tenant.id)
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
        # SEMANTIC write. The loop above talks to the session store directly
        # rather than to /tools/save_field, so without this a demo call left
        # mem0 completely empty and the memory panel had nothing to show.
        _spawn_background(
            get_memory(tenant.id).save_field(
                caller_id=profile["caller_id"],
                field_id=field.id,
                value=answers.get(field.id, SKIP_SENTINEL),
                schema=schema,
                language=profile.get("preferred_language", "en"),
            )
        )
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

    result = await generate_form(GenerateFormRequest(session_id=session_id), tenant)

    # EPISODIC + PROCEDURAL writes. A real call gets these from
    # /session/complete when the agent hangs up; a demo call never reaches that
    # endpoint, so it has to close its own record or two of the three tiers
    # stay permanently empty.
    final = await store.get(session_id)
    _spawn_background(
        get_memory(tenant.id).record_episode(
            caller_id=profile["caller_id"],
            session_id=session_id,
            form_id=schema.form_id,
            fields_collected=len(final.values),
            completed=result.status == "complete",
            language=profile.get("preferred_language", "en"),
        )
    )
    _spawn_background(
        get_memory(tenant.id).learn_from_session(
            caller_id=profile["caller_id"],
            values={fid: fv.value for fid, fv in final.values.items()},
            schema=schema,
            language=profile.get("preferred_language", "en"),
        )
    )

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


@app.get("/sessions/{session_id}/checklist")
async def session_checklist(
    session_id: str, tenant: TenantDep, _: None = Depends(require_shared_secret)
) -> dict[str, Any]:
    """Supporting documents this applicant still has to attach.

    The completed PDF is half the deliverable; USCIS also wants photos, the
    I-94, a copy of the prior EAD. Which ones apply depends on their answers.
    """
    session = await _load_session(session_id, tenant)
    schema = _resolve_form(session.form_id)
    checklist = await fetch_document_checklist(schema.form_id)
    plain = {fid: fv.value for fid, fv in session.values.items()}
    return {
        "form_id": schema.form_id,
        "source": checklist.get("source"),
        "items": applicable_items(checklist, plain),
    }


#: What each role looks like across forms. The I-765 calls the applicant's
#: address `email`; the N-400 calls it `p14_line5_email_address`. Field ids are
#: derived per form from that form's own PDF, so nothing outside I-765 can be
#: addressed by a literal id.
_ROLE_PATTERNS: dict[str, tuple[str, ...]] = {
    "email": ("email",),
    "given_name": ("given_name", "first_name"),
}


def _field_by_role(session: Session, schema: Any, role: str) -> str:
    """Find a value by what it means rather than by what this form calls it.

    Delivery used to read `values["email"]`, which exists on the I-765 and on no
    other onboarded form, so emailing an N-400 packet reported "no usable email
    address" while the address sat in the session under a different id.
    """
    by_type = [f for f in schema.fields if getattr(f, "type", "") == role]
    patterns = _ROLE_PATTERNS.get(role, (role,))
    by_name = [f for f in schema.fields if any(p in f.id for p in patterns)]
    for form_field in [*by_type, *by_name]:
        value = session.usable_value(form_field.id)
        if value:
            return value
    return ""


@app.post("/sessions/{session_id}/deliver")
async def session_deliver(
    session_id: str, tenant: TenantDep, _: None = Depends(require_shared_secret)
) -> dict[str, Any]:
    """Email the finished packet to the applicant.

    The person called from a phone and hung up; a PDF on a dashboard they are
    not looking at is not a delivered outcome.
    """
    session = await _load_session(session_id, tenant)
    schema = _resolve_form(session.form_id)
    if not session.pdf_path:
        raise HTTPException(status_code=409, detail="no completed form for this session yet")

    plain = {fid: fv.value for fid, fv in session.values.items()}
    checklist = await fetch_document_checklist(schema.form_id)
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    result = await deliver_packet(
        to_email=_field_by_role(session, schema, "email"),
        form_id=schema.form_id,
        pdf_url=f"{base}/forms/{session_id}.pdf?t={download_token(session_id)}",
        checklist=applicable_items(checklist, plain),
        applicant_name=_field_by_role(session, schema, "given_name"),
    )
    return {"session_id": session_id, **result}


@app.post("/tools/identify_form")
async def identify_form(
    tenant: TenantDep,
    payload: IdentifyFormRequest | None = None,
    text: str = Query(default="", description="what the applicant said"),
    url: str = Query(default="", description="a uscis.gov link they pasted"),
    session_id: str = Query(default=""),
    _: None = Depends(require_shared_secret),
) -> dict[str, Any]:
    """Work out which form someone means, and get it ready.

    Applicants say "my work permit", not "I-765". If the match is confident and
    the form is not loaded yet, it is onboarded from its PDF on the spot, so the
    interview can begin in the same breath.

    Confidence is returned rather than acted on blindly: the agent reads the
    form name back before switching. Silently starting the wrong application
    wastes an hour of a stressed person's time.
    """
    # ElevenLabs server tools POST a JSON body. Accepting the query form too
    # keeps curl and the docs page working; the body wins when both are sent.
    if payload is not None:
        text = payload.text or text
        url = payload.url or url
        session_id = payload.session_id or session_id

    hit = await identify(text=text, url=url)
    if hit is None:
        return {"found": False, "known_forms": list_forms()}

    form_id = hit["form_id"]
    # The allowlist has to hold on the path an applicant actually reaches, not
    # only on /forms/onboard. This endpoint identifies a form AND onboards it,
    # so a tenant restricted to I-765 could say "certificate of citizenship" and
    # get N-600 registered for the whole deployment. Telling them what it is
    # remains fine -- refusing to name a form nobody may file helps no one.
    if not tenant.may_file(form_id):
        return {
            "found": True,
            "form_id": form_id,
            "ready": False,
            "confidence": hit.get("confidence", 0.0),
            "why": hit.get("why", ""),
            "refused": f"{tenant.name} is not configured to file {form_id}",
            "known_forms": list_forms(),
        }

    ready = form_id.upper() in {f.upper() for f in list_forms()}
    if not ready:
        try:
            await onboard(form_id)
            ready = True
        except OnboardingError as exc:
            logger.warning("could not onboard %s: %s", form_id, exc)

    title = ""
    if ready:
        title = _resolve_form(form_id).title
    if session_id:
        await _load_or_open_session(session_id, tenant=tenant)
        _resolve_form(form_id, tenant)  # refuse a form this tenant may not file
        try:
            await get_session_store().set_form(session_id, form_id)
        except SessionNotFoundError:
            pass
    return {"found": True, "ready": ready, "title": title, **hit}


@app.post("/session/set_form")
async def session_set_form(
    tenant: TenantDep,
    payload: SetFormRequest | None = None,
    session_id: str = Query(default=""),
    form_id: str = Query(default=""),
    _: None = Depends(require_shared_secret),
) -> dict[str, Any]:
    """Switch the form an in-progress call is filling.

    Answers already given are kept: a name and address collected for one form
    are the same name and address on the next one.
    """
    if payload is not None:
        session_id = payload.session_id or session_id
        form_id = payload.form_id or form_id
    if not session_id or not form_id:
        raise HTTPException(status_code=422, detail="session_id and form_id are required")

    await _load_or_open_session(session_id, tenant=tenant)
    schema = _resolve_form(form_id, tenant)
    session = await get_session_store().set_form(session_id, schema.form_id)
    remaining, known = counts(session, schema)
    await _publish(
        FORM_CHANGED,
        session_id,
        {
            "form_id": schema.form_id,
            "title": schema.title,
            "edition": schema.edition,
            "remaining_count": remaining,
            "known_count": known,
        },
    )
    return {
        "session_id": session_id,
        "form_id": schema.form_id,
        "title": schema.title,
        "edition": schema.edition,
        "remaining_count": remaining,
        "known_count": known,
        "carried_over": known,
    }


@app.post("/sessions/{session_id}/orchestrate")
async def orchestrate(
    session_id: str, tenant: TenantDep, _: None = Depends(require_shared_secret)
) -> dict[str, Any]:
    """Hand this filing to the Band agent fleet and return what they did.

    The agents open a room, address each other, run their own tools, and close
    the record. What comes back is the room id and every turn, including which
    reasoner decided it -- a trail that does not say whether the model or the
    fallback chose is making a claim it cannot support.
    """
    session = await _load_session(session_id, tenant)
    # Resolved before the capability check, so that getting it wrong shows up
    # wherever this endpoint is exercised rather than only where a fleet happens
    # to be running. Built from the tenant this request already proved, because
    # deriving one from nothing raises the moment a registry exists -- which is
    # why the previous version failed in production and nowhere else.
    principal = Principal(tenant=tenant, user_id=session.caller_id)

    fleet = get_fleet()
    if not fleet.is_running:
        raise HTTPException(
            status_code=503,
            detail=(
                "the Band fleet is not running: agent credentials or the model "
                "gateway are unavailable. The deterministic pipeline at "
                "/tools/generate_form still fills this form."
            ),
        )
    collab = await fleet.collaborate(session_id, principal, OUT_DIR)
    if collab is None:
        raise HTTPException(status_code=503, detail="the fleet declined the work")
    return collab.as_dict()


@app.get("/agents")
async def agents_index(_: None = Depends(require_shared_secret)) -> dict[str, Any]:
    """The fleet: who is connected, what each may touch, and how they run.

    Behind the shared secret. It was open, and it returns the Band agent ids
    that every audit entry is attributed to -- not a catastrophe on its own, but
    it is the fleet's identity roster and there is no reason for it to be public.
    """
    fleet = get_fleet()
    ids = agent_ids()
    return {
        "fleet_running": fleet.is_running,
        "connected": sorted(fleet.agents),
        "roles": [
            {
                "key": r.key,
                "name": r.agent_name,
                "agent_id": ids.get(r.key, ""),
                "does": r.description,
                "tools": list(r.tools),
            }
            for r in BAND_ROLES
        ],
        "transport": "band",
        "runs": "in-process; each agent holds its own WebSocket to Band",
        "note": (
            "Agents address each other by Band mention. The order work happens "
            "in emerges from that conversation rather than from a fixed loop."
        ),
    }


@app.get("/sessions/{session_id}/audit")
async def session_audit(
    session_id: str, tenant: TenantDep, _: None = Depends(require_shared_secret)
) -> dict[str, Any]:
    """Which agent did what, in what order, and why.

    In a legal-filing domain this is the point of the whole arrangement: when a
    form comes back rejected months later, somebody has to reconstruct it. The
    record is the collaboration -- every turn, the tools it ran, and whether the
    model or the fallback decided it.

    Read from memory while the room is still in the process, and from the
    durable ledger after. "Months later" was the claim, and this used to answer
    404 as soon as the service restarted -- which on a free instance is most of
    the time.
    """
    await _load_session(session_id, tenant)
    fleet = get_fleet()
    for collab in fleet.by_room.values():
        if collab.session_id == session_id:
            return collab.as_dict()
    stored = await ledger.replay(tenant.id, session_id)
    if stored is not None:
        return stored
    raise HTTPException(
        status_code=404,
        detail=(
            "no collaboration for this session yet -- POST "
            f"/sessions/{session_id}/orchestrate to run one"
        ),
    )


@app.get("/sessions/{session_id}/room")
async def session_room(
    session_id: str, tenant: TenantDep, _: None = Depends(require_shared_secret)
) -> dict[str, Any]:
    """The same conversation, read back out of Band rather than out of Zuzu.

    /audit is Zuzu's account of what its agents did. This is Band's, fetched
    from Band's own API with an agent's own credentials: who is in the room, and
    every message, in the order Band recorded them.

    Two independent records of one conversation is the point. Zuzu's trail is
    only trustworthy to whoever trusts Zuzu, and "the agents really did talk to
    each other over Band" is a claim that should be checkable without taking
    this service's word for it.

    Only available while the room is still known to this process -- Band keys
    the transcript by room id, and after eviction /audit is the durable record.
    """
    await _load_session(session_id, tenant)
    fleet = get_fleet()
    collab = next(
        (c for c in fleet.by_room.values() if c.session_id == session_id),
        None,
    )
    if collab is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "no live room for this session -- "
                f"GET /sessions/{session_id}/audit for the stored trail"
            ),
        )
    reader = fleet.agents.get("auditor")
    if reader is None:
        raise HTTPException(status_code=503, detail="the fleet is not connected to Band")
    try:
        participants = await reader.client.participants(collab.room_id)
        messages = await fleet.room_transcript(collab.room_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Band did not answer: {exc}") from exc
    return {
        "session_id": session_id,
        "room_id": collab.room_id,
        "source": "band",
        "participants": participants,
        "messages": messages,
        "message_count": len(messages),
        "note": (
            "Band's own record, read back with the agents' own credentials. "
            "Every message carries its sender, its mentions, and Band's delivery "
            "status per recipient."
        ),
    }


@app.get("/sessions/{session_id}/memory")
async def session_memory(
    session_id: str, tenant: TenantDep, _: None = Depends(require_shared_secret)
) -> dict[str, Any]:
    """Everything remembered about this caller, by tier.

    Semantic facts, past calls, and learned rules are shown separately because
    they are different kinds of knowledge with different lifetimes -- and an
    applicant is entitled to see exactly what is held about them.
    """
    session = await _load_session(session_id, tenant)
    schema = _resolve_form(session.form_id)
    profile = await get_memory(session.tenant_id).load_profile(session.caller_id, schema)
    return {
        "caller_key": (
            _user_key(session.caller_id, session.tenant_id) if session.caller_id else None
        ),
        "is_returning": profile.is_returning,
        # Where these tiers were actually read from. Three empty tiers because
        # the caller is new and three empty tiers because recall is down look
        # identical on screen unless the panel is told which it is.
        "source": profile.source,
        "degraded_reason": profile.degraded_reason,
        "summary": summarize(profile, schema),
        "semantic": [
            {
                "field_id": fid,
                "label": (
                    schema.get_field(fid).id.replace("_", " ") if schema.get_field(fid) else fid
                ),
                "value": _display_value(fid, value, schema),
            }
            for fid, value in profile.known_values.items()
        ],
        "episodic": [e.model_dump() for e in profile.episodes],
        "procedural": [p.model_dump() for p in profile.procedures],
    }


@app.get("/forms")
async def forms_index() -> dict[str, Any]:
    """What Zuzu can fill now, and what it can learn on request."""
    ready = list_forms()
    catalog = [
        {**entry, "ready": entry["form_id"].upper() in {f.upper() for f in ready}}
        for entry in load_catalog()
    ]
    return {"ready": ready, "catalog": catalog}


@app.post("/forms/onboard")
async def forms_onboard(
    tenant: TenantDep,
    form_id: str = Query(..., description="e.g. N-400"),
    pdf_url: str | None = Query(
        default=None, description="fillable PDF url, if not in the catalog"
    ),
    _: None = Depends(require_shared_secret),
) -> dict[str, Any]:
    """Teach Zuzu a new USCIS form while it is running.

    Scoped: a tenant restricted to a form list cannot widen the deployment by
    onboarding something outside it.

    Fetches the fillable PDF, extracts its AcroForm inventory, derives the
    questions from the PDF's own screen-reader tooltips, and registers it. No
    deploy and no code change -- which is the whole "a form is data" claim,
    tested rather than asserted.
    """
    if not tenant.may_file(form_id.upper()):
        raise HTTPException(
            status_code=403,
            detail=f"{tenant.name} is not configured to file {form_id.upper()}",
        )
    try:
        return await onboard(form_id, pdf_url)
    except OnboardingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
                "label": f.label or f.id.replace("_", " "),
                "group": f.group,
                "sensitive": f.sensitive,
                "required": f.required,
            }
            for f in schema.fields
        ],
    }


@app.get("/sessions/recent")
async def sessions_recent(
    tenant: TenantDep, _: None = Depends(require_shared_secret)
) -> dict[str, Any]:
    """Most recent sessions for THIS organisation, so the dashboard can attach
    to a live call without anyone copying a conversation id by hand mid-demo.

    Filtered by tenant. Unfiltered this was a directory listing of every
    organisation's sessions -- which turned every "if you know the session id"
    weakness elsewhere into something anybody could simply look up.
    """
    store = get_session_store()
    sessions = getattr(store, "_sessions", {})
    mine = [
        session
        for session in sessions.values()
        if session.tenant_id == tenant.id
        or (not session.tenant_id and tenant.id == DEFAULT_TENANT.id)
    ]
    ordered = sorted(mine, key=lambda s: s.created_at, reverse=True)
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
    session_id: str, tenant: TenantDep, _: None = Depends(require_shared_secret)
) -> dict[str, Any]:
    """Current values for a session, so a dashboard opened mid-call can paint
    the fields already collected instead of starting blank."""
    session = await _load_session(session_id, tenant)
    schema = _resolve_form(session.form_id)
    remaining, known = counts(session, schema)
    return {
        "session_id": session_id,
        "form_id": schema.form_id,
        "form_title": schema.title,
        "form_edition": schema.edition,
        "is_returning": session.is_returning,
        "remaining_count": remaining,
        "known_count": known,
        "has_pdf": bool(session.pdf_path),
        # The download is token-authorised now, and a plain <a href> cannot send
        # a header -- so the dashboard is handed the signed link rather than
        # building one from the session id and getting a 404.
        "pdf_url": (
            f"/forms/{session_id}.pdf?t={download_token(session_id)}" if session.pdf_path else None
        ),
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


@app.get("/deck")
async def deck() -> FileResponse:
    """The presentation. Same no-build-step rule as the dashboard."""
    page = REPO_ROOT / "dashboard" / "deck.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="deck not found")
    return FileResponse(page, media_type="text/html")


@app.post("/session/forget")
async def session_forget(
    tenant: TenantDep,
    caller_id: str = Query(...),
    tier: str | None = Query(default=None, description="semantic|episodic|procedural"),
    _: None = Depends(require_shared_secret),
) -> dict[str, Any]:
    """Delete what is remembered about a caller, optionally one tier only.

    The pitch promises an applicant can say "delete my data". Tier scoping means
    they can drop their call history without losing the profile that saves them
    an hour on the next form.
    """
    # `tier` is a free string on the query, so FastAPI never validates it and
    # Tier("all") raised ValueError straight into a 500 with a traceback -- on
    # the one endpoint whose whole job is to answer "is my data gone". "all" is
    # also the obvious thing to send, since the response already echoes it.
    try:
        scope = Tier(tier) if tier and tier != "all" else None
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown tier {tier!r}; use one of "
                f"{', '.join(t.value for t in Tier)}, or omit it for all"
            ),
        ) from exc
    try:
        removed = await get_memory(tenant.id).forget(caller_id, scope)
    except DeletionUnverifiable as exc:
        # Never answer "ok" to a deletion that did not happen. Someone who is
        # told their data is gone stops asking, which is the worst outcome here.
        raise HTTPException(
            status_code=503,
            detail=f"deletion could not be carried out or confirmed: {exc}",
        ) from exc
    return {"ok": True, "entries_removed": removed, "tier": tier or "all"}


@app.get("/")
async def root() -> Response:
    return Response(
        content=json.dumps({"service": "zuzu", "docs": "/docs", "health": "/health"}),
        media_type="application/json",
    )

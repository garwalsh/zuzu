"""Contract tests: the happy path, and every "must not" from Issue #1.

The negative tests are the point. In a legal-filing context the dangerous
failures are the quiet ones -- a fabricated value, a logged SSN, a form that
reports success while dropping an answer.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import event_bus, memory, session_store

SECRET = "test-secret"
REPO_ROOT = Path(__file__).resolve().parent.parent
PERSONA = json.loads((REPO_ROOT / "data" / "demo_personas.json").read_text())["personas"]["maria"]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Fresh singletons and a known secret for every test."""
    monkeypatch.setenv("ZUZU_SHARED_SECRET", SECRET)
    monkeypatch.setenv("STORE_BACKEND", "memory")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://testserver")
    # Keep the suite hermetic: a real MEM0_API_KEY in the developer's shell would
    # otherwise send session_init to the live mem0 API and prefill fields,
    # breaking the "fresh session" contract these tests assert.
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    session_store.reset_session_store()
    event_bus.reset_event_bus()
    memory.reset_memory()
    yield
    session_store.reset_session_store()
    event_bus.reset_event_bus()
    memory.reset_memory()


@pytest.fixture
def client():
    from api.main import app

    with TestClient(app) as test_client:
        yield test_client


def auth() -> dict[str, str]:
    return {"X-Zuzu-Secret": SECRET}


def start_session(client, conversation_id: str = "conv_test") -> str:
    resp = client.post(
        "/session/init",
        json={"caller_id": "+15551234567", "conversation_id": conversation_id},
        headers=auth(),
    )
    assert resp.status_code == 200
    return conversation_id


def run_full_call(client, conversation_id: str) -> dict:
    """Drive the agent loop to completion, answering from the demo persona."""
    answers = PERSONA["answers"]
    for _ in range(200):
        resp = client.post(
            "/tools/get_missing_fields",
            json={"session_id": conversation_id, "form_id": "I-765"},
            headers=auth(),
        )
        assert resp.status_code == 200
        field = resp.json()["next_field"]
        if field is None:
            break
        client.post(
            "/tools/save_field",
            json={
                "session_id": conversation_id,
                "field_id": field["id"],
                "value": answers.get(field["id"], "__skip__"),
            },
            headers=auth(),
        ).raise_for_status()
    else:
        pytest.fail("interview did not terminate within 200 turns")

    return client.post(
        "/tools/generate_form", json={"session_id": conversation_id}, headers=auth()
    ).json()


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_health_needs_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # I-765 is the MVP form; anything else present has been onboarded from its
    # PDF into data/forms/ and is served without a code change.
    assert "I-765" in body["form_ids"]


def test_onboarded_forms_are_served_without_a_code_change(client):
    """Every schema under data/forms/ should be answerable."""
    listed = client.get("/forms").json()
    for form_id in listed["ready"]:
        resp = client.get(f"/forms/{form_id}/schema")
        assert resp.status_code == 200, form_id
        assert resp.json()["fields"], f"{form_id} has no questions"


def test_catalog_reports_what_is_ready(client):
    listed = client.get("/forms").json()
    assert listed["catalog"], "the form catalog should not be empty"
    i765 = next(e for e in listed["catalog"] if e["form_id"] == "I-765")
    assert i765["ready"] is True


def test_full_call_produces_a_downloadable_pdf(client):
    conversation_id = start_session(client, "conv_happy")
    result = run_full_call(client, conversation_id)

    assert result["status"] == "complete", result
    assert result["missing"] == []
    assert result["pdf_url"].endswith(f"/forms/{conversation_id}.pdf")

    download = client.get(f"/forms/{conversation_id}.pdf")
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/pdf"
    assert download.content[:5] == b"%PDF-"


def test_interview_terminates_and_counts_go_down(client):
    conversation_id = start_session(client, "conv_counts")
    first = client.post(
        "/tools/get_missing_fields",
        json={"session_id": conversation_id, "form_id": "I-765"},
        headers=auth(),
    ).json()
    assert first["known_count"] == 0
    assert first["remaining_count"] > 0

    client.post(
        "/tools/save_field",
        json={
            "session_id": conversation_id,
            "field_id": first["next_field"]["id"],
            "value": "renewal",
        },
        headers=auth(),
    )
    second = client.post(
        "/tools/get_missing_fields",
        json={"session_id": conversation_id, "form_id": "I-765"},
        headers=auth(),
    ).json()
    assert second["known_count"] == 1
    assert second["next_field"]["id"] != first["next_field"]["id"]


def test_form_id_spelling_variants_resolve(client):
    conversation_id = start_session(client, "conv_variants")
    for spelling in ("I-765", "i765", "i-765"):
        resp = client.post(
            "/tools/get_missing_fields",
            json={"session_id": conversation_id, "form_id": spelling},
            headers=auth(),
        )
        assert resp.status_code == 200, spelling


def test_demo_mode_runs_without_voice(client):
    resp = client.post("/demo/run", headers=auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "complete"
    assert body["fields_asked"]


# --------------------------------------------------------------------------
# MUST NOT: authentication
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/session/init", {"caller_id": "+1", "conversation_id": "c"}),
        ("/tools/get_missing_fields", {"session_id": "c", "form_id": "I-765"}),
        ("/tools/save_field", {"session_id": "c", "field_id": "given_name", "value": "x"}),
        ("/tools/generate_form", {"session_id": "c"}),
        ("/session/complete", {"conversation_id": "c"}),
    ],
)
def test_every_tool_endpoint_rejects_a_missing_secret(client, path, payload):
    assert client.post(path, json=payload).status_code == 401


def test_wrong_secret_is_rejected(client):
    resp = client.post(
        "/session/init",
        json={"caller_id": "+1", "conversation_id": "c"},
        headers={"X-Zuzu-Secret": "not-the-secret"},
    )
    assert resp.status_code == 401


def test_must_not_authorize_on_a_blank_configured_secret(client, monkeypatch):
    """An unset secret must fail closed, not accept a blank header."""
    monkeypatch.delenv("ZUZU_SHARED_SECRET", raising=False)
    resp = client.post(
        "/session/init",
        json={"caller_id": "+1", "conversation_id": "c"},
        headers={"X-Zuzu-Secret": ""},
    )
    assert resp.status_code == 500


# --------------------------------------------------------------------------
# MUST NOT: fabricate, or accept what it cannot place
# --------------------------------------------------------------------------


def test_must_not_accept_an_unknown_field_id(client):
    conversation_id = start_session(client, "conv_unknown_field")
    resp = client.post(
        "/tools/save_field",
        json={"session_id": conversation_id, "field_id": "favourite_colour", "value": "blue"},
        headers=auth(),
    )
    assert resp.status_code == 422


def test_must_not_fabricate_a_skipped_value(client):
    """A skipped field must be blank on the PDF, not defaulted or inferred."""
    from pypdf import PdfReader

    conversation_id = start_session(client, "conv_skip")
    run_full_call(client, conversation_id)

    from api.i765_schema import get_i765_schema

    schema = get_i765_schema()
    # The persona explicitly skips these two.
    skipped = [f for f in schema.fields if PERSONA["answers"].get(f.id) == "__skip__"]
    assert skipped, "expected the demo persona to skip at least one field"

    fields = PdfReader(str(REPO_ROOT / "out" / f"{conversation_id}.pdf")).get_fields() or {}
    for form_field in skipped:
        if not form_field.pdf_field:
            continue
        value = fields.get(form_field.pdf_field, {}).get("/V")
        assert value in (None, ""), f"{form_field.id} was fabricated as {value!r}"


def test_must_not_write_a_guessed_eligibility_category(tmp_path):
    """An unparseable code leaves all three boxes blank rather than guessing."""
    from pypdf import PdfReader

    from api.i765_schema import get_i765_schema
    from api.pdf_engine import fill_i765

    schema = get_i765_schema()
    values = dict(PERSONA["answers"])
    values["eligibility_category"] = "I think I'm a student?"

    out = fill_i765(values, tmp_path / "guess.pdf", schema)
    fields = PdfReader(str(out)).get_fields() or {}
    elig = schema.get_field("eligibility_category")
    for part in elig.pdf_field_parts:
        assert fields.get(part, {}).get("/V") in (None, ""), f"{part} got a guessed value"


def test_must_not_write_an_invalid_state_code(tmp_path):
    """State combos have no Edit flag; free text is silently dropped by the
    viewer, so the engine must refuse it rather than appear to have written it."""
    from pypdf import PdfReader

    from api.pdf_engine import fill_i765

    values = dict(PERSONA["answers"])
    values["mailing_state"] = "California"

    out = fill_i765(values, tmp_path / "state.pdf")
    fields = PdfReader(str(out)).get_fields() or {}
    assert fields.get("form1[0].Page2[0].Pt2Line5_State[0]", {}).get("/V") in (None, "")


def test_incomplete_generation_writes_no_pdf(client):
    conversation_id = start_session(client, "conv_incomplete")
    result = client.post(
        "/tools/generate_form", json={"session_id": conversation_id}, headers=auth()
    ).json()

    assert result["status"] == "incomplete"
    assert result["pdf_url"] is None
    assert result["missing"]
    assert client.get(f"/forms/{conversation_id}.pdf").status_code == 404


# --------------------------------------------------------------------------
# MUST NOT: leak sensitive values into logs
# --------------------------------------------------------------------------


def test_must_not_log_sensitive_values(client, caplog):
    conversation_id = start_session(client, "conv_logs")
    secret_ssn = "123456789"
    secret_anum = "987654321"

    with caplog.at_level(logging.DEBUG):
        for field_id, value in (("ssn", secret_ssn), ("a_number", secret_anum)):
            client.post(
                "/tools/save_field",
                json={"session_id": conversation_id, "field_id": field_id, "value": value},
                headers=auth(),
            )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    logged += "\n".join(str(record.__dict__) for record in caplog.records)
    assert secret_ssn not in logged, "an SSN reached the logs"
    assert secret_anum not in logged, "an A-Number reached the logs"
    # Field ids are fine to log, and we want them there for debugging.
    assert any(getattr(r, "field_id", None) == "ssn" for r in caplog.records)


def test_sensitive_fields_are_flagged_for_read_back(client):
    conversation_id = start_session(client, "conv_sensitive")
    resp = client.post(
        "/tools/save_field",
        json={"session_id": conversation_id, "field_id": "a_number", "value": "123456789"},
        headers=auth(),
    ).json()
    assert resp["needs_confirmation"] is True


def test_low_confidence_is_flagged_for_read_back(client):
    conversation_id = start_session(client, "conv_lowconf")
    resp = client.post(
        "/tools/save_field",
        json={
            "session_id": conversation_id,
            "field_id": "given_name",
            "value": "Maria",
            "confidence": 0.4,
        },
        headers=auth(),
    ).json()
    assert resp["needs_confirmation"] is True


# --------------------------------------------------------------------------
# MUST NOT: invent sessions or forms
# --------------------------------------------------------------------------


def test_tool_path_opens_a_session_when_the_init_webhook_never_fired(client):
    """The conversation-initiation webhook is an optimisation, not a
    precondition. It does not fire reliably for the embedded widget, and when it
    does not, every tool call used to 404 and the agent told the applicant it
    could not get the next question. The call must survive that."""
    resp = client.post(
        "/tools/get_missing_fields",
        json={"session_id": "conv_no_init_webhook", "form_id": "I-765"},
        headers=auth(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["next_field"] is not None
    assert body["known_count"] == 0


def test_a_whole_call_works_without_session_init(client):
    """End to end with no init webhook at all: interview, then a real PDF."""
    conversation_id = "conv_widget_only"
    result = run_full_call(client, conversation_id)
    assert result["status"] == "complete", result
    assert client.get(f"/forms/{conversation_id}.pdf").status_code == 200


def test_read_paths_stay_strict_about_unknown_sessions(client):
    """Opening a session on the write path is resilience. Inventing one on a
    read path would be reporting data that does not exist."""
    assert client.get("/sessions/never-created/values", headers=auth()).status_code == 404
    assert client.get("/forms/never-created.pdf").status_code == 404


def test_unknown_form_is_404(client):
    conversation_id = start_session(client, "conv_badform")
    resp = client.post(
        "/tools/get_missing_fields",
        json={"session_id": conversation_id, "form_id": "I-999"},
        headers=auth(),
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Real ElevenLabs payloads carry more than the integration contract's example
# --------------------------------------------------------------------------


def test_session_init_accepts_the_real_webhook_payload(client):
    """The live conversation-initiation webhook sends called_number, call_sid
    and source alongside the three documented keys. Rejecting those is a 422
    before the applicant has said a word."""
    resp = client.post(
        "/session/init",
        json={
            "caller_id": "+15551234567",
            "agent_id": "agent_xxx",
            "conversation_id": "conv_extra",
            "called_number": "+18005551212",
            "call_sid": "CA123",
            "source": "twilio",
        },
        headers=auth(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["type"] == "conversation_initiation_client_data"


def test_session_complete_accepts_the_real_post_call_payload(client):
    conversation_id = start_session(client, "conv_postcall")
    resp = client.post(
        "/session/complete",
        json={
            "conversation_id": conversation_id,
            "transcript": [{"role": "agent", "message": "hello"}],
            "collected": {},
            "agent_id": "agent_xxx",
            "status": "done",
            "metadata": {"call_duration_secs": 42},
            "analysis": {"call_successful": "success"},
        },
        headers=auth(),
    )
    assert resp.status_code == 200, resp.text


def test_tool_calls_tolerate_extra_params(client):
    """ElevenLabs may add fields to a tool call payload over time."""
    conversation_id = start_session(client, "conv_toolextra")
    resp = client.post(
        "/tools/save_field",
        json={
            "session_id": conversation_id,
            "field_id": "given_name",
            "value": "Maria",
            "confidence": 0.95,
            "language": "es",
            "tool_call_id": "tc_abc123",
        },
        headers=auth(),
    )
    assert resp.status_code == 200, resp.text


def test_unknown_field_id_is_still_rejected_despite_lenient_envelope(client):
    """Tolerating unknown envelope keys must not tolerate an unknown field_id:
    a value with nowhere to go on the form must never look collected."""
    conversation_id = start_session(client, "conv_strictfield")
    resp = client.post(
        "/tools/save_field",
        json={"session_id": conversation_id, "field_id": "not_a_field", "value": "x"},
        headers=auth(),
    )
    assert resp.status_code == 422


def test_returning_caller_gets_a_language_override(client, monkeypatch):
    """A caller we know speaks Spanish should be greeted in Spanish, rather than
    having to speak first so the agent can detect it."""
    from api import main as main_mod
    from api.memory import ApplicantProfile

    async def fake_profile(caller_id, schema=None):
        return ApplicantProfile(
            caller_id=caller_id,
            display_name="Maria",
            preferred_language="es",
            known_values={"given_name": "Maria"},
            is_returning=True,
        )

    monkeypatch.setattr(main_mod.get_memory(), "load_profile", fake_profile)
    resp = client.post(
        "/session/init",
        json={"caller_id": "+15551234567", "conversation_id": "conv_es"},
        headers=auth(),
    )
    body = resp.json()
    assert body["dynamic_variables"]["applicant_name"] == "Maria"
    assert body["dynamic_variables"]["is_returning"] is True
    assert body["conversation_config_override"] == {"agent": {"language": "es"}}


# --------------------------------------------------------------------------
# ElevenLabs server tools POST a JSON body, never a query string
# --------------------------------------------------------------------------


def test_identify_form_reads_a_json_body(client):
    """The tools were written against query params while the agent sends a
    body, so `text` arrived empty and every call answered 'I can't find that'.
    This is the shape ElevenLabs actually sends."""
    resp = client.post(
        "/tools/identify_form",
        json={"text": "I want to become a citizen", "session_id": "conv_body"},
        headers=auth(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["found"] is True
    assert body["form_id"] == "N-400"


def test_identify_form_handles_plain_language_and_urls(client):
    cases = [
        ({"text": "I need my work permit"}, "I-765"),
        ({"text": "necesito permiso de trabajo"}, "I-765"),
        ({"text": "my green card is expiring"}, "I-485"),
        ({"text": "", "url": "https://www.uscis.gov/n-400"}, "N-400"),
        ({"text": "I need form I-131"}, "I-131"),
    ]
    for payload, expected in cases:
        body = client.post("/tools/identify_form", json=payload, headers=auth()).json()
        assert body.get("form_id") == expected, f"{payload} -> {body}"


def test_set_form_reads_a_json_body_and_keeps_answers(client):
    conversation_id = start_session(client, "conv_switch")
    client.post(
        "/tools/save_field",
        json={"session_id": conversation_id, "field_id": "given_name", "value": "Maria"},
        headers=auth(),
    )
    resp = client.post(
        "/session/set_form",
        json={"session_id": conversation_id, "form_id": "I-765"},
        headers=auth(),
    )
    assert resp.status_code == 200, resp.text
    # Switching forms must not discard what the applicant already told us.
    assert resp.json()["carried_over"] >= 1


def test_set_form_requires_both_values(client):
    assert client.post("/session/set_form", json={"form_id": ""}, headers=auth()).status_code == 422


def test_websocket_rejects_a_bad_secret(client):
    """Closed with 1008 (policy violation), not merely dropped."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws/conv_x?secret=wrong") as ws:
            ws.receive_text()
    assert excinfo.value.code == 1008


def test_websocket_streams_events_with_a_good_secret(client):
    conversation_id = start_session(client, "conv_ws")
    with client.websocket_connect(f"/ws/{conversation_id}?secret={SECRET}") as ws:
        first = json.loads(ws.receive_text())
        assert first["type"] == "session_started"
        assert first["session_id"] == conversation_id

        client.post(
            "/tools/save_field",
            json={"session_id": conversation_id, "field_id": "given_name", "value": "Maria"},
            headers=auth(),
        )
        event = json.loads(ws.receive_text())
        assert event["type"] == "field_saved"
        assert event["data"]["field_id"] == "given_name"
        # The event carries the field id for the dashboard, never the value.
        assert "Maria" not in ws.__class__.__name__ + json.dumps(event)

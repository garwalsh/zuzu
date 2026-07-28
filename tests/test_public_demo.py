"""The public demo is a whole organisation, not a restricted view.

The live link has to work -- open a session, switch forms, fill them, generate
the PDF, read the memory, run the agents. A read-only replay demonstrates that
we can render a screenshot, which is not the claim being made.

So these tests come in two halves. The first says a visitor with no credentials
of their own can do everything. The second says none of it reaches a real
organisation. Both halves have to hold, or the feature is either useless or a
hole.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from api import memory, public_demo, session_store, tenancy
from api.tenancy import TENANT_HEADER

SECRET = "deployment-secret"
DEMO = "public-demo-secret"
KEY_A = "tenant-key-a"


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@pytest.fixture
def client(tmp_path, monkeypatch):
    registry = tmp_path / "tenants.json"
    registry.write_text(
        json.dumps(
            {
                "tenants": [
                    {"id": "clinic-a", "name": "Clinic A", "api_key_hashes": [_digest(KEY_A)]},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ZUZU_SHARED_SECRET", SECRET)
    monkeypatch.setenv("ZUZU_DEMO_SECRET", DEMO)
    monkeypatch.setenv("STORE_BACKEND", "memory")
    monkeypatch.setenv("TOKENROUTER_API_KEY", "")

    tenancy.reset_registry()
    monkeypatch.setattr(tenancy, "_registry", tenancy.TenantRegistry(registry))
    session_store.reset_session_store()
    memory.reset_memory()
    public_demo.reset_budget()

    from api.main import app

    with TestClient(app) as c:
        yield c

    tenancy.reset_registry()
    session_store.reset_session_store()
    memory.reset_memory()
    public_demo.reset_budget()


def visitor() -> dict[str, str]:
    """What the browser sends after reading /config. No tenant key at all."""
    return {"X-Zuzu-Secret": DEMO, "Content-Type": "application/json"}


def operator(key: str = KEY_A) -> dict[str, str]:
    return {
        "X-Zuzu-Secret": SECRET,
        TENANT_HEADER: key,
        "Content-Type": "application/json",
    }


def _answers() -> dict[str, str]:
    import pathlib

    return json.loads(
        (pathlib.Path(__file__).resolve().parent.parent / "data" / "demo_personas.json").read_text()
    )["personas"]["maria"]["answers"]


# ---------------------------------------------------------------------------
# Half one: it actually works
# ---------------------------------------------------------------------------


def test_the_browser_is_handed_a_credential(client):
    """The page needs no query string. /config gives it what to authenticate
    with, and that credential is deliberately public."""
    cfg = client.get("/config").json()
    assert cfg["public_demo"] == DEMO
    assert cfg["public_demo"] != SECRET, "the deployment secret is never served"


def test_the_public_demo_is_off_unless_it_is_configured(client, monkeypatch):
    monkeypatch.delenv("ZUZU_DEMO_SECRET", raising=False)
    assert client.get("/config").json()["public_demo"] == ""
    assert client.get("/sessions/anything/values", headers=visitor()).status_code == 401


def test_a_visitor_can_run_a_whole_filing(client):
    """Open, fill, generate, download. The entire product, no credentials."""
    sid = "visitor-filing"
    opened = client.post(
        "/session/init",
        json={"conversation_id": sid, "caller_id": "+14155550142"},
        headers=visitor(),
    )
    assert opened.status_code == 200, opened.text

    for field_id, value in _answers().items():
        r = client.post(
            "/tools/save_field",
            json={"session_id": sid, "field_id": field_id, "value": value},
            headers=visitor(),
        )
        assert r.status_code == 200, f"{field_id}: {r.text}"

    gen = client.post("/tools/generate_form", json={"session_id": sid}, headers=visitor())
    assert gen.status_code == 200, gen.text
    assert gen.json()["status"] == "complete"

    pdf = client.get(f"/forms/{sid}.pdf", headers=visitor())
    assert pdf.status_code == 200
    assert pdf.content[:4] == b"%PDF"
    assert len(pdf.content) > 20000


def test_a_visitor_can_switch_forms_mid_call(client):
    """A form is data. The demo has to be able to show that."""
    sid = "visitor-switch"
    client.post(
        "/session/init",
        json={"conversation_id": sid, "caller_id": "+14155550142"},
        headers=visitor(),
    )
    client.post(
        "/tools/save_field",
        json={"session_id": sid, "field_id": "given_name", "value": "Maria"},
        headers=visitor(),
    )

    switched = client.post(
        "/session/set_form", json={"session_id": sid, "form_id": "N-400"}, headers=visitor()
    )
    assert switched.status_code == 200, switched.text

    state = client.get(f"/sessions/{sid}/values", headers=visitor()).json()
    assert state["form_id"] == "N-400"
    assert state["values"].get("given_name") == "Maria", "answers carry across the switch"


def test_a_visitor_gets_the_interview_and_the_checklist(client):
    sid = "visitor-interview"
    client.post(
        "/session/init",
        json={"conversation_id": sid, "caller_id": "+14155550142"},
        headers=visitor(),
    )
    nxt = client.post(
        "/tools/get_missing_fields", json={"session_id": sid, "form_id": "I-765"}, headers=visitor()
    )
    assert nxt.status_code == 200, nxt.text
    assert nxt.json()["next_field"]["id"]

    assert client.get(f"/sessions/{sid}/checklist", headers=visitor()).status_code == 200


def test_a_visitor_sees_all_three_memory_tiers(client):
    sid = "visitor-memory"
    client.post(
        "/session/init",
        json={"conversation_id": sid, "caller_id": "+14155550142"},
        headers=visitor(),
    )
    for field_id, value in _answers().items():
        client.post(
            "/tools/save_field",
            json={"session_id": sid, "field_id": field_id, "value": value},
            headers=visitor(),
        )
    client.post(
        "/session/complete",
        json={"conversation_id": sid, "collected": {}},
        headers=visitor(),
    )

    mem = client.get(f"/sessions/{sid}/memory", headers=visitor())
    assert mem.status_code == 200, mem.text
    body = mem.json()
    for tier in ("semantic", "episodic", "procedural"):
        assert tier in body, tier
    assert body["semantic"], "the demo must show real recall, not an empty panel"
    assert body["caller_key"], "and say which key it is scoped to"


def test_a_visitor_can_watch_their_own_call_live(client):
    sid = "visitor-ws"
    client.post(
        "/session/init",
        json={"conversation_id": sid, "caller_id": "+14155550142"},
        headers=visitor(),
    )
    with client.websocket_connect(f"/ws/{sid}?secret={DEMO}") as ws:
        client.post(
            "/tools/save_field",
            json={"session_id": sid, "field_id": "given_name", "value": "Maria"},
            headers=visitor(),
        )
        event = json.loads(ws.receive_text())
        assert event["session_id"] == sid


def test_a_visitor_can_run_the_sample_application(client):
    r = client.post("/demo/run", headers=visitor())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "complete"


def test_the_demo_tenant_may_file_every_registered_form(client):
    """Restricting the demo to two forms would demonstrate the opposite of the
    claim the demo exists to make."""
    from api.form_registry import list_forms

    for form_id in list_forms():
        assert public_demo.PUBLIC_TENANT.may_file(form_id), form_id


# ---------------------------------------------------------------------------
# Half two: and it is not a way in
# ---------------------------------------------------------------------------


def test_the_demo_credential_cannot_reach_a_real_organisation(client):
    """The attack this whole design exists to stop."""
    client.post(
        "/session/init",
        json={"conversation_id": "clinic-session", "caller_id": "+14155550142"},
        headers=operator(),
    )
    for path in ("values", "memory", "audit", "checklist"):
        r = client.get(f"/sessions/clinic-session/{path}", headers=visitor())
        assert r.status_code == 404, f"{path}: {r.status_code}"

    assert (
        client.post(
            "/tools/save_field",
            json={"session_id": "clinic-session", "field_id": "given_name", "value": "ATTACKER"},
            headers=visitor(),
        ).status_code
        == 404
    )
    owner = client.get("/sessions/clinic-session/values", headers=operator()).json()
    assert owner["values"].get("given_name") != "ATTACKER"


def test_presenting_a_real_tenant_key_with_the_demo_secret_still_lands_in_the_demo(client):
    """The credential decides which organisation you are, not the header that
    happens to accompany it. Otherwise the demo secret plus a leaked tenant key
    would be a full compromise of that tenant."""
    client.post(
        "/session/init",
        json={"conversation_id": "clinic-session-2", "caller_id": "+14155550142"},
        headers=operator(),
    )
    headers = {"X-Zuzu-Secret": DEMO, TENANT_HEADER: KEY_A, "Content-Type": "application/json"}
    assert client.get("/sessions/clinic-session-2/values", headers=headers).status_code == 404


def test_a_real_tenant_cannot_read_the_demo_either(client):
    """Isolation runs both ways or it is not isolation."""
    sid = "demo-private"
    client.post(
        "/session/init",
        json={"conversation_id": sid, "caller_id": "+14155550142"},
        headers=visitor(),
    )
    assert client.get(f"/sessions/{sid}/values", headers=operator()).status_code == 404


def test_the_demo_never_persists_an_identifier(client):
    """A visitor may speak an A-number into the demo. It belongs on the page and
    in the PDF, not in a store that outlives the session."""
    assert public_demo.PUBLIC_TENANT.store_sensitive is False

    sid = "demo-sensitive"
    client.post(
        "/session/init",
        json={"conversation_id": sid, "caller_id": "+14155550142"},
        headers=visitor(),
    )
    for field_id, value in _answers().items():
        client.post(
            "/tools/save_field",
            json={"session_id": sid, "field_id": field_id, "value": value},
            headers=visitor(),
        )
    kept = {
        row["field_id"]
        for row in client.get(f"/sessions/{sid}/memory", headers=visitor()).json()["semantic"]
    }
    for identifier in ("a_number", "ssn", "passport_number", "sevis_number", "i94_number"):
        assert identifier not in kept, f"{identifier} was persisted by the public demo"


def test_the_demo_memory_namespace_is_nobody_elses(client):
    """Derived, not checked: a different tenant id hashes to a different scope,
    so the same caller in the demo and at a clinic are two separate files."""
    from api.tenancy import DEFAULT_TENANT, scope_key

    caller = "+14155550142"
    demo = scope_key(public_demo.PUBLIC_TENANT.id, caller)
    assert demo != scope_key("clinic-a", caller)
    assert demo != scope_key(DEFAULT_TENANT.id, caller)


def test_the_demo_secret_is_not_the_deployment_secret(client):
    """They authenticate different things. If they were ever the same value the
    public page would be handing out full access."""
    from api.security import verify_secret

    assert verify_secret(DEMO) is True
    assert verify_secret(SECRET) is True
    assert verify_secret("neither") is False
    assert public_demo.is_demo_secret(SECRET) is False
    assert public_demo.is_demo_secret(DEMO) is True


def test_the_public_budget_is_finite(client):
    """The credential is public, so the expensive paths behind it are reachable
    by anyone with a browser and a held-down refresh key."""
    public_demo.reset_budget()
    allowed = sum(1 for _ in range(public_demo.BUDGET + 5) if public_demo.take_budget())
    assert allowed == public_demo.BUDGET
    assert public_demo.take_budget() is False


# ---------------------------------------------------------------------------
# Choosing the form from what somebody actually said.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "said,expect",
    [
        ("I need my work permit", "I-765"),
        ("permiso de trabajo", "I-765"),
        ("I want to become a citizen", "N-400"),
        ("I need to renew my green card", "I-90"),
        ("I want to bring my wife to America", "I-130"),
        ("bring my husband here", "I-130"),
        ("I need to travel and come back", None),
        ("advance parole", "I-131"),
        ("form I-485", "I-485"),
        ("i 765", "I-765"),
    ],
)
def test_plain_words_reach_the_right_form(said, expect):
    """Nobody says "I-765". The deterministic paths have to carry most of this,
    because they are the ones that run when the model is unreachable."""
    from api.form_finder import from_form_number, from_intent

    hit = from_form_number(said) or from_intent(said)
    got = hit["form_id"] if hit else None
    if expect is None:
        return  # only asserts the ones that must match deterministically
    assert got == expect, f"{said!r} -> {got}"


@pytest.mark.asyncio
async def test_the_model_only_ever_answers_with_a_form_we_have(monkeypatch):
    """The guard that makes asking a model safe here: a form the deployment does
    not have is not a form, whatever the model called it."""
    import api.form_finder as finder

    monkeypatch.setenv("TOKENROUTER_API_KEY", "sk-test")

    async def hallucinate(prompt, system=None, **kw):
        return {"form_id": "I-9999", "why": "made up"}

    monkeypatch.setattr(finder, "load_catalog", finder.load_catalog)
    monkeypatch.setattr("api.inference.complete_json", hallucinate)
    assert await finder.from_model("something unusual") is None

    async def real(prompt, system=None, **kw):
        return {"form_id": "i-589", "why": "asylum"}

    monkeypatch.setattr("api.inference.complete_json", real)
    hit = await finder.from_model("I am afraid to go home")
    assert hit is not None
    assert hit["form_id"] == "I-589"
    assert hit["confidence"] < 0.9, "a judgement must be read back before switching"


@pytest.mark.asyncio
async def test_the_model_is_never_asked_when_a_phrase_already_matched(monkeypatch):
    """It costs a round trip and it cannot beat an exact match."""
    import api.form_finder as finder

    called = []

    async def spy(text):
        called.append(text)
        return None

    monkeypatch.setattr(finder, "from_model", spy)
    hit = await finder.identify(text="I need my work permit")
    assert hit["form_id"] == "I-765"
    assert not called, "the phrase table already answered"


@pytest.mark.asyncio
async def test_an_unreachable_model_costs_nothing(monkeypatch):
    """No model configured is the normal case for a fresh checkout."""
    import api.form_finder as finder

    monkeypatch.delenv("TOKENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert await finder.from_model("something nobody wrote a phrase for") is None


def test_the_band_page_is_public_and_can_read_what_it_shows(client):
    """It renders a real collaboration, so it needs the roster and the trail.
    Both are behind the shared secret; the demo credential satisfies it."""
    assert client.get("/band").status_code == 200

    roster = client.get("/agents", headers=visitor())
    assert roster.status_code == 200, roster.text
    assert len(roster.json()["roles"]) == 6

    assert client.get("/agents").status_code == 401, "still not open to nobody"


def test_the_widget_offers_typing_as_well_as_talking(client):
    """Half the people this is for will not say their A-number out loud in a
    waiting room. The payload pins text input so a re-run cannot drop it."""
    from tools.create_elevenlabs_agent import build_payload

    widget = build_payload("https://example.test", "s")["platform_settings"]["widget"]
    assert widget["text_input_enabled"] is True
    assert widget["supports_text_only"] is True


# ---------------------------------------------------------------------------
# The voice agent guesses field ids. Found by simulating a real conversation
# against the deployed ElevenLabs agent: it asked the right question, got the
# right answer, and filed it under `applicant_name`, `place_of_birth`, `gender`
# and `alien_number` -- none of which exist on the I-765. Every one of those
# answers was correct and was thrown away with a 422, mid-call.
# ---------------------------------------------------------------------------


def _open(client, sid: str) -> None:
    client.post(
        "/session/init",
        json={"conversation_id": sid, "caller_id": "+14155550142"},
        headers=visitor(),
    )


@pytest.mark.parametrize(
    "guessed,becomes",
    [
        ("gender", "sex"),
        ("alien_number", "a_number"),
        ("dob", "date_of_birth"),
        ("last_name", "family_name"),
        ("first_name", "given_name"),
        ("zip_code", "mailing_zip"),
        ("nationality", "country_of_citizenship"),
        ("GIVEN NAME", "given_name"),
    ],
)
def test_a_guessed_field_id_still_lands_on_the_right_field(client, guessed, becomes):
    sid = f"alias-{guessed.replace(' ', '-')}"
    _open(client, sid)
    r = client.post(
        "/tools/save_field",
        json={"session_id": sid, "field_id": guessed, "value": "Reyes"},
        headers=visitor(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["field_id"] == becomes, "and the agent is told what it became"
    values = client.get(f"/sessions/{sid}/values", headers=visitor()).json()["values"]
    assert becomes in values


def test_an_answer_is_attributed_to_the_question_that_was_just_asked(client):
    """The rule that generalises: it needs no list of names anybody thought of
    in advance. An answer arriving after a question is an answer to it."""
    sid = "attributed"
    _open(client, sid)
    asked = client.post(
        "/tools/get_missing_fields",
        json={"session_id": sid, "form_id": "I-765"},
        headers=visitor(),
    ).json()["next_field"]["id"]

    r = client.post(
        "/tools/save_field",
        json={"session_id": sid, "field_id": "something_the_model_invented", "value": "renewal"},
        headers=visitor(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["field_id"] == asked

    values = client.get(f"/sessions/{sid}/values", headers=visitor()).json()["values"]
    assert values.get(asked) == "renewal"
    assert "something_the_model_invented" not in values


def test_a_value_with_nowhere_to_go_is_still_refused(client):
    """The fallback must not become "put it anywhere". With nothing asked and no
    alias, an unknown id is still a value we must not pretend to have."""
    sid = "nowhere"
    _open(client, sid)
    r = client.post(
        "/tools/save_field",
        json={"session_id": sid, "field_id": "favourite_colour", "value": "blue"},
        headers=visitor(),
    )
    assert r.status_code == 422, r.text
    assert "unknown field_id" in r.text


def test_the_resolved_id_is_what_gets_remembered_and_broadcast(client):
    """Storing it under the guessed name would put it in memory, on the
    dashboard and in the logs under a name the form does not have."""
    sid = "resolved-everywhere"
    _open(client, sid)
    client.post(
        "/tools/save_field",
        json={"session_id": sid, "field_id": "gender", "value": "female"},
        headers=visitor(),
    )
    values = client.get(f"/sessions/{sid}/values", headers=visitor()).json()["values"]
    assert values.get("sex") == "female"
    assert "gender" not in values


def test_the_fallback_fires_once_and_then_stops(client):
    """Without this the rule is "put it wherever we last asked", and three
    unrelated values in a row all land on the same field, each overwriting the
    last -- worse than refusing them, because it looks like it worked."""
    sid = "fallback-once"
    _open(client, sid)
    asked = client.post(
        "/tools/get_missing_fields",
        json={"session_id": sid, "form_id": "I-765"},
        headers=visitor(),
    ).json()["next_field"]["id"]

    first = client.post(
        "/tools/save_field",
        json={"session_id": sid, "field_id": "invented_one", "value": "renewal"},
        headers=visitor(),
    )
    assert first.status_code == 200
    assert first.json()["field_id"] == asked

    # The question now has an answer, so there is nothing outstanding to
    # attribute the next unrecognised id to.
    second = client.post(
        "/tools/save_field",
        json={"session_id": sid, "field_id": "invented_two", "value": "Guadalajara"},
        headers=visitor(),
    )
    assert second.status_code == 422, second.text

    values = client.get(f"/sessions/{sid}/values", headers=visitor()).json()["values"]
    assert values[asked] == "renewal", "the first answer must not have been overwritten"

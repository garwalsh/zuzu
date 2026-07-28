"""One organisation must not be able to read another's call.

The tenant key proves which organisation is asking. It does not prove which
sessions that organisation may read, and session ids are not secret -- a demo
one is `web_maria_<unix seconds>`, which is a timestamp. So every endpoint that
names a session checks that the session belongs to the caller.

These run against the real ASGI app with a real two-tenant registry, because the
thing being tested is the wiring, and a unit test of the guard function would
pass just as happily with the guard never called.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from api import memory, session_store, tenancy
from api.tenancy import TENANT_HEADER

SECRET = "test-secret"
KEY_A = "tenant-key-a"
KEY_B = "tenant-key-b"


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@pytest.fixture
def client(tmp_path, monkeypatch):
    registry_path = tmp_path / "tenants.json"
    registry_path.write_text(
        json.dumps(
            {
                "tenants": [
                    {"id": "clinic-a", "name": "Clinic A", "api_key_hashes": [_digest(KEY_A)]},
                    {"id": "clinic-b", "name": "Clinic B", "api_key_hashes": [_digest(KEY_B)]},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ZUZU_SHARED_SECRET", SECRET)
    monkeypatch.setenv("STORE_BACKEND", "memory")
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    monkeypatch.setenv("TOKENROUTER_API_KEY", "")

    tenancy.reset_registry()
    monkeypatch.setattr(tenancy, "_registry", tenancy.TenantRegistry(registry_path))
    session_store.reset_session_store()
    memory.reset_memory()

    from api.main import app

    with TestClient(app) as c:
        yield c

    tenancy.reset_registry()
    session_store.reset_session_store()
    memory.reset_memory()


def _headers(tenant_key: str | None) -> dict[str, str]:
    headers = {"X-Zuzu-Secret": SECRET, "Content-Type": "application/json"}
    if tenant_key:
        headers[TENANT_HEADER] = tenant_key
    return headers


def _open_session(client, tenant_key: str, conversation_id: str) -> str:
    resp = client.post(
        "/session/init",
        json={"conversation_id": conversation_id, "caller_id": "+14155550142"},
        headers=_headers(tenant_key),
    )
    assert resp.status_code == 200, resp.text
    return conversation_id


def test_a_session_is_readable_by_its_own_tenant(client):
    sid = _open_session(client, KEY_A, "conv-a-1")
    resp = client.get(f"/sessions/{sid}/values", headers=_headers(KEY_A))
    assert resp.status_code == 200
    assert resp.json()["session_id"] == sid


@pytest.mark.parametrize(
    "path",
    ["/sessions/{sid}/values", "/sessions/{sid}/memory", "/sessions/{sid}/checklist"],
)
def test_another_tenant_cannot_read_the_session(client, path):
    """The disclosure this whole design exists to prevent."""
    sid = _open_session(client, KEY_A, "conv-a-2")
    resp = client.get(path.format(sid=sid), headers=_headers(KEY_B))
    assert resp.status_code == 404, resp.text
    # 404 and not 403: whether that id exists inside another organisation is
    # itself something this caller is not entitled to learn.
    assert "no such session" in resp.text.lower()


def test_a_request_without_a_tenant_key_is_refused(client):
    """Multi-tenant means the question has more than one answer."""
    sid = _open_session(client, KEY_A, "conv-a-3")
    resp = client.get(f"/sessions/{sid}/values", headers=_headers(None))
    assert resp.status_code == 401
    assert "tenant key" in resp.text.lower()


def test_an_unrecognised_tenant_key_is_refused(client):
    resp = client.post(
        "/session/init",
        json={"conversation_id": "conv-x", "caller_id": "+1415"},
        headers=_headers("not-a-real-key"),
    )
    assert resp.status_code == 401


def test_orchestration_cannot_be_started_on_another_tenants_session(client):
    """Handing another organisation's filing to your agents is the same leak."""
    sid = _open_session(client, KEY_A, "conv-a-4")
    resp = client.post(f"/sessions/{sid}/orchestrate", headers=_headers(KEY_B))
    assert resp.status_code == 404, resp.text


def test_ownership_follows_whoever_opened_the_session(client):
    """Not whoever asks for it afterwards."""
    sid = _open_session(client, KEY_B, "conv-b-1")
    assert client.get(f"/sessions/{sid}/values", headers=_headers(KEY_B)).status_code == 200
    assert client.get(f"/sessions/{sid}/values", headers=_headers(KEY_A)).status_code == 404


def test_two_tenants_store_the_same_caller_under_different_keys(client):
    """The same person at two clinics is two files, not one."""
    _open_session(client, KEY_A, "conv-a-5")
    _open_session(client, KEY_B, "conv-b-2")

    a = client.get("/sessions/conv-a-5/memory", headers=_headers(KEY_A)).json()
    b = client.get("/sessions/conv-b-2/memory", headers=_headers(KEY_B)).json()
    assert a["caller_key"] and b["caller_key"]
    assert a["caller_key"] != b["caller_key"], (
        "the same caller id at two tenants must not share a memory namespace"
    )


def test_orchestrate_resolves_its_principal_in_multi_tenant_mode(client):
    """A 503 means "no fleet"; a 500 means we broke before getting there.

    This endpoint built its principal by re-deriving the tenant from nothing,
    which is the default in a single-organisation install and an error once a
    registry exists -- so it worked everywhere except production. Resolving the
    principal before the capability check is what makes that reachable here.
    """
    sid = _open_session(client, KEY_A, "conv-a-orch")
    resp = client.post(f"/sessions/{sid}/orchestrate", headers=_headers(KEY_A))
    assert resp.status_code == 503, resp.text
    assert "fleet is not running" in resp.text


# ---------------------------------------------------------------------------
# The attacks an audit actually ran against this service. Every one of these
# succeeded once. The suite passed throughout, because it only ever pointed a
# second tenant at three read endpoints and never at the voice path.
# ---------------------------------------------------------------------------


def _fill(client, tenant_key: str, sid: str) -> None:
    """Take a session far enough that it holds real answers."""
    import json
    import pathlib

    answers = json.loads(
        (pathlib.Path(__file__).resolve().parent.parent / "data" / "demo_personas.json").read_text()
    )["personas"]["maria"]["answers"]
    for field_id, value in answers.items():
        client.post(
            "/tools/save_field",
            json={"session_id": sid, "field_id": field_id, "value": value},
            headers=_headers(tenant_key),
        )


def test_another_tenant_cannot_write_answers_into_your_filing(client):
    """Proven: clinic-b injected given_name=ATTACKER into clinic-a's session."""
    sid = _open_session(client, KEY_A, "conv-write")
    resp = client.post(
        "/tools/save_field",
        json={"session_id": sid, "field_id": "given_name", "value": "ATTACKER"},
        headers=_headers(KEY_B),
    )
    assert resp.status_code == 404, resp.text

    values = client.get(f"/sessions/{sid}/values", headers=_headers(KEY_A)).json()["values"]
    assert values.get("given_name") != "ATTACKER"


def test_another_tenant_cannot_generate_your_form_or_get_its_link(client):
    """Proven: /tools/generate_form took a tenant and never applied it, so
    clinic-b got back a valid signed pdf_url for clinic-a's applicant."""
    sid = _open_session(client, KEY_A, "conv-gen")
    _fill(client, KEY_A, sid)

    resp = client.post("/tools/generate_form", json={"session_id": sid}, headers=_headers(KEY_B))
    assert resp.status_code == 404, resp.text
    assert "pdf_url" not in resp.text


def test_the_shared_secret_alone_does_not_open_a_completed_form(client):
    """There is one shared secret for the whole deployment, so accepting it in
    place of the signed token let any tenant fetch any applicant's PDF."""
    sid = _open_session(client, KEY_A, "conv-pdf")
    _fill(client, KEY_A, sid)
    client.post("/tools/generate_form", json={"session_id": sid}, headers=_headers(KEY_A))

    # Secret but the wrong organisation.
    assert client.get(f"/forms/{sid}.pdf", headers=_headers(KEY_B)).status_code == 404
    # Secret and no organisation at all.
    assert client.get(f"/forms/{sid}.pdf", headers={"X-Zuzu-Secret": SECRET}).status_code == 401
    # The owner still gets it.
    assert client.get(f"/forms/{sid}.pdf", headers=_headers(KEY_A)).status_code == 200


def test_another_tenant_cannot_switch_the_form_out_from_under_your_call(client):
    """Proven: identify_form on someone else's session reset form_id and threw
    away the generated PDF."""
    sid = _open_session(client, KEY_A, "conv-switch")
    _fill(client, KEY_A, sid)
    client.post("/tools/generate_form", json={"session_id": sid}, headers=_headers(KEY_A))

    for path, body in (
        ("/session/set_form", {"session_id": sid, "form_id": "N-400"}),
        ("/tools/identify_form", {"session_id": sid, "text": "green card renewal"}),
    ):
        assert client.post(path, json=body, headers=_headers(KEY_B)).status_code == 404, path

    state = client.get(f"/sessions/{sid}/values", headers=_headers(KEY_A)).json()
    assert state["form_id"] == "I-765", "the form must not have been switched"
    assert state["has_pdf"] is True, "and the filing must not have been discarded"


def test_the_session_list_is_not_a_directory_of_every_organisation(client):
    """Unfiltered, this turned every "if you know the session id" weakness into
    something anybody holding the shared secret could simply look up."""
    a = _open_session(client, KEY_A, "conv-list-a")
    b = _open_session(client, KEY_B, "conv-list-b")

    listed = client.get("/sessions/recent", headers=_headers(KEY_B)).json()["sessions"]
    seen_by_b = {s["session_id"] for s in listed}
    assert b in seen_by_b
    assert a not in seen_by_b, "clinic-b must not be able to enumerate clinic-a's calls"


def test_the_interview_itself_is_not_readable_across_tenants(client):
    sid = _open_session(client, KEY_A, "conv-interview")
    resp = client.post(
        "/tools/get_missing_fields",
        json={"session_id": sid, "form_id": "I-765"},
        headers=_headers(KEY_B),
    )
    assert resp.status_code == 404, resp.text

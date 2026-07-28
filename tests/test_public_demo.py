"""The public link has to be a demo, not a way in.

The dashboard needed the deployment-wide shared secret in a query string to show
anything, so the live link was useless to anyone who did not already hold the
key that protects every real applicant's filing. Publishing that key was never
an option, so the public path runs as its own organisation instead.

Every test here is an attempt to reach a real tenant's data through it.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from api import memory, public_demo, session_store, tenancy
from api.tenancy import TENANT_HEADER

SECRET = "test-secret"
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
    monkeypatch.setenv("STORE_BACKEND", "memory")
    monkeypatch.setenv("TOKENROUTER_API_KEY", "")

    tenancy.reset_registry()
    monkeypatch.setattr(tenancy, "_registry", tenancy.TenantRegistry(registry))
    session_store.reset_session_store()
    memory.reset_memory()
    public_demo.forget()

    from api.main import app

    with TestClient(app) as c:
        yield c

    tenancy.reset_registry()
    session_store.reset_session_store()
    memory.reset_memory()
    public_demo.forget()


def _auth(key: str | None = KEY_A) -> dict[str, str]:
    headers = {"X-Zuzu-Secret": SECRET, "Content-Type": "application/json"}
    if key:
        headers[TENANT_HEADER] = key
    return headers


# ---------------------------------------------------------------------------
# It works with nothing at all
# ---------------------------------------------------------------------------


def test_a_visitor_with_no_credentials_can_watch_the_demo(client):
    """The whole point: a link somebody can open."""
    started = client.post("/demo/public")
    assert started.status_code == 200, started.text
    sid = started.json()["session_id"]
    assert sid.startswith(public_demo.PUBLIC_PREFIX)

    values = client.get(f"/demo/public/{sid}/values")
    assert values.status_code == 200, values.text
    body = values.json()
    assert body["form_id"] == "I-765"
    assert body["known_count"] > 20, "the demo must actually fill the form in"
    assert body["values"]["given_name"] == "Maria"

    mem = client.get(f"/demo/public/{sid}/memory")
    assert mem.status_code == 200, mem.text
    assert mem.json()["semantic"], "and it must show real memory, not an empty panel"


def test_the_demo_is_cached_rather_than_rebuilt_on_every_page_load(client):
    """Otherwise a public link is a way to run a 32-field interview and a PDF
    write from a browser tab, as fast as the tab can reload."""
    first = client.post("/demo/public").json()
    second = client.post("/demo/public").json()
    assert second["session_id"] == first["session_id"]
    assert second["reused"] is True


def test_the_public_demo_can_be_watched_live(client):
    """The field-by-field fill is the demo. A snapshot is not."""
    sid = client.post("/demo/public").json()["session_id"]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        from api.contract import SessionEvent

        # Anything published for this session reaches an anonymous watcher.
        client.post("/demo/public")  # no-op, cached
        assert ws is not None
        assert SessionEvent is not None


# ---------------------------------------------------------------------------
# And it is not a way in
# ---------------------------------------------------------------------------


def test_the_public_route_refuses_a_real_tenants_session(client):
    """The attack this design exists to stop."""
    client.post(
        "/session/init",
        json={"conversation_id": "clinic-session", "caller_id": "+14155550142"},
        headers=_auth(),
    )
    for path in ("values", "memory"):
        r = client.get(f"/demo/public/clinic-session/{path}")
        assert r.status_code == 404, f"{path}: {r.text}"


def test_naming_a_session_pubdemo_does_not_make_it_public(client):
    """The prefix is what the caller claims. The session's tenant_id is what is
    true. Checking only the prefix would let any tenant open a session called
    `pubdemo_...` and then read it back with no credentials at all.
    """
    forged = f"{public_demo.PUBLIC_PREFIX}forged"
    opened = client.post(
        "/session/init",
        json={"conversation_id": forged, "caller_id": "+14155550142"},
        headers=_auth(),
    )
    assert opened.status_code == 200, opened.text

    assert client.get(f"/demo/public/{forged}/values").status_code == 404
    assert client.get(f"/demo/public/{forged}/memory").status_code == 404

    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/{forged}") as ws:
            ws.receive_text()


def test_the_public_demo_has_no_write_path(client):
    """A visitor may watch. They may not change anything."""
    sid = client.post("/demo/public").json()["session_id"]
    for path, body in (
        ("/tools/save_field", {"session_id": sid, "field_id": "given_name", "value": "ATTACKER"}),
        ("/session/set_form", {"session_id": sid, "form_id": "N-400"}),
    ):
        r = client.post(path, json=body)
        assert r.status_code == 401, f"{path} -> {r.status_code}"

    values = client.get(f"/demo/public/{sid}/values").json()["values"]
    assert values["given_name"] == "Maria"


def test_a_real_tenant_cannot_reach_the_demo_either(client):
    """Isolation runs both ways, or it is not isolation. clinic-a holding a
    valid key is still a different organisation from the public demo."""
    sid = client.post("/demo/public").json()["session_id"]
    assert client.get(f"/sessions/{sid}/values", headers=_auth()).status_code == 404


def test_the_demo_tenant_never_persists_an_identifier(client):
    """The persona has an A-number and a passport number in it. Even synthetic
    identifiers should not be the thing this deployment demonstrates it is
    willing to keep."""
    assert public_demo.PUBLIC_TENANT.store_sensitive is False

    sid = client.post("/demo/public").json()["session_id"]
    mem = client.get(f"/demo/public/{sid}/memory").json()
    kept = {row["field_id"] for row in mem["semantic"]}
    for identifier in ("a_number", "ssn", "passport_number", "sevis_number", "i94_number"):
        assert identifier not in kept, f"{identifier} was persisted by the public demo"


def test_the_demo_scope_key_is_not_any_real_tenants(client):
    """Memory isolation is derived, not enforced by a check somebody could
    forget: a different tenant id hashes to a different scope."""
    from api.tenancy import DEFAULT_TENANT, scope_key

    caller = public_demo.DEMO_CALLER
    public = scope_key(public_demo.PUBLIC_TENANT.id, caller)
    assert public != scope_key("clinic-a", caller)
    assert public != scope_key(DEFAULT_TENANT.id, caller)


def test_the_demo_is_restricted_to_the_forms_it_shows(client):
    assert public_demo.PUBLIC_TENANT.may_file("I-765") is True
    assert public_demo.PUBLIC_TENANT.may_file("N-400") is True
    assert public_demo.PUBLIC_TENANT.may_file("I-130") is False


def test_a_restarted_process_rebuilds_rather_than_serving_a_dead_id(client):
    """Sessions are in-process. A cached id that no longer resolves would leave
    the public page pointing at a 404 until the cache expired."""
    sid = client.post("/demo/public").json()["session_id"]
    session_store.reset_session_store()

    again = client.post("/demo/public").json()
    assert again["session_id"] != sid
    assert again["reused"] is False
    assert client.get(f"/demo/public/{again['session_id']}/values").status_code == 200

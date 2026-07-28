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

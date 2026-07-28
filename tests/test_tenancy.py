"""Isolation between organisations, and between the people inside them.

The rule these hold down: two tenants share nothing, and the same person at two
tenants is two different people as far as storage is concerned.

This matters because the design it replaced keyed everything on a hash of the
phone number alone. That is one global namespace: two clinics running Zuzu would
have shared a memory pool, and an applicant who called both would have had their
file silently merged across organisations that are not allowed to see each
other's clients.
"""

from __future__ import annotations

import json

import pytest

from api import tenancy
from api.tenancy import (
    DEFAULT_TENANT,
    Principal,
    TenancyError,
    TenantRegistry,
    principal_for,
    scope_key,
)

CALLER = "+14155550142"


@pytest.fixture(autouse=True)
def _fresh():
    tenancy.reset_registry()
    yield
    tenancy.reset_registry()


def test_the_same_person_at_two_tenants_is_two_keys():
    """The whole point of the tenant half of the key."""
    a = scope_key("clinic-a", CALLER)
    b = scope_key("clinic-b", CALLER)
    assert a != b
    assert not a.startswith(b[:12]), "the keys must not merely differ in a suffix"


def test_two_people_at_one_tenant_are_two_keys():
    assert scope_key("clinic-a", "+14155550142") != scope_key("clinic-a", "+14155550143")


def test_the_key_cannot_be_confused_by_a_boundary():
    """Tenant "a" + user "bc" must not collide with tenant "ab" + user "c"."""
    assert scope_key("ab", "c") != scope_key("a", "bc")


def test_the_key_reveals_nothing():
    key = scope_key("clinic-a", CALLER)
    assert CALLER not in key
    assert "4155550142" not in key
    assert key.startswith("zt_")


def test_the_same_input_is_always_the_same_key():
    """Recall would be impossible otherwise."""
    assert scope_key("clinic-a", CALLER) == scope_key("clinic-a", CALLER)
    assert scope_key("clinic-a", f"  {CALLER} ") == scope_key("clinic-a", CALLER)


def test_an_anonymous_principal_is_not_identified():
    """Every caller-less session would otherwise derive one shared key."""
    for blank in ("", "   ", None):
        principal = Principal(tenant=DEFAULT_TENANT, user_id=blank or "")
        assert principal.is_identified is False
        assert "anonymous" in principal.describe()


def test_a_single_tenant_deployment_still_works():
    """No registry means one tenant, not no isolation."""
    principal = principal_for(CALLER)
    assert principal.tenant_id == DEFAULT_TENANT.id
    assert principal.is_identified
    assert principal.scope_key == scope_key(DEFAULT_TENANT.id, CALLER)


def _registry(tmp_path, entries):
    path = tmp_path / "tenants.json"
    path.write_text(json.dumps({"tenants": entries}), encoding="utf-8")
    return TenantRegistry(path)


def test_a_key_resolves_only_its_own_tenant(tmp_path, monkeypatch):
    import hashlib

    def h(raw):
        return hashlib.sha256(raw.encode()).hexdigest()

    registry = _registry(
        tmp_path,
        [
            {"id": "clinic-a", "name": "Clinic A", "api_key_hashes": [h("key-a")]},
            {"id": "clinic-b", "name": "Clinic B", "api_key_hashes": [h("key-b")]},
        ],
    )
    assert registry.resolve_key("key-a").id == "clinic-a"
    assert registry.resolve_key("key-b").id == "clinic-b"
    with pytest.raises(TenancyError):
        registry.resolve_key("key-c")


def test_multi_tenant_refuses_a_request_that_names_a_tenant_without_a_key(
    tmp_path, monkeypatch
):
    """Naming a tenant is not proof of belonging to it.

    Without this, anyone holding the service secret could read another
    organisation's applicants simply by passing their tenant id.
    """
    import hashlib

    registry = _registry(
        tmp_path,
        [{"id": "clinic-a", "name": "A", "api_key_hashes": [hashlib.sha256(b"key-a").hexdigest()]}],
    )
    monkeypatch.setattr(tenancy, "_registry", registry)

    with pytest.raises(TenancyError):
        tenancy.resolve_tenant(tenant_key=None, tenant_id="clinic-a")
    assert tenancy.resolve_tenant(tenant_key="key-a").id == "clinic-a"


def test_two_tenants_cannot_share_a_key(tmp_path):
    import hashlib

    same = hashlib.sha256(b"shared").hexdigest()
    with pytest.raises(TenancyError):
        _registry(
            tmp_path,
            [
                {"id": "clinic-a", "api_key_hashes": [same]},
                {"id": "clinic-b", "api_key_hashes": [same]},
            ],
        )


def test_a_tenant_id_must_be_a_usable_slug(tmp_path):
    with pytest.raises(TenancyError):
        _registry(tmp_path, [{"id": "Clinic A!", "api_key_hashes": []}])


def test_form_allowlisting_is_per_tenant():
    from api.tenancy import Tenant

    limited = Tenant(id="clinic-a", name="A", allowed_forms=("I-765",))
    assert limited.may_file("I-765")
    assert limited.may_file("i-765"), "case must not decide access"
    assert not limited.may_file("N-400")
    assert Tenant(id="clinic-b", name="B").may_file("N-400"), "empty means all"

"""Keep every test hermetic with respect to deployment configuration.

A tenant registry is deployment config. If one happens to exist -- on a
developer's machine, in a checkout, in an environment variable -- it flips the
whole service into multi-tenant mode, and every test that does not send a tenant
key starts failing. That is exactly what happened: adding one registry file
turned 23 green tests red at once, none of which were about tenancy.

So the default here is single-tenant, always, regardless of the machine. Tests
that are about multi-tenant behaviour opt in by installing their own registry.
"""

from __future__ import annotations

import pytest

from api import tenancy


@pytest.fixture(autouse=True)
def _single_tenant_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("ZUZU_TENANTS_JSON", raising=False)
    # Point the registry at somewhere that cannot exist, rather than at the
    # repo's data directory.
    monkeypatch.setattr(tenancy, "REGISTRY_PATH", tmp_path / "no-registry.json")
    tenancy.reset_registry()
    yield
    tenancy.reset_registry()

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

from api import memory, memory_store, tenancy


@pytest.fixture(autouse=True)
def _clean_deployment(tmp_path, monkeypatch):
    """No test inherits another test's deployment, tenants or memory."""
    monkeypatch.delenv("ZUZU_TENANTS_JSON", raising=False)
    # Point the registry at somewhere that cannot exist, rather than at the
    # repo's data directory.
    monkeypatch.setattr(tenancy, "REGISTRY_PATH", tmp_path / "no-registry.json")

    # And give every test its own memory database. Sharing one is not a
    # theoretical problem: the suite writes real facts for the demo caller, and
    # /session/init prefills from them -- so a test asserting that an empty
    # session is incomplete found it complete, because an earlier test had
    # already told Zuzu everything about that applicant.
    monkeypatch.setattr(memory_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    memory_store.reset_backend()
    memory.reset_memory()
    tenancy.reset_registry()
    yield
    memory_store.reset_backend()
    memory.reset_memory()
    tenancy.reset_registry()

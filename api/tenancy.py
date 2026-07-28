"""Who is asking, and on whose behalf.

Every stored thing in Zuzu belongs to somebody. An applicant's date of birth, a
call that happened, a rule learned about how to serve them, the audit trail of a
filing -- each one is owned by a person, inside an organisation, and neither of
those may leak into the other.

The model is three levels, and it is deliberately not two:

    TENANT      An organisation that runs Zuzu: a legal aid clinic, a firm, a
                university's international office. Owns its users and everything
                they collect. Two tenants never see each other's anything.

    USER        A person within that tenant. For a caseworker-operated clinic
                this is the caseworker; for a direct-to-applicant deployment it
                is the applicant. Either way it is the unit that memory is keyed
                to, because memory is about a person.

    SESSION     One call. Belongs to exactly one (tenant, user) pair and cannot
                be read through any other.

The reason this exists rather than a single caller id: the previous design keyed
memory on a hash of the phone number alone, which meant one global namespace.
Two clinics using Zuzu would have shared a memory pool, and an applicant who
called two different organisations would have had their file merged across
both -- which is exactly the disclosure this domain cannot afford.

Scope keys are derived, never stored raw. `scope_key` hashes tenant and user
together with a domain separator, so the same person at two tenants produces two
unrelated keys and no key can be reversed into a phone number.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

#: Where the tenant registry lives. A JSON file is honest about what this is:
#: small, rarely changing, and read far more than written. The interface below
#: is what a database would implement, so moving it is a swap not a rewrite.
REGISTRY_PATH = Path(__file__).resolve().parent.parent / "data" / "tenants.json"

#: Separates the two halves of a scope key so that a tenant called "a" with user
#: "bc" cannot collide with tenant "ab" and user "c".
_SEPARATOR = "\x1f"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")


class TenancyError(RuntimeError):
    """Raised when a request cannot be attributed to a tenant."""


@dataclass(frozen=True)
class Tenant:
    """An organisation running Zuzu."""

    id: str
    name: str
    #: Whether this tenant's applicants may have sensitive values persisted to
    #: the memory store. Off unless the organisation has turned it on, because
    #: holding an SSN for one call is a different posture from parking it in a
    #: third-party store indefinitely.
    store_sensitive: bool = False
    #: Forms this tenant is allowed to file. Empty means all registered forms.
    allowed_forms: tuple[str, ...] = ()

    def may_file(self, form_id: str) -> bool:
        return not self.allowed_forms or form_id.upper() in self.allowed_forms


@dataclass(frozen=True)
class Principal:
    """A tenant and the user acting inside it. The unit of authorisation.

    Nothing in Zuzu is read or written without one of these. Passing a bare
    caller id around is what allowed the old global namespace, so the type
    itself is the fix: a function that needs to touch stored data takes a
    Principal, and there is no way to construct one without a tenant.
    """

    tenant: Tenant
    user_id: str

    @property
    def tenant_id(self) -> str:
        return self.tenant.id

    @property
    def scope_key(self) -> str:
        """The pseudonymous key this principal's data is stored under.

        Derived from both halves, so the same person at two organisations has
        two unrelated keys, and no key can be turned back into a phone number.
        """
        return scope_key(self.tenant.id, self.user_id)

    @property
    def is_identified(self) -> bool:
        """Whether there is a person here at all.

        A call that arrives with no user id -- the widget path, where the
        conversation-init webhook never fires -- is anonymous. Anonymous
        principals get no memory in either direction, because every one of them
        would otherwise derive the same key and read each other's answers.
        """
        return bool(self.user_id and self.user_id.strip())

    def describe(self) -> str:
        """Safe for logs: names the tenant, never the user."""
        return f"{self.tenant.id}/{self.scope_key[:12] if self.is_identified else 'anonymous'}"


def scope_key(tenant_id: str, user_id: str) -> str:
    """Stable pseudonymous key for one user inside one tenant."""
    material = f"{tenant_id.strip()}{_SEPARATOR}{user_id.strip()}".encode()
    return f"zt_{hashlib.sha256(material).hexdigest()[:24]}"


def _hash_key(raw: str) -> str:
    """Tenant API keys are stored hashed, like any other credential."""
    return hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()


class TenantRegistry:
    """The tenants this deployment serves, and the keys that identify them."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or REGISTRY_PATH
        self._tenants: dict[str, Tenant] = {}
        self._by_key_hash: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        # A registry is deployment configuration that happens to contain key
        # hashes, so it is supplied as a secret rather than committed. Having it
        # in the repo also silently flips every checkout -- and the whole test
        # suite -- into multi-tenant mode, which is how this arrived: 23 tests
        # that send no tenant key started failing the moment the file existed.
        if inline := os.environ.get("ZUZU_TENANTS_JSON", "").strip():
            self._load(json.loads(inline))
            logger.info("tenant registry loaded from the environment")
            return
        if not self._path.exists():
            logger.info("no tenant registry at %s; running single-tenant", self._path)
            self._tenants, self._by_key_hash = {}, {}
            return
        self._load(json.loads(self._path.read_text(encoding="utf-8")))

    def _load(self, raw: dict[str, Any]) -> None:
        tenants: dict[str, Tenant] = {}
        by_key: dict[str, str] = {}
        for entry in raw.get("tenants", []):
            tenant = Tenant(
                id=entry["id"],
                name=entry.get("name", entry["id"]),
                store_sensitive=bool(entry.get("store_sensitive", False)),
                allowed_forms=tuple(f.upper() for f in entry.get("allowed_forms", [])),
            )
            if not _SLUG_RE.match(tenant.id):
                raise TenancyError(f"tenant id is not a usable slug: {tenant.id!r}")
            tenants[tenant.id] = tenant
            for key_hash in entry.get("api_key_hashes", []):
                if key_hash in by_key:
                    raise TenancyError("two tenants share an API key")
                by_key[key_hash] = tenant.id
        self._tenants, self._by_key_hash = tenants, by_key
        logger.info("tenant registry loaded: %d tenant(s)", len(tenants))

    def __len__(self) -> int:
        return len(self._tenants)

    def get(self, tenant_id: str) -> Tenant:
        try:
            return self._tenants[tenant_id]
        except KeyError as exc:
            raise TenancyError(f"unknown tenant: {tenant_id!r}") from exc

    def all(self) -> list[Tenant]:
        return list(self._tenants.values())

    def resolve_key(self, raw_key: str) -> Tenant:
        """The tenant this API key belongs to.

        Compared by constant-time digest match so a wrong key cannot be found
        by timing how long the rejection took.
        """
        if not raw_key:
            raise TenancyError("no tenant key supplied")
        candidate = _hash_key(raw_key)
        for key_hash, tenant_id in self._by_key_hash.items():
            if hmac.compare_digest(candidate, key_hash):
                return self._tenants[tenant_id]
        raise TenancyError("tenant key not recognised")


#: The tenant a deployment falls back to when no registry is configured.
#:
#: Zuzu has to keep working as a single-organisation install -- that is the
#: hackathon demo and it is also a perfectly real deployment. Isolation is not
#: weakened by this: there is simply one tenant, and every key is still derived
#: through it, so turning the registry on later renames nobody's data.
DEFAULT_TENANT = Tenant(
    id=os.environ.get("ZUZU_DEFAULT_TENANT", "zuzu-demo"),
    name=os.environ.get("ZUZU_DEFAULT_TENANT_NAME", "Zuzu demo organisation"),
    store_sensitive=os.environ.get("ZUZU_MEMORY_STORE_SENSITIVE", "").strip().lower()
    in ("1", "true", "yes", "on"),
)

_registry: TenantRegistry | None = None


def get_registry() -> TenantRegistry:
    global _registry
    if _registry is None:
        _registry = TenantRegistry()
    return _registry


def reset_registry() -> None:
    """Drop the cached registry. For tests and for a live reload."""
    global _registry
    _registry = None


def resolve_tenant(tenant_key: str | None, tenant_id: str | None = None) -> Tenant:
    """Work out which organisation a request belongs to.

    A tenant key names its own tenant and is the only thing that can. The
    explicit `tenant_id` is honoured only when no registry is configured, which
    is the single-tenant case -- otherwise anyone holding the service secret
    could read another organisation's applicants by naming them.
    """
    registry = get_registry()
    if len(registry):
        if tenant_key:
            return registry.resolve_key(tenant_key)
        raise TenancyError("this deployment is multi-tenant; a tenant key is required")
    if tenant_id and tenant_id != DEFAULT_TENANT.id:
        raise TenancyError(f"unknown tenant: {tenant_id!r}")
    return DEFAULT_TENANT


def principal_for(
    user_id: str,
    tenant_key: str | None = None,
    tenant_id: str | None = None,
) -> Principal:
    """The principal for one caller. The only supported way to build one."""
    return Principal(tenant=resolve_tenant(tenant_key, tenant_id), user_id=user_id or "")


#: The header a caller names its organisation with.
TENANT_HEADER = "X-Zuzu-Tenant-Key"


async def require_tenant(
    x_zuzu_tenant_key: str | None = Header(default=None, alias=TENANT_HEADER),
) -> Tenant:
    """The organisation this request belongs to.

    In a single-organisation install there is no registry and this resolves to
    the deployment's own tenant, so nothing has to change to run Zuzu for one
    clinic. As soon as a registry exists the key becomes mandatory, because at
    that point "which organisation is this" has more than one answer and
    guessing it wrong discloses somebody's immigration file.
    """
    try:
        return resolve_tenant(x_zuzu_tenant_key)
    except TenancyError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def guard_session(session_tenant_id: str, tenant: Tenant) -> None:
    """Refuse a session that belongs to a different organisation.

    Holding a valid key proves which tenant you are, not which sessions you may
    read. Without this check any authenticated tenant could pull another
    organisation's call by knowing its session id, and session ids are not
    secret -- a demo one is `web_maria_<unix seconds>`.

    A session with no tenant recorded predates this check or was opened lazily
    before its init webhook landed. Those are readable by the deployment's own
    tenant only, which is the same thing they were before.
    """
    if not session_tenant_id:
        if tenant.id == DEFAULT_TENANT.id:
            return
        raise HTTPException(status_code=404, detail="no such session")
    if session_tenant_id != tenant.id:
        # 404 rather than 403: whether a session id exists in another
        # organisation is itself information this caller is not entitled to.
        raise HTTPException(status_code=404, detail="no such session")


def as_dict(principal: Principal) -> dict[str, Any]:
    """What may be shown about a principal. Never the raw user id."""
    return {
        "tenant_id": principal.tenant_id,
        "tenant_name": principal.tenant.name,
        "scope_key": principal.scope_key if principal.is_identified else None,
        "identified": principal.is_identified,
    }

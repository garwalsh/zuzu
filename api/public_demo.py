"""A demo anybody can watch, without handing out a credential.

The dashboard is the product. Until now it needed the deployment-wide shared
secret in a query string to show anything at all, so the live link was useless
to anyone who did not already have that secret -- and giving it out would hand
over the key that protects every real applicant's filing.

So the public path gets its own tenant instead. `public-demo` exists only in
this module, never in the registry, and holds exactly one thing: a synthetic
applicant from data/demo_personas.json, run through the real interview loop.
Nothing a visitor can reach touches a real organisation's sessions, memory, or
forms, because a different tenant id derives a different scope key for every
read and write in the system.

Three properties make this safe rather than a hole:

    ITS OWN TENANT      Scope keys hash the tenant id, so public-demo memory is
                        unreachable from any real tenant's namespace and vice
                        versa. This is the same isolation two paying clinics get
                        from each other.

    ITS OWN PREFIX      Public session ids start with `pubdemo_`, and the public
                        endpoints refuse anything else. Naming a real session id
                        does not make it public -- ownership is still checked
                        against the tenant that owns the session.

    READ ONLY           A visitor can start the canned demo and read it back.
                        There is no public write path, and no public route
                        accepts a session id that it did not mint.

The session is cached and rebuilt on a timer. Rebuilding on every page load
would let anyone turn a link into a PDF-generation loop on a free instance,
which is a denial of service with extra steps.
"""

from __future__ import annotations

import asyncio
import logging
import time

from api.tenancy import Principal, Tenant

logger = logging.getLogger(__name__)

#: The organisation the public demo runs as. Deliberately constructed here and
#: not loaded from the registry: it cannot be granted anything by editing config,
#: and a real tenant cannot accidentally be given this id -- `_SLUG_RE` would
#: allow it, so `guard_public` checks the session's tenant, not just the prefix.
PUBLIC_TENANT = Tenant(
    id="public-demo",
    name="Public demo",
    #: Never. The persona has an A-number and a passport number in it, and even
    #: synthetic identifiers should not be the thing this deployment proves it
    #: is willing to persist.
    store_sensitive=False,
    #: The two forms the demo actually shows.
    allowed_forms=("I-765", "N-400"),
)

#: Public session ids start with this. Checked by every public route.
PUBLIC_PREFIX = "pubdemo_"

#: How long a demo session is reused before a fresh one is built. A page load
#: must not be able to trigger a full 32-field interview plus a PDF write.
REBUILD_AFTER = 900.0

#: The caller the demo runs as. Its memory accumulates across rebuilds, which is
#: the point: the second run of the demo shows a returning applicant, which is
#: the whole returning-caller story working in front of a visitor.
DEMO_CALLER = "+14155550100"

_lock = asyncio.Lock()
_current: tuple[str, float] | None = None


def public_principal() -> Principal:
    return Principal(tenant=PUBLIC_TENANT, user_id=DEMO_CALLER)


def is_public_session(session_id: str) -> bool:
    return session_id.startswith(PUBLIC_PREFIX)


def new_session_id() -> str:
    return f"{PUBLIC_PREFIX}{int(time.time())}"


def cached() -> str | None:
    """The current demo session id, if one is still fresh."""
    if _current is None:
        return None
    session_id, built = _current
    return session_id if (time.time() - built) < REBUILD_AFTER else None


def remember(session_id: str) -> None:
    global _current
    _current = (session_id, time.time())


def forget() -> None:
    """For tests, and for a deliberate rebuild."""
    global _current
    _current = None


async def guard_public(session_id: str) -> None:
    """Raise unless this is a real public-demo session.

    Two checks, not one. The prefix says what the caller is claiming; the
    session's own tenant_id says whether it is true. Without the second, anyone
    could create a session named `pubdemo_…` through the authenticated path at
    their own tenant and then read it back with no credentials at all.
    """
    from fastapi import HTTPException

    from api.session_store import SessionNotFoundError, get_session_store

    if not is_public_session(session_id):
        raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
    try:
        session = await get_session_store().get(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"unknown session: {session_id}") from exc
    if session.tenant_id != PUBLIC_TENANT.id:
        # Same 404 as a missing session. Whether that id exists inside a real
        # organisation is not something an anonymous caller may learn.
        raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")

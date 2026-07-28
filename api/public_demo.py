"""The public demo: a whole organisation anyone may use.

The live link has to *work* -- start a call, switch forms, fill them, run the
agents, read the memory, download the PDF. A read-only replay demonstrates that
we can render a screenshot, which is not the claim.

So the public path is not a restricted view of the real deployment. It is a
second organisation with its own credential, and inside it a visitor has every
capability a paying tenant has. Isolation comes from tenancy, which is already
the thing that keeps two clinics apart and is tested as such: scope keys hash
the tenant id, sessions record the tenant that opened them, and every read and
write is checked against it.

    ZUZU_DEMO_SECRET    Authenticates as the public demo and nothing else. It is
                        served by /config to any browser, on purpose -- it is
                        meant to be public. It is NOT the deployment's shared
                        secret, and presenting it can never resolve to a real
                        organisation, because require_tenant maps it to this
                        tenant before it looks at any tenant key.

What a visitor can reach: sessions they or another visitor opened inside
`public-demo`. That is a shared sandbox, which is what a public demo is. What
they cannot reach: anything belonging to a real tenant -- the same boundary a
clinic has, enforced the same way.

Sensitive values are never persisted here. `store_sensitive` is False, so an
A-number or SSN spoken into the demo fills the page and is gone with the
session, rather than being kept in a third-party store.
"""

from __future__ import annotations

import hmac
import logging
import os
import time

from api.tenancy import Tenant

logger = logging.getLogger(__name__)

#: The organisation the public demo runs as.
PUBLIC_TENANT = Tenant(
    id="public-demo",
    name="Public demo",
    #: Never. A visitor may speak an identifier into the demo; it belongs on the
    #: page and in the PDF, not in a store that outlives the session.
    store_sensitive=False,
    #: Empty means every registered form. The demo is meant to show that a form
    #: is data -- restricting it to two would be demonstrating the opposite.
    allowed_forms=(),
)


def demo_secret() -> str:
    """The public credential, or "" when the public demo is switched off."""
    return os.environ.get("ZUZU_DEMO_SECRET", "").strip()


def is_enabled() -> bool:
    return bool(demo_secret())


def is_demo_secret(supplied: str | None) -> bool:
    """Whether this request is authenticating as the public demo.

    Constant-time, like every other secret comparison here: a timing oracle on
    the demo credential is not dangerous, but having two different standards for
    comparing secrets in one file is how the dangerous one eventually gets the
    weaker treatment.
    """
    configured = demo_secret()
    if not configured or not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), configured.encode("utf-8"))


# ---------------------------------------------------------------------------
# Rate limiting
#
# The demo credential is public, so the demo's expensive operations -- running a
# whole scripted interview, writing a PDF, waking six agents -- are reachable by
# anyone with a browser. Unbounded, the live link is a way to exhaust a free
# instance from a tab that is holding down refresh.
# ---------------------------------------------------------------------------

#: Expensive public operations allowed per window, and the window in seconds.
BUDGET = 30
WINDOW = 60.0

_hits: list[float] = []


def take_budget() -> bool:
    """Consume one unit of the public demo's budget. False when spent."""
    now = time.time()
    cutoff = now - WINDOW
    while _hits and _hits[0] < cutoff:
        _hits.pop(0)
    if len(_hits) >= BUDGET:
        return False
    _hits.append(now)
    return True


def reset_budget() -> None:
    """For tests."""
    _hits.clear()

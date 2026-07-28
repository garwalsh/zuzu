# shared_secret_auth.py
from __future__ import annotations

import hashlib
import hmac
import os
import time

from fastapi import Header, HTTPException, status

_MISCONFIGURED_DETAIL = "server misconfigured: ZUZU_SHARED_SECRET is not set"
_INVALID_SECRET_DETAIL = "invalid or missing X-Zuzu-Secret"


def _get_configured_secret() -> str:
    secret = os.environ.get("ZUZU_SHARED_SECRET")
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MISCONFIGURED_DETAIL,
        )
    return secret


def verify_secret(supplied: str | None) -> bool:
    """Whether this request carries a credential this deployment accepts.

    Two are accepted, and they are not equivalent. The shared secret speaks for
    the whole deployment. The public demo secret speaks for exactly one
    organisation -- `require_tenant` maps it to the public tenant before it
    looks at any tenant key, so presenting it can never resolve to a real
    clinic. That mapping is the security property; this function only decides
    whether the caller gets past the door at all.
    """
    from api.public_demo import is_demo_secret

    configured = _get_configured_secret()
    if not supplied:
        return False
    if hmac.compare_digest(supplied.encode("utf-8"), configured.encode("utf-8")):
        return True
    return is_demo_secret(supplied)


async def require_shared_secret(
    x_zuzu_secret: str | None = Header(default=None, alias="X-Zuzu-Secret"),
) -> None:
    if not verify_secret(x_zuzu_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_SECRET_DETAIL,
        )


#: How long a completed-form link stays good. Long enough to survive an email
#: sitting unread over a weekend, short enough that a forwarded link does not
#: work forever.
DOWNLOAD_TTL_SECONDS = 7 * 24 * 60 * 60


def sign_download(session_id: str, expires_at: int) -> str:
    """A token proving the bearer was told about this specific form."""
    configured = _get_configured_secret()
    message = f"{session_id}:{expires_at}".encode()
    digest = hmac.new(configured.encode("utf-8"), message, hashlib.sha256).hexdigest()[:32]
    return f"{expires_at}.{digest}"


def verify_download(session_id: str, token: str | None) -> bool:
    """Whether this token really was issued for this session, and still stands.

    The completed form carries the applicant's name, date of birth and often
    their SSN, and the link goes in an email to someone who has no shared
    secret. Session ids are guessable -- `web_maria_<unix seconds>` for a demo
    run -- so the id alone cannot be what authorises the download.
    """
    if not token or "." not in token:
        return False
    stamp, _, digest = token.partition(".")
    if not stamp.isdigit() or len(digest) != 32:
        return False
    expires_at = int(stamp)
    if expires_at < int(time.time()):
        return False
    return hmac.compare_digest(sign_download(session_id, expires_at), token)


def download_token(session_id: str) -> str:
    """Mint a fresh token for a newly generated form."""
    return sign_download(session_id, int(time.time()) + DOWNLOAD_TTL_SECONDS)

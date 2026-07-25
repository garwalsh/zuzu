# shared_secret_auth.py
from __future__ import annotations

import hmac
import os

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
    configured = _get_configured_secret()
    if not supplied:
        return False

    return hmac.compare_digest(
        supplied.encode("utf-8"),
        configured.encode("utf-8"),
    )


async def require_shared_secret(
    x_zuzu_secret: str | None = Header(default=None, alias="X-Zuzu-Secret"),
) -> None:
    if not verify_secret(x_zuzu_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_SECRET_DETAIL,
        )

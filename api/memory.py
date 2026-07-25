"""Cross-session applicant memory, keyed by caller id.

Spec: prompts/memory_Python.prompt

The moat: a returning caller is greeted by name and never asked for their date
of birth twice. Exact values are carried in mem0's `metadata`, not parsed back
out of its LLM-extracted prose -- "User's family name is Reyes" is fine for a
human to read and useless for filling a legal form.

This module sits on the live call path at /session/init, so its failure
behaviour matters as much as its success behaviour: every call here degrades to
an empty profile rather than raising.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

import httpx
from pydantic import BaseModel, Field

from api.i765_schema import SKIP_SENTINEL, FormSchema

logger = logging.getLogger(__name__)

MEM0_BASE_URL = "https://api.mem0.ai/v1"
#: A slow memory service must never stall a greeting on a live call.
LOOKUP_TIMEOUT_SECONDS = 3.0
WRITE_TIMEOUT_SECONDS = 5.0


class ApplicantProfile(BaseModel):
    """What we already know about a caller, before they say anything."""

    caller_id: str
    display_name: str = ""
    preferred_language: str = "en"
    known_values: dict[str, str] = Field(default_factory=dict)
    is_returning: bool = False


def _store_sensitive() -> bool:
    """Whether sensitive values may persist in a third-party memory store.

    Off by default. Holding an SSN for the duration of one call is a materially
    different privacy posture from persisting it indefinitely somewhere else,
    and that should be a deliberate choice.
    """
    return os.environ.get("ZUZU_MEMORY_STORE_SENSITIVE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _user_key(caller_id: str) -> str:
    """Stable pseudonymous key so mem0 never holds a raw phone number."""
    digest = hashlib.sha256(caller_id.strip().encode("utf-8")).hexdigest()
    return f"zuzu_{digest[:20]}"


def _log_id(caller_id: str) -> str:
    return _user_key(caller_id)[:12]


class ApplicantMemory:
    """Thin async wrapper over the mem0 REST API."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("MEM0_API_KEY", "")

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._api_key}", "Content-Type": "application/json"}

    async def load_profile(
        self, caller_id: str, schema: FormSchema | None = None
    ) -> ApplicantProfile:
        """Everything we know about this caller. Never raises."""
        empty = ApplicantProfile(caller_id=caller_id)
        if not self.enabled:
            return empty

        try:
            async with httpx.AsyncClient(timeout=LOOKUP_TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    f"{MEM0_BASE_URL}/memories/",
                    params={"user_id": _user_key(caller_id)},
                    headers=self._headers(),
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            # A memory outage must not stop someone filing their form.
            logger.warning(
                "mem0 lookup failed for caller=%s: %s", _log_id(caller_id), type(exc).__name__
            )
            return empty

        entries = payload if isinstance(payload, list) else payload.get("results", [])
        known: dict[str, str] = {}
        language = "en"
        for entry in entries:
            metadata = entry.get("metadata") or {}
            field_id = metadata.get("field_id")
            value = metadata.get("value")
            if field_id and isinstance(value, str) and value:
                known[field_id] = value
            if metadata.get("preferred_language"):
                language = str(metadata["preferred_language"])

        if schema is not None:
            # Drop anything the current form no longer has a home for.
            known = {k: v for k, v in known.items() if schema.get_field(k) is not None}

        profile = ApplicantProfile(
            caller_id=caller_id,
            display_name=known.get("given_name", ""),
            preferred_language=language,
            known_values=known,
            is_returning=bool(known),
        )
        logger.info(
            "mem0 lookup caller=%s returning=%s known_fields=%d",
            _log_id(caller_id),
            profile.is_returning,
            len(known),
        )
        return profile

    async def save_field(
        self,
        caller_id: str,
        field_id: str,
        value: str,
        schema: FormSchema,
        language: str = "en",
    ) -> bool:
        """Persist one confirmed answer. Returns whether it was stored."""
        if not self.enabled or not value or value == SKIP_SENTINEL:
            return False

        form_field = schema.get_field(field_id)
        if form_field is None:
            return False
        if form_field.sensitive and not _store_sensitive():
            logger.info("mem0 skip sensitive field=%s caller=%s", field_id, _log_id(caller_id))
            return False

        # Keyed by the schema's memory_key so the stored shape survives the form
        # being renumbered in a future edition.
        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": f"My {form_field.memory_key} is {value}."}],
            "user_id": _user_key(caller_id),
            "metadata": {
                "field_id": field_id,
                "memory_key": form_field.memory_key,
                "value": value,
                "form_id": schema.form_id,
                "preferred_language": language,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=WRITE_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    f"{MEM0_BASE_URL}/memories/", json=body, headers=self._headers()
                )
                resp.raise_for_status()
        except Exception as exc:
            logger.warning(
                "mem0 write failed field=%s caller=%s: %s",
                field_id,
                _log_id(caller_id),
                type(exc).__name__,
            )
            return False
        # Never log the value itself.
        logger.info("mem0 stored field=%s caller=%s", field_id, _log_id(caller_id))
        return True

    async def forget(self, caller_id: str) -> int:
        """Delete everything stored for this caller.

        The playbook promises an applicant can say "delete my data"; that
        promise needs an implementation.
        """
        if not self.enabled:
            return 0
        try:
            async with httpx.AsyncClient(timeout=WRITE_TIMEOUT_SECONDS) as client:
                listing = await client.get(
                    f"{MEM0_BASE_URL}/memories/",
                    params={"user_id": _user_key(caller_id)},
                    headers=self._headers(),
                )
                listing.raise_for_status()
                payload = listing.json()
                entries = payload if isinstance(payload, list) else payload.get("results", [])
                removed = 0
                for entry in entries:
                    memory_id = entry.get("id")
                    if not memory_id:
                        continue
                    deleted = await client.delete(
                        f"{MEM0_BASE_URL}/memories/{memory_id}/", headers=self._headers()
                    )
                    if deleted.status_code < 300:
                        removed += 1
        except Exception as exc:
            logger.warning(
                "mem0 forget failed caller=%s: %s", _log_id(caller_id), type(exc).__name__
            )
            return 0
        logger.info("mem0 forgot caller=%s entries=%d", _log_id(caller_id), removed)
        return removed


def summarize(profile: ApplicantProfile, schema: FormSchema) -> str:
    """The short spoken line the agent greets a returning caller with."""
    if not profile.known_values:
        return ""

    labels: list[str] = []
    for field_id in profile.known_values:
        form_field = schema.get_field(field_id)
        if form_field is None or form_field.sensitive:
            continue
        labels.append(form_field.id.replace("_", " "))
        if len(labels) == 3:
            break

    if not labels:
        return ""
    if len(labels) == 1:
        spoken = labels[0]
    else:
        spoken = ", ".join(labels[:-1]) + f", and {labels[-1]}"
    return f"We already have your {spoken}."


_memory: ApplicantMemory | None = None


def get_memory() -> ApplicantMemory:
    global _memory
    if _memory is None:
        _memory = ApplicantMemory()
    return _memory


def reset_memory() -> None:
    """Drop the singleton. For tests only."""
    global _memory
    _memory = None

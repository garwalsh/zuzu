"""Cross-session applicant memory in three tiers.

Spec: prompts/memory_Python.prompt

A single flat store is the wrong shape for this problem. Remembering a passport
number, remembering that someone called last Tuesday, and remembering that they
need numbers read back slowly are three different kinds of knowledge, with three
different lifetimes and three different privacy postures.

    SEMANTIC    Stable facts about the applicant: name, date of birth, passport
                number. Long-lived. This is what prefills the next form.

    EPISODIC    What happened on a particular call: which form, how many fields,
                whether a PDF came out, when. Time-bound. This is what lets Zuzu
                say "last time we filed your renewal on the 25th" rather than
                greeting a returning caller like a stranger.

    PROCEDURAL  How to serve this person, and how this process works. "Speak
                Spanish." "Has no SSN, stop asking." "Category (c)(3)(B) needs
                an I-20 attached." Learned once, applied on every later call.

The tiers are separated in mem0 metadata so each can be recalled, summarised,
and forgotten independently -- an applicant can drop their call history without
losing the profile that saves them an hour on the next form.

Everything here degrades to empty rather than raising: this module sits on the
live call path at /session/init, and a memory outage must never end a call.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
from pydantic import BaseModel, Field

from api.i765_schema import SKIP_SENTINEL, FormSchema

logger = logging.getLogger(__name__)

MEM0_BASE_URL = "https://api.mem0.ai/v1"
#: A slow memory service must never stall a greeting on a live call.
LOOKUP_TIMEOUT_SECONDS = 3.0
WRITE_TIMEOUT_SECONDS = 5.0

#: Write-through mirror of everything this process has stored, keyed by the same
#: hashed user key mem0 is given.
#:
#: mem0's read and write quotas are metered separately, and the read quota is the
#: one that runs out first: writes keep returning 200 while every recall comes
#: back 429. The old behaviour swallowed that and returned an empty profile,
#: which is indistinguishable from a caller who has never called before -- so a
#: caller with a full memory silently looked like a stranger, on the live path
#: and on screen.
#:
#: The mirror is not a second source of truth and is not durable: it is what this
#: process wrote, held so the tiers can still be shown and a returning caller
#: still recognised while recall is unavailable. Reads always prefer mem0, and
#: anything served from here is labelled as such rather than passed off as mem0.
_MIRROR: dict[str, list[dict[str, Any]]] = {}
#: Per-caller cap. Far above a real applicant's footprint, low enough that a
#: long-running process cannot grow without bound.
MIRROR_LIMIT = 200


def _trim_mirror(entries: list[dict[str, Any]]) -> None:
    """Hold the mirror under its cap without discarding who the applicant is.

    A flat FIFO cap looks reasonable until you run a long form through it. The
    N-400 writes 269 non-sensitive fields in page order, so a 200-entry cap
    evicts the first 69 -- which, because the form opens with the applicant's
    name, means the mirror silently forgets `family_name` and `given_name` while
    faithfully keeping their mailing zip.

    Semantic facts are therefore trimmed last: they are the whole reason the
    mirror exists. Episodes are events and the oldest genuinely matter least.
    Procedural rules are few and each one is load-bearing.
    """
    surplus = len(entries) - MIRROR_LIMIT
    if surplus <= 0:
        return
    # Give up the least precious tier first, oldest within it.
    doomed: set[int] = set()
    for tier in (Tier.EPISODIC, Tier.PROCEDURAL, Tier.SEMANTIC):
        for index, entry in enumerate(entries):
            if len(doomed) >= surplus:
                break
            if (entry.get("metadata") or {}).get("tier") == tier:
                doomed.add(index)
        if len(doomed) >= surplus:
            break
    entries[:] = [e for i, e in enumerate(entries) if i not in doomed]


class DeletionUnverifiable(RuntimeError):
    """Raised when a deletion cannot be confirmed, rather than reported as done.

    A "your data is deleted" that was never carried out is worse than an error,
    because the applicant stops asking.
    """


class Tier(StrEnum):
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"


class Episode(BaseModel):
    """One past call."""

    session_id: str
    form_id: str
    at: str
    fields_collected: int = 0
    completed: bool = False
    language: str = "en"


class Procedure(BaseModel):
    """One learned rule about serving this applicant, or about the process."""

    key: str
    value: str
    #: `applicant` rules are personal; `process` rules are about the form itself
    #: and could in principle be shared across applicants.
    kind: str = "applicant"


class ApplicantProfile(BaseModel):
    """Everything the three tiers know, assembled for one call."""

    caller_id: str
    display_name: str = ""
    preferred_language: str = "en"
    known_values: dict[str, str] = Field(default_factory=dict)
    episodes: list[Episode] = Field(default_factory=list)
    procedures: list[Procedure] = Field(default_factory=list)
    is_returning: bool = False
    #: Where this profile was actually read from: `mem0`, the in-process
    #: `mirror` when recall was unavailable, or `none`. Displayed, not decorative
    #: -- an operator has to be able to tell a real empty profile from a
    #: recall outage.
    source: str = "mem0"
    #: Why recall fell back, when it did. Empty on the healthy path.
    degraded_reason: str = ""

    @property
    def last_episode(self) -> Episode | None:
        return self.episodes[0] if self.episodes else None


def _store_sensitive() -> bool:
    """Whether sensitive values may persist in a third-party memory store.

    Off by default. Holding an SSN for one call is a materially different
    privacy posture from parking it somewhere else indefinitely.
    """
    return os.environ.get("ZUZU_MEMORY_STORE_SENSITIVE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _user_key(caller_id: str, tenant_id: str | None = None) -> str:
    """Stable pseudonymous key so mem0 never holds a raw phone number.

    The tenant is part of the key, not decoration on it. Without it there is a
    single global namespace: two organisations running Zuzu would share one
    memory pool, and an applicant who called both would have their file merged
    across two parties who are not permitted to see each other's clients.

    The tenant defaults to this deployment's own, so a single-organisation
    install keeps working and its keys are already derived the multi-tenant way
    -- turning the registry on later renames nobody's data.
    """
    from api.tenancy import DEFAULT_TENANT

    tenant = (tenant_id or DEFAULT_TENANT.id).strip()
    # \x1f between the halves so tenant "ab" + user "c" cannot collide with
    # tenant "a" + user "bc".
    material = f"{tenant}\x1f{caller_id.strip()}".encode()
    return f"zuzu_{hashlib.sha256(material).hexdigest()[:20]}"


def identifies_a_caller(caller_id: str) -> bool:
    """Whether this id names a person, as opposed to naming nobody.

    A session opened lazily -- the widget path, where the conversation-init
    webhook never fires -- carries no caller id at all. Hashing "" is perfectly
    stable, which is the danger: every anonymous caller hashes to the same key,
    so without this check they would all read and write one shared profile and
    each would be greeted with the last stranger's name, date of birth and
    passport number.

    Anonymous calls therefore get no memory in either direction. Losing the
    returning-caller greeting for a session we cannot identify is the correct
    trade; the alternative is disclosing one applicant's identity to the next.
    """
    return bool(caller_id and caller_id.strip())


def _log_id(caller_id: str) -> str:
    return _user_key(caller_id)[:12]


class ApplicantMemory:
    """Async mem0 wrapper, tier-aware."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("MEM0_API_KEY", "")

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._api_key}", "Content-Type": "application/json"}

    async def _write(self, caller_id: str, text: str, metadata: dict[str, Any]) -> bool:
        """One mem0 write. Never raises into the caller."""
        if not identifies_a_caller(caller_id):
            # Every anonymous session hashes to the same key. Writing here would
            # file this applicant's facts into a bucket the next one reads.
            logger.info("mem0 skip write tier=%s: session has no caller", metadata.get("tier"))
            return False
        body = {
            "messages": [{"role": "user", "content": text}],
            "user_id": _user_key(caller_id),
            "metadata": metadata,
        }
        try:
            async with httpx.AsyncClient(timeout=WRITE_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    f"{MEM0_BASE_URL}/memories/", json=body, headers=self._headers()
                )
                resp.raise_for_status()
        except Exception as exc:
            logger.warning(
                "mem0 write failed tier=%s caller=%s: %s",
                metadata.get("tier"),
                _log_id(caller_id),
                type(exc).__name__,
            )
            return False
        self._mirror_put(caller_id, text, metadata)
        return True

    @staticmethod
    def _mirror_put(caller_id: str, text: str, metadata: dict[str, Any]) -> None:
        """Record a successful write so it can still be shown if recall fails."""
        entries = _MIRROR.setdefault(_user_key(caller_id), [])
        tier = metadata.get("tier")
        # A semantic fact and a procedural rule are corrections of the previous
        # value for the same key, not additions to it. Episodes are events and
        # accumulate.
        identity = (
            ("field_id", metadata.get("field_id"))
            if tier == Tier.SEMANTIC
            else ("key", metadata.get("key"))
            if tier == Tier.PROCEDURAL
            else None
        )
        if identity is not None and identity[1]:
            key, value = identity
            entries[:] = [
                e
                for e in entries
                if not (
                    (e.get("metadata") or {}).get("tier") == tier
                    and (e.get("metadata") or {}).get(key) == value
                )
            ]
        entries.append({"memory": text, "metadata": dict(metadata)})
        _trim_mirror(entries)

    async def _read_all(self, caller_id: str) -> tuple[list[dict[str, Any]], str, str]:
        """Every memory for this caller, across tiers, with its provenance.

        Returns (entries, source, reason). Never raises: a memory outage must
        not stop someone filing their form. When recall fails, the in-process
        mirror is served instead of an empty list, because "we cannot reach the
        memory" and "this caller is new" are different answers and only one of
        them should make Zuzu ask thirty-three questions again.
        """
        if not identifies_a_caller(caller_id):
            # Not "this caller has nothing" -- "there is no caller here". Reading
            # the shared empty-string bucket would hand the previous anonymous
            # applicant's profile to this one.
            return ([], "anonymous", "this session has no caller id, so nothing is recalled")
        mirrored = list(_MIRROR.get(_user_key(caller_id), []))
        if not self.enabled:
            return (mirrored, "mirror" if mirrored else "none", "mem0 is not configured")
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
            reason = type(exc).__name__
            if isinstance(exc, httpx.HTTPStatusError):
                reason = f"mem0 recall HTTP {exc.response.status_code}"
                if exc.response.status_code == 429:
                    # mem0 meters reads and writes separately, so this says
                    # nothing about whether the writes landed.
                    reason = "mem0 read quota exhausted for this billing period"
            logger.warning("mem0 lookup failed caller=%s: %s", _log_id(caller_id), reason)
            return (mirrored, "mirror" if mirrored else "none", reason)
        entries = payload if isinstance(payload, list) else payload.get("results", [])
        return (entries, "mem0", "")

    # ---- reads -----------------------------------------------------------

    async def load_profile(
        self, caller_id: str, schema: FormSchema | None = None
    ) -> ApplicantProfile:
        """Assemble all three tiers into one profile. Never raises."""
        entries, source, degraded_reason = await self._read_all(caller_id)
        known: dict[str, str] = {}
        episodes: list[Episode] = []
        procedures: list[Procedure] = []
        language = "en"

        for entry in entries:
            meta = entry.get("metadata") or {}
            tier = meta.get("tier", Tier.SEMANTIC)

            if tier == Tier.SEMANTIC:
                field_id, value = meta.get("field_id"), meta.get("value")
                if field_id and isinstance(value, str) and value:
                    known[field_id] = value
                if meta.get("preferred_language"):
                    language = str(meta["preferred_language"])

            elif tier == Tier.EPISODIC:
                try:
                    episodes.append(
                        Episode(
                            session_id=str(meta.get("session_id", "")),
                            form_id=str(meta.get("form_id", "")),
                            at=str(meta.get("at", "")),
                            fields_collected=int(meta.get("fields_collected", 0) or 0),
                            completed=bool(meta.get("completed", False)),
                            language=str(meta.get("language", "en")),
                        )
                    )
                except Exception:
                    continue

            elif tier == Tier.PROCEDURAL:
                key, value = meta.get("key"), meta.get("value")
                if key and value:
                    procedures.append(
                        Procedure(
                            key=str(key),
                            value=str(value),
                            kind=str(meta.get("kind", "applicant")),
                        )
                    )

        if schema is not None:
            # Drop anything the current form no longer has a home for.
            known = {k: v for k, v in known.items() if schema.get_field(k) is not None}

        # A procedural language rule outranks whatever a stale semantic row says.
        for proc in procedures:
            if proc.key == "language" and proc.value.startswith("speak "):
                language = proc.value.split()[1]

        episodes.sort(key=lambda e: e.at, reverse=True)
        profile = ApplicantProfile(
            caller_id=caller_id,
            display_name=known.get("given_name", ""),
            preferred_language=language,
            known_values=known,
            episodes=episodes[:10],
            procedures=procedures,
            is_returning=bool(known or episodes),
            source=source,
            degraded_reason=degraded_reason,
        )
        logger.info(
            "mem0 recall caller=%s source=%s returning=%s semantic=%d episodic=%d procedural=%d%s",
            _log_id(caller_id),
            source,
            profile.is_returning,
            len(known),
            len(episodes),
            len(procedures),
            f" degraded={degraded_reason}" if degraded_reason else "",
        )
        return profile

    # ---- writes ----------------------------------------------------------

    async def save_field(
        self,
        caller_id: str,
        field_id: str,
        value: str,
        schema: FormSchema,
        language: str = "en",
    ) -> bool:
        """SEMANTIC: one confirmed fact about the applicant."""
        if not self.enabled or not value or value == SKIP_SENTINEL:
            return False
        form_field = schema.get_field(field_id)
        if form_field is None:
            return False
        if form_field.sensitive and not _store_sensitive():
            logger.info("mem0 skip sensitive field=%s caller=%s", field_id, _log_id(caller_id))
            return False

        ok = await self._write(
            caller_id,
            f"My {form_field.memory_key} is {value}.",
            {
                "tier": Tier.SEMANTIC,
                "field_id": field_id,
                # Keyed by memory_key so the stored shape survives the form
                # being renumbered in a future edition.
                "memory_key": form_field.memory_key,
                "value": value,
                "form_id": schema.form_id,
                "preferred_language": language,
            },
        )
        if ok:
            logger.info("mem0 stored field=%s caller=%s", field_id, _log_id(caller_id))
        return ok

    async def record_episode(
        self,
        caller_id: str,
        session_id: str,
        form_id: str,
        fields_collected: int,
        completed: bool,
        language: str = "en",
    ) -> bool:
        """EPISODIC: what happened on this call."""
        if not self.enabled:
            return False
        when = datetime.now(UTC).isoformat()
        outcome = "completed it and generated the PDF" if completed else "did not finish"
        return await self._write(
            caller_id,
            f"On {when[:10]} I worked on form {form_id}, answered "
            f"{fields_collected} questions, and {outcome}.",
            {
                "tier": Tier.EPISODIC,
                "session_id": session_id,
                "form_id": form_id,
                "at": when,
                "fields_collected": fields_collected,
                "completed": completed,
                "language": language,
            },
        )

    async def learn(self, caller_id: str, key: str, value: str, kind: str = "applicant") -> bool:
        """PROCEDURAL: a rule worth applying on every future call."""
        if not self.enabled or not key or not value:
            return False
        return await self._write(
            caller_id,
            f"When helping me, remember: {value}",
            {"tier": Tier.PROCEDURAL, "key": key, "value": value, "kind": kind},
        )

    async def learn_from_session(
        self, caller_id: str, values: dict[str, str], schema: FormSchema, language: str
    ) -> int:
        """Derive procedural rules from what actually happened on this call.

        Deliberately conservative. A wrong rule is worse than no rule: it makes
        Zuzu confidently skip a question the applicant could have answered.
        """
        learned = 0
        if language and not language.startswith("en"):
            learned += await self.learn(
                caller_id, "language", f"speak {language} with this applicant", "applicant"
            )

        # A skipped identifier usually means they do not have one at all, so
        # leading with it next time wastes the opening of the call.
        for field_id in ("ssn", "a_number", "uscis_online_account_number", "sevis_number"):
            if values.get(field_id) == SKIP_SENTINEL:
                learned += await self.learn(
                    caller_id,
                    f"no_{field_id}",
                    f"they do not have a {field_id.replace('_', ' ')}; ask late or not at all",
                    "applicant",
                )

        category = values.get("eligibility_category")
        if category and category != SKIP_SENTINEL:
            learned += await self.learn(
                caller_id,
                "eligibility_category",
                f"their eligibility category is {category}; confirm it rather than re-derive it",
                "process",
            )
        return learned

    async def forget(self, caller_id: str, tier: Tier | None = None) -> int:
        """Delete memories for this caller, optionally just one tier.

        The pitch promises an applicant can say "delete my data". Tier-scoped
        deletion means they can drop their call history without losing the
        profile that saves them an hour next time.

        Raises DeletionUnverifiable when the store cannot be enumerated, because
        the caller has to be told the difference between "deleted" and "we could
        not tell what there was to delete".
        """
        if not self.enabled or not identifies_a_caller(caller_id):
            return 0
        entries, source, reason = await self._read_all(caller_id)
        if source != "mem0":
            # Deletion works by listing what exists and deleting each id. With
            # recall unavailable there is no list, so nothing would be deleted
            # remotely -- and clearing the local mirror here would destroy the
            # only remaining evidence of what is still held, while reporting
            # success. Refuse instead, and leave the mirror intact so a retry
            # once recall returns can still find and delete the real records.
            raise DeletionUnverifiable(
                reason or "memory cannot be read, so deletion cannot be confirmed"
            )
        removed = 0
        try:
            async with httpx.AsyncClient(timeout=WRITE_TIMEOUT_SECONDS) as client:
                for entry in entries:
                    meta = entry.get("metadata") or {}
                    if tier is not None and meta.get("tier") != tier:
                        continue
                    memory_id = entry.get("id")
                    if not memory_id:
                        continue
                    resp = await client.delete(
                        f"{MEM0_BASE_URL}/memories/{memory_id}/", headers=self._headers()
                    )
                    if resp.status_code < 300:
                        removed += 1
        except Exception as exc:
            logger.warning(
                "mem0 forget failed caller=%s: %s", _log_id(caller_id), type(exc).__name__
            )
            return removed
        # Only once the remote deletion actually ran does the local copy go.
        mirror = _MIRROR.get(_user_key(caller_id))
        if mirror is not None:
            if tier is None:
                _MIRROR.pop(_user_key(caller_id), None)
            else:
                mirror[:] = [e for e in mirror if (e.get("metadata") or {}).get("tier") != tier]
        logger.info(
            "mem0 forgot caller=%s tier=%s entries=%d", _log_id(caller_id), tier or "all", removed
        )
        return removed


def summarize(profile: ApplicantProfile, schema: FormSchema) -> str:
    """The spoken line the agent greets a returning caller with.

    Draws on all three tiers, because "we have your date of birth" is a weaker
    greeting than "last time we filed your renewal, and I remember you prefer
    Spanish".
    """
    parts: list[str] = []

    episode = profile.last_episode
    if episode and episode.at:
        outcome = "we finished it" if episode.completed else "we did not finish"
        parts.append(
            f"Last time, on {episode.at[:10]}, you worked on {episode.form_id} and {outcome}."
        )

    labels = [
        schema.get_field(fid).id.replace("_", " ")
        for fid in profile.known_values
        if schema.get_field(fid) is not None and not schema.get_field(fid).sensitive
    ][:3]
    if labels:
        spoken = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + f", and {labels[-1]}"
        parts.append(f"We already have your {spoken}.")

    if profile.procedures:
        parts.append("I also remember how you like to work.")

    return " ".join(parts)


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

"""Cross-session applicant memory in three tiers.

A single flat store is the wrong shape for this problem. Remembering a passport
number, remembering that someone called last Tuesday, and remembering that they
need numbers read back slowly are three different kinds of knowledge, with three
different lifetimes and three different privacy postures.

    SEMANTIC    Stable facts about the applicant: name, date of birth, country
                of citizenship. Long-lived. This is what prefills the next form.

    EPISODIC    What happened on a particular call: which form, how many fields,
                whether a PDF came out, when. Time-bound. This is what lets Zuzu
                say "last time we filed your renewal" rather than greeting a
                returning caller like a stranger.

    PROCEDURAL  How to serve this person. "Speak Spanish." "Has no SSN, stop
                asking." Learned once, applied on every later call.

This module is the profile layer: it assembles those tiers into something a
greeting can be built from, and applies the handful of rules worth learning
automatically. Where the records physically live is api/memory_store.py.

WHY IT NO LONGER TALKS TO A VENDOR DIRECTLY

It used to hold an HTTP client for hosted mem0 plus an in-process mirror to
cover for that service's read quota. Two problems: the mirror was a second copy
of the truth, and -- worse -- the Band agents had meanwhile been given their own
memory tools writing to the store, so an applicant had two memories that never
saw each other. The endpoints wrote to one and the agents to the other.

There is now one store, and both reach it through here.

Everything degrades to empty rather than raising: this sits on the live call
path at /session/init, and a memory outage must never end a call.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from api.i765_schema import SKIP_SENTINEL, FormSchema
from api.memory_store import Record, get_backend

logger = logging.getLogger(__name__)


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
    #: Which store answered. Displayed rather than decorative: an operator has to
    #: be able to tell an empty profile from a store that could not be reached.
    source: str = "sqlite"
    #: Why recall degraded, when it did. Empty on the healthy path.
    degraded_reason: str = ""

    @property
    def last_episode(self) -> Episode | None:
        return self.episodes[0] if self.episodes else None


def _user_key(caller_id: str, tenant_id: str | None = None) -> str:
    """Stable pseudonymous key so no store holds a raw phone number.

    The tenant is part of the key, not decoration on it. Without it there is a
    single global namespace: two organisations running Zuzu would share one
    memory pool, and an applicant who called both would have their file merged
    across two parties who are not permitted to see each other's clients.
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
    webhook never fires -- carries no caller id. Hashing "" is perfectly stable,
    which is the danger: every anonymous caller hashes to the same key, so
    without this check they would all read and write one shared profile and each
    would be greeted with the last stranger's name and date of birth.
    """
    return bool(caller_id and caller_id.strip())


def _log_id(caller_id: str, tenant_id: str | None = None) -> str:
    return _user_key(caller_id, tenant_id)[:12]


class ApplicantMemory:
    """The three tiers for one organisation, over whichever store is configured."""

    def __init__(self, tenant_id: str | None = None) -> None:
        #: Bound once, here, rather than passed to every method. Threading a
        #: tenant through a dozen call sites is how one of them ends up without
        #: it, and the one that forgets is a cross-organisation disclosure.
        self._tenant_id = tenant_id

    @property
    def enabled(self) -> bool:
        """There is always a store. SQLite needs nothing to be available."""
        return True

    def _key(self, caller_id: str) -> str:
        return _user_key(caller_id, self._tenant_id)

    def _store_sensitive(self) -> bool:
        from api.tenancy import DEFAULT_TENANT, get_registry

        if not self._tenant_id:
            return DEFAULT_TENANT.store_sensitive
        try:
            return get_registry().get(self._tenant_id).store_sensitive
        except Exception:
            return DEFAULT_TENANT.store_sensitive

    # ---- reads -----------------------------------------------------------

    async def load_profile(
        self, caller_id: str, schema: FormSchema | None = None
    ) -> ApplicantProfile:
        """Assemble all three tiers into one profile. Never raises."""
        if not identifies_a_caller(caller_id):
            # Not "this caller has nothing" -- "there is no caller here".
            return ApplicantProfile(
                caller_id=caller_id,
                source="anonymous",
                degraded_reason="this session has no caller id, so nothing is recalled",
            )

        backend = get_backend()
        records = await backend.all(self._key(caller_id))

        known: dict[str, str] = {}
        episodes: list[Episode] = []
        procedures: list[Procedure] = []
        language = "en"

        for record in records:
            if record.tier == Tier.SEMANTIC:
                known[record.key] = record.value
            elif record.tier == Tier.EPISODIC:
                meta = record.meta or {}
                episodes.append(
                    Episode(
                        session_id=record.key,
                        form_id=str(meta.get("form_id", "")),
                        at=datetime.fromtimestamp(record.at, UTC).isoformat(),
                        fields_collected=int(meta.get("answers", 0) or 0),
                        completed=bool(meta.get("completed", False)),
                        language=str(meta.get("language", "en")),
                    )
                )
            elif record.tier == Tier.PROCEDURAL:
                procedures.append(
                    Procedure(
                        key=record.key,
                        value=record.value,
                        kind=str((record.meta or {}).get("kind", "applicant")),
                    )
                )

        if schema is not None:
            # Drop anything the current form no longer has a home for.
            known = {k: v for k, v in known.items() if schema.get_field(k) is not None}

        # A procedural language rule outranks anything a stale record says.
        for procedure in procedures:
            if procedure.key.startswith("language") and " " in procedure.value:
                parts = procedure.value.split()
                if len(parts) >= 2 and parts[0] == "speak":
                    language = parts[1]

        episodes.sort(key=lambda e: e.at, reverse=True)
        profile = ApplicantProfile(
            caller_id=caller_id,
            display_name=known.get("given_name", ""),
            preferred_language=language,
            known_values=known,
            episodes=episodes[:10],
            procedures=procedures,
            is_returning=bool(known or episodes),
            source=backend.name,
        )
        logger.info(
            "recall caller=%s store=%s returning=%s semantic=%d episodic=%d procedural=%d",
            _log_id(caller_id, self._tenant_id),
            backend.name,
            profile.is_returning,
            len(known),
            len(episodes),
            len(procedures),
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
        if not identifies_a_caller(caller_id) or not value or value == SKIP_SENTINEL:
            return False
        form_field = schema.get_field(field_id)
        if form_field is None:
            return False
        if form_field.sensitive and not self._store_sensitive():
            logger.info(
                "skip sensitive field=%s caller=%s",
                field_id,
                _log_id(caller_id, self._tenant_id),
            )
            return False
        return await get_backend().put(
            self._key(caller_id),
            Record(
                tier=Tier.SEMANTIC,
                key=field_id,
                value=value,
                meta={
                    # Keyed by memory_key so the stored shape survives the form
                    # being renumbered in a future edition.
                    "memory_key": form_field.memory_key,
                    "form_id": schema.form_id,
                    "preferred_language": language,
                },
            ),
        )

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
        if not identifies_a_caller(caller_id):
            return False
        return await get_backend().put(
            self._key(caller_id),
            Record(
                tier=Tier.EPISODIC,
                key=session_id,
                value="completed it and generated the PDF" if completed else "did not finish",
                meta={
                    "form_id": form_id,
                    "answers": fields_collected,
                    "completed": completed,
                    "language": language,
                },
            ),
        )

    async def learn(self, caller_id: str, key: str, value: str, kind: str = "applicant") -> bool:
        """PROCEDURAL: a rule worth applying on every future call."""
        if not identifies_a_caller(caller_id) or not key or not value:
            return False
        return await get_backend().put(
            self._key(caller_id),
            Record(tier=Tier.PROCEDURAL, key=key, value=value, meta={"kind": kind}),
        )

    async def learn_from_session(
        self,
        caller_id: str,
        values: dict[str, str],
        schema: FormSchema,
        language: str = "en",
    ) -> int:
        """Derive the rules worth keeping from how this call went.

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
        """Delete what is remembered, optionally one tier only.

        Tier scoping means an applicant can drop their call history without
        losing the profile that saves them an hour next time.
        """
        if not identifies_a_caller(caller_id):
            return 0
        removed = await get_backend().drop(self._key(caller_id), tier.value if tier else None)
        logger.info(
            "forgot caller=%s tier=%s entries=%d",
            _log_id(caller_id, self._tenant_id),
            tier or "all",
            removed,
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
#: One store per organisation; see get_memory.
_by_tenant: dict[str, ApplicantMemory] = {}


def get_memory(tenant_id: str | None = None) -> ApplicantMemory:
    """The memory for one organisation.

    One instance per tenant, cached, because the tenant is baked into every key
    it derives. The deployment's own tenant resolves to the same object as the
    default, so a single-organisation install keeps one view of a caller.
    """
    from api.tenancy import DEFAULT_TENANT

    global _memory
    if tenant_id and tenant_id != DEFAULT_TENANT.id:
        store = _by_tenant.get(tenant_id)
        if store is None:
            store = _by_tenant[tenant_id] = ApplicantMemory(tenant_id=tenant_id)
        return store
    if _memory is None:
        _memory = ApplicantMemory()
    return _memory


def reset_memory() -> None:
    """Drop the cached instances. For tests only."""
    global _memory
    _memory = None
    _by_tenant.clear()

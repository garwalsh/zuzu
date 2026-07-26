"""The three memory tiers, and what happens when recall is unavailable.

These exist because of a real failure, not a hypothetical one. mem0 meters reads
and writes separately; the read quota ran out first, so every write kept
returning 200 while every recall came back 429. The old read path swallowed that
and returned an empty profile -- which is exactly what a brand-new caller looks
like. A caller with a full history was silently treated as a stranger, on the
live call path and on screen, and nothing anywhere said so.

The rule these lock down: an outage must never be reported as an absence.
"""

from __future__ import annotations

import httpx
import pytest

from api import memory
from api.form_registry import get_form
from api.memory import ApplicantMemory, Tier

CALLER = "+14155550142"


@pytest.fixture(autouse=True)
def _clear_mirror():
    memory._MIRROR.clear()
    yield
    memory._MIRROR.clear()


@pytest.fixture
def schema():
    return get_form("I-765")


def _quota_error() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"{memory.MEM0_BASE_URL}/memories/")
    response = httpx.Response(429, request=request, json={"error": "quota exceeded"})
    return httpx.HTTPStatusError("429", request=request, response=response)


@pytest.mark.asyncio
async def test_successful_write_is_mirrored(monkeypatch, schema):
    """Anything accepted by mem0 is also held locally, so it can still be shown."""
    store = ApplicantMemory(api_key="k")
    monkeypatch.setattr(ApplicantMemory, "_write", ApplicantMemory._write)

    async def ok(self, caller_id, text, metadata):
        self._mirror_put(caller_id, text, metadata)
        return True

    monkeypatch.setattr(ApplicantMemory, "_write", ok)
    await store.save_field(CALLER, "given_name", "Maria", schema)

    mirrored = memory._MIRROR[memory._user_key(CALLER)]
    assert len(mirrored) == 1
    assert mirrored[0]["metadata"]["field_id"] == "given_name"


@pytest.mark.asyncio
async def test_failed_write_is_not_mirrored(monkeypatch, schema):
    """A write that mem0 rejected must not be shown as though it were stored."""

    async def boom(self, caller_id, text, metadata):
        return False

    monkeypatch.setattr(ApplicantMemory, "_write", boom)
    store = ApplicantMemory(api_key="k")
    await store.save_field(CALLER, "given_name", "Maria", schema)

    assert memory._user_key(CALLER) not in memory._MIRROR


@pytest.mark.asyncio
async def test_recall_outage_is_reported_not_swallowed(monkeypatch, schema):
    """A 429 on recall must surface as degraded, never as a caller with no history."""
    store = ApplicantMemory(api_key="k")
    store._mirror_put(
        CALLER,
        "family name is Reyes",
        {"tier": Tier.SEMANTIC, "field_id": "family_name", "value": "Reyes"},
    )

    # Fail the recall the way mem0 does, and let the real error handling run.
    monkeypatch.setattr(
        httpx.AsyncClient, "get", lambda *a, **k: (_ for _ in ()).throw(_quota_error())
    )

    profile = await store.load_profile(CALLER, schema)

    assert profile.source == "mirror"
    assert "quota" in profile.degraded_reason.lower()
    # The whole point: the caller is still recognised.
    assert profile.is_returning is True
    assert profile.known_values["family_name"] == "Reyes"


@pytest.mark.asyncio
async def test_all_three_tiers_survive_an_outage(monkeypatch, schema):
    """Semantic, episodic and procedural each come back from the mirror."""
    store = ApplicantMemory(api_key="k")
    store._mirror_put(
        CALLER, "dob", {"tier": Tier.SEMANTIC, "field_id": "date_of_birth", "value": "1998-04-12"}
    )
    store._mirror_put(
        CALLER,
        "called",
        {
            "tier": Tier.EPISODIC,
            "session_id": "s1",
            "form_id": "I-765",
            "at": "2026-07-26T00:00:00+00:00",
            "fields_collected": 32,
            "completed": True,
        },
    )
    store._mirror_put(
        CALLER,
        "speaks es",
        {"tier": Tier.PROCEDURAL, "key": "language", "value": "speak es with this applicant"},
    )

    monkeypatch.setattr(
        httpx.AsyncClient, "get", lambda *a, **k: (_ for _ in ()).throw(_quota_error())
    )
    profile = await store.load_profile(CALLER, schema)

    assert profile.known_values["date_of_birth"] == "1998-04-12"
    assert [e.form_id for e in profile.episodes] == ["I-765"]
    assert [p.key for p in profile.procedures] == ["language"]
    assert profile.preferred_language == "es"


@pytest.mark.asyncio
async def test_semantic_and_procedural_are_corrected_not_appended(schema):
    """Re-answering a question replaces the fact; it does not stack up beside it."""
    store = ApplicantMemory(api_key="k")
    for value in ("Reyes", "Reyes-Lopez"):
        store._mirror_put(
            CALLER, "n", {"tier": Tier.SEMANTIC, "field_id": "family_name", "value": value}
        )
    store._mirror_put(
        CALLER, "l", {"tier": Tier.PROCEDURAL, "key": "language", "value": "speak es"}
    )
    store._mirror_put(
        CALLER, "l", {"tier": Tier.PROCEDURAL, "key": "language", "value": "speak en"}
    )

    entries = memory._MIRROR[memory._user_key(CALLER)]
    assert len(entries) == 2
    assert entries[0]["metadata"]["value"] == "Reyes-Lopez"
    assert entries[1]["metadata"]["value"] == "speak en"


@pytest.mark.asyncio
async def test_episodes_accumulate(schema):
    """Calls are events. A second call does not overwrite the first."""
    store = ApplicantMemory(api_key="k")
    for session in ("s1", "s2"):
        store._mirror_put(
            CALLER,
            "called",
            {
                "tier": Tier.EPISODIC,
                "session_id": session,
                "form_id": "I-765",
                "at": f"2026-07-2{session[-1]}T00:00:00+00:00",
            },
        )
    assert len(memory._MIRROR[memory._user_key(CALLER)]) == 2


@pytest.mark.asyncio
async def test_forget_refuses_rather_than_reporting_a_deletion_it_cannot_make(monkeypatch, schema):
    """Deleting works by listing what exists and deleting each id.

    With recall unavailable there is no list, so nothing is deleted remotely.
    Clearing the local mirror at that point would destroy the only remaining
    record of what is still held out there, and report success while doing it --
    the applicant would believe their data was gone and stop asking.
    """
    store = ApplicantMemory(api_key="k")
    store._mirror_put(
        CALLER, "dob", {"tier": Tier.SEMANTIC, "field_id": "date_of_birth", "value": "1998-04-12"}
    )
    monkeypatch.setattr(
        httpx.AsyncClient, "get", lambda *a, **k: (_ for _ in ()).throw(_quota_error())
    )

    with pytest.raises(memory.DeletionUnverifiable):
        await store.forget(CALLER)

    # Nothing was thrown away on the strength of a deletion that never happened.
    assert memory._MIRROR[memory._user_key(CALLER)]


@pytest.mark.asyncio
async def test_forget_clears_the_mirror_once_the_deletion_really_ran(monkeypatch, schema):
    """A deletion that leaves our own copy behind is not a deletion either."""
    store = ApplicantMemory(api_key="k")
    store._mirror_put(
        CALLER, "dob", {"tier": Tier.SEMANTIC, "field_id": "date_of_birth", "value": "1998-04-12"}
    )
    store._mirror_put(
        CALLER, "called", {"tier": Tier.EPISODIC, "session_id": "s1", "form_id": "I-765"}
    )

    class _Resp:
        status_code = 204

    async def ok_get(self, url, **kwargs):
        request = httpx.Request("GET", url)
        return httpx.Response(200, request=request, json={"results": []})

    async def ok_delete(self, url, **kwargs):
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "get", ok_get)
    monkeypatch.setattr(httpx.AsyncClient, "delete", ok_delete)

    await store.forget(CALLER, tier=Tier.EPISODIC)
    remaining = memory._MIRROR[memory._user_key(CALLER)]
    assert [e["metadata"]["tier"] for e in remaining] == [Tier.SEMANTIC]

    await store.forget(CALLER)
    assert memory._user_key(CALLER) not in memory._MIRROR


@pytest.mark.asyncio
async def test_an_anonymous_session_gets_no_memory_at_all(monkeypatch, schema):
    """Every caller-less session hashes to the same key.

    Reading or writing that shared bucket would hand one applicant's name, date
    of birth and passport number to the next anonymous caller.
    """
    store = ApplicantMemory(api_key="k")

    # Patch the transport, not _write -- the guard lives in _write, and stubbing
    # it out would be testing the stub.
    posted: list[str] = []

    async def spy_post(self, url, **kwargs):
        posted.append(url)
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, json={})

    monkeypatch.setattr(httpx.AsyncClient, "post", spy_post)
    monkeypatch.setattr(
        httpx.AsyncClient, "get", lambda *a, **k: (_ for _ in ()).throw(_quota_error())
    )
    for blank in ("", "   "):
        assert await store.save_field(blank, "given_name", "Maria", schema) is False
        profile = await store.load_profile(blank, schema)
        assert profile.known_values == {}
        assert profile.is_returning is False
        assert profile.source == "anonymous"
    assert posted == [], "nothing may be written under an empty caller id"

    # And a real caller is unaffected by the guard.
    assert await store.save_field(CALLER, "given_name", "Maria", schema) is True
    assert posted, "a session with a caller must still write"

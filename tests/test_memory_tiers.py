"""The profile layer: three tiers assembled into something a greeting can use.

This file used to test an in-process mirror that covered for hosted mem0's read
quota. Both are gone -- the mirror was a second copy of the truth, and while it
existed the Band agents had their own memory tools writing somewhere else
entirely, so an applicant had two memories that never saw each other.

There is one store now, and these are the behaviours that survive that change:
what a returning caller looks like, what an anonymous one must not, and which
records correct a previous value versus accumulate beside it.
"""

from __future__ import annotations

import pytest

from api import memory
from api.form_registry import get_form
from api.i765_schema import SKIP_SENTINEL
from api.memory import Tier, get_memory, summarize

CALLER = "+14155550142"


@pytest.fixture
def schema():
    return get_form("I-765")


@pytest.fixture
def store():
    return get_memory()


@pytest.mark.asyncio
async def test_a_caller_with_nothing_stored_is_not_returning(store, schema):
    profile = await store.load_profile(CALLER, schema)
    assert profile.is_returning is False
    assert profile.known_values == {}


@pytest.mark.asyncio
async def test_a_saved_fact_comes_back(store, schema):
    assert await store.save_field(CALLER, "given_name", "Maria", schema) is True

    profile = await store.load_profile(CALLER, schema)
    assert profile.is_returning is True
    assert profile.known_values["given_name"] == "Maria"
    assert profile.display_name == "Maria"


@pytest.mark.asyncio
async def test_an_anonymous_session_gets_no_memory_in_either_direction(store, schema):
    """Every caller-less session derives the same key.

    Reading or writing that shared bucket would hand one applicant's name and
    date of birth to the next anonymous caller.
    """
    for blank in ("", "   "):
        assert await store.save_field(blank, "given_name", "Maria", schema) is False
        profile = await store.load_profile(blank, schema)
        assert profile.is_returning is False
        assert profile.source == "anonymous"
        assert profile.known_values == {}


@pytest.mark.asyncio
async def test_a_skipped_answer_is_never_a_remembered_fact(store, schema):
    assert await store.save_field(CALLER, "middle_name", SKIP_SENTINEL, schema) is False


@pytest.mark.asyncio
async def test_sensitive_values_are_not_persisted_unless_opted_in(store, schema):
    assert await store.save_field(CALLER, "ssn", "123456789", schema) is False
    profile = await store.load_profile(CALLER, schema)
    assert "ssn" not in profile.known_values


@pytest.mark.asyncio
async def test_all_three_tiers_assemble_into_one_profile(store, schema):
    await store.save_field(CALLER, "given_name", "Maria", schema)
    await store.record_episode(CALLER, "s1", "I-765", 32, True, "es")
    await store.learn(CALLER, "language", "speak es with this applicant")

    profile = await store.load_profile(CALLER, schema)
    assert profile.known_values["given_name"] == "Maria"
    assert [e.form_id for e in profile.episodes] == ["I-765"]
    assert [p.key for p in profile.procedures] == ["language"]
    # A procedural language rule outranks anything else.
    assert profile.preferred_language == "es"


@pytest.mark.asyncio
async def test_a_fact_corrects_and_a_call_accumulates(store, schema):
    await store.save_field(CALLER, "given_name", "Maria", schema)
    await store.save_field(CALLER, "given_name", "Maria Elena", schema)
    await store.record_episode(CALLER, "s1", "I-765", 10, False)
    await store.record_episode(CALLER, "s2", "I-765", 32, True)

    profile = await store.load_profile(CALLER, schema)
    assert profile.known_values["given_name"] == "Maria Elena", "a fact is a correction"
    assert len(profile.episodes) == 2, "two calls are two events"


@pytest.mark.asyncio
async def test_what_is_learned_from_a_call_is_conservative(store, schema):
    """A wrong rule is worse than no rule: it skips a question they could answer."""
    learned = await store.learn_from_session(
        CALLER,
        {"ssn": SKIP_SENTINEL, "eligibility_category": "(c)(3)(B)"},
        schema,
        language="es",
    )
    assert learned >= 3

    profile = await store.load_profile(CALLER, schema)
    rules = {p.key: p.value for p in profile.procedures}
    assert "speak es" in rules["language"]
    assert "do not have a ssn" in rules["no_ssn"]
    assert "(c)(3)(B)" in rules["eligibility_category"]


@pytest.mark.asyncio
async def test_forgetting_one_tier_keeps_the_others(store, schema):
    """The whole point of separating them: drop your history, keep the profile."""
    await store.save_field(CALLER, "given_name", "Maria", schema)
    await store.record_episode(CALLER, "s1", "I-765", 32, True)

    await store.forget(CALLER, Tier.EPISODIC)

    profile = await store.load_profile(CALLER, schema)
    assert profile.episodes == []
    assert profile.known_values["given_name"] == "Maria"

    await store.forget(CALLER)
    assert (await store.load_profile(CALLER, schema)).is_returning is False


@pytest.mark.asyncio
async def test_two_tenants_never_share_a_caller(schema):
    """The disclosure the tenancy design exists to prevent."""
    a = memory.ApplicantMemory(tenant_id="clinic-a")
    b = memory.ApplicantMemory(tenant_id="clinic-b")
    await a.save_field(CALLER, "given_name", "Maria", schema)

    assert (await a.load_profile(CALLER, schema)).is_returning is True
    assert (await b.load_profile(CALLER, schema)).is_returning is False


@pytest.mark.asyncio
async def test_the_greeting_draws_on_every_tier(store, schema):
    await store.save_field(CALLER, "given_name", "Maria", schema)
    await store.record_episode(CALLER, "s1", "I-765", 32, True)
    await store.learn(CALLER, "language", "speak es with this applicant")

    spoken = summarize(await store.load_profile(CALLER, schema), schema)
    assert "I-765" in spoken
    assert "given name" in spoken
    assert "how you like to work" in spoken


@pytest.mark.asyncio
async def test_a_greeting_for_someone_new_says_nothing(store, schema):
    assert summarize(await store.load_profile(CALLER, schema), schema) == ""

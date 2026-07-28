"""Memory as something the agents do, and cannot do to each other's tenants.

Before this, memory was written by the HTTP endpoints and the agents never
touched it -- so "the agents communicate about memory" was not true, and the
three tiers existed beside the orchestration rather than inside it.

The rule that keeps it safe: an agent decides *that* something is worth
remembering; it never supplies the value. `remember_fact` reads what the
applicant actually said out of the session, so a model cannot write a date of
birth by asserting one.
"""

from __future__ import annotations

import pytest

from api import memory_store
from api.band.tools import SessionTools
from api.i765_schema import SKIP_SENTINEL
from api.session_store import get_session_store, reset_session_store
from api.tenancy import Principal, Tenant

CALLER = "+14155550142"
CLINIC_A = Tenant(id="clinic-a", name="Clinic A")
CLINIC_B = Tenant(id="clinic-b", name="Clinic B")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_BACKEND", "memory")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.setattr(memory_store, "DB_PATH", tmp_path / "memory.db")
    memory_store.reset_backend()
    reset_session_store()
    yield
    memory_store.reset_backend()
    reset_session_store()


async def _session(session_id: str, tenant: Tenant, **values: str) -> SessionTools:
    store = get_session_store()
    await store.create(session_id, CALLER, "I-765", tenant_id=tenant.id)
    for field_id, value in values.items():
        await store.save_field(session_id=session_id, field_id=field_id, value=value)
    return SessionTools(
        session_id, Principal(tenant=tenant, user_id=CALLER), __import__("pathlib").Path("/tmp")
    )


@pytest.mark.asyncio
async def test_an_agent_keeps_a_fact_it_did_not_author(tmp_path):
    """The value comes from the session, never from the agent."""
    tools = await _session("s1", CLINIC_A, given_name="Maria")
    result = await tools.remember_fact(field_id="given_name")
    assert result["stored"] is True

    recalled = await tools.recall_profile()
    assert recalled["is_returning"] is True
    assert [f["value"] for f in recalled["semantic"]] == ["Maria"]


@pytest.mark.asyncio
async def test_a_field_the_applicant_never_answered_cannot_be_remembered():
    tools = await _session("s2", CLINIC_A, given_name="Maria")
    assert (await tools.remember_fact(field_id="family_name"))["stored"] is False
    assert (await tools.remember_fact(field_id="not_a_field"))["stored"] is False


@pytest.mark.asyncio
async def test_a_skipped_answer_is_not_a_fact():
    tools = await _session("s3", CLINIC_A, middle_name=SKIP_SENTINEL)
    result = await tools.remember_fact(field_id="middle_name")
    assert result["stored"] is False
    assert "skipped" in result["reason"]


@pytest.mark.asyncio
async def test_sensitive_values_need_the_tenant_to_opt_in():
    """Holding an SSN for one call is not the same as parking it in a store."""
    tools = await _session("s4", CLINIC_A, ssn="123456789")
    assert (await tools.remember_fact(field_id="ssn"))["stored"] is False

    opted_in = Tenant(id="clinic-c", name="Clinic C", store_sensitive=True)
    tools2 = await _session("s5", opted_in, ssn="123456789")
    assert (await tools2.remember_fact(field_id="ssn"))["stored"] is True
    # Even then it is never read back into a room transcript.
    recalled = await tools2.recall_profile()
    assert recalled["semantic"][0]["value"] == "[withheld]"


@pytest.mark.asyncio
async def test_all_three_tiers_round_trip():
    tools = await _session("s6", CLINIC_A, given_name="Maria")
    await tools.remember_fact(field_id="given_name")
    await tools.record_episode(outcome="completed")
    await tools.learn_rule(rule="speaks Spanish")

    profile = await tools.recall_profile()
    assert profile["counts"] == {"semantic": 1, "episodic": 1, "procedural": 1}


@pytest.mark.asyncio
async def test_a_rule_must_be_a_sentence_not_a_paragraph():
    tools = await _session("s7", CLINIC_A)
    assert (await tools.learn_rule(rule=""))["stored"] is False
    assert (await tools.learn_rule(rule="x"))["stored"] is False
    assert (await tools.learn_rule(rule="a" * 500))["stored"] is False
    assert (await tools.learn_rule(rule="speaks Spanish"))["stored"] is True


@pytest.mark.asyncio
async def test_a_fact_is_a_correction_and_a_call_is_an_event():
    """Re-answering replaces; calling again accumulates."""
    tools = await _session("s8", CLINIC_A, given_name="Maria")
    await tools.remember_fact(field_id="given_name")
    await tools.remember_fact(field_id="given_name")
    await tools.record_episode(outcome="first")

    other = await _session("s9", CLINIC_A, given_name="Maria")
    await other.record_episode(outcome="second")

    profile = await tools.recall_profile()
    assert profile["counts"]["semantic"] == 1, "the same fact twice is one fact"
    assert profile["counts"]["episodic"] == 2, "two calls are two events"


@pytest.mark.asyncio
async def test_one_tenants_memory_is_invisible_to_another():
    """The disclosure the whole tenancy design exists to prevent."""
    a = await _session("s10", CLINIC_A, given_name="Maria")
    await a.remember_fact(field_id="given_name")
    await a.learn_rule(rule="speaks Spanish")

    b = await _session("s11", CLINIC_B, given_name="Maria")
    profile = await b.recall_profile()
    assert profile["is_returning"] is False
    assert profile["counts"] == {"semantic": 0, "episodic": 0, "procedural": 0}


@pytest.mark.asyncio
async def test_an_agent_cannot_call_a_tool_outside_its_whitelist():
    tools = await _session("s12", CLINIC_A, given_name="Maria")
    from api.band.tools import ToolDenied

    with pytest.raises(ToolDenied):
        await tools.call("remember_fact", ("session_state",), field_id="given_name")
    # And the whitelist is what permits it, not the prompt.
    assert (await tools.call("remember_fact", ("remember_fact",), field_id="given_name"))["stored"]


@pytest.mark.asyncio
async def test_an_unreachable_store_degrades_loudly_rather_than_silently(monkeypatch, caplog):
    """Neither extreme is acceptable.

    Failing hard means one wrong character in a key takes memory down for
    everybody. Failing quietly is the bug this codebase has already had twice --
    every read returns empty and every caller looks new. So it falls back to
    SQLite and says so, in the log and in the status.
    """
    import logging

    monkeypatch.setenv("SUPABASE_URL", "https://nonexistent-project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "not-a-real-key")
    memory_store.reset_backend()

    with caplog.at_level(logging.ERROR):
        status = await memory_store.check_backend()

    assert status["backend"] == "sqlite", "the service must keep working"
    assert status["reachable"] is True
    assert status["degraded_from"] == "supabase", "and must say what it lost"
    assert status["degraded_reason"]
    assert any("NOT reachable" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_a_working_store_is_not_reported_as_degraded():
    memory_store.reset_backend()
    status = await memory_store.check_backend()
    assert status["reachable"] is True
    assert "degraded_from" not in status

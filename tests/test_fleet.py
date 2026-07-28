"""The orchestration layer, which had no tests at all.

Three modules ran the whole agent story -- fleet, brain, protocol -- and not one
line of them was covered. Every bug found in them was found by watching six
agents fail in a live Band room, which is an expensive way to learn that a set
literal contains a typo.

The most important test here is the first one. `BAND_TOOLS_OFFERED` was
populated from the service names Band's REST API reports for an agent
(`list_chat_participants_service`), while the schemas the SDK actually hands the
model are `band_`-prefixed. The names never intersected, so the filter matched
nothing, so the agents were offered no Band tools -- and nothing failed. The
room opened, the agents talked, the PDF came out. It just was not doing the
thing it was written to do, and no amount of watching it would show that.
"""

from __future__ import annotations

import asyncio

import pytest

from api.band import brain, fleet, ledger
from api.band.roles import BY_KEY, PIPELINE_ORDER, ROLES, by_display, role_for, roster
from api.band.tools import TOOL_PARAMS, TOOL_SCHEMAS

# ---------------------------------------------------------------------------
# The names, pinned to the SDK
# ---------------------------------------------------------------------------


def test_every_band_tool_we_offer_actually_exists_in_the_sdk():
    """The regression that could not be seen from the outside.

    If the SDK renames one of these, this fails here rather than silently
    reverting the agents to having no way to talk to each other.
    """
    from band.runtime.tools import ALL_TOOL_NAMES

    known = set(ALL_TOOL_NAMES)
    unknown = fleet.BAND_TOOLS_OFFERED - known
    assert not unknown, (
        f"offered tools the SDK does not define: {sorted(unknown)}. "
        f"Band's REST API reports service names, the SDK's schemas are band_-prefixed; "
        f"filtering on the wrong one matches nothing and fails silently."
    )


def test_the_offered_set_covers_what_collaboration_requires():
    """Sending, and finding out who to send to. Without both there is no room."""
    assert "band_send_message" in fleet.BAND_TOOLS_OFFERED
    assert fleet.BAND_TOOLS_OFFERED & {"band_get_participants", "band_lookup_peers"}


# ---------------------------------------------------------------------------
# Roles: the whitelist is the security boundary, so it gets asserted
# ---------------------------------------------------------------------------


def test_six_roles_each_with_a_brief_and_a_tool_whitelist():
    assert len(ROLES) == 6
    for role in ROLES:
        assert role.brief.strip(), f"{role.key} has no brief"
        assert role.tools, f"{role.key} may call nothing, so it can do nothing"
        assert "never" in role.system_prompt.lower() or "Never" in role.system_prompt


def test_no_agent_may_call_a_tool_that_does_not_exist():
    """A whitelist entry with no implementation is a permission granted to
    nothing -- harmless, but it means the whitelist is not being read."""
    for role in ROLES:
        for tool in role.tools:
            assert tool in TOOL_SCHEMAS, f"{role.key} whitelists unknown tool {tool!r}"


def test_only_the_filler_can_write_the_pdf():
    """The boundary that matters: five of six agents cannot produce a filing."""
    writers = [r.key for r in ROLES if "write_form" in r.tools]
    assert writers == ["filler"]


def test_every_memory_tier_has_an_owner():
    """All three tiers are written by an agent, not by the endpoints alone."""
    owners = {
        tool: [r.key for r in ROLES if tool in r.tools]
        for tool in ("remember_fact", "record_episode", "learn_rule")
    }
    for tool, who in owners.items():
        assert who, f"no agent can write {tool}: that tier is not agent-driven"


def test_role_lookup_helpers_agree_with_each_other():
    assert set(PIPELINE_ORDER) == set(BY_KEY)
    for role in ROLES:
        assert role_for(role.key) is role
        assert by_display(role.display) is role
        assert by_display(role.display.upper()) is role
    assert by_display("Nobody") is None
    with pytest.raises(KeyError):
        role_for("nobody")
    assert roster() == [r.display for r in ROLES]


def test_agent_names_are_namespaced_so_two_deployments_can_coexist(monkeypatch):
    """Band agent names are unique per account and cannot be deleted once used."""
    import importlib

    monkeypatch.setenv("BAND_FLEET_NAMESPACE", "staging")
    roles = importlib.reload(importlib.import_module("api.band.roles"))
    try:
        assert roles.ROLES[0].agent_name.startswith("Zuzu-staging-")
    finally:
        monkeypatch.delenv("BAND_FLEET_NAMESPACE", raising=False)
        importlib.reload(roles)


# ---------------------------------------------------------------------------
# Hand-off order
# ---------------------------------------------------------------------------


def test_the_fixed_order_runs_out_rather_than_looping():
    f = fleet.Fleet()
    assert f.next_after("intake") == "extractor"
    assert f.next_after(PIPELINE_ORDER[-1]) is None
    assert f.next_after("nobody") is None


def test_an_unconnected_agent_is_never_addressed():
    """`ids_for` resolves display names the model produced. A name that is not
    in the room must resolve to nothing rather than to a plausible id."""
    f = fleet.Fleet()
    assert f.ids_for(["Intake", "Auditor", "Hallucinated"]) == []


# ---------------------------------------------------------------------------
# Brain: the parsing that decides whether a turn happened
# ---------------------------------------------------------------------------


def test_reasoning_blocks_never_reach_the_room():
    assert brain.strip_reasoning("<think>weighing it up</think>Done.") == "Done."
    # Truncation leaves the block open, which is what actually shipped into a
    # transcript before this existed.
    assert brain.strip_reasoning("Done.<think>I should also") == "Done."
    assert brain.strip_reasoning("") == ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"say":"hi","done":true}', {"say": "hi", "done": True}),
        ('```json\n{"say":"hi"}\n```', {"say": "hi"}),
        ('Sure! {"say":"hi"} hope that helps', {"say": "hi"}),
        ("no json here", None),
        ('{"unbalanced": ', None),
        ("[1,2,3]", None),
    ],
)
def test_decision_json_survives_the_ways_a_model_wraps_it(raw, expected):
    assert brain._extract_json(raw) == expected


def test_a_decision_reached_no_other_way_is_not_labelled_as_the_model():
    """Three outcomes, three labels. A trail that files an unusable answer under
    "minimax-m3" is claiming a decision the model never made."""
    assert len({brain.REASONER_MODEL, brain.REASONER_FALLBACK, brain.REASONER_UNUSABLE}) == 3
    assert brain.Decision().usable is True
    assert brain.Decision(usable=False).usable is False


def test_the_fallback_says_it_is_the_fallback():
    d = brain.fallback_decision("intake", "extractor", ())
    assert d.to == ["extractor"]
    assert d.done is False
    assert "model was unavailable" in d.because
    # The last role has nowhere to hand on to, so it finishes rather than stalls.
    assert brain.fallback_decision("auditor", None, ()).done is True


def test_the_fallback_only_runs_tools_that_need_no_arguments():
    """It used to run every tool with `{}`. remember_fact("") stored nothing and
    the trail showed a tool call that did nothing -- worse than no call."""
    needs_args = {n for n, p in TOOL_PARAMS.items() if p.get("required")}
    assert "remember_fact" in needs_args, "guard assumes this tool takes arguments"
    assert "session_state" not in needs_args


@pytest.mark.parametrize("role", ROLES)
def test_every_role_can_still_do_something_without_the_model(role):
    """The fallback is the deterministic pipeline. If a role's whole toolset
    needs arguments, that role becomes a no-op the moment the key expires."""
    argless = [t for t in role.tools if not TOOL_PARAMS.get(t, {}).get("required")]
    assert argless, f"{role.key} does nothing at all without the model"


def test_the_model_is_unavailable_without_a_key(monkeypatch):
    monkeypatch.delenv("TOKENROUTER_API_KEY", raising=False)
    assert brain.is_available() is False
    monkeypatch.setenv("TOKENROUTER_API_KEY", "sk-test")
    assert brain.is_available() is True


def test_reaching_the_model_without_a_key_raises_rather_than_inventing(monkeypatch):
    monkeypatch.delenv("TOKENROUTER_API_KEY", raising=False)
    with pytest.raises(brain.BrainUnavailable):
        asyncio.run(brain.think("s", [{"role": "user", "content": "hi"}]))


# ---------------------------------------------------------------------------
# The fleet declines rather than pretends
# ---------------------------------------------------------------------------


def test_a_fleet_with_no_agents_refuses_the_work():
    f = fleet.Fleet()
    assert f.is_running is False
    assert asyncio.run(f.collaborate("s1", _principal(), tmp_out())) is None


def test_the_fleet_does_not_start_without_a_model(monkeypatch):
    monkeypatch.delenv("TOKENROUTER_API_KEY", raising=False)
    f = fleet.Fleet()
    assert asyncio.run(f.start()) is False
    assert f.is_running is False


def test_closing_an_unknown_room_is_not_an_error():
    asyncio.run(fleet.Fleet().close("no-such-room", "whatever"))


def test_finished_rooms_do_not_accumulate_forever():
    """Each collaboration holds a Principal and a SessionTools. Unbounded, that
    is a leak proportional to traffic -- and eviction is safe because the trail
    is already in the ledger by the time a room is finished."""
    f = fleet.Fleet()
    for index in range(fleet.MAX_ROOMS_IN_MEMORY + 10):
        collab = _collab(f"s{index}", f"room{index}", started=float(index))
        collab.finished.set()
        f.by_room[f"room{index}"] = collab
    f._evict()
    assert len(f.by_room) == fleet.MAX_ROOMS_IN_MEMORY
    # The oldest go first, so the most recent filing is the one still cached.
    assert f"room{fleet.MAX_ROOMS_IN_MEMORY + 9}" in f.by_room
    assert "room0" not in f.by_room


def test_an_unfinished_room_is_never_evicted():
    """It is still being written to, and it is not in the ledger yet."""
    f = fleet.Fleet()
    live = _collab("live", "room-live", started=0.0)  # oldest, and still open
    f.by_room["room-live"] = live
    for index in range(fleet.MAX_ROOMS_IN_MEMORY + 5):
        done = _collab(f"s{index}", f"room{index}", started=float(index + 1))
        done.finished.set()
        f.by_room[f"room{index}"] = done
    f._evict()
    assert "room-live" in f.by_room


# ---------------------------------------------------------------------------
# The ledger: the claim was "months later", so it has to outlive the process
# ---------------------------------------------------------------------------


def test_a_trail_survives_the_process_that_produced_it(tmp_path, monkeypatch):
    _use_sqlite(tmp_path, monkeypatch)
    collab = _collab("sess-audit", "room-audit")
    collab.turns.append(
        fleet.Turn(
            role="intake",
            agent_id="agent-1",
            said="Two answers still missing.",
            addressed=["Extractor"],
            tool_calls=[{"tool": "next_question", "field_id": "given_name"}],
            because="the interview is not complete",
            reasoner=brain.REASONER_MODEL,
        )
    )
    collab.turns.append(
        fleet.Turn(role="auditor", agent_id="agent-6", said="Sealed.", reasoner="runtime")
    )

    kept = asyncio.run(ledger.record(collab))
    assert kept == 2

    # Nothing of the original process is used to read it back.
    replayed = asyncio.run(ledger.replay("clinic-a", "sess-audit"))
    assert replayed is not None
    assert replayed["replayed"] is True
    assert replayed["turn_count"] == 2
    assert [t["role"] for t in replayed["turns"]] == ["intake", "auditor"]
    assert replayed["turns"][0]["tool_calls"][0]["tool"] == "next_question"
    assert replayed["turns"][0]["reasoner"] == brain.REASONER_MODEL


def test_turns_replay_in_the_order_they_happened(tmp_path, monkeypatch):
    """Keys sort lexically, so a run that used the full turn budget must not
    come back with turn 10 before turn 2."""
    _use_sqlite(tmp_path, monkeypatch)
    collab = _collab("sess-order", "room-order")
    for index in range(12):
        collab.turns.append(fleet.Turn(role=f"r{index}", agent_id="a", said=f"turn {index}"))
    asyncio.run(ledger.record(collab))
    replayed = asyncio.run(ledger.replay("clinic-a", "sess-order"))
    assert [t["role"] for t in replayed["turns"]] == [f"r{i}" for i in range(12)]


def test_one_tenants_trail_is_not_readable_as_another(tmp_path, monkeypatch):
    _use_sqlite(tmp_path, monkeypatch)
    asyncio.run(ledger.record(_collab("shared-id", "room-x")))
    assert asyncio.run(ledger.replay("clinic-a", "shared-id")) is not None
    assert asyncio.run(ledger.replay("clinic-b", "shared-id")) is None


def test_an_audit_scope_can_never_collide_with_an_applicants_memory():
    """Same table. Disjoint prefixes are what keeps a filing's audit trail out
    of the answer to "what do you remember about me"."""
    from api.tenancy import scope_key

    audit = ledger.scope_for("clinic-a", "sess-1")
    applicant = scope_key("clinic-a", "+14155550142")
    assert audit.startswith(ledger.SCOPE_PREFIX)
    assert not applicant.startswith(ledger.SCOPE_PREFIX)
    assert audit != applicant


def test_replaying_a_session_that_never_ran_returns_nothing(tmp_path, monkeypatch):
    _use_sqlite(tmp_path, monkeypatch)
    assert asyncio.run(ledger.replay("clinic-a", "never-happened")) is None


def test_a_store_that_is_down_costs_the_trail_and_nothing_else(tmp_path, monkeypatch):
    """The PDF is already written by the time this runs. It must not raise."""
    _use_sqlite(tmp_path, monkeypatch)

    class Broken:
        name = "broken"

        async def put(self, *_a, **_k):
            raise RuntimeError("store is down")

        async def all(self, *_a, **_k):
            raise RuntimeError("store is down")

    monkeypatch.setattr(ledger, "get_backend", lambda: Broken())
    assert asyncio.run(ledger.record(_collab("sess-broken", "room-broken"))) == 0
    assert asyncio.run(ledger.replay("clinic-a", "sess-broken")) is None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _use_sqlite(tmp_path, monkeypatch):
    """Each test gets its own database. Sharing one let an earlier test's writes
    satisfy a later test's assertion, which has already happened here once."""
    import api.memory_store as store

    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "memory.db")
    store.reset_backend()


def _principal(tenant_id: str = "clinic-a"):
    from api.tenancy import Principal, Tenant

    return Principal(tenant=Tenant(id=tenant_id, name=tenant_id), user_id="+14155550142")


def tmp_out():
    from pathlib import Path

    return Path(".")


def _collab(
    session_id: str, room_id: str, started: float | None = None, tenant_id: str = "clinic-a"
) -> fleet.Collaboration:
    from api.band.tools import SessionTools

    principal = _principal(tenant_id)
    collab = fleet.Collaboration(
        session_id=session_id,
        room_id=room_id,
        principal=principal,
        tools=SessionTools(session_id, principal, tmp_out()),
    )
    if started is not None:
        collab.started = started
    return collab


# ---------------------------------------------------------------------------
# Fleet.start(), which nothing ever called.
#
# It shipped an AttributeError to production: a method that start() invokes was
# defined on RoleAgent instead of Fleet, so every boot logged "band fleet failed
# to start" and the service ran the deterministic path. Everything else passed
# -- health was 200, forms were filled -- because the fleet failing is a
# designed-for degradation, and a degradation nobody asserts is invisible.
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, agent_id: str, claims: str | None = None) -> None:
        self.agent_id = agent_id
        self._claims = claims if claims is not None else agent_id

    async def whoami(self):
        return {"data": {"id": self._claims, "name": f"Zuzu-{self.agent_id}"}}


def _fake_credentials():
    return {r.key: {"id": f"id-{r.key}", "api_key": f"key-{r.key}"} for r in ROLES}


@pytest.fixture
def startable(monkeypatch):
    """A fleet whose agents connect instantly and answer whoami honestly."""
    monkeypatch.setenv("TOKENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(fleet, "load_credentials", _fake_credentials)

    async def fake_start(self):
        self.client = _FakeClient(self.agent_id)

    monkeypatch.setattr(fleet.RoleAgent, "start", fake_start)
    monkeypatch.setattr(fleet.RoleAgent, "stop", lambda self: _noop())
    return fleet.Fleet()


async def _noop():
    return None


def test_the_fleet_starts_all_six_agents(startable):
    """The test that would have caught the AttributeError."""
    assert asyncio.run(startable.start()) is True
    assert startable.is_running is True
    assert sorted(startable.agents) == sorted(r.key for r in ROLES)


def test_starting_twice_is_a_no_op(startable):
    assert asyncio.run(startable.start()) is True
    assert asyncio.run(startable.start()) is True
    assert len(startable.agents) == len(ROLES)


def test_a_partial_credential_set_starts_nothing(monkeypatch):
    """Six agents or none. A room with a hole in it is worse than the
    deterministic path, because the missing role's job silently never happens."""
    monkeypatch.setenv("TOKENROUTER_API_KEY", "sk-test")
    partial = _fake_credentials()
    partial.pop("auditor")
    monkeypatch.setattr(fleet, "load_credentials", lambda: partial)
    f = fleet.Fleet()
    assert asyncio.run(f.start()) is False
    assert f.agents == {}


def test_an_agent_that_cannot_connect_takes_the_whole_fleet_down_cleanly(monkeypatch):
    monkeypatch.setenv("TOKENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(fleet, "load_credentials", _fake_credentials)

    async def fails_on_the_fourth(self):
        if self.role.key == "validator":
            raise RuntimeError("Band refused the connection")
        self.client = _FakeClient(self.agent_id)

    monkeypatch.setattr(fleet.RoleAgent, "start", fails_on_the_fourth)
    monkeypatch.setattr(fleet.RoleAgent, "stop", lambda self: _noop())
    f = fleet.Fleet()
    assert asyncio.run(f.start()) is False
    assert f.is_running is False
    assert f.agents == {}, "a half-connected fleet must not be left behind"


def test_a_credential_pointing_at_the_wrong_agent_is_reported(startable, caplog, monkeypatch):
    """A drifted credentials file connects fine and attributes every audit entry
    to an agent that means something else now. Only a warning -- worth knowing,
    not worth refusing to fill somebody's form over."""

    async def wrong_identity(self):
        claims = "someone-else" if self.role.key == "mapper" else self.agent_id
        self.client = _FakeClient(self.agent_id, claims=claims)

    import logging

    monkeypatch.setattr(fleet.RoleAgent, "start", wrong_identity)
    with caplog.at_level(logging.WARNING, logger="api.band.fleet"):
        assert asyncio.run(startable.start()) is True

    warned = [r.getMessage() for r in caplog.records]
    assert any("someone-else" in m and "mapper" in m for m in warned), (
        f"no mismatch warning in {warned}"
    )
    # It still runs: a mismatch is a provenance problem, not a filing problem.
    assert startable.is_running is True


def test_whoami_failing_does_not_stop_the_fleet(startable, monkeypatch):
    """Band being slow to answer a courtesy call is not a reason to refuse work."""

    async def start_with_broken_whoami(self):
        class Broken(_FakeClient):
            async def whoami(self):
                raise RuntimeError("Band did not answer")

        self.client = Broken(self.agent_id)

    monkeypatch.setattr(fleet.RoleAgent, "start", start_with_broken_whoami)
    assert asyncio.run(startable.start()) is True


def test_two_runs_on_one_session_are_two_records_not_a_chimera(tmp_path, monkeypatch):
    """A retry after a 503 or the 300s timeout used to rewrite the first run.

    Turn keys were positional and both backends upsert on (scope, tier, key),
    so run 2 overwrote turn-000 onward of run 1 and left run 1's later turns
    behind. The replayed trail was attributed to one room and contained turns
    from two -- in the case actually reproduced, carrying both "collaboration
    timed out" and "auditor sealed the record" for what it presented as a single
    collaboration.
    """
    _use_sqlite(tmp_path, monkeypatch)

    first = _collab("sess-retry", "room-A", started=1000.0)
    for role in ("intake", "extractor", "mapper", "validator", "filler", "auditor"):
        first.turns.append(fleet.Turn(role=role, agent_id="a", said=f"{role} spoke in A"))
    first.turns.append(fleet.Turn(role="fleet", agent_id="", said="Collaboration closed: sealed."))
    asyncio.run(ledger.record(first))

    second = _collab("sess-retry", "room-B", started=2000.0)
    second.turns.append(fleet.Turn(role="intake", agent_id="a", said="intake spoke in B"))
    second.turns.append(
        fleet.Turn(role="fleet", agent_id="", said="Collaboration closed: timed out.")
    )
    asyncio.run(ledger.record(second))

    replayed = asyncio.run(ledger.replay("clinic-a", "sess-retry"))
    assert replayed is not None
    assert replayed["collaborations"] == 2, "two runs happened and the record must say so"
    assert replayed["room_id"] == "room-B", "the latest run is what /audit means"
    assert replayed["turn_count"] == 2, f"room-B had 2 turns, got {replayed['turn_count']}"
    said = " ".join(t.get("said", "") for t in replayed["turns"])
    assert "in A" not in said, "room-A's turns must not appear inside room-B's record"
    assert all(t.get("room_id") == "room-B" for t in replayed["turns"])


def test_writing_the_pdf_does_not_stall_the_event_loop(tmp_path, monkeypatch):
    """The one path the whole design promises never to block.

    fill_form opens, decrypts and rewrites a 720 KB AES-encrypted AcroForm --
    real CPU and blocking file I/O -- and generate_form is an `async def`, so
    FastAPI runs it directly on the loop rather than in its sync-handler
    threadpool. Every other applicant mid-sentence on that worker stalled for
    the duration, while save_field's docstring said "No LLM, no filesystem, no
    PDF work happens here."
    """
    import time as _time

    import api.pdf_engine as pdf_engine

    BLOCK = 0.30

    def slow_fill(values, out_path, schema):
        _time.sleep(BLOCK)
        out_path.write_bytes(b"%PDF-1.7\n")

        from api.pdf_engine import FillReport

        return FillReport(path=out_path, filled=list(values), dropped={})

    monkeypatch.setattr(pdf_engine, "fill_form", slow_fill)

    async def scenario():
        from api.session_store import get_session_store, reset_session_store

        reset_session_store()
        store = get_session_store()
        await store.create("sess-block", "+14155550142", "I-765")
        # It must actually reach the write. An incomplete session is refused
        # before fill_form is ever called, which would pass this test for the
        # wrong reason.
        import json as _json
        import pathlib as _pathlib

        answers = _json.loads(
            (
                _pathlib.Path(__file__).resolve().parent.parent / "data" / "demo_personas.json"
            ).read_text()
        )["personas"]["maria"]["answers"]
        for field_id, value in answers.items():
            await store.save_field(session_id="sess-block", field_id=field_id, value=value)

        from api.band.tools import SessionTools

        tools = SessionTools("sess-block", _principal(), tmp_path)

        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        try:
            report = await tools.write_form()
        finally:
            beat.cancel()
        assert report.get("written"), f"the write must actually happen: {report}"
        return ticks

    ticks = asyncio.run(scenario())
    # A loop blocked for 300 ms gets ~0 ticks; a free one gets ~30.
    assert ticks > 10, (
        f"the event loop only ran {ticks} times during a {BLOCK}s PDF write -- "
        "it is blocked, and every concurrent caller is waiting on it"
    )

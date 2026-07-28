"""The running agents, and the room they collaborate in.

Each role is a real external agent, connected to Band over its own WebSocket
with its own credentials. They are not five function calls wearing agent names:
Band delivers a mention to a process, that process decides what to do, and it
addresses whichever peer should act next. The order the work happens in emerges
from that conversation rather than from a for-loop.

The loop is bounded, because an emergent conversation is also how you get two
agents politely handing a task back and forth forever. Every room has a turn
budget and a deadline, and when either runs out the collaboration is closed and
recorded as unfinished. That is a real outcome, not an error.

WHY IN-PROCESS

The agents run as asyncio tasks inside the API process. One service, one deploy,
one place to look when something is wrong. It also means the agents share the
session store they are reasoning about, so a tool call is a function call rather
than another network hop that can fail halfway.

WHAT IS DELIBERATELY NOT HERE

No agent writes a value. The tools they may call are in tools.py and the model
never authors an applicant's answer -- see brain.py for why that line is drawn
where it is.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from api.band import brain, ledger, protocol
from api.band.credentials import load_credentials
from api.band.roles import ROLES, Role, role_for, roster
from api.band.tools import SessionTools, ToolDenied, ToolFailed, schemas_for
from api.tenancy import Principal

logger = logging.getLogger(__name__)


#: How many finished collaborations stay in memory. Anything evicted is still
#: readable -- it went to the ledger on close -- so this is a cache size, not a
#: retention policy, and the retention policy is "durably, in the memory store".
MAX_ROOMS_IN_MEMORY = 32

#: A collaboration that has not finished in this many turns has stopped making
#: progress. Six agents each acting twice is a generous ceiling for one filing.
MAX_TURNS = 18
#: Wall-clock ceiling. The applicant has hung up, but a room held open forever
#: is a leak.
MAX_SECONDS = 300.0

#: Band tools the agents are offered, by their real schema name.
#:
#: These are `band_`-prefixed, which is not what /me/agents/{id}/tools calls
#: them -- that endpoint reports service names like
#: `list_chat_participants_service`. Filtering on those matched nothing, so the
#: agents were offered no Band tools at all while appearing to be.
#:
#: What is deliberately left out, and why:
#:   band_send_event      422s on this plan.
#:   band_*_memory        gated behind ff_memory, which is false on Free. Zuzu's
#:                        own memory tools cover the three tiers anyway.
#:   band_create_chatroom the fleet opens rooms; an agent opening another one
#:                        mid-filing splits the record in half.
#:   band_remove_participant  an agent that can remove a peer can end a
#:                        collaboration by accident.
#:   band_*_contact       a contact graph is an account-level concern, not
#:                        something to change while filling somebody's form.
BAND_TOOLS_OFFERED = {
    "band_send_message",
    "band_get_participants",
    "band_lookup_peers",
    "band_add_participant",
}


@dataclass
class Turn:
    """One thing one agent did, for the trail."""

    role: str
    agent_id: str
    said: str
    addressed: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    because: str = ""
    #: Which decided this turn: the model, or the deterministic fallback. A
    #: trail that does not distinguish them is making a claim it cannot support.
    reasoner: str = "minimax-m3"
    at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "agent_id": self.agent_id,
            "said": self.said,
            "addressed": self.addressed,
            "tool_calls": self.tool_calls,
            "because": self.because,
            "reasoner": self.reasoner,
            "at": self.at,
        }


@dataclass
class Collaboration:
    """One room, one filing, and everything that happened in it."""

    session_id: str
    room_id: str
    principal: Principal
    tools: SessionTools
    turns: list[Turn] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def expired(self) -> bool:
        return len(self.turns) >= MAX_TURNS or (time.time() - self.started) > MAX_SECONDS

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "room_id": self.room_id,
            "tenant_id": self.principal.tenant_id,
            "turns": [t.as_dict() for t in self.turns],
            "turn_count": len(self.turns),
            "seconds": round(time.time() - self.started, 2),
            "finished": self.finished.is_set(),
        }


class RoleAgent:
    """One Band-connected agent, thinking with MiniMax.

    The adapter is created lazily against the SDK so that importing this module
    does not require band-sdk to be installed -- the rest of Zuzu must keep
    working when the fleet is switched off.
    """

    def __init__(self, role: Role, agent_id: str, api_key: str, fleet: Fleet) -> None:
        self.role = role
        self.agent_id = agent_id
        self.client = protocol.BandAgentClient(api_key, agent_id)
        self._fleet = fleet
        self._api_key = api_key
        self._agent = None

    async def start(self) -> None:
        from band.agent import Agent, SimpleAdapter

        outer = self

        class Adapter(SimpleAdapter):
            async def on_message(
                self,
                msg,
                tools,
                history,
                participants_msg,
                contacts_msg,
                *,
                is_session_bootstrap: bool,
                room_id: str,
            ) -> None:
                await outer._handle(
                    str(getattr(msg, "content", msg) or ""), room_id, band_tools=tools
                )

        self._agent = Agent.create(adapter=Adapter(), agent_id=self.agent_id, api_key=self._api_key)
        await self._agent.start()
        logger.info("band agent up: %s", self.role.agent_name)

    async def stop(self) -> None:
        if self._agent is not None:
            try:
                await self._agent.stop()
            except Exception as exc:
                logger.info("agent %s stop: %s", self.role.key, type(exc).__name__)
            self._agent = None

    async def _handle(self, content: str, room_id: str, band_tools: Any = None) -> None:
        """A peer addressed this agent. Decide, act, and pass the work on."""
        collab = self._fleet.by_room.get(room_id)
        if collab is None or collab.finished.is_set():
            return
        if collab.expired:
            # Which budget ran out. `expired` is a disjunction, and this used
            # to say "turn budget exhausted" either way -- so a collaboration
            # that hit the 300s wall was recorded, permanently, as having used
            # up its turns. A caseworker reading that reaches for the wrong fix.
            spent = len(collab.turns)
            why = (
                f"turn budget exhausted after {spent} turns"
                if spent >= MAX_TURNS
                else f"time budget exhausted after {MAX_SECONDS:.0f}s"
            )
            await self._fleet.close(room_id, why)
            return

        transcript = "\n".join(f"{t.role}: {t.said}" for t in collab.turns[-8:]) or "(nothing yet)"
        context = (
            f"You are working on session {collab.session_id} for tenant "
            f"{collab.principal.tenant_id}.\n\n"
            f"The message addressed to you:\n{content}\n\n"
            f"What has been said so far:\n{transcript}"
        )

        # Band describes its own tools in OpenAI format, so the model can be
        # given Band's real vocabulary -- addressing a peer, pulling somebody
        # into the room, seeing who is here -- rather than a paraphrase of it.
        # Domain tools stay ours; nobody else knows how to fill an I-765.
        band_schemas: list[dict[str, Any]] = []
        if band_tools is not None:
            try:
                band_schemas = [
                    schema
                    for schema in band_tools.get_openai_tool_schemas(include_contacts=False)
                    if (schema.get("function") or {}).get("name") in BAND_TOOLS_OFFERED
                ]
            except Exception as exc:
                logger.info("band tool schemas unavailable: %s", type(exc).__name__)

        band_names = {(s.get("function") or {}).get("name") for s in band_schemas}

        async def run_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
            """Whatever the model asked for, if this agent is allowed it."""
            if name in band_names and band_tools is not None:
                try:
                    return {
                        "ok": True,
                        "result": str(await band_tools.execute_tool_call(name, args))[:800],
                    }
                except Exception as exc:
                    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            try:
                return {
                    "ok": True,
                    "result": await collab.tools.call(name, self.role.tools, **args),
                }
            except (ToolDenied, ToolFailed) as exc:
                return {"ok": False, "error": str(exc)}

        reasoner = brain.REASONER_MODEL
        try:
            decision = await brain.decide(
                self.role.system_prompt,
                context,
                roster=roster(),
                tools=[*schemas_for(self.role.tools), *band_schemas],
                execute=run_tool,
            )
            results = decision.ran
            if not decision.usable:
                reasoner = brain.REASONER_UNUSABLE
        except brain.BrainUnavailable as exc:
            # The model is judgement, not capability. Losing it costs the room
            # its reasoning and nothing else: the agent still runs its own tools
            # and still hands on, along the fixed pipeline order.
            logger.warning("%s could not think (%s); falling back", self.role.key, exc)
            reasoner = brain.REASONER_FALLBACK
            nxt = self._fleet.next_after(self.role.key)
            nxt_display = role_for(nxt).display if nxt else None
            decision = brain.fallback_decision(self.role.key, nxt_display, self.role.tools)
            # Only the tools that take no arguments. remember_fact and
            # learn_rule need the model to say WHICH field or WHAT rule, so
            # calling them argument-less stored nothing while the trail showed
            # a tool call that looked like it had. An absent call beats a
            # silent no-op recorded as "ok": true.
            from api.band.tools import TOOL_PARAMS

            results = [
                {"tool": name, **await run_tool(name, {})}
                for name in self.role.tools
                if not TOOL_PARAMS.get(name, {}).get("required")
            ]

        said = decision.say or f"{self.role.display} acted."
        if results:
            said = f"{said}\n[tools: {json.dumps(results, default=str)[:600]}]"

        collab.turns.append(
            Turn(
                role=self.role.key,
                agent_id=self.agent_id,
                said=said,
                addressed=decision.to,
                tool_calls=results,
                because=decision.because,
                reasoner=reasoner,
            )
        )

        targets = self._fleet.ids_for(decision.to)
        if targets and not decision.done:
            try:
                await self.client.say(room_id, said, targets)
            except protocol.BandError as exc:
                logger.warning("%s could not speak: %s", self.role.key, exc)
                await self._fleet.close(room_id, "a message could not be delivered")
            return

        # Either the agent said it was finished, or it named nobody -- which
        # amounts to the same thing, because a turn that addresses no one ends
        # the conversation. Both used to leave the room open until it timed out:
        # the Auditor ran its tool, failed to answer in the required shape, and
        # everyone waited three minutes for a deadline instead of closing.
        nxt = self._fleet.next_after(self.role.key)
        if nxt is None:
            await self._fleet.close(
                room_id,
                "auditor sealed the record"
                if self.role.key == "auditor"
                else "no agent left to act",
            )
            return
        try:
            await self.client.say(room_id, said, [self._fleet.agents[nxt].agent_id])
        except protocol.BandError as exc:
            logger.warning("%s could not hand on: %s", self.role.key, exc)
            await self._fleet.close(room_id, "a hand-off could not be delivered")


class Fleet:
    """Every role, connected, and the rooms they are working in."""

    def __init__(self) -> None:
        self.agents: dict[str, RoleAgent] = {}
        #: Bounded. A finished collaboration is already in the ledger, so the
        #: only thing evicting it costs is a dictionary lookup on the way to a
        #: database read. Unbounded, this held every Principal and SessionTools
        #: for the life of the process.
        self.by_room: dict[str, Collaboration] = {}
        self._started = False

    # ---- lifecycle -------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._started and bool(self.agents)

    async def start(self) -> bool:
        """Connect every agent. Returns False if the fleet cannot run.

        A missing credential or an unreachable Band is not fatal to Zuzu: the
        deterministic pipeline still fills forms. The fleet is orchestration and
        provenance, and losing it must never cost somebody their filing.
        """
        if self._started:
            return True
        if not brain.is_available():
            logger.info("band fleet not started: TOKENROUTER_API_KEY is not set")
            return False
        creds = load_credentials()
        missing = [r.key for r in ROLES if r.key not in creds]
        if missing:
            logger.info("band fleet not started: no credentials for %s", ", ".join(missing))
            return False
        try:
            for role in ROLES:
                agent = RoleAgent(role, creds[role.key]["id"], creds[role.key]["api_key"], self)
                await agent.start()
                self.agents[role.key] = agent
            await self._verify_identities()
        except Exception as exc:
            logger.warning("band fleet failed to start: %s: %s", type(exc).__name__, exc)
            await self.stop()
            return False
        self._started = True
        logger.info("band fleet running: %d agents", len(self.agents))
        return True

    async def _verify_identities(self) -> None:
        """Ask Band who each agent actually is, and say so if it disagrees.

        A credentials file that has drifted from the account is not a failure
        anything notices: the WebSocket connects, the room opens, and every
        audit entry is attributed to an agent id that means something else now.
        /agents once reported five of six roles unregistered for exactly this
        reason, and it took a live run to find out.

        Only ever a warning. A mismatch is worth knowing about; it is not worth
        refusing to fill somebody's form over.
        """
        for key, agent in self.agents.items():
            try:
                me = await agent.client.whoami()
            except Exception as exc:
                logger.warning("could not confirm identity of %s: %s", key, exc)
                continue
            data = me.get("data", me)
            claimed = str(data.get("id", ""))
            if claimed and claimed != agent.agent_id:
                logger.warning(
                    "credential for %s belongs to agent %s, not %s -- the audit trail will "
                    "attribute this role to the wrong agent",
                    key,
                    claimed,
                    agent.agent_id,
                )
            else:
                logger.info("%s connected as %s", key, data.get("name") or agent.agent_id)

    async def stop(self) -> None:
        for agent in list(self.agents.values()):
            await agent.stop()
        self.agents.clear()
        self._started = False

    # ---- roster helpers --------------------------------------------------

    def ids_for(self, displays: list[str]) -> list[str]:
        """Resolve display names an agent addressed into real agent ids."""
        out = []
        for display in displays:
            for role in ROLES:
                if role.display.lower() == display.lower() and role.key in self.agents:
                    out.append(self.agents[role.key].agent_id)
        return out

    def next_after(self, key: str) -> str | None:
        keys = [r.key for r in ROLES]
        try:
            index = keys.index(key)
        except ValueError:
            return None
        return keys[index + 1] if index + 1 < len(keys) else None

    # ---- collaboration ---------------------------------------------------

    async def collaborate(
        self, session_id: str, principal: Principal, out_dir: Path
    ) -> Collaboration | None:
        """Open a room, gather the agents, and let them work.

        Returns when the Auditor seals the record, or when the budget runs out.
        """
        if not self.is_running:
            return None

        # The Auditor opens the room and hands the work to Intake. It has to be
        # somebody other than the first worker: Band rejects a message whose
        # only mention is its own sender, and the Auditor opening the file is
        # also just true -- it owns the record from before the first question.
        opener = self.agents["auditor"]
        title = f"Zuzu {principal.tenant_id} · {session_id}"
        room_id = await opener.client.open_room(title)

        collab = Collaboration(
            session_id=session_id,
            room_id=room_id,
            principal=principal,
            tools=SessionTools(session_id, principal, out_dir),
        )
        self.by_room[room_id] = collab

        # Everything from here to the opening message can raise: invite is a
        # network call and protocol._raise_for turns any status >= 300 into a
        # BandError. Unguarded, the exception escaped with the room already
        # registered and never finished -- so it was never persisted, never
        # closed, and never evicted (eviction only considers finished rooms).
        # A Band outage therefore leaked one Collaboration, with its Principal
        # and SessionTools, per attempt, forever.
        try:
            for key, agent in self.agents.items():
                if key != "auditor":
                    await opener.client.invite(room_id, agent.agent_id)

            state = await collab.tools.session_state()
            await opener.client.say(
                room_id,
                (
                    f"Opening the file for {state['form_id']}: {state['answered']} answers "
                    f"collected, {state['remaining']} still missing. Intake, take it from here -- "
                    "establish whether the interview is complete, then hand on."
                ),
                [self.agents["intake"].agent_id],
            )
        except Exception as exc:
            logger.warning("could not open the room for %s: %s", session_id, exc)
            await self.close(room_id, f"the room could not be opened: {exc}")
            return collab

        try:
            await asyncio.wait_for(collab.finished.wait(), timeout=MAX_SECONDS)
        except TimeoutError:
            await self.close(room_id, "collaboration timed out")
        return collab

    async def room_transcript(self, room_id: str) -> list[dict[str, Any]]:
        """The whole conversation, as Band recorded it.

        Band scopes an agent's context to what it sent or was mentioned in, so
        no single agent can see the room. Six partial views, unioned by message
        id, is the whole of it -- and the mention-only scoping is exactly why
        that works: every message has a sender and at least one mention, so it
        appears in at least two views.

        Errors from one agent are skipped rather than fatal: five sixths of an
        independent record still beats none.
        """
        seen: dict[str, dict[str, Any]] = {}
        for key, agent in self.agents.items():
            try:
                for message in await agent.client.transcript(room_id):
                    message_id = str(message.get("id") or "")
                    if message_id and message_id not in seen:
                        seen[message_id] = message
            except Exception as exc:
                logger.warning("could not read %s's view of room %s: %s", key, room_id, exc)
        return sorted(seen.values(), key=lambda m: str(m.get("inserted_at") or ""))

    async def close(self, room_id: str, why: str) -> None:
        collab = self.by_room.get(room_id)
        if collab is None or collab.finished.is_set():
            return
        logger.info("collaboration closed room=%s turns=%d: %s", room_id, len(collab.turns), why)
        collab.turns.append(
            Turn(
                role="fleet",
                agent_id="",
                said=f"Collaboration closed: {why}.",
                because=why,
                # Not a decision anyone reasoned to; the runtime ended the room.
                reasoner="runtime",
            )
        )
        # Written down before the waiter is released, so a caller that gets its
        # response and immediately asks for the trail finds one. This is also
        # the point where the record stops depending on this process staying up.
        kept = await ledger.record(collab)
        logger.info("audit trail persisted room=%s turns=%d", room_id, kept)
        collab.finished.set()
        self._evict()

    def _evict(self) -> None:
        """Keep only the most recent finished rooms in memory."""
        finished = [(c.started, rid) for rid, c in self.by_room.items() if c.finished.is_set()]
        if len(finished) <= MAX_ROOMS_IN_MEMORY:
            return
        for _, room_id in sorted(finished)[: len(finished) - MAX_ROOMS_IN_MEMORY]:
            self.by_room.pop(room_id, None)


_fleet: Fleet | None = None


def get_fleet() -> Fleet:
    global _fleet
    if _fleet is None:
        _fleet = Fleet()
    return _fleet

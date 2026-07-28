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

from api.band import brain, protocol
from api.band.credentials import load_credentials
from api.band.roles import ROLES, Role, role_for, roster
from api.band.tools import SessionTools, ToolDenied, ToolFailed, schemas_for
from api.tenancy import Principal

logger = logging.getLogger(__name__)


#: A collaboration that has not finished in this many turns has stopped making
#: progress. Six agents each acting twice is a generous ceiling for one filing.
MAX_TURNS = 18
#: Wall-clock ceiling. The applicant has hung up, but a room held open forever
#: is a leak.
MAX_SECONDS = 180.0


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
                await outer._handle(str(getattr(msg, "content", msg) or ""), room_id)

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

    async def _handle(self, content: str, room_id: str) -> None:
        """A peer addressed this agent. Decide, act, and pass the work on."""
        collab = self._fleet.by_room.get(room_id)
        if collab is None or collab.finished.is_set():
            return
        if collab.expired:
            await self._fleet.close(room_id, "turn budget exhausted")
            return

        transcript = "\n".join(f"{t.role}: {t.said}" for t in collab.turns[-8:]) or "(nothing yet)"
        context = (
            f"You are working on session {collab.session_id} for tenant "
            f"{collab.principal.tenant_id}.\n\n"
            f"The message addressed to you:\n{content}\n\n"
            f"What has been said so far:\n{transcript}"
        )

        reasoner = brain.REASONER_MODEL
        try:
            decision = await brain.decide(
                self.role.system_prompt,
                context,
                roster=roster(),
                tools=schemas_for(self.role.tools),
            )
        except brain.BrainUnavailable as exc:
            # The model is judgement, not capability. Losing it costs the room
            # its reasoning and nothing else: the agent still runs its own tools
            # and still hands on, along the fixed pipeline order.
            logger.warning("%s could not think (%s); falling back", self.role.key, exc)
            reasoner = brain.REASONER_FALLBACK
            nxt = self._fleet.next_after(self.role.key)
            nxt_display = role_for(nxt).display if nxt else None
            decision = brain.fallback_decision(self.role.key, nxt_display, self.role.tools)

        results: list[dict[str, Any]] = []
        for call in decision.calls[:3]:
            name = str(call.get("tool", ""))
            try:
                result = await collab.tools.call(name, self.role.tools)
                results.append({"tool": name, "ok": True, "result": result})
            except (ToolDenied, ToolFailed) as exc:
                results.append({"tool": name, "ok": False, "error": str(exc)})

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

        if decision.done and self.role.key == "auditor":
            await self._fleet.close(room_id, "auditor sealed the record")
        elif decision.done:
            # Somebody finished their part without naming a successor. The
            # pipeline order is the fallback, so the work does not simply stop.
            nxt = self._fleet.next_after(self.role.key)
            if nxt is None:
                await self._fleet.close(room_id, "no agent left to act")
            else:
                await self.client.say(room_id, said, [self._fleet.agents[nxt].agent_id])


class Fleet:
    """Every role, connected, and the rooms they are working in."""

    def __init__(self) -> None:
        self.agents: dict[str, RoleAgent] = {}
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
        except Exception as exc:
            logger.warning("band fleet failed to start: %s: %s", type(exc).__name__, exc)
            await self.stop()
            return False
        self._started = True
        logger.info("band fleet running: %d agents", len(self.agents))
        return True

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

        try:
            await asyncio.wait_for(collab.finished.wait(), timeout=MAX_SECONDS)
        except TimeoutError:
            await self.close(room_id, "collaboration timed out")
        return collab

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
        collab.finished.set()


_fleet: Fleet | None = None


def get_fleet() -> Fleet:
    global _fleet
    if _fleet is None:
        _fleet = Fleet()
    return _fleet

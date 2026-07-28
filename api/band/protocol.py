"""Band's agent-side REST API, as verified against the live service.

Band has two API surfaces and they are not the same thing:

    /api/v1/me/*      the Human API -- what a person's key can do. Creating and
                      reading chats through it requires an Enterprise plan, so
                      nothing here uses it beyond registering agents.

    /api/v1/agent/*   the Agent API -- what an agent's own key can do. This one
                      works on the free tier, and it is the whole basis of the
                      orchestration: an agent can open a room, pull other agents
                      into it, and address them.

Every shape below was established by calling the live API and reading what it
rejected, because the published bootstrap guide documents provisioning rather
than the wire format. The two that are easy to get wrong:

    participants   {"participant": {"participant_id": "<uuid>"}}
                   Not `agent_id`, not `handle` -- a UUID under that exact key.

    messages       {"message": {"content": ..., "mentions": [{"id": "<uuid>",
                                 "type": "Agent"}]}}
                   `mentions` is required and must hold at least one entry.
                   There is no such thing as an unaddressed agent message: the
                   mention IS the routing, which is why agent-to-agent delegation
                   works at all.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BAND_REST_URL = os.environ.get("BAND_REST_URL", "https://app.band.ai")
API = f"{BAND_REST_URL}/api/v1"

#: Band is a coordination surface, never the critical path of a call. Every
#: call here is bounded so a slow platform degrades the trail, not the filing.
TIMEOUT = 20.0


class BandError(RuntimeError):
    """Band refused something, with what it said."""


def _raise_for(resp: httpx.Response, what: str) -> Any:
    if resp.status_code >= 300:
        detail = resp.text[:300]
        raise BandError(f"{what} failed: HTTP {resp.status_code} {detail}")
    return resp.json() if resp.content else {}


class BandAgentClient:
    """One agent's own view of Band, authenticated as that agent."""

    def __init__(self, api_key: str, agent_id: str) -> None:
        self._key = api_key
        self.agent_id = agent_id

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._key, "Content-Type": "application/json"}

    async def whoami(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            return _raise_for(await c.get(f"{API}/agent/me", headers=self._headers), "whoami")

    async def open_room(self, title: str) -> str:
        """Open a room for one piece of work and return its id."""
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            body = _raise_for(
                await c.post(
                    f"{API}/agent/chats", headers=self._headers, json={"chat": {"title": title}}
                ),
                "open room",
            )
        return body["data"]["id"]

    async def invite(self, room_id: str, participant_id: str) -> dict[str, Any]:
        """Pull another agent into the room. Idempotent enough to retry."""
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            resp = await c.post(
                f"{API}/agent/chats/{room_id}/participants",
                headers=self._headers,
                json={"participant": {"participant_id": participant_id}},
            )
        if resp.status_code == 422 and "already" in resp.text.lower():
            return {"already_present": True}
        return _raise_for(resp, "invite")

    async def participants(self, room_id: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            body = _raise_for(
                await c.get(f"{API}/agent/chats/{room_id}/participants", headers=self._headers),
                "participants",
            )
        return body.get("data", [])

    async def say(self, room_id: str, content: str, to: list[str]) -> dict[str, Any]:
        """Address one or more agents in the room.

        `to` is a list of agent UUIDs. Band requires at least one, and that is
        the point rather than a nuisance: a message with no recipient would be
        an agent talking to nobody, which is what a log line is for.
        """
        if not to:
            raise BandError("a Band message must address at least one participant")
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            return _raise_for(
                await c.post(
                    f"{API}/agent/chats/{room_id}/messages",
                    headers=self._headers,
                    json={
                        "message": {
                            "content": content,
                            "mentions": [{"id": agent_id, "type": "Agent"} for agent_id in to],
                        }
                    },
                ),
                "say",
            )

    async def transcript(self, room_id: str, page_size: int = 100) -> list[dict[str, Any]]:
        """This agent's view of the room, oldest first.

        `/context`, not `/messages`. The messages route answers 200 with an
        empty list for an agent key -- no error, nothing to notice -- which is
        the worst way for an endpoint to be wrong: it looked exactly like a room
        where nothing had been said.

        Band scopes this to what the agent sent or was mentioned in, which is
        the right default for an agent rebuilding its own state and the wrong
        one for reading a whole conversation. `Fleet.room_transcript` unions the
        six views to get that.
        """
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            body = _raise_for(
                await c.get(
                    f"{API}/agent/chats/{room_id}/context",
                    headers=self._headers,
                    params={"page_size": page_size},
                ),
                "transcript",
            )
        return body.get("data") or []


async def register_agent(user_api_key: str, name: str, description: str) -> dict[str, str]:
    """Create an external agent and return its id and its own key.

    The returned key is the only time Band shows it. Registration is a
    build-time act, not something the running service does.
    """
    async with httpx.AsyncClient(timeout=40.0) as c:
        body = _raise_for(
            await c.post(
                f"{API}/me/agents/register",
                headers={"X-API-Key": user_api_key, "Content-Type": "application/json"},
                json={"agent": {"name": name, "description": description}},
            ),
            f"register {name}",
        )
    data = body["data"]
    return {"id": data["agent"]["id"], "api_key": data["credentials"]["api_key"]}


async def list_registered(user_api_key: str) -> list[dict[str, Any]]:
    """Every agent registered under this account."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        body = _raise_for(
            await c.get(f"{API}/me/agents", headers={"X-API-Key": user_api_key}),
            "list agents",
        )
    return body.get("data", [])


async def delete_agent(user_api_key: str, agent_id: str) -> bool:
    """Remove an agent. Used to clean up probes, not in the running service."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        resp = await c.delete(f"{API}/me/agents/{agent_id}", headers={"X-API-Key": user_api_key})
    return resp.status_code < 300

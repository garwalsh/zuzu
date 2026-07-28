"""What an agent thinks with: MiniMax-M3, reached through TokenRouter.

TokenRouter is an OpenAI-compatible gateway, so the model is reached with the
ordinary chat-completions shape and tool-calling works unchanged. That matters
because Band hands out OpenAI-format tool schemas for its own agent tools, so
the model can be given "here is how you talk to the other agents" without any
translation layer in between.

WHAT THE MODEL IS AND IS NOT ALLOWED TO DO

This is an immigration filing. The division is deliberate and it is the most
important thing in this module:

    The model decides what to DO      -- which agent should act next, whether a
                                        finding is worth escalating, what to say
                                        to a peer, when the work is finished.

    Code decides what is TRUE         -- every field value, every validation
                                        outcome, every byte written to the PDF.

So the model plans, delegates and explains. It never authors an applicant's
answer. A hallucinated delegation wastes a round trip; a hallucinated date of
birth is a rejected filing months later, and the applicant is the one who pays
for it.

MiniMax also emits a `<think>` block and, on truncation, an unclosed one. It is
stripped here rather than at each call site, because a reasoning block leaking
into a room is how a transcript stops being readable.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TOKENROUTER_BASE_URL = os.environ.get("TOKENROUTER_BASE_URL", "https://api.tokenrouter.com/v1")
TOKENROUTER_MODEL = os.environ.get("TOKENROUTER_MODEL", "MiniMax-M3")

#: An agent deciding what to do next is not on a human's latency budget -- the
#: applicant has hung up -- but a wedged call must not hold a room open forever.
TIMEOUT = 90.0
MAX_TOKENS = 1600

_THINK = re.compile(r"<think>.*?</think>", re.S)
_THINK_OPEN = re.compile(r"<think>.*$", re.S)


class BrainUnavailable(RuntimeError):
    """The model could not be reached, or returned nothing usable."""


@dataclass
class Decision:
    """What an agent decided to do this turn."""

    #: What it wants said in the room, already free of reasoning blocks.
    say: str = ""
    #: Agent role names it wants to address. Resolved to ids by the caller,
    #: which is what stops the model inventing a participant.
    to: list[str] = field(default_factory=list)
    #: Tool calls it wants run. Names are checked against a whitelist before
    #: anything executes.
    calls: list[dict[str, Any]] = field(default_factory=list)
    #: Whether this agent considers its part done.
    done: bool = False
    #: Free-text reason, kept for the audit trail.
    because: str = ""


def strip_reasoning(text: str) -> str:
    """Remove MiniMax's thinking, including a block truncation left open."""
    text = _THINK.sub("", text or "")
    text = _THINK_OPEN.sub("", text)
    return text.strip()


def is_available() -> bool:
    return bool(os.environ.get("TOKENROUTER_API_KEY"))


async def think(
    system: str,
    messages: list[dict[str, str]],
    *,
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """One turn of reasoning. Raises rather than returning something invented."""
    key = os.environ.get("TOKENROUTER_API_KEY", "")
    if not key:
        raise BrainUnavailable("TOKENROUTER_API_KEY is not set")

    payload: dict[str, Any] = {
        "model": TOKENROUTER_MODEL,
        "messages": [{"role": "system", "content": system}, *messages],
        "max_tokens": MAX_TOKENS,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                f"{TOKENROUTER_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        raise BrainUnavailable(
            f"{TOKENROUTER_MODEL} refused: HTTP {exc.response.status_code} "
            f"{exc.response.text[:200]}"
        ) from exc
    except Exception as exc:
        raise BrainUnavailable(f"{TOKENROUTER_MODEL} unreachable: {type(exc).__name__}") from exc

    # Parsed inside the same failure contract as the request: a gateway out of
    # credit answers 200 with an error body and no choices, and reaching into
    # that outside the guard turns a billing problem into a crash.
    try:
        return data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        detail = str(data.get("error") or data)[:200] if isinstance(data, dict) else str(data)[:200]
        raise BrainUnavailable(f"{TOKENROUTER_MODEL} returned no completion: {detail}") from exc


#: The shape every agent is asked to answer in. Kept small on purpose: a bigger
#: schema is a bigger surface for the model to get wrong, and everything that
#: actually matters is executed by code anyway.
DECISION_SCHEMA = """Reply with ONE JSON object and nothing else:

{
  "say":     "what you want the other agents to read, one or two sentences",
  "to":      ["role names you are addressing, from the roster you were given"],
  "calls":   [{"tool": "<tool name>", "args": {...}}],
  "done":    true or false,
  "because": "one short sentence on why you decided this"
}

Rules that do not bend:
- Address someone real. Only use role names from the roster.
- Never state an applicant's answer as fact unless a tool result gave it to you.
- If a tool can establish something, call the tool rather than assuming it.
- Set done to true only when your own part is genuinely finished."""


#: How a decision was reached. Carried into the audit trail because "the model
#: chose this" and "the fallback chose this" are different claims, and a trail
#: that blurs them is worse than no trail.
REASONER_MODEL = "minimax-m3"
REASONER_FALLBACK = "deterministic-fallback"


def fallback_decision(
    role_key: str, next_role: str | None, tool_names: tuple[str, ...]
) -> Decision:
    """What an agent does when the model cannot be reached.

    This is not a stand-in for reasoning and is never labelled as one. It runs
    each agent's own tools and hands to the next role in the fixed order, which
    is exactly the deterministic pipeline Zuzu had before any of this -- so a
    dead model key costs the orchestration its judgement, not the applicant
    their filing.
    """
    return Decision(
        say=f"{role_key} acted without the model; deterministic hand-off.",
        to=[next_role] if next_role else [],
        calls=[{"tool": name} for name in tool_names],
        done=next_role is None,
        because="the model was unavailable, so the fixed pipeline order was used",
    )


async def decide(
    system: str,
    context: str,
    *,
    roster: list[str],
    tools: list[dict[str, Any]] | None = None,
) -> Decision:
    """Ask an agent what it wants to do, and refuse anything malformed.

    A model that answers with prose instead of JSON, or addresses an agent that
    is not in the room, is treated as having decided nothing. Silence is a safe
    failure here: the orchestrator moves on deterministically.
    """
    prompt = f"{context}\n\nThe agents you can address: {', '.join(roster)}\n\n{DECISION_SCHEMA}"
    message = await think(system, [{"role": "user", "content": prompt}], tools=tools)

    raw = strip_reasoning(message.get("content") or "")
    parsed = _extract_json(raw)
    if parsed is None:
        logger.info("agent returned no usable decision: %r", raw[:160])
        return Decision(because="the model did not answer in the required shape")

    # Only names actually in the room survive. This is the guard that keeps a
    # hallucinated peer from becoming a dropped message.
    wanted = parsed.get("to") or []
    if isinstance(wanted, str):
        wanted = [wanted]
    known = {r.lower(): r for r in roster}
    addressed = [known[str(w).lower()] for w in wanted if str(w).lower() in known]

    calls = parsed.get("calls") or []
    if not isinstance(calls, list):
        calls = []

    return Decision(
        say=str(parsed.get("say") or "").strip(),
        to=addressed,
        calls=[c for c in calls if isinstance(c, dict) and c.get("tool")],
        done=bool(parsed.get("done")),
        because=str(parsed.get("because") or "").strip(),
    )


def _extract_json(text: str) -> dict[str, Any] | None:
    """The first JSON object in the text, tolerant of fences and stray prose."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return value if isinstance(value, dict) else None
    return None

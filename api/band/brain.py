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
    #: Tool results already gathered this turn. The agent has seen these; they
    #: are here so the audit trail can show what it looked at before deciding.
    ran: list[dict[str, Any]] = field(default_factory=list)
    #: Whether this agent considers its part done.
    done: bool = False
    #: Whether the model produced something actionable at all.
    usable: bool = True
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
  "done":    true or false,
  "because": "one short sentence on why you decided this"
}

Rules that do not bend:
- Address someone real. Only use role names from the roster.
- Never state an applicant's answer as fact unless a tool result gave it to you.
- Call tools directly when you need a fact; do not describe calling them.
- Set done to true only when your own part is genuinely finished."""


#: How a decision was reached. Carried into the audit trail because "the model
#: chose this" and "the fallback chose this" are different claims, and a trail
#: that blurs them is worse than no trail.
REASONER_MODEL = "minimax-m3"
REASONER_FALLBACK = "deterministic-fallback"
#: The model was reached and said something, but not in a shape that could be
#: acted on. Distinct from both of the above: the turn was neither reasoned nor
#: deterministically chosen, it was salvaged. A trail that files this under
#: "minimax-m3" is claiming a decision the model did not make.
REASONER_UNUSABLE = "model-answered-unusably"


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
        done=next_role is None,
        because="the model was unavailable, so the fixed pipeline order was used",
    )


#: How many times an agent may call tools before it has to commit to a decision.
#: Each round is a model round trip, and an agent that has looked twice
#: and still cannot say what it wants is not going to on the third, and each
#: round is a round trip the applicant is not waiting on but the room is.
MAX_TOOL_ROUNDS = 3

#: How many of a round's tool calls are executed. A model that asks for twenty
#: things at once is not being helped by all twenty.
MAX_CALLS_PER_ROUND = 4


async def decide(
    system: str,
    context: str,
    *,
    roster: list[str],
    tools: list[dict[str, Any]] | None = None,
    execute: Any = None,
) -> Decision:
    """Let an agent look things up, then commit to what it wants to do.

    MiniMax answers a tool-bearing prompt the OpenAI way: `content` holds only
    the reasoning block and the real intent is in `tool_calls`. Reading `content`
    alone therefore saw an agent that had decided nothing, when in fact it had
    asked two perfectly sensible questions -- so the room stalled on its first
    turn and timed out.

    The loop is the fix and it is also what makes these agents worth calling
    agents: look, get answers back, then decide in light of them. Tool results
    are appended as `tool` messages, which is how the model sees what it learned.

    Anything malformed at the end is treated as having decided nothing. Silence
    is a safe failure here -- the caller falls back to the fixed pipeline order,
    and no applicant's answer depends on this.
    """
    prompt = f"{context}\n\nThe agents you can address: {', '.join(roster)}\n\n{DECISION_SCHEMA}"
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    ran: list[dict[str, Any]] = []

    for round_number in range(MAX_TOOL_ROUNDS):
        last_round = round_number == MAX_TOOL_ROUNDS - 1
        message = await think(system, messages, tools=tools)
        calls = message.get("tool_calls") or []

        if not calls:
            break

        # Record the assistant turn verbatim: an OpenAI-shaped exchange is only
        # valid if every tool result answers a call the assistant actually made.
        # Truncate BEFORE announcing them. Appending the full list and then
        # executing only four leaves an assistant turn claiming N tool calls
        # with four replies -- exactly the invariant this block exists to keep,
        # violated by the block itself. The gateway then rejects the next turn.
        calls = calls[:MAX_CALLS_PER_ROUND]
        messages.append(
            {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": calls,
            }
        )
        for call in calls:
            name = (call.get("function") or {}).get("name", "")
            try:
                args = json.loads((call.get("function") or {}).get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if execute is None:
                result: dict[str, Any] = {"error": "no tool runner available"}
            else:
                result = await execute(name, args)
            ran.append({"tool": name, **result})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": json.dumps(result, default=str)[:2000],
                }
            )
        # Only push for a decision on the final round. Nudging after every round
        # is what made an agent's first tool call also its last: the Extractor
        # looked at the collected values and was then told to stop, so it never
        # got to keep any of them. Multi-step work needs a second turn.
        messages.append(
            {
                "role": "user",
                "content": (
                    "You have the tool results above. Now reply with ONLY the decision "
                    "JSON described earlier. Do not call any more tools."
                    if last_round
                    else "You have the tool results above. Call another tool if your job "
                    "needs one, otherwise reply with the decision JSON."
                ),
            }
        )
    else:
        message = await think(system, messages, tools=None)

    raw = strip_reasoning(message.get("content") or "")
    parsed = _extract_json(raw)
    if parsed is None:
        logger.info("agent returned no usable decision: %r", raw[:160])
        return Decision(
            ran=ran,
            usable=False,
            because="the model did not answer in the required shape",
        )

    # Only names actually in the room survive. This is the guard that keeps a
    # hallucinated peer from becoming a dropped message.
    wanted = parsed.get("to") or []
    if isinstance(wanted, str):
        wanted = [wanted]
    known = {r.lower(): r for r in roster}
    addressed = [known[str(w).lower()] for w in wanted if str(w).lower() in known]

    return Decision(
        say=str(parsed.get("say") or "").strip(),
        to=addressed,
        ran=ran,
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

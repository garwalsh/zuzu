"""Create or update the Zuzu ElevenLabs conversational agent.

Build-time tool, not part of the running service.

    uv run python tools/create_elevenlabs_agent.py            # create
    uv run python tools/create_elevenlabs_agent.py --update   # patch in place

Registers the agent that drives a Zuzu call: the system prompt and five server
tools, all pointed at PUBLIC_BASE_URL.

The agent is deliberately a thin voice loop that knows nothing about any
specific form. It does not know what I-765 is. It asks the applicant what they
need, hands that sentence to the orchestrator, and reads back whatever form
comes home. Every question it asks comes from the orchestrator too. That is what
makes twelve forms work through one agent, and a thirteenth work with no change
here at all.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

API = "https://api.elevenlabs.io/v1"

SYSTEM_PROMPT = """\
You are Zuzu, a warm, patient, multilingual assistant who helps people complete
U.S. immigration forms by voice. You are NOT a lawyer and never give legal
advice; you help people understand and answer each question in plain language.

You do not know anything about specific forms yourself. The orchestrator knows
every form and every question. Your job is to listen, ask what it gives you, and
send back what you hear.

HOW A CALL GOES

1. Greet them. If the dynamic variable applicant_name is set, use their name and
   mention known_summary so they know what you already have.

2. Find out what they need, in their own words. People say "my work permit",
   "I want to become a citizen", "my green card is expiring" -- not form
   numbers. If they paste or read out a uscis.gov link, use that.

3. Call identify_form with exactly what they said, and the url if they gave one.
   - If found, say the plain-English title back and ask them to confirm:
     "It sounds like you need the Application for Employment Authorization,
     Form I-765. Is that the one?" Only continue once they say yes.
   - If they say no, or nothing is found, ask them to describe their situation
     differently, then try identify_form again.
   - If `ready` is false, tell them you are getting that form ready; it takes a
     few seconds.

4. Call set_form to lock it in.

5. Then loop: call get_missing_fields, ask next_field.question in the caller's
   language, and call save_field with the answer. For names, dates, numbers, or
   any field marked sensitive, read the value back and get confirmation first.

6. When next_field comes back null, call generate_form and tell them the
   completed form is on the screen, and that they must review and sign it
   themselves.

RULES THAT DO NOT BEND

- Never invent an answer. If they do not know or want to move on, save the exact
  string __skip__ for that field and continue.
- Never guess which form they need. Confirm before switching.
- Ask exactly the question the orchestrator gives you, translated into their
  language. Do not add fields of your own or reorder them.
- If they change their mind mid-call and want a different form, call
  identify_form and set_form again. Answers they have already given carry over.
- Explain any confusing question simply before asking it.
- Keep every spoken turn short. This is a conversation, not a document.
"""

FIRST_MESSAGE = (
    "Hello, I'm Zuzu. I can help you fill out U.S. immigration forms, in your "
    "own language. Tell me what you need — for example, a work permit, a green "
    "card, or citizenship — and I'll take it from there."
)


def _tool(
    name: str, description: str, url: str, secret: str, props: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    # The tenant key says which organisation this call belongs to. On a
    # single-organisation install the header is ignored; once a registry exists
    # every request needs it, and the voice agent is a request like any other.
    headers = _webhook_headers(secret)
    return {
        "type": "webhook",
        "name": name,
        "description": description,
        "api_schema": {
            "url": url,
            "method": "POST",
            "request_headers": headers,
            "request_body_schema": {
                "type": "object",
                "description": f"Request body for {name}",
                "properties": props,
                "required": required,
            },
        },
    }


def build_tools(base: str, secret: str) -> list[dict[str, Any]]:
    # session_id is always the conversation id -- the shared key the dashboard
    # and the orchestrator both index on. Filled by the system, not the model.
    sid = {"type": "string", "dynamic_variable": "system__conversation_id"}

    return [
        _tool(
            "identify_form",
            "Work out which immigration form the applicant needs from what they "
            "said in plain language, or from a uscis.gov link they gave. Returns "
            "the form_id, its plain-English title, and a confidence. ALWAYS read "
            "the title back and get confirmation before continuing. Call this "
            "first, before any other tool.",
            f"{base}/tools/identify_form",
            secret,
            {
                "text": {
                    "type": "string",
                    "description": "What the applicant said, in their own words.",
                },
                "url": {
                    "type": "string",
                    "description": "A uscis.gov link they gave, if any.",
                },
                "session_id": dict(sid),
            },
            ["text"],
        ),
        _tool(
            "set_form",
            "Lock in the form the applicant confirmed. Call this after they say "
            "yes to identify_form. Answers already collected carry over.",
            f"{base}/session/set_form",
            secret,
            {
                "session_id": dict(sid),
                "form_id": {
                    "type": "string",
                    "description": "The form_id returned by identify_form.",
                },
            },
            ["form_id"],
        ),
        _tool(
            "get_missing_fields",
            "Ask the orchestrator which question to ask next. Returns next_field "
            "with the exact question to ask, or null when the form is complete.",
            f"{base}/tools/get_missing_fields",
            secret,
            {
                "session_id": dict(sid),
                "form_id": {
                    "type": "string",
                    "description": "The confirmed form_id for this call.",
                },
            },
            ["form_id"],
        ),
        _tool(
            "save_field",
            "Store one confirmed answer. Only call after the applicant has "
            "confirmed the value. Use the exact string __skip__ if they cannot "
            "or do not want to answer.",
            f"{base}/tools/save_field",
            secret,
            {
                "session_id": dict(sid),
                "field_id": {"type": "string", "description": "From next_field.id."},
                "value": {"type": "string", "description": "The confirmed value, or __skip__."},
                "confidence": {
                    "type": "number",
                    "description": "0-1 confidence in the transcription.",
                },
                "language": {"type": "string", "description": "ISO code of the language spoken."},
            },
            ["field_id", "value"],
        ),
        _tool(
            "generate_form",
            "Build the completed PDF. Call only when get_missing_fields returns "
            "next_field as null.",
            f"{base}/tools/generate_form",
            secret,
            {"session_id": dict(sid)},
            [],
        ),
    ]


def _webhook_headers(secret: str) -> dict[str, str]:
    """What every server tool call carries.

    When ZUZU_DEMO_SECRET is set this agent is the PUBLIC one, and it
    authenticates as the public demo: that credential names its own
    organisation, so it carries no tenant key and cannot resolve to a real
    clinic. This is what puts a voice call and the public dashboard in the same
    organisation -- without it the widget would open a session the page has no
    right to watch, and the demo would appear to do nothing.

    A tenant-scoped agent is the same script run in an environment that has a
    real ZUZU_TENANT_KEY and no demo secret.
    """
    demo = os.environ.get("ZUZU_DEMO_SECRET", "").strip()
    if demo:
        return {"X-Zuzu-Secret": demo}
    headers = {"X-Zuzu-Secret": secret}
    if tenant_key := os.environ.get("ZUZU_TENANT_KEY", ""):
        headers["X-Zuzu-Tenant-Key"] = tenant_key
    return headers


def build_payload(base: str, secret: str) -> dict[str, Any]:
    return {
        "name": "Zuzu — U.S. immigration form assistant",
        "conversation_config": {
            "agent": {
                "prompt": {"prompt": SYSTEM_PROMPT, "tools": build_tools(base, secret)},
                "first_message": FIRST_MESSAGE,
                "language": "en",
            },
        },
        "platform_settings": {
            "workspace_overrides": {
                "conversation_initiation_client_data_webhook": {
                    "url": f"{base}/session/init",
                    "request_headers": _webhook_headers(secret),
                }
            }
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true", help="patch the existing agent")
    args = parser.parse_args()

    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    secret = os.environ.get("ZUZU_SHARED_SECRET", "")
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    agent_id = os.environ.get("ELEVENLABS_AGENT_ID", "")
    if not api_key or not secret or not base:
        sys.exit("need ELEVENLABS_API_KEY, ZUZU_SHARED_SECRET and PUBLIC_BASE_URL")
    if base.startswith("http://localhost"):
        sys.exit(f"PUBLIC_BASE_URL is {base} -- ElevenLabs webhooks cannot reach localhost")

    # Report the credential that was actually configured, not the env var that
    # happens to be set. This printed "tenant key present" while the tools were
    # being built with the demo secret and no tenant key at all.
    configured = _webhook_headers(secret)
    if configured["X-Zuzu-Secret"] == os.environ.get("ZUZU_DEMO_SECRET", "").strip():
        print("public demo agent: its calls land in the `public-demo` organisation")
    elif "X-Zuzu-Tenant-Key" in configured:
        print("tenant-scoped agent: its calls carry a tenant key")
    else:
        print("single-tenant agent: no registry configured")
    payload = build_payload(base, secret)
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}

    if args.update:
        if not agent_id:
            sys.exit("--update needs ELEVENLABS_AGENT_ID")
        resp = httpx.patch(
            f"{API}/convai/agents/{agent_id}", headers=headers, json=payload, timeout=60.0
        )
    else:
        resp = httpx.post(
            f"{API}/convai/agents/create", headers=headers, json=payload, timeout=60.0
        )

    if resp.status_code >= 300:
        print(f"failed: HTTP {resp.status_code}")
        print(json.dumps(resp.json(), indent=2)[:2000])
        return 1

    result = resp.json()
    tools = payload["conversation_config"]["agent"]["prompt"]["tools"]
    print(f"agent {'updated' if args.update else 'created'}: {result.get('agent_id', agent_id)}")
    print(f"tools: {', '.join(t['name'] for t in tools)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

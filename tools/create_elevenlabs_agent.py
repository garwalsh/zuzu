"""Create (or update) the Zuzu ElevenLabs conversational agent.

Build-time tool, not part of the running service. It registers the agent that
drives a Zuzu call: the Appendix A system prompt, the three server tools, and
the conversation-initiation / post-call webhooks -- all pointed at PUBLIC_BASE_URL.

    uv run python tools/create_elevenlabs_agent.py

Reads ELEVENLABS_API_KEY, ZUZU_SHARED_SECRET, and PUBLIC_BASE_URL from the
environment. Prints the agent id to add to .env as ELEVENLABS_AGENT_ID.

The agent is deliberately a thin voice loop: it asks whatever question the
orchestrator hands it and reports back. All the decisions about what to ask,
what to store, and when the form is done live in the orchestrator.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

API = "https://api.elevenlabs.io/v1"

SYSTEM_PROMPT = """\
You are Zuzu, a warm, patient, multilingual assistant that helps people complete
USCIS immigration forms by voice. You are NOT a lawyer and never give legal advice;
you help people understand and answer each question in plain language.

- Detect the caller's language from their first words and speak only that language.
- If the dynamic variable applicant_name is set, greet them by name and mention
  known_summary so they know what you already have. Never re-ask a known field.
- Loop: call get_missing_fields, ask next_field.question in the caller's language,
  and for names, dates, numbers, or any field marked sensitive, read the value back
  and get confirmation before saving.
- Call save_field with each confirmed value. Never invent or guess data. If the
  caller does not know an answer or wants to move on, save the exact string
  __skip__ for that field and continue.
- Explain any confusing question simply before asking it. If someone does not know
  their eligibility category, reassure them and let them skip it.
- When next_field is null, call generate_form and tell them their completed form is
  ready on the screen, and that they should review and sign it themselves.

Keep every spoken turn short -- this is a live conversation, not a document.
"""

FIRST_MESSAGE = (
    "Hello, I'm Zuzu. I can help you fill out your work permit application, "
    "in your own language. Whenever you're ready, just tell me a little about yourself."
)


def tool(name: str, description: str, base: str, secret: str, props: dict[str, Any]) -> dict:
    """One server tool, wired to an orchestrator endpoint."""
    return {
        "type": "webhook",
        "name": name,
        "description": description,
        "api_schema": {
            "url": f"{base}/tools/{name}",
            "method": "POST",
            "request_headers": {"X-Zuzu-Secret": secret},
            "request_body_schema": {
                "type": "object",
                "description": f"Request body for {name}",
                "properties": props,
                "required": [k for k, v in props.items() if v.pop("_required", True)],
            },
        },
    }


def build_payload(base: str, secret: str) -> dict[str, Any]:
    # session_id is always the ElevenLabs conversation id -- that is the shared
    # key the dashboard and the orchestrator both index on. The API rejects a
    # property carrying both `description` and `dynamic_variable`, so this one
    # is filled from the system variable rather than by the model.
    session_id = {
        "type": "string",
        "dynamic_variable": "system__conversation_id",
    }
    return {
        "name": "Zuzu — USCIS form assistant",
        "conversation_config": {
            "agent": {
                "prompt": {
                    "prompt": SYSTEM_PROMPT,
                    "tools": [
                        tool(
                            "get_missing_fields",
                            "Ask the orchestrator which question to ask next. Returns "
                            "next_field with the exact plain-language question, or null "
                            "when the form is complete.",
                            base,
                            secret,
                            {
                                "session_id": dict(session_id),
                                "form_id": {
                                    "type": "string",
                                    "description": "Form id, normally I-765.",
                                },
                            },
                        ),
                        tool(
                            "save_field",
                            "Store one confirmed answer. Only call this after the caller "
                            "has confirmed the value. Use the exact string __skip__ if "
                            "they cannot or do not want to answer.",
                            base,
                            secret,
                            {
                                "session_id": dict(session_id),
                                "field_id": {
                                    "type": "string",
                                    "description": "The field id from next_field.id.",
                                },
                                "value": {
                                    "type": "string",
                                    "description": "The confirmed value, or __skip__.",
                                },
                                "confidence": {
                                    "type": "number",
                                    "description": "0-1 confidence in the transcription.",
                                },
                                "language": {
                                    "type": "string",
                                    "description": "ISO code of the language spoken.",
                                },
                            },
                        ),
                        tool(
                            "generate_form",
                            "Build the completed PDF. Call this only when "
                            "get_missing_fields returns next_field as null.",
                            base,
                            secret,
                            {"session_id": dict(session_id)},
                        ),
                    ],
                },
                "first_message": FIRST_MESSAGE,
                "language": "en",
            },
        },
        "platform_settings": {
            "workspace_overrides": {
                "conversation_initiation_client_data_webhook": {
                    "url": f"{base}/session/init",
                    "request_headers": {"X-Zuzu-Secret": secret},
                }
            }
        },
    }


def main() -> int:
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    secret = os.environ.get("ZUZU_SHARED_SECRET", "")
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not api_key or not secret or not base:
        sys.exit("need ELEVENLABS_API_KEY, ZUZU_SHARED_SECRET and PUBLIC_BASE_URL")
    if base.startswith("http://localhost"):
        sys.exit(f"PUBLIC_BASE_URL is {base} -- ElevenLabs webhooks cannot reach localhost")

    payload = build_payload(base, secret)
    resp = httpx.post(
        f"{API}/convai/agents/create",
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=60.0,
    )
    if resp.status_code >= 300:
        print(f"create failed: HTTP {resp.status_code}")
        print(json.dumps(resp.json(), indent=2)[:2000])
        return 1

    agent_id = resp.json().get("agent_id")
    print(f"agent created: {agent_id}")
    print(f"add to .env:  ELEVENLABS_AGENT_ID={agent_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

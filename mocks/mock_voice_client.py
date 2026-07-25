#!/usr/bin/env python3
"""Drive a full Zuzu call over HTTP, exactly as the ElevenLabs agent does.

Spec: prompts/mock_voice_client_Python.prompt

This is both the integration harness and the on-stage fallback. It imports
nothing from `api` on purpose: it must exercise the service the way an external
caller does, over HTTP only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
PERSONA_PATH = REPO_ROOT / "data" / "demo_personas.json"

SKIP = "__skip__"
MAX_TURNS = 100
SENSITIVE_HINT = ("ssn", "a_number", "passport", "i94", "uscis_online", "sevis")


def mask(field_id: str, value: str) -> str:
    """Mask anything identity-bearing: this output goes on a projector."""
    if value == SKIP:
        return "(skipped)"
    if any(hint in field_id for hint in SENSITIVE_HINT) and len(value) > 2:
        return "*" * (len(value) - 2) + value[-2:]
    return value


def load_persona(name: str) -> dict[str, Any]:
    personas = json.loads(PERSONA_PATH.read_text(encoding="utf-8"))["personas"]
    if name not in personas:
        sys.exit(f"unknown persona {name!r}; available: {', '.join(sorted(personas))}")
    return personas[name]


async def run_call(base_url: str, secret: str, persona_name: str) -> int:
    persona = load_persona(persona_name)
    answers: dict[str, str] = persona["answers"]
    conversation_id = f"mock_{persona_name}_{int(time.time())}"
    headers = {"X-Zuzu-Secret": secret}

    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30.0) as client:
        init = await client.post(
            "/session/init",
            json={"caller_id": persona["caller_id"], "conversation_id": conversation_id},
        )
        init.raise_for_status()
        print(f"\n  call {conversation_id}  ({persona['display_name']})")
        print(f"  {persona['story']}\n")

        last_field: str | None = None
        turns = 0
        while turns < MAX_TURNS:
            turns += 1
            resp = await client.post(
                "/tools/get_missing_fields",
                json={"session_id": conversation_id, "form_id": "I-765"},
            )
            resp.raise_for_status()
            body = resp.json()
            field = body["next_field"]
            if field is None:
                break

            field_id = field["id"]
            if field_id == last_field:
                print(f"\n  server keeps asking for {field_id!r} -- aborting.")
                return 1
            last_field = field_id

            # Only ever answer from the persona. A harness that invents values
            # can make the orchestrator look more complete than it is.
            value = answers.get(field_id, SKIP)
            print(f"  Q ({body['remaining_count']:2d} left)  {field['question']}")
            print(f"  A                {mask(field_id, value)}")

            saved = await client.post(
                "/tools/save_field",
                json={
                    "session_id": conversation_id,
                    "field_id": field_id,
                    "value": value,
                    "confidence": 0.95,
                    "language": persona.get("preferred_language", "en"),
                },
            )
            saved.raise_for_status()

        gen = await client.post("/tools/generate_form", json={"session_id": conversation_id})
        gen.raise_for_status()
        result = gen.json()

        await client.post(
            "/session/complete",
            json={"conversation_id": conversation_id, "transcript": [], "collected": {}},
        )

        if result["status"] != "complete":
            print(f"\n  form incomplete; missing: {', '.join(result['missing'])}")
            return 1

        print(f"\n  form ready after {turns} questions")
        print(f"  {result['pdf_url']}\n")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Drive a scripted Zuzu call over HTTP.")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--secret", default=os.environ.get("ZUZU_SHARED_SECRET", ""))
    parser.add_argument("--persona", default="maria")
    args = parser.parse_args()

    if not args.secret:
        sys.exit("no secret: pass --secret or set ZUZU_SHARED_SECRET")
    return asyncio.run(run_call(args.base_url, args.secret, args.persona))


if __name__ == "__main__":
    raise SystemExit(main())

"""Outbound delivery: get the finished packet to the applicant.

Spec: prompts/delivery_Python.prompt

Zuzu ends with a completed PDF sitting on a dashboard. That is useless to
someone who called from a phone and has now hung up. This module hands the
packet -- the filled form plus the document checklist -- to RocketRide, which
runs it as a declarative pipeline and delivers it by email.

RocketRide's model is pipeline-as-JSON: `POST /task` takes a source and a set of
nodes rather than a "send this email" call, so the whole delivery step is
configuration. That is the same operating rule the form schema follows, which
is why it belongs here rather than an SMTP client.

Status on this deployment: the API is reachable and the key authenticates, but
it currently lacks the `task.control` permission, so execution returns
`Permission 'task.control' denied`. `tool_gmail` additionally needs a Google
service account or user OAuth configured on the RocketRide account. Everything
below is written against the real API shape and degrades to a queued-locally
result rather than pretending mail went out.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ROCKETRIDE_BASE_URL = "https://api.rocketride.ai"
TIMEOUT_SECONDS = 60.0


class DeliveryUnavailable(RuntimeError):
    """RocketRide is not configured, not permitted, or refused the pipeline."""


def is_available() -> bool:
    return bool(os.environ.get("ROCKETRIDE_API_KEY"))


def build_packet(
    form_id: str,
    pdf_url: str,
    checklist: list[dict[str, str]],
    applicant_name: str = "",
) -> tuple[str, str]:
    """The subject and body an applicant actually receives.

    Deliberately plain language and explicit about what Zuzu did *not* do:
    this person is about to file a legal document and must not believe it has
    been submitted on their behalf.
    """
    greeting = f"Hello {applicant_name}," if applicant_name else "Hello,"
    lines = [
        greeting,
        "",
        f"Your {form_id} is filled in and ready. You can download it here:",
        f"  {pdf_url}",
        "",
        "Before you send it to USCIS:",
        "  1. Read every answer and correct anything that is wrong.",
        "  2. Sign and date it by hand. Zuzu cannot sign for you.",
        "  3. Attach the documents listed below.",
        "",
        "Documents to include:",
    ]
    for item in checklist:
        lines.append(f"  - {item.get('item', '')}")
        why = item.get("why")
        if why:
            lines.append(f"      {why}")
    lines += [
        "",
        "Zuzu has not submitted this form and has not paid any fee. You file it "
        "yourself, and you can change any answer before you do.",
        "",
        "-- Zuzu",
    ]
    return f"Your {form_id} is ready to review and sign", "\n".join(lines)


def build_pipeline(to_email: str, subject: str, body: str, name: str) -> dict[str, Any]:
    """The declarative pipeline RocketRide runs to deliver one packet.

    Config, not code -- a second delivery channel is another node here rather
    than another branch in Python.
    """
    return {
        "name": f"zuzu-delivery-{name}",
        "source": {"type": "trigger", "data": {"to": to_email, "subject": subject, "body": body}},
        "nodes": [
            {
                "id": "send",
                "service": "tool_gmail",
                "action": "send",
                "data": {"to": to_email, "subject": subject, "body": body},
            }
        ],
    }


async def deliver_packet(
    to_email: str,
    form_id: str,
    pdf_url: str,
    checklist: list[dict[str, str]],
    applicant_name: str = "",
) -> dict[str, Any]:
    """Email the completed form and checklist. Never raises into the caller.

    Returns a dict whose `delivered` flag is the honest answer. A caller must
    be able to tell the applicant the truth about whether mail went out.
    """
    subject, body = build_packet(form_id, pdf_url, checklist, applicant_name)

    if not to_email or "@" not in to_email:
        return {"delivered": False, "reason": "no usable email address for this applicant"}
    if not is_available():
        return {"delivered": False, "reason": "ROCKETRIDE_API_KEY is not set", "subject": subject}

    key = os.environ.get("ROCKETRIDE_API_KEY", "")
    pipeline = build_pipeline(to_email, subject, body, form_id.lower())

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{ROCKETRIDE_BASE_URL}/task",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=pipeline,
            )
            payload = resp.json() if resp.content else {}
    except Exception as exc:
        logger.warning("rocketride unreachable: %s", type(exc).__name__)
        return {"delivered": False, "reason": f"rocketride unreachable: {type(exc).__name__}"}

    if payload.get("status") == "OK":
        logger.info("delivered %s packet via rocketride", form_id)
        return {"delivered": True, "via": "rocketride/tool_gmail", "subject": subject}

    reason = json.dumps(payload.get("error", payload))[:200]
    logger.warning("rocketride refused the pipeline: %s", reason)
    return {"delivered": False, "reason": reason, "subject": subject, "body": body}

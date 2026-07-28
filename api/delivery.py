"""Outbound delivery: get the finished packet to the applicant.

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
RUN_TIMEOUT_SECONDS = 180.0


def _gmail_config() -> dict[str, Any] | None:
    """Credentials for RocketRide's tool_gmail, if any are configured.

    tool_gmail requires `type`, `authType` and `access`, satisfied either by a
    Google Workspace service account or by a user OAuth token obtained through
    RocketRide's own consent flow. Without one of those the agent has no
    mailbox to send from, and the pipeline completes having sent nothing.
    """
    if token := os.environ.get("ROCKETRIDE_GMAIL_USER_TOKEN"):
        return {
            "type": "tool_gmail",
            "authType": "oauth",
            "access": "full",
            "userToken": token,
        }
    key = os.environ.get("ROCKETRIDE_GMAIL_SERVICE_KEY")
    admin = os.environ.get("ROCKETRIDE_GMAIL_ADMIN_EMAIL")
    if key and admin:
        return {
            "type": "tool_gmail",
            "authType": "service",
            "access": "full",
            "serviceKey": key,
            "adminEmail": admin,
            "customerId": os.environ.get("ROCKETRIDE_GMAIL_CUSTOMER_ID", ""),
        }
    return None


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


def instruction_for(to_email: str, subject: str, body: str) -> str:
    """What the agent is told to do when the pipeline runs."""
    return (
        f"Send an email using the gmail tool. To: {to_email}. "
        f"Subject: {subject}. Body, exactly as written:\n\n{body}\n\n"
        "Send it now. Do not ask any questions."
    )


def build_pipeline(to_email: str, subject: str, body: str, name: str) -> dict[str, Any]:
    """The declarative pipeline RocketRide runs to deliver one packet.

    Config, not code -- a second delivery channel is another component here
    rather than another branch in Python.

    Shape per RocketRide's pipeline reference: components carry `id`,
    `provider` and `config`, and are wired by `input` entries naming a `lane`
    and the `from` component. `source` is the id of the entry point.
    """
    # tool_gmail declares no lanes: it is a tool an agent calls, not a pipeline
    # node. So the shape is webhook(questions) -> agent(+gmail) -> response.
    instruction = (
        f"Send an email to {to_email} with the subject {subject!r} and exactly "
        f"this body, unchanged:\n\n{body}"
    )
    gmail = _gmail_config()
    agent_config: dict[str, Any] = {"tools": ["tool_gmail"], "prompt": instruction}
    if gmail:
        agent_config["tool_gmail"] = gmail

    return {
        "name": f"zuzu-delivery-{name}",
        "source": "in",
        "components": [
            {"id": "in", "provider": "webhook", "config": {}},
            {
                "id": "agent",
                "provider": "agent_rocketride",
                "config": agent_config,
                "input": [{"lane": "questions", "from": "in"}],
            },
            {
                "id": "out",
                "provider": "response",
                "config": {"laneName": "answers"},
                "input": [{"lane": "answers", "from": "agent"}],
            },
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

    if payload.get("status") != "OK":
        reason = json.dumps(payload.get("error", payload))[:200]
        logger.warning("rocketride refused the pipeline: %s", reason)
        return {"delivered": False, "reason": reason, "subject": subject, "body": body}

    token = (payload.get("data") or {}).get("token")
    if not token:
        return {"delivered": False, "reason": "rocketride returned no run token"}

    if not _gmail_config():
        # Be blunt rather than reporting a success nobody can see in an inbox.
        return {
            "delivered": False,
            "reason": (
                "RocketRide tool_gmail has no credentials. Connect Gmail in the "
                "RocketRide dashboard and set ROCKETRIDE_GMAIL_USER_TOKEN, or set "
                "ROCKETRIDE_GMAIL_SERVICE_KEY and ROCKETRIDE_GMAIL_ADMIN_EMAIL."
            ),
            "pipeline_token": token,
            "subject": subject,
            "body": body,
        }

    # Feed the pipeline. Creating it only reserves a token; this is the run.
    try:
        async with httpx.AsyncClient(timeout=RUN_TIMEOUT_SECONDS) as client:
            run = await client.post(
                f"{ROCKETRIDE_BASE_URL}/task/data",
                params={"token": token},
                headers={"Authorization": f"Bearer {key}"},
                files={"question": (None, f"{instruction_for(to_email, subject, body)}")},
            )
            run_payload = run.json() if run.content else {}
    except Exception as exc:
        return {"delivered": False, "reason": f"pipeline run failed: {type(exc).__name__}"}

    produced = (run_payload.get("data") or {}).get("resultTypes") or {}
    if run_payload.get("status") == "OK" and produced:
        logger.info("rocketride delivered %s packet", form_id)
        return {
            "delivered": True,
            "via": "rocketride/tool_gmail",
            "pipeline_token": token,
            "subject": subject,
        }
    return {
        "delivered": False,
        "reason": (
            "the pipeline ran but the agent produced no output, which is what "
            "an unauthenticated tool_gmail looks like"
        ),
        "pipeline_token": token,
        "subject": subject,
        "body": body,
    }

    reason = json.dumps(payload.get("error", payload))[:200]
    logger.warning("rocketride refused the pipeline: %s", reason)
    return {"delivered": False, "reason": reason, "subject": subject, "body": body}

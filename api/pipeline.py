"""Form intake as a declarative RocketRide pipeline.

Spec: prompts/pipeline_Python.prompt

Onboarding a USCIS form has two jobs a pipeline is genuinely better at than
Python: reading a document, and choosing a model to phrase its questions. Both
are now one JSON document sent to RocketRide, which means the *shape* of form
intake is configuration -- the same rule the form schemas themselves follow.

    webhook ──► parse ─────────────────► instruction text from the PDF
    webhook ──► question ──► llm ──────► polished spoken questions

The model is selected in that JSON, not in this file. Swapping MiniMax for
another provider is an edit to a config dict, and routing MiniMax through
TokenRouter is what the `serverbase` field is doing below.

None of this is on the call path. Onboarding is a one-time background step per
form, and the deterministic tooltip-derived questions already work -- this
improves their wording, so every failure here is silent and harmless.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ROCKETRIDE_BASE_URL = "https://api.rocketride.ai"
CREATE_TIMEOUT = 60.0
RUN_TIMEOUT = 280.0
#: parse on a 500KB PDF exceeds this; onboarding falls back rather than hanging.
MAX_POLISH_BATCH = 12


class PipelineUnavailable(RuntimeError):
    """RocketRide is not configured, or refused the pipeline."""


def is_available() -> bool:
    return bool(os.environ.get("ROCKETRIDE_API_KEY"))


def _model_component(component_id: str, source: str) -> dict[str, Any]:
    """The model, selected in configuration rather than in code.

    `profile: custom` takes an inline key, base URL and model name, so the
    provider is a config value. TokenRouter is an OpenAI-compatible gateway, so
    it slots into `serverbase` unchanged -- which is how model selection ends up
    living in the same JSON as the pipeline shape.
    """
    return {
        "id": component_id,
        "provider": os.environ.get("ROCKETRIDE_LLM_PROVIDER", "llm_minimax"),
        "config": {
            "profile": "custom",
            "custom": {
                "model": os.environ.get("TOKENROUTER_MODEL", "MiniMax-M3"),
                "serverbase": os.environ.get(
                    "TOKENROUTER_BASE_URL", "https://api.tokenrouter.com/v1"
                ),
                "apikey": os.environ.get("TOKENROUTER_API_KEY", ""),
                "modelTotalTokens": 32768,
            },
        },
        "input": [{"lane": "questions", "from": source}],
    }


def build_intake_pipeline(name: str) -> dict[str, Any]:
    """Text in, model-polished text out.

    `llm_minimax` consumes a `questions` lane, and raw webhook text does not
    populate it -- the `question` component is what converts one to the other.
    Wiring the model straight to the webhook produces no output at all.
    """
    return {
        "name": f"zuzu-intake-{name}",
        "source": "in",
        "components": [
            {"id": "in", "provider": "webhook", "config": {}},
            {
                "id": "q",
                "provider": "question",
                "config": {"type": "question"},
                "input": [{"lane": "text", "from": "in"}],
            },
            _model_component("llm", "q"),
            {
                "id": "out",
                "provider": "response",
                "config": {"laneName": "answers"},
                "input": [{"lane": "answers", "from": "llm"}],
            },
        ],
    }


def build_document_pipeline(name: str) -> dict[str, Any]:
    """A document in, its readable text out.

    `parse` needs no credentials, which is why document intake can run on the
    RocketRide key alone.
    """
    return {
        "name": f"zuzu-doc-{name}",
        "source": "in",
        "components": [
            {"id": "in", "provider": "webhook", "config": {}},
            {
                "id": "p",
                "provider": "parse",
                "config": {},
                "input": [{"lane": "tags", "from": "in"}],
            },
            {
                "id": "out",
                "provider": "response",
                "config": {"laneName": "text"},
                "input": [{"lane": "text", "from": "p"}],
            },
        ],
    }


async def _create(pipeline: dict[str, Any]) -> str:
    key = os.environ.get("ROCKETRIDE_API_KEY", "")
    async with httpx.AsyncClient(timeout=CREATE_TIMEOUT) as client:
        resp = await client.post(
            f"{ROCKETRIDE_BASE_URL}/task",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=pipeline,
        )
        payload = resp.json() if resp.content else {}
    if payload.get("status") != "OK":
        raise PipelineUnavailable(str(payload.get("error", payload))[:200])
    token = (payload.get("data") or {}).get("token")
    if not token:
        raise PipelineUnavailable("no run token returned")
    return token


async def _run(token: str, field: str, value: Any) -> dict[str, Any]:
    """Feed a created pipeline. Creating one only reserves a token."""
    key = os.environ.get("ROCKETRIDE_API_KEY", "")
    async with httpx.AsyncClient(timeout=RUN_TIMEOUT) as client:
        resp = await client.post(
            f"{ROCKETRIDE_BASE_URL}/task/data",
            params={"token": token},
            headers={"Authorization": f"Bearer {key}"},
            files={field: value},
        )
        return resp.json() if resp.content else {}


def _first_text(payload: dict[str, Any], lane: str) -> str:
    objects = ((payload.get("data") or {}).get("objects") or {}).values()
    for obj in objects:
        chunk = obj.get(lane)
        if chunk:
            return "".join(chunk) if isinstance(chunk, list) else str(chunk)
    return ""


def _strip_reasoning(text: str) -> str:
    """MiniMax emits <think> blocks, and an unclosed one on truncation."""
    import re

    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"<think>.*$", "", text, flags=re.S)
    return text.strip()


async def polish_questions(labels: list[str], form_id: str) -> dict[str, str]:
    """Improve the wording of derived questions, through the pipeline.

    Returns a mapping of original question to improved question, and an empty
    mapping on any failure -- the deterministic questions are already usable, so
    this is allowed to do nothing.
    """
    if not is_available() or not labels:
        return {}
    batch = labels[:MAX_POLISH_BATCH]
    prompt = (
        f"These are field labels from USCIS form {form_id}. Rewrite each as one "
        "short question a patient person would ask out loud, in plain English. "
        "Reply with one rewritten question per line, in the same order, and "
        "nothing else.\n\n" + "\n".join(batch)
    )
    try:
        token = await _create(build_intake_pipeline(form_id.lower()))
        payload = await _run(token, "text", (None, prompt))
    except (PipelineUnavailable, Exception) as exc:
        logger.info("question polish unavailable for %s: %s", form_id, type(exc).__name__)
        return {}

    answer = _strip_reasoning(_first_text(payload, "answers"))
    lines = [ln.strip(" -•\t") for ln in answer.splitlines() if ln.strip()]
    if len(lines) < len(batch):
        logger.info("%s: polish returned %d of %d lines, skipping", form_id, len(lines), len(batch))
        return {}
    return dict(zip(batch, lines[: len(batch)], strict=False))


async def read_document(path: str, form_id: str) -> str:
    """Extract a document's readable text through the pipeline.

    Used for a form's instruction text -- fees, mailing address, who qualifies --
    which lives in prose the AcroForm inventory says nothing about.
    """
    if not is_available():
        return ""
    try:
        token = await _create(build_document_pipeline(form_id.lower()))
        with open(path, "rb") as handle:
            payload = await asyncio.wait_for(
                _run(token, "file", (os.path.basename(path), handle.read())),
                timeout=RUN_TIMEOUT,
            )
    except Exception as exc:
        logger.info("document read unavailable for %s: %s", form_id, type(exc).__name__)
        return ""
    text = _first_text(payload, "text")
    logger.info("rocketride parsed %s: %d chars", form_id, len(text))
    return text

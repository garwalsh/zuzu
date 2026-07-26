"""The inference lane: one OpenAI-compatible client, provider-agnostic.

Spec: prompts/inference_Python.prompt

Deliberately not tied to a vendor. Base URL, model, and key are configuration,
so TokenRouter (the working default), Cerebras, Groq, or anything else
OpenAI-shaped is a config flip rather than a code change. Cerebras is the
motivating example: the playbook assigns it the speed lane, the account returns
402, and nothing in this file had to change to keep going.

Nothing here sits on the live voice path. `save_field` and `get_missing_fields`
must return in tens of milliseconds and never call this module.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120.0


class InferenceUnavailable(RuntimeError):
    """No provider is configured, or the provider refused the request."""


def _config() -> tuple[str, str, str]:
    """(base_url, api_key, model) for whichever provider is configured.

    TokenRouter first because it is what actually works today; Cerebras and
    OpenAI are here so switching is an env change, not a patch.
    """
    if key := os.environ.get("TOKENROUTER_API_KEY"):
        return (
            os.environ.get("TOKENROUTER_BASE_URL", "https://api.tokenrouter.com/v1").rstrip("/"),
            key,
            os.environ.get("TOKENROUTER_MODEL", "MiniMax-M3"),
        )
    if key := os.environ.get("CEREBRAS_API_KEY"):
        return (
            os.environ.get("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1").rstrip("/"),
            key,
            os.environ.get("CEREBRAS_MODEL", "zai-glm-4.7"),
        )
    if key := os.environ.get("OPENAI_API_KEY"):
        return (
            os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            key,
            os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        )
    raise InferenceUnavailable(
        "no inference provider configured -- set TOKENROUTER_API_KEY, "
        "CEREBRAS_API_KEY, or OPENAI_API_KEY"
    )


def is_available() -> bool:
    try:
        _config()
        return True
    except InferenceUnavailable:
        return False


def _strip_reasoning(text: str) -> str:
    """Remove <think> blocks. MiniMax-M3 emits them and they are not the answer."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def _extract_json(text: str) -> Any:
    """Pull the first JSON value out of a model response.

    Models fence JSON, prefix it with prose, or append a closing remark. Being
    strict here means a good answer gets thrown away over punctuation.
    """
    cleaned = _strip_reasoning(text)
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, flags=re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"no JSON in model response: {cleaned[:200]}")


async def complete(
    prompt: str,
    system: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """One completion. Raises InferenceUnavailable rather than returning junk."""
    base, key, model = _config()
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        # 402 is the live Cerebras failure. Say so plainly instead of a stack trace.
        detail = exc.response.text[:200] if exc.response is not None else str(exc)
        raise InferenceUnavailable(f"{model} refused the request: {detail}") from exc
    except Exception as exc:
        raise InferenceUnavailable(f"{model} unreachable: {type(exc).__name__}") from exc

    # Parsed here, inside the same failure contract as the request. A gateway out
    # of credit answers 200 with {"error": ...} and no "choices", and reaching
    # into that outside the guard raised KeyError -- an exception every caller of
    # this module is written to let through, so an ordinary billing problem
    # surfaced as a 500 instead of the graceful fallback each caller already has.
    try:
        return _strip_reasoning(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        detail = str(data.get("error") or data)[:200] if isinstance(data, dict) else str(data)[:200]
        raise InferenceUnavailable(f"{model} returned no completion: {detail}") from exc


async def complete_json(prompt: str, system: str | None = None, **kwargs: Any) -> Any:
    """A completion parsed as JSON, tolerant of fences and stray prose."""
    return _extract_json(await complete(prompt, system=system, **kwargs))


async def normalise_spoken_value(value: str, field_type: str, question: str) -> str | None:
    """Turn a spoken answer into what the form will accept.

    A voice caller says "nineteen ninety-eight, April twelfth" and the PDF wants
    1998-04-12; they say "see three bee" and the form wants (c)(3)(B). This is
    the gap between a voice product and a form product.

    Returns None when the value cannot be normalised confidently. Never guesses
    -- a wrong date on an immigration filing is worse than a blank one.
    """
    if not value.strip():
        return None
    system = (
        "You normalise spoken answers for a USCIS form. Reply with ONLY the "
        "normalised value and nothing else. If you cannot determine the value "
        "with high confidence, reply with exactly: UNKNOWN"
    )
    rules = {
        "date": "Output an ISO date, YYYY-MM-DD.",
        "zip": "Output exactly 5 digits.",
        "phone": "Output exactly 10 digits, no punctuation.",
        "ssn": "Output exactly 9 digits, no punctuation.",
        "a_number": "Output 8 or 9 digits with no leading 'A'.",
        "state": "Output the 2-letter USPS state code, uppercase.",
        "eligibility_category": "Output the form (c)(3)(B): letter, digits, optional letter.",
    }.get(field_type, "Output the value as it should appear on the form.")

    try:
        out = await complete(
            f"Question asked: {question}\nSpoken answer: {value}\n\n{rules}",
            system=system,
            max_tokens=64,
            timeout=20.0,
        )
    except InferenceUnavailable as exc:
        logger.warning("normalisation unavailable, keeping raw value: %s", exc)
        return None

    out = out.strip().strip('"').splitlines()[0].strip() if out.strip() else ""
    if not out or out.upper() == "UNKNOWN":
        return None
    return out


async def translate_questions(questions: list[str], language: str) -> dict[str, str]:
    """Translate spoken questions into the caller's language.

    The schema is authored in English. An applicant who speaks Haitian Creole
    should not get an English question read at them because nobody wrote a
    Creole schema.
    """
    if not questions or language.lower().startswith("en"):
        return {}
    try:
        out = await complete_json(
            "Translate each question into the language with ISO code "
            f"'{language}'. Keep them short and spoken-natural, suitable to be "
            "read aloud on a phone call. Reply with ONLY a JSON object mapping "
            "each original English question to its translation.\n\n"
            + json.dumps(questions, ensure_ascii=False),
            system="You translate plain-language form questions for a voice assistant.",
            max_tokens=4096,
        )
    except (InferenceUnavailable, ValueError) as exc:
        logger.warning("translation unavailable, falling back to English: %s", exc)
        return {}
    return {k: str(v) for k, v in out.items()} if isinstance(out, dict) else {}

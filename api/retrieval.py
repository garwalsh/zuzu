"""Fetch form requirements and supporting-document lists from the web.

Spec: prompts/retrieval_Python.prompt

Zuzu produces a completed form, but a completed form is only half of what an
applicant has to put in the envelope. USCIS wants photos, a copy of the prior
EAD, the I-94, proof of status -- and which documents apply depends on the
eligibility category. Getting that wrong is a rejected filing.

That list lives on uscis.gov as prose, not as an API, and uscis.gov returns 403
to a plain scripted GET. rtrvr.ai drives a real browser, so it gets the page;
the inference lane then turns the prose into a structured checklist.

Both halves degrade: no rtrvr key or a failed fetch falls back to a curated
baseline checklist rather than telling an applicant they need nothing.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from api.inference import InferenceUnavailable, complete_json

logger = logging.getLogger(__name__)

RTRVR_BASE_URL = "https://api.rtrvr.ai"
FETCH_TIMEOUT_SECONDS = 120.0

#: Used when the live fetch is unavailable. Conservative and category-agnostic:
#: better to over-list documents an applicant already has than to omit one that
#: gets their application rejected.
BASELINE_CHECKLIST: dict[str, list[dict[str, str]]] = {
    "I-765": [
        {
            "item": "Two identical passport-style photographs",
            "why": "Required with every Form I-765 filing.",
            "when": "always",
        },
        {
            "item": "Copy of your Form I-94, arrival/departure record",
            "why": "Shows your lawful admission and current status.",
            "when": "always",
        },
        {
            "item": "Copy of the photo page of your passport",
            "why": "Establishes your identity.",
            "when": "always",
        },
        {
            "item": "Copy of your previous Employment Authorization Document",
            "why": "Required when renewing or replacing an existing EAD.",
            "when": "reason is renewal or replacement",
        },
        {
            "item": "Copy of your Form I-20 endorsed for employment",
            "why": "Required for F-1 students filing under a (c)(3) category.",
            "when": "eligibility category begins with (c)(3)",
        },
        {
            "item": "Copy of the receipt or approval notice for your underlying case",
            "why": "Shows the pending application your work permission rests on.",
            "when": "category depends on a pending application",
        },
    ]
}


def is_available() -> bool:
    return bool(os.environ.get("RTRVR_API_KEY"))


async def scrape(urls: list[str], instruction: str | None = None) -> str:
    """Return page text for `urls` via rtrvr's browser agent.

    Raises RuntimeError rather than returning a misleading empty string, so the
    caller can decide to fall back.
    """
    key = os.environ.get("RTRVR_API_KEY", "")
    if not key:
        raise RuntimeError("RTRVR_API_KEY is not set")

    body: dict[str, Any] = {"urls": urls, "response": {"verbosity": "final"}}
    if instruction:
        body["input"] = instruction

    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{RTRVR_BASE_URL}/scrape",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=body,
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        raise RuntimeError(f"rtrvr fetch failed: {type(exc).__name__}") from exc

    if not payload.get("success"):
        raise RuntimeError(f"rtrvr returned success=false: {str(payload)[:200]}")

    chunks: list[str] = []
    for tab in payload.get("tabs", []):
        tree = tab.get("tree")
        if isinstance(tree, str):
            chunks.append(tree)
        elif tree is not None:
            chunks.append(str(tree))
    text = "\n".join(chunks)
    logger.info("rtrvr fetched %d url(s), %d chars", len(urls), len(text))
    return text


async def fetch_document_checklist(form_id: str) -> dict[str, Any]:
    """The supporting documents an applicant must attach to `form_id`.

    Live from uscis.gov when possible, falling back to the curated baseline.
    The result always names its own source so a reviewer can tell which they
    are looking at -- an invented checklist would be worse than no checklist.
    """
    slug = form_id.lower().replace(" ", "")
    url = f"https://www.uscis.gov/{slug}"
    baseline = BASELINE_CHECKLIST.get(form_id.upper(), [])

    if not is_available():
        return {"form_id": form_id, "source": "baseline", "items": baseline}

    try:
        page = await scrape([url], f"Find the documents required to file form {form_id}.")
    except RuntimeError as exc:
        logger.warning("checklist fetch failed for %s: %s", form_id, exc)
        return {"form_id": form_id, "source": "baseline", "items": baseline, "error": str(exc)}

    try:
        parsed = await complete_json(
            "From this USCIS page, list the supporting documents an applicant "
            f"must submit with form {form_id}.\n\n"
            "Reply with ONLY a JSON array. Each element: "
            '{"item": short name, "why": one sentence, "when": "always" or the '
            "condition under which it applies}.\n"
            "Include only documents the page actually names. Do not invent "
            "requirements: a wrong checklist causes a rejected filing.\n\n"
            f"{page[:60000]}",
            system="You extract filing requirements from official USCIS pages.",
            max_tokens=4096,
            timeout=300.0,
        )
    except (InferenceUnavailable, ValueError) as exc:
        logger.warning("checklist parse failed for %s: %s", form_id, exc)
        return {"form_id": form_id, "source": "baseline", "items": baseline, "error": str(exc)}

    items = parsed if isinstance(parsed, list) else parsed.get("items", [])
    clean = [
        {
            "item": str(i.get("item", "")).strip(),
            "why": str(i.get("why", "")).strip(),
            "when": str(i.get("when", "always")).strip(),
        }
        for i in items
        if isinstance(i, dict) and i.get("item")
    ]
    if not clean:
        return {"form_id": form_id, "source": "baseline", "items": baseline}

    logger.info("checklist for %s: %d item(s) from %s", form_id, len(clean), url)
    return {"form_id": form_id, "source": url, "items": clean}


def applicable_items(checklist: dict[str, Any], values: dict[str, str]) -> list[dict[str, str]]:
    """Narrow a checklist to this applicant, using what they told us.

    Conditions are prose, so matching is deliberately loose and errs toward
    including an item. Showing someone a document they do not need costs them a
    moment; omitting one costs them months.
    """
    reason = (values.get("reason") or "").lower()
    category = (values.get("eligibility_category") or "").lower().replace(" ", "")
    out: list[dict[str, str]] = []

    for item in checklist.get("items", []):
        when = (item.get("when") or "always").lower()
        if when in ("always", "", "all", "required"):
            out.append(item)
            continue
        if "renewal" in when or "replacement" in when:
            if reason in ("renewal", "replacement"):
                out.append(item)
            continue
        if "(c)(3)" in when or "c3" in when.replace(" ", ""):
            if category.startswith("(c)(3") or category.startswith("c3"):
                out.append(item)
            continue
        out.append(item)
    return out

"""Work out which immigration form someone is asking for.

An applicant on the phone does not say "I-765". They say "I need my work
permit", "my green card is expiring", "I want to become a citizen". And someone
at a keyboard may just paste a uscis.gov link. Both have to land on the right
form without the person knowing its number.

Three strategies, cheapest first:

  1. An explicit form number anywhere in the text.
  2. Plain-language intent matched against the catalog's own synonyms.
  3. A URL, read with rtrvr, since uscis.gov refuses scripted GETs.

Everything returns a confidence and the reason it matched, because the agent
must *confirm* before switching forms. Silently starting the wrong application
wastes an hour of a stressed person's time.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from api.form_onboarding import catalog_entry, load_catalog

logger = logging.getLogger(__name__)

#: What people actually say, mapped to the form they mean.
INTENT_SYNONYMS: dict[str, tuple[str, ...]] = {
    "I-765": (
        "work permit",
        "work authorization",
        "employment authorization",
        "ead",
        "permission to work",
        "work card",
        "permiso de trabajo",
        "opt",
        "work papers",
    ),
    "N-400": (
        "citizen",
        "citizenship",
        "naturalization",
        "naturalize",
        "become american",
        "ciudadania",
        "us citizen",
        "pass the citizenship test",
    ),
    "I-130": (
        "petition for relative",
        "sponsor my",
        "bring my family",
        "family petition",
        "petition my spouse",
        "petition my mother",
        "petition my father",
        "relative visa",
        # People name the person, not the category. "Bring my family" was in the
        # table; "bring my wife" -- which is what somebody actually says -- was
        # not, and fell through to no match at all.
        "bring my wife",
        "bring my husband",
        "bring my spouse",
        "bring my mother",
        "bring my father",
        "bring my son",
        "bring my daughter",
        "bring my child",
        "bring my parents",
        "bring my brother",
        "bring my sister",
        "reunite with my family",
        "green card for my wife",
        "green card for my husband",
        "green card for my spouse",
    ),
    "I-485": (
        "green card",
        "adjust status",
        "adjustment of status",
        "permanent residence",
        "residencia",
        "become a permanent resident",
    ),
    "I-131": (
        "travel document",
        "advance parole",
        "reentry permit",
        "re-entry permit",
        "permission to travel",
        "refugee travel",
    ),
    "I-90": (
        "replace my green card",
        "renew my green card",
        "lost green card",
        "green card expiring",
        "replace permanent resident card",
    ),
    "I-751": (
        "remove conditions",
        "conditional green card",
        "two year green card",
        "remove the conditions on my residence",
    ),
    "I-864": ("affidavit of support", "sponsor financially", "financial sponsor"),
    "I-821D": ("daca", "deferred action", "dreamer"),
    "I-589": ("asylum", "asilo", "persecution", "refugee claim", "withholding of removal"),
    "G-28": ("attorney appearance", "my lawyer is filing", "accredited representative"),
    "I-539": (
        "extend my status",
        "change my status",
        "extend my visa",
        "change of status",
        "stay longer",
        "extend my stay",
    ),
}

_FORM_NUMBER = re.compile(r"\b([INGig])[\s\-]?(\d{2,4})([A-Za-z]?)\b")


def _canonical(prefix: str, digits: str, suffix: str) -> str:
    return f"{prefix.upper()}-{digits}{suffix.upper()}"


def from_form_number(text: str) -> dict[str, Any] | None:
    """An explicit form number, however it was spoken or typed."""
    for match in _FORM_NUMBER.finditer(text or ""):
        candidate = _canonical(*match.groups())
        if catalog_entry(candidate):
            return {
                "form_id": candidate,
                "confidence": 0.98,
                "matched_on": f"form number {match.group(0)!r}",
            }
    return None


def from_intent(text: str) -> dict[str, Any] | None:
    """Plain language: 'my work permit', 'I want to become a citizen'."""
    lowered = f" {(text or '').lower()} "
    best: tuple[str, str] | None = None
    for form_id, phrases in INTENT_SYNONYMS.items():
        for phrase in phrases:
            if phrase in lowered and (best is None or len(phrase) > len(best[1])):
                best = (form_id, phrase)
    if best is None:
        return None
    return {
        "form_id": best[0],
        # Below 0.9 on purpose: the agent must read this back before switching.
        "confidence": 0.72,
        "matched_on": f"the phrase {best[1]!r}",
    }


def from_url(url: str) -> dict[str, Any] | None:
    """A uscis.gov link, matched against the catalog without a fetch."""
    lowered = (url or "").lower()
    for entry in load_catalog():
        slug = entry["form_id"].lower()
        if f"/{slug}" in lowered or slug.replace("-", "") in lowered.replace("-", ""):
            return {
                "form_id": entry["form_id"],
                "confidence": 0.95,
                "matched_on": f"the url {url}",
            }
    return None


async def from_url_content(url: str) -> dict[str, Any] | None:
    """Read an unfamiliar page with rtrvr and identify the form from its title.

    This is the path for a form Zuzu has never seen: uscis.gov returns 403 to a
    scripted GET, so a real browser has to fetch it.
    """
    from api.retrieval import is_available, scrape

    if not is_available():
        return None
    try:
        page = await scrape([url], "Identify which USCIS form this page is about.")
    except RuntimeError as exc:
        logger.info("could not read %s: %s", url, exc)
        return None

    hit = from_form_number(page[:8000])
    if hit:
        hit["matched_on"] = f"the page at {url}"
        hit["confidence"] = 0.9
        return hit
    return None


#: What the model is allowed to answer with. Anything else is discarded.
_CLASSIFIER_SYSTEM = """You route a person to the right USCIS form.

You are given what they said, in their own words and possibly not in English,
and the list of forms this system can actually file. Choose the one form that
matches, or say none.

Rules that do not bend:
- Answer only with a form_id from the list you were given. Never invent one.
- If nothing in the list clearly matches, answer {"form_id": null}.
- A wrong form wastes an hour of a stressed person's time and can cost them a
  filing fee, so a confident wrong answer is far worse than "none"."""


async def from_model(text: str) -> dict[str, Any] | None:
    """Ask the model, when the phrase table has nothing.

    The table is exact-substring matching over phrases somebody thought of in
    advance, which is fine until a person says "I want to bring my wife to
    America" and no phrase contains it. This is the case the inference lane
    genuinely earns: an intent that is obvious to a human and invisible to a
    substring match.

    Safe to be wrong: the answer is validated against the catalog so a
    hallucinated form id is discarded rather than filed, the confidence is low
    enough that the voice agent reads the form name back before switching, and
    nothing here is on the live answer path -- identifying the form happens once
    per call, not once per question.
    """
    from api.inference import InferenceUnavailable, complete_json, is_available

    if not is_available() or not (text or "").strip():
        return None

    catalog = [{"form_id": e["form_id"], "title": e.get("title", "")} for e in load_catalog()]
    prompt = (
        f"The person said: {text.strip()!r}\n\n"
        f"Forms available:\n{json.dumps(catalog, indent=1)}\n\n"
        'Reply with one JSON object: {"form_id": "<id or null>", "why": "<a few words>"}'
    )
    try:
        answer = await complete_json(prompt, system=_CLASSIFIER_SYSTEM)
    except (InferenceUnavailable, Exception) as exc:  # noqa: B014 - never fatal
        logger.info("model could not classify the intent: %s", type(exc).__name__)
        return None

    if not isinstance(answer, dict):
        return None
    form_id = str(answer.get("form_id") or "").strip().upper()
    # The guard that makes this safe: a form the deployment does not have is not
    # a form, whatever the model called it.
    if not form_id or not catalog_entry(form_id):
        return None
    why = str(answer.get("why") or "").strip()[:80]
    return {
        "form_id": form_id,
        # Lower than the phrase table on purpose. This is a judgement, and the
        # agent must read the form name back before it switches.
        "confidence": 0.6,
        "matched_on": f"the model read this as {why or form_id}",
    }


async def identify(text: str = "", url: str = "") -> dict[str, Any] | None:
    """Best guess at the form being asked for, cheapest strategy first."""
    for candidate in (
        from_form_number(text),
        from_url(url) if url else None,
        from_intent(text),
    ):
        if candidate:
            logger.info("form identified: %s via %s", candidate["form_id"], candidate["matched_on"])
            return candidate
    # Only once the cheap, deterministic paths have all missed.
    if text:
        hit = await from_model(text)
        if hit:
            logger.info("form identified: %s via %s", hit["form_id"], hit["matched_on"])
            return hit
    if url:
        return await from_url_content(url)
    return None

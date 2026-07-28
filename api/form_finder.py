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
    if url:
        return await from_url_content(url)
    return None

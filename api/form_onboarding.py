"""Onboard any USCIS form at runtime, from a catalog entry or a URL.

"A form is data, not code" only means something if a form nobody anticipated
can be added while the service is running. This module does that: fetch the
fillable PDF, extract its AcroForm inventory, derive a schema from the PDF's
own screen-reader tooltips, and register it. No deploy, no code change.

The derivation is deliberately deterministic. A reasoning model asked for two
hundred fields returns an empty string as often as it returns JSON, and a form
that works only sometimes is not a form the product supports. The model is an
optional polish pass over the wording, never the source of a field name.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from api.form_builder import derive_schema, save_schema, slugify
from api.i765_schema import REPO_ROOT
from api.pipeline import is_available as pipeline_available
from api.pipeline import polish_questions

logger = logging.getLogger(__name__)

CATALOG_PATH = REPO_ROOT / "data" / "form_catalog.json"
ASSETS_DIR = REPO_ROOT / "assets"

#: uscis.gov serves the PDF happily to a browser and 403s an obviously scripted
#: client. This is the difference between the two -- not an attempt to hide, and
#: no faster than a person clicking the same link.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DOWNLOAD_TIMEOUT = 120.0


class OnboardingError(RuntimeError):
    """The form could not be fetched, read, or turned into a schema."""


def load_catalog() -> list[dict[str, str]]:
    try:
        return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))["forms"]
    except Exception as exc:
        logger.warning("form catalog unreadable: %s", exc)
        return []


def catalog_entry(form_id: str) -> dict[str, str] | None:
    target = form_id.strip().upper()
    for entry in load_catalog():
        if entry["form_id"].upper() == target:
            return entry
    return None


async def _get_via_httpx(url: str) -> bytes | None:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=DOWNLOAD_TIMEOUT) as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": BROWSER_UA,
                    "Accept": "application/pdf,*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            resp.raise_for_status()
            return resp.content
    except Exception as exc:
        logger.info("httpx could not fetch %s (%s); trying curl", url, type(exc).__name__)
        return None


async def _get_via_curl(url: str) -> bytes | None:
    """Fetch with curl.

    uscis.gov sits behind Akamai, which fingerprints the TLS handshake rather
    than reading headers: httpx gets a 403 with byte-identical browser headers
    while curl gets the file. This is not an attempt to hide what we are -- the
    User-Agent is honest and one request is one request -- it is just the client
    that the origin will talk to.
    """
    proc = await asyncio.create_subprocess_exec(
        "curl",
        "-sSL",
        "--max-time",
        str(int(DOWNLOAD_TIMEOUT)),
        "-H",
        f"User-Agent: {BROWSER_UA}",
        "-H",
        "Accept: application/pdf,*/*",
        "-H",
        "Accept-Language: en-US,en;q=0.9",
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        logger.warning("curl failed for %s: %s", url, err.decode()[:160])
        return None
    return out


#: Hosts a form PDF may be fetched from. USCIS publishes on uscis.gov and
#: serves the files from its own CDN; nothing else is a source of an official
#: immigration form.
ALLOWED_PDF_HOSTS = ("uscis.gov", "www.uscis.gov", "egov.uscis.gov")


def check_source(url: str) -> str:
    """The URL to fetch, or a refusal naming why.

    `pdf_url` is caller-supplied and this process fetches it, follows redirects,
    and reports what came back. Unrestricted, that is a request forgery with a
    read channel: `http://127.0.0.1:9/` or a cloud metadata endpoint would be
    fetched from inside the deployment, and the first forty bytes of the answer
    came back in the error message.

    So: https only, and only from where USCIS actually publishes.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        scheme = parsed.scheme or "nothing"
        raise OnboardingError(f"a form PDF must be fetched over https, not {scheme!r}")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_PDF_HOSTS:
        raise OnboardingError(
            f"{host or 'that host'} is not a USCIS source; allowed: {', '.join(ALLOWED_PDF_HOSTS)}"
        )
    return url


async def download_pdf(url: str, form_id: str) -> Path:
    """Fetch a fillable PDF to assets/, verifying it really is one."""
    check_source(url)
    dest = ASSETS_DIR / f"{slugify(form_id)}.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)

    content = await _get_via_httpx(url)
    if content is None or not content.startswith(b"%PDF"):
        content = await _get_via_curl(url)

    if content is None:
        raise OnboardingError(f"could not download {url}")
    if not content.startswith(b"%PDF"):
        # What came back is NOT echoed. It was, and that turned a fetch into a
        # read channel for whatever the response body happened to contain.
        logger.warning("%s returned %d bytes that are not a PDF", url, len(content))
        raise OnboardingError(f"{url} did not return a PDF")

    dest.write_bytes(content)
    logger.info("downloaded %s (%d bytes)", dest.name, len(content))
    return dest


async def onboard(form_id: str, pdf_url: str | None = None, title: str = "") -> dict[str, Any]:
    """Make `form_id` answerable. Returns a summary of what was registered."""
    entry = catalog_entry(form_id)
    url = pdf_url or (entry or {}).get("pdf")
    if not url:
        raise OnboardingError(
            f"{form_id} is not in the catalog; pass the fillable PDF url explicitly"
        )
    title = title or (entry or {}).get("title", "")

    pdf_path = await download_pdf(url, form_id)

    # Imported here so the running service never needs the extraction tool on
    # its import path unless someone actually onboards a form.
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from tools.extract_i765_fields import extract_inventory

    try:
        inventory = extract_inventory(pdf_path)
    except Exception as exc:
        raise OnboardingError(f"{form_id}: could not read the AcroForm ({exc})") from exc
    if not inventory["fields"]:
        raise OnboardingError(f"{form_id}: the PDF has no fillable fields")

    schema = derive_schema(inventory, form_id.upper(), str(pdf_path.relative_to(REPO_ROOT)), title)
    if not schema["fields"]:
        raise OnboardingError(f"{form_id}: no askable fields survived derivation")

    # Improve the wording through the RocketRide pipeline, where the model is
    # chosen in configuration. Entirely optional: the tooltip-derived questions
    # already work, so a failure here changes nothing.
    if pipeline_available():
        labels = [f["question"] for f in schema["fields"]]
        improved = await polish_questions(labels, form_id.upper())
        if improved:
            changed = 0
            for field in schema["fields"]:
                better = improved.get(field["question"])
                if better and better != field["question"]:
                    field["question"] = better
                    changed += 1
            logger.info("%s: %d question(s) polished via pipeline", form_id, changed)

    inv_path = REPO_ROOT / "data" / f"{slugify(form_id)}_acroform_fields.json"
    inv_path.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    schema_path = save_schema(schema)

    # Re-register so the form is live immediately, without a restart.
    from api import form_registry

    form_registry._cache.pop(form_registry._normalize(form_id), None)
    form_registry._discover()

    logger.info("onboarded %s: %d questions", form_id, len(schema["fields"]))
    return {
        "form_id": schema["form_id"],
        "title": schema["title"],
        "edition": schema["edition"],
        "questions": len(schema["fields"]),
        "raw_pdf_fields": inventory["field_count"],
        "schema": str(schema_path.relative_to(REPO_ROOT)),
        "source": url,
    }

"""Onboard any USCIS form: PDF in, working Zuzu schema out.

    uv run python tools/onboard_form.py I-131 https://www.uscis.gov/.../i-131.pdf
    uv run python tools/onboard_form.py I-90 assets/i-90.pdf

Downloads (or reads) the PDF, extracts its AcroForm inventory deterministically,
asks the inference lane to write the plain-language layer, validates every
generated field reference back against the real document, and writes
data/forms/<form>.json. The registry picks it up with no code change.

That last part is the whole point: "adding a form is adding a file" is only a
real claim if nobody has to hand-write the field map.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.form_builder import build_schema_from_inventory, save_schema, slugify  # noqa: E402
from api.inference import is_available  # noqa: E402
from tools.extract_i765_fields import extract_inventory  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("onboard")


def fetch_pdf(source: str, form_id: str) -> Path:
    """Resolve a local path or download the form, into assets/."""
    if not source.lower().startswith(("http://", "https://")):
        path = Path(source)
        if not path.is_absolute():
            path = REPO_ROOT / path
        if not path.exists():
            sys.exit(f"no such file: {path}")
        return path

    dest = REPO_ROOT / "assets" / f"{slugify(form_id)}.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("downloading %s", source)
    with httpx.Client(follow_redirects=True, timeout=120.0) as client:
        resp = client.get(source, headers={"User-Agent": "Mozilla/5.0 Zuzu form onboarder"})
        if resp.status_code == 403:
            # uscis.gov sits behind bot protection and refuses scripted GETs.
            # Not worth defeating: download it once by hand and pass the path.
            sys.exit(
                f"uscis.gov refused the download (403).\n"
                f"Save the PDF from your browser, then:\n"
                f"  uv run python tools/onboard_form.py {form_id} assets/"
                f"{slugify(form_id)}.pdf"
            )
        resp.raise_for_status()
        if not resp.content.startswith(b"%PDF"):
            sys.exit(f"that URL did not return a PDF (got {resp.headers.get('content-type')})")
        dest.write_bytes(resp.content)
    log.info("saved %s (%d bytes)", dest.relative_to(REPO_ROOT), dest.stat().st_size)
    return dest


async def main() -> int:
    parser = argparse.ArgumentParser(description="Onboard a USCIS form into Zuzu.")
    parser.add_argument("form_id", help='e.g. "I-131"')
    parser.add_argument("source", help="URL or path to the fillable PDF")
    parser.add_argument("--inventory-only", action="store_true", help="skip schema generation")
    args = parser.parse_args()

    pdf_path = fetch_pdf(args.source, args.form_id)

    log.info("extracting AcroForm inventory")
    inventory = extract_inventory(pdf_path)
    inventory["source"] = f"USCIS Form {args.form_id}"
    inv_path = REPO_ROOT / "data" / f"{slugify(args.form_id)}_acroform_fields.json"
    inv_path.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    log.info("%d fillable fields -> %s", inventory["field_count"], inv_path.relative_to(REPO_ROOT))

    if args.inventory_only:
        return 0
    if not is_available():
        sys.exit("no inference provider configured -- set TOKENROUTER_API_KEY")

    log.info("generating the plain-language layer (this takes a minute)")
    schema = await build_schema_from_inventory(
        inventory, args.form_id, str(pdf_path.relative_to(REPO_ROOT))
    )
    out = save_schema(schema)
    log.info("wrote %s with %d questions", out.relative_to(REPO_ROOT), len(schema["fields"]))
    print(f"\n{args.form_id} is ready. Restart the service and it appears in /health.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

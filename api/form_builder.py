"""Turn any USCIS AcroForm PDF into a Zuzu form schema.

Spec: prompts/form_builder_Python.prompt

This is the module that makes the product claim literal. Zuzu says a form is
data, not code; that is only true if adding a form does not require a person to
hand-write 32 field mappings the way I-765's were written.

The split matters:

  * The **field inventory** is extracted deterministically from the PDF. Field
    names, types, checkbox export values, and length limits are facts, and a
    model must never be asked to recall them -- that is how you ship a form
    that silently drops half an applicant's answers.
  * The **plain-language layer** is generated. Turning
    "Pt2Line5_AptSteFlrNumber[0]" into "What is your apartment number?", in a
    sensible asking order, is genuinely a language problem, and long-context
    models are good at it.

Every generated schema is then validated back against the inventory, so a
hallucinated field name fails loudly at build time rather than quietly at
filing time.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from api.i765_schema import REPO_ROOT, FormSchema
from api.inference import complete_json

logger = logging.getLogger(__name__)

FORMS_DIR = REPO_ROOT / "data" / "forms"

#: Anything matching these is identity-bearing and must be flagged sensitive
#: regardless of what the model decides.
SENSITIVE_PATTERNS = (
    "ssn",
    "social",
    "alien",
    "a_number",
    "anumber",
    "passport",
    "i94",
    "i-94",
    "sevis",
    "account",
    "receipt",
)

SYSTEM = """\
You convert a USCIS form's raw PDF field inventory into a conversational schema
for a voice assistant that helps immigrants complete immigration forms by phone.

The people answering may have low literacy, may not speak English natively, and
are under real stress. Questions must sound like a patient human asking, not
like a form label read aloud.

Rules:
- Use ONLY pdf field names that appear in the inventory given to you. Never
  invent, correct, or complete a field name.
- Ask one thing at a time, in the order a person would naturally answer:
  who you are, where you live, your identifiers, your history, your contact.
- Skip fields the applicant cannot answer: barcodes, attorney/preparer blocks,
  signature boxes, and any field marked read-only.
- Mark anything identity-bearing as sensitive: SSN, A-Number, passport, I-94,
  SEVIS, USCIS account, receipt numbers.
- Output ONLY valid JSON. No prose, no code fences.
- Answer directly. Do not deliberate at length before responding: a long
  internal monologue exhausts the response budget and returns nothing usable.
"""


def _looks_sensitive(field_id: str, pdf_name: str) -> bool:
    blob = f"{field_id} {pdf_name}".lower()
    return any(p in blob for p in SENSITIVE_PATTERNS)


def _askable(entry: dict[str, Any]) -> bool:
    """Whether a raw PDF field is worth asking a human about."""
    name = entry["name"].lower()
    if "barcode" in name or "pdf417" in name:
        return False
    if "ReadOnly" in (entry.get("flags") or []):
        return False
    # Attorney, interpreter, and preparer blocks are filled by someone else.
    if any(t in name for t in ("attorney", "interpreter", "preparer", "representative")):
        return False
    if "signature" in name:
        return False
    return True


def _digest_inventory(inventory: dict[str, Any], limit: int = 220) -> list[dict[str, Any]]:
    """Compact the inventory into what the model actually needs to decide."""
    out: list[dict[str, Any]] = []
    for entry in inventory["fields"]:
        if not _askable(entry):
            continue
        item: dict[str, Any] = {
            "pdf_field": entry["name"],
            "type": entry["type"],
            "page": entry.get("page"),
            "label": (entry.get("label") or "")[:220],
        }
        if entry.get("max_len"):
            item["max_len"] = entry["max_len"]
        if entry.get("on_value"):
            item["on_value"] = entry["on_value"]
        if entry.get("options"):
            item["options"] = entry["options"][:8] + (["..."] if len(entry["options"]) > 8 else [])
        out.append(item)
        if len(out) >= limit:
            break
    return out


def build_prompt(inventory: dict[str, Any], form_id: str) -> str:
    return (
        f"Form: {form_id} ({inventory.get('source', '')}), "
        f"edition {inventory.get('edition', 'unknown')}.\n\n"
        "Raw fillable field inventory:\n"
        f"{json.dumps(_digest_inventory(inventory), indent=1)}\n\n"
        "Produce a JSON object with keys: form_id, title, agency, edition, pdf, fields.\n"
        "Each entry in `fields` has:\n"
        '  id            snake_case, e.g. "family_name"\n'
        "  question      the spoken question, one sentence, plain language\n"
        "  type          text|date|choice|state|zip|phone|email|ssn|a_number\n"
        "  group         one of: reason, name, address, identifiers, "
        "birth_citizenship, arrival_status, eligibility, contact\n"
        '  memory_key    dotted path, e.g. "personal.family_name"\n'
        "  sensitive     boolean\n"
        "  required      boolean\n"
        "  pdf_field     the exact pdf_field from the inventory (text fields)\n"
        "  options       for choice fields: [{value,label,pdf_field,pdf_value}] "
        "where pdf_value is that field's on_value from the inventory\n"
    )


def validate_against_inventory(schema: dict[str, Any], inventory: dict[str, Any]) -> list[str]:
    """Every generated pdf reference must exist in the real document."""
    known = {f["name"]: f for f in inventory["fields"]}
    problems: list[str] = []
    seen_ids: set[str] = set()

    for field in schema.get("fields", []):
        fid = field.get("id", "?")
        if fid in seen_ids:
            problems.append(f"{fid}: duplicate field id")
        seen_ids.add(fid)

        refs: list[tuple[str, str | None]] = []
        if field.get("pdf_field"):
            refs.append((field["pdf_field"], None))
        for opt in field.get("options", []) or []:
            refs.append((opt.get("pdf_field", ""), opt.get("pdf_value")))
        if not refs:
            problems.append(f"{fid}: no pdf destination")

        for name, on_value in refs:
            real = known.get(name)
            if real is None:
                problems.append(f"{fid}: invented pdf field {name!r}")
                continue
            if on_value is not None and real.get("on_value") != on_value:
                problems.append(
                    f"{fid}/{name}: on_value {on_value!r} != actual {real.get('on_value')!r}"
                )
    return problems


def repair(schema: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    """Drop what cannot be trusted and harden what can.

    Correcting a checkbox export value is safe -- the truth is in the document.
    Correcting an invented field name is not, so those entries are dropped: a
    question whose answer goes nowhere wastes a real person's time.
    """
    known = {f["name"]: f for f in inventory["fields"]}
    kept: list[dict[str, Any]] = []

    for field in schema.get("fields", []):
        if field.get("pdf_field") and field["pdf_field"] not in known:
            logger.warning("dropping %s: invented pdf field", field.get("id"))
            continue

        options = []
        for opt in field.get("options", []) or []:
            real = known.get(opt.get("pdf_field", ""))
            if real is None:
                continue
            opt["pdf_value"] = real.get("on_value") or opt.get("pdf_value")
            options.append(opt)
        if field.get("options") is not None:
            if not options:
                logger.warning("dropping %s: no valid options", field.get("id"))
                continue
            field["options"] = options

        # Length limits and sensitivity are facts about the document, not
        # opinions the model gets to hold.
        if field.get("pdf_field"):
            real_max = known[field["pdf_field"]].get("max_len")
            if real_max:
                field["max_len"] = min(field.get("max_len") or real_max, real_max)
        if _looks_sensitive(field.get("id", ""), field.get("pdf_field", "")):
            field["sensitive"] = True

        field.setdefault("required", False)
        field.setdefault("read_back", bool(field.get("sensitive")))
        kept.append(field)

    schema["fields"] = kept
    return schema


#: Fields per request. Tuned by watching it fail: at 220 fields the model spent
#: its entire budget inside <think> and returned an empty string; at 35 only one
#: batch in four came back as parseable JSON, one of them truncated mid-object.
#: Small batches plus a large ceiling leave room for the reasoning AND the
#: answer. Smaller batches also produce better questions, because the model
#: reads one part of the form instead of skimming all of it.
CHUNK_SIZE = 14


async def build_schema_from_inventory(
    inventory: dict[str, Any], form_id: str, pdf_rel_path: str
) -> dict[str, Any]:
    """Generate, repair, and validate a schema for one form, in batches."""
    askable = [e for e in inventory["fields"] if _askable(e)]
    batches = [askable[i : i + CHUNK_SIZE] for i in range(0, len(askable), CHUNK_SIZE)]
    logger.info("%s: %d askable fields in %d batch(es)", form_id, len(askable), len(batches))

    raw: dict[str, Any] = {"fields": []}
    seen_ids: set[str] = set()

    for index, batch in enumerate(batches, start=1):
        sub_inventory = dict(inventory)
        sub_inventory["fields"] = batch
        try:
            # Generous timeout on purpose: a build-time step against a reasoning
            # model, never a live request.
            part = await complete_json(
                build_prompt(sub_inventory, form_id),
                system=SYSTEM,
                max_tokens=32768,
                timeout=600.0,
            )
        except Exception as exc:
            # One bad batch should not lose the whole form.
            logger.warning(
                "%s batch %d/%d failed (%s); continuing", form_id, index, len(batches), exc
            )
            continue

        fields = part.get("fields", []) if isinstance(part, dict) else part
        if not isinstance(fields, list):
            continue
        for field in fields:
            fid = field.get("id")
            if not fid or fid in seen_ids:
                continue
            seen_ids.add(fid)
            raw["fields"].append(field)
        # Carry the form-level metadata from whichever batch supplied it first.
        if isinstance(part, dict):
            for key in ("title", "agency"):
                raw.setdefault(key, part.get(key))
        logger.info(
            "%s: batch %d/%d -> %d fields so far",
            form_id,
            index,
            len(batches),
            len(raw["fields"]),
        )

    if not raw["fields"]:
        raise ValueError(f"{form_id}: the model returned no usable fields")

    raw["title"] = raw.get("title") or f"USCIS Form {form_id}"
    raw.setdefault("form_id", form_id)
    raw.setdefault("agency", "USCIS")
    raw["edition"] = inventory.get("edition", "unknown")
    raw["pdf"] = pdf_rel_path
    raw["notes"] = (
        "Generated by api/form_builder.py from the PDF's own field inventory, "
        "then validated back against it. Field names are extracted, never recalled."
    )

    raw = repair(raw, inventory)
    problems = validate_against_inventory(raw, inventory)
    if problems:
        logger.warning("%s: %d issue(s) after repair: %s", form_id, len(problems), problems[:5])
    if not raw["fields"]:
        raise ValueError(f"{form_id}: no usable fields survived validation")

    # Parsing it proves the result is loadable by the running service.
    FormSchema.model_validate(raw)
    logger.info("built schema form=%s fields=%d", form_id, len(raw["fields"]))
    return raw


def slugify(form_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", form_id.lower()).strip("_")


# ---------------------------------------------------------------------------
# Deterministic path
# ---------------------------------------------------------------------------

_GROUP_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("name", ("familyname", "givenname", "middlename", "othernames", "fullname")),
    (
        "address",
        (
            "street",
            "city",
            "state",
            "zip",
            "province",
            "postal",
            "apt",
            "ste",
            "flr",
            "unit",
            "incareof",
            "mailing",
            "physicaladdress",
            "countryaddress",
        ),
    ),
    (
        "identifiers",
        (
            "aliennumber",
            "anumber",
            "elisaccount",
            "uscisaccount",
            "ssn",
            "socialsecurity",
            "receipt",
            "accountnumber",
        ),
    ),
    (
        "birth_citizenship",
        (
            "dateofbirth",
            "dob",
            "countryofbirth",
            "cityofbirth",
            "placeofbirth",
            "citizenship",
            "nationality",
        ),
    ),
    (
        "arrival_status",
        (
            "i94",
            "passport",
            "traveldoc",
            "sevis",
            "arrival",
            "departure",
            "lastentry",
            "status",
            "admission",
            "visa",
        ),
    ),
    ("eligibility", ("eligibility", "category", "classification", "basis")),
    ("contact", ("phone", "mobile", "email", "daytime", "telephone", "fax")),
)

_TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("date", ("date", "dob", "expdate", "expiration", "validfrom", "validto")),
    ("email", ("email",)),
    ("phone", ("phone", "telephone", "mobile", "fax")),
    ("zip", ("zipcode", "zip")),
    ("ssn", ("ssn", "socialsecurity")),
    ("a_number", ("aliennumber", "anumber")),
    ("state", ("state",)),
)


def _classify(name: str, label: str) -> tuple[str, str]:
    """(group, type) for a raw field, from its name and tooltip."""
    blob = re.sub(r"[^a-z0-9]", "", f"{name} {label}".lower())
    group = "other"
    for candidate, keys in _GROUP_RULES:
        if any(k in blob for k in keys):
            group = candidate
            break
    kind = "text"
    for candidate, keys in _TYPE_RULES:
        if any(k in blob for k in keys):
            kind = candidate
            break
    return group, kind


def _question_from_label(label: str, field_id: str) -> str:
    """Turn a USCIS tooltip into something a person can answer out loud.

    Tooltips read like "Part 2. Information About You. 1.a. Family Name (Last
    Name)". The part/item prefix is navigation for someone holding the paper
    form, and noise to someone on a phone call.
    """
    text = re.sub(r"\s+", " ", label or "").strip()
    # Drop leading "Part N." / "Item Number 3.a." / bare "1.b." prefixes.
    text = re.sub(
        r"^(part\s*\d+\.?\s*|item\s*number\s*[\d.a-z]*\.?\s*|\d+\.[a-z]?\.?\s*)+",
        "",
        text,
        flags=re.I,
    )
    # Drop a leading section title that ends in a period, e.g. "Information About You. "
    text = re.sub(r"^[A-Z][^.]{4,60}\.\s+(?=[A-Z0-9])", "", text)
    # Item numbering also shows up mid-label after a section title -- "Your Name.
    # 4. A. Enter Family Name" -- so strip these tokens wherever they appear.
    text = re.sub(r"\b\d{1,2}\.\s*(?:[A-Za-z]\.\s*)?", "", text)
    text = re.sub(r"^[A-Za-z]\.\s+", "", text)
    text = re.sub(r"^[A-Z][a-z]+(?:\s+[A-Za-z]+){0,3}\.\s+(?=[A-Z])", "", text)
    text = re.sub(r"^(enter|provide|select|type|please)\s+", "", text, flags=re.I)
    # Parenthetical hints and cross-references are for someone holding the paper
    # form, not for someone answering out loud.
    text = re.sub(r"\s*\([^)]{12,}\)", "", text)
    text = re.sub(r"\s*(see the instructions|if any|if applicable).*$", "", text, flags=re.I)
    text = text.strip(" .:;,-")
    # One clause only: some USCIS labels run to a whole paragraph.
    text = re.split(r"(?<=[a-z])\.\s+(?=[A-Z])", text)[0].strip(" .")
    if len(text) > 92:
        text = text[:92].rsplit(" ", 1)[0]
    if not text:
        text = field_id.replace("_", " ")
    if text.endswith("?"):
        return text
    # Avoid "What is your your ..." when the label already begins with "Your".
    lead = re.sub(r"^your\s+", "", text, flags=re.I).strip(" .")
    if not lead:
        lead = field_id.replace("_", " ")
    # Only downcase an ordinary word. "U S C I S" and "U.S." are acronyms and
    # lowercasing them makes the question read as a typo.
    first = lead.split(" ", 1)[0]
    if not (first.isupper() or "." in first or len(first) == 1):
        lead = lead[0].lower() + lead[1:]
    return f"What is your {lead}?"


def _field_id_from(name: str, label: str, taken: set[str]) -> str:
    """A stable snake_case id, preferring the PDF's own leaf name."""
    leaf = name.split(".")[-1]
    leaf = re.sub(r"\[\d+\]$", "", leaf)
    leaf = re.sub(r"^(Pt\d+)?Line\d*[a-z]?_?", "", leaf)
    base = re.sub(r"(?<!^)(?=[A-Z])", "_", leaf).lower()
    base = re.sub(r"[^a-z0-9]+", "_", base).strip("_") or "field"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}_{n}", n + 1
    taken.add(candidate)
    return candidate


def derive_schema(
    inventory: dict[str, Any], form_id: str, pdf_rel_path: str, title: str = ""
) -> dict[str, Any]:
    """Build a working schema from the PDF alone, with no model involved.

    Every USCIS field carries a `/TU` tooltip written for screen readers, which
    is already close to a spoken question. Using it means a new form works
    immediately and identically every time, instead of depending on a reasoning
    model returning well-formed JSON for two hundred fields.

    `build_schema_from_inventory` can then improve the wording. This is the
    floor, not the ceiling -- but a reliable floor is what makes the claim that
    Zuzu supports any USCIS form actually true.
    """
    fields: list[dict[str, Any]] = []
    taken: set[str] = set()
    # Group checkbox siblings so a set of related boxes becomes one question.
    button_groups: dict[str, list[dict[str, Any]]] = {}

    for entry in inventory["fields"]:
        if not _askable(entry):
            continue
        name, label = entry["name"], entry.get("label") or ""
        if entry["type"] == "/Btn":
            parent = re.sub(r"\[\d+\]$", "", name)
            button_groups.setdefault(parent, []).append(entry)
            continue

        field_id = _field_id_from(name, label, taken)
        group, kind = _classify(name, label)
        if entry.get("options"):
            kind = "state" if kind == "state" else "text"
        field: dict[str, Any] = {
            "id": field_id,
            "question": _question_from_label(label, field_id),
            "type": kind,
            "group": group,
            "memory_key": f"{group}.{field_id}",
            "sensitive": _looks_sensitive(field_id, name),
            "required": False,
            "pdf_field": name,
        }
        if entry.get("max_len"):
            field["max_len"] = entry["max_len"]
        if field["sensitive"]:
            field["read_back"] = True
        fields.append(field)

    for parent, entries in button_groups.items():
        usable = [e for e in entries if e.get("on_value")]
        if not usable:
            continue
        label = usable[0].get("label") or parent
        field_id = _field_id_from(parent, label, taken)
        group, _ = _classify(parent, label)
        fields.append(
            {
                "id": field_id,
                "question": _question_from_label(label, field_id),
                "type": "choice",
                "group": group,
                "memory_key": f"{group}.{field_id}",
                "sensitive": False,
                "required": False,
                "options": [
                    {
                        "value": (e.get("on_value") or "").strip("/ ").lower() or f"option_{i}",
                        "label": (e.get("label") or "")[:80] or f"Option {i + 1}",
                        "pdf_field": e["name"],
                        "pdf_value": e["on_value"],
                    }
                    for i, e in enumerate(usable)
                ],
            }
        )

    schema = {
        "form_id": form_id,
        "title": title or f"USCIS Form {form_id}",
        "agency": "USCIS",
        "edition": inventory.get("edition", "unknown"),
        "pdf": pdf_rel_path,
        "notes": (
            "Derived deterministically from the PDF's own field inventory and "
            "screen-reader tooltips. No field name is ever recalled by a model."
        ),
        "fields": fields,
    }
    FormSchema.model_validate(schema)
    logger.info("derived schema form=%s fields=%d (deterministic)", form_id, len(fields))
    return schema


def save_schema(schema: dict[str, Any]) -> Path:
    FORMS_DIR.mkdir(parents=True, exist_ok=True)
    path = FORMS_DIR / f"{slugify(schema['form_id'])}.json"
    path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    return path

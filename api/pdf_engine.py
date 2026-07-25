"""Fill the official USCIS I-765 AcroForm.

Spec: prompts/pdf_engine_Python.prompt

Every non-obvious step below exists because the obvious version fails silently:
it reports success and hands the applicant a blank form.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import BooleanObject, DictionaryObject, NameObject, TextStringObject

from api.i765_schema import SKIP_SENTINEL, FormField, FormSchema, get_i765_schema

logger = logging.getLogger(__name__)

#: (c)(3)(B), c3b, C 3 b -- letter, digits, optional trailing letter.
_ELIGIBILITY_RE = re.compile(r"^\(?([A-Za-z])\)?\s*\(?(\d{1,2})\)?\s*\(?([A-Za-z])?\)?$")

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class PdfFillError(RuntimeError):
    """Raised when the source PDF cannot be opened or filled."""


def _normalize(value: str, form_field: FormField, schema_options: list[str]) -> str | None:
    """Coerce a spoken answer into what this particular field will accept."""
    text = value.strip()
    if not text:
        return None

    kind = form_field.type
    if kind in ("ssn", "a_number", "zip", "phone"):
        text = re.sub(r"\D", "", text)
    elif kind == "state":
        text = text.upper()
        if schema_options and text not in schema_options:
            # The combo has no Edit flag: anything not in /Opt is discarded by
            # the viewer anyway, so refuse rather than write a value that will
            # vanish without trace.
            logger.warning("field=%s rejected: not a valid state export code", form_field.id)
            return None
    elif kind == "date":
        match = _DATE_RE.match(text)
        if match:
            year, month, day = match.groups()
            text = f"{month}/{day}/{year}"

    if not text:
        return None
    if form_field.max_len:
        text = text[: form_field.max_len]
    return text


def _split_eligibility(value: str) -> tuple[str, str, str] | None:
    match = _ELIGIBILITY_RE.match(value.strip())
    if not match:
        return None
    letter, digits, trailing = match.groups()
    return letter.lower(), digits, (trailing or "").upper()


def _usable(values: dict[str, str], field_id: str) -> str | None:
    raw = values.get(field_id)
    if raw is None:
        return None
    text = raw.strip()
    if not text or text == SKIP_SENTINEL:
        return None
    return text


def missing_required(values: dict[str, str], schema: FormSchema | None = None) -> list[str]:
    """Required fields with no usable value, so callers can refuse early."""
    schema = schema or get_i765_schema()
    return [f.id for f in schema.fields if f.required and _usable(values, f.id) is None]


def fill_i765(
    values: dict[str, str],
    out_path: str | Path,
    schema: FormSchema | None = None,
) -> Path:
    """Write a filled I-765 to `out_path` and return it.

    `values` is keyed by schema field id, never by PDF field name.
    """
    schema = schema or get_i765_schema()
    source = schema.pdf_path()
    if not source.exists():
        raise PdfFillError(f"source PDF not found: {source}")

    try:
        reader = PdfReader(str(source))
    except Exception as exc:  # pragma: no cover - depends on the vendored file
        raise PdfFillError(f"could not open {source}: {exc}") from exc

    if reader.is_encrypted:
        # Published USCIS forms are AES-128 encrypted with an EMPTY user
        # password. Needs pypdf[crypto]; without it this raises DependencyError.
        try:
            if not reader.decrypt(""):
                raise PdfFillError("could not decrypt the source PDF with an empty password")
        except PdfFillError:
            raise
        except Exception as exc:
            raise PdfFillError(
                f"decrypting {source.name} failed ({exc}). Install the crypto extra: "
                "uv sync  (pypdf[crypto] provides the AES backend)"
            ) from exc

    writer = PdfWriter(clone_from=reader)
    acroform = writer._root_object.get("/AcroForm")
    if acroform is not None:
        acroform = acroform.get_object()
    if not isinstance(acroform, DictionaryObject):
        raise PdfFillError("no /AcroForm in the source PDF; it is not a fillable form")

    # This is a hybrid AcroForm + static XFA file. Filling only the AcroForm
    # leaves the XFA datasets packet stale, and Acrobat prefers XFA -- so the
    # applicant opens a "completed" form and sees a blank one. The XFA here is
    # static (dynamicRender=forbidden), so dropping it costs nothing.
    acroform.pop(NameObject("/XFA"), None)

    # 112 of 119 text fields ship with no appearance stream and NeedAppearances
    # is not set, so /V alone renders nothing in many viewers.
    acroform[NameObject("/NeedAppearances")] = BooleanObject(True)

    # Read the state combo's legal export values straight from the document.
    valid_states: list[str] = []
    for page in writer.pages:
        for annot in page.get("/Annots") or []:
            obj = annot.get_object()
            opts = obj.get("/Opt")
            if opts:
                for opt in opts:
                    opt = opt.get_object()
                    code = str(opt[0].get_object()) if isinstance(opt, list) else str(opt)
                    valid_states.append(code)
                break

    text_updates: dict[str, str] = {}
    button_updates: dict[str, str] = {}
    filled_ids: list[str] = []
    skipped_ids: list[str] = []

    for form_field in schema.fields:
        raw = _usable(values, form_field.id)
        if raw is None:
            skipped_ids.append(form_field.id)
            continue

        if form_field.options:
            option = form_field.option_for(raw)
            if option is None:
                logger.warning("field=%s rejected: not a known option", form_field.id)
                skipped_ids.append(form_field.id)
                continue
            for candidate in form_field.options:
                button_updates[candidate.pdf_field] = (
                    option.pdf_value if candidate is option else "/Off"
                )
            filled_ids.append(form_field.id)
            continue

        if form_field.pdf_field_parts:
            parts = _split_eligibility(raw)
            if parts is None:
                # A wrong eligibility category is the most consequential error
                # on this form. Leave all three boxes blank rather than guess.
                logger.warning("field=%s unparseable; leaving blank", form_field.id)
                skipped_ids.append(form_field.id)
                continue
            for name, part in zip(form_field.pdf_field_parts, parts, strict=False):
                if part:
                    text_updates[name] = part
            filled_ids.append(form_field.id)
            continue

        normalized = _normalize(raw, form_field, valid_states)
        if normalized is None:
            skipped_ids.append(form_field.id)
            continue
        if form_field.pdf_field:
            text_updates[form_field.pdf_field] = normalized
            filled_ids.append(form_field.id)

    for page in writer.pages:
        if text_updates:
            writer.update_page_form_field_values(page, text_updates, auto_regenerate=False)
        for annot in page.get("/Annots") or []:
            obj = annot.get_object()
            name = _qualified_name(obj)
            if name in button_updates:
                state = NameObject(button_updates[name])
                obj[NameObject("/V")] = state
                obj[NameObject("/AS")] = state

    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        writer.write(handle)

    # Field ids only -- never values. This log line is safe to ship.
    logger.info(
        "filled form=%s fields_written=%d fields_blank=%d out=%s",
        schema.form_id,
        len(filled_ids),
        len(skipped_ids),
        destination.name,
    )
    return destination


def _qualified_name(obj: DictionaryObject) -> str:
    """Fully-qualified field name, walking /Parent to the root."""
    parts: list[str] = []
    node: object = obj
    seen: set[int] = set()
    while isinstance(node, DictionaryObject):
        if id(node) in seen:
            break
        seen.add(id(node))
        partial = node.get("/T")
        if partial is not None:
            value = partial.get_object()
            parts.append(str(value) if not isinstance(value, TextStringObject) else str(value))
        parent = node.get("/Parent")
        node = parent.get_object() if parent is not None else None
    return ".".join(reversed(parts))

"""Extract the AcroForm field inventory from the official USCIS I-765 PDF.

This is a build-time data tool, not part of the running service. It produces
``data/i765_acroform_fields.json`` -- the ground-truth list of every fillable
field name on the form, which the I-765 schema in ``api/forms/`` maps onto.

Regenerate whenever USCIS publishes a new edition::

    uv run --no-project --with 'pypdf[crypto]' python tools/extract_i765_fields.py

Why this exists: the field names on this form do not match the printed item
numbers (``Line7_AlienNumber`` is item 8, ``Line19_DOB`` is item 16), and the
checkbox export values are irregular per-field names rather than a uniform
``/Yes``. Hand-transcribing them is how you ship a form that silently drops
half the applicant's answers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

REPO_ROOT = Path(__file__).resolve().parent.parent
PDF_PATH = REPO_ROOT / "assets" / "i-765.pdf"
OUT_PATH = REPO_ROOT / "data" / "i765_acroform_fields.json"

# /Ff bit positions we care about (1-indexed per the PDF spec).
FLAG_READ_ONLY = 1 << 0
FLAG_MULTILINE = 1 << 12
FLAG_COMB = 1 << 24


def _resolve(obj: Any) -> Any:
    """Follow an indirect reference to the object it points at."""
    return obj.get_object() if isinstance(obj, IndirectObject) else obj


def _qualified_name(field: DictionaryObject) -> str:
    """Build the fully-qualified field name by walking /Parent up to the root."""
    parts: list[str] = []
    node: Any = field
    seen: set[int] = set()
    while isinstance(node, DictionaryObject):
        if id(node) in seen:  # defensive: malformed PDFs can cycle
            break
        seen.add(id(node))
        partial = node.get("/T")
        if partial is not None:
            parts.append(str(_resolve(partial)))
        node = _resolve(node.get("/Parent"))
    return ".".join(reversed(parts))


def _inherited(field: DictionaryObject, key: str) -> Any:
    """Look up a key on the field, falling back to inherited /Parent values."""
    node: Any = field
    seen: set[int] = set()
    while isinstance(node, DictionaryObject):
        if id(node) in seen:
            break
        seen.add(id(node))
        if key in node:
            return _resolve(node[key])
        node = _resolve(node.get("/Parent"))
    return None


def _checkbox_on_value(field: DictionaryObject) -> str | None:
    """Read a button's 'on' export value from its normal appearance dictionary.

    Every checkbox on this form is an independent field with its own unique
    on-state (``/1``, ``/Y``, ``/Single``, ``/ APT `` ...). There are no radio
    groups, so each one must be set individually.
    """
    appearances = _resolve(field.get("/AP"))
    if not isinstance(appearances, DictionaryObject):
        return None
    normal = _resolve(appearances.get("/N"))
    if not isinstance(normal, DictionaryObject):
        return None
    for state in normal:
        if str(state) != "/Off":
            return str(state)
    return None


def _decode_flags(flags: int) -> list[str]:
    names = []
    if flags & FLAG_READ_ONLY:
        names.append("ReadOnly")
    if flags & FLAG_MULTILINE:
        names.append("Multiline")
    if flags & FLAG_COMB:
        names.append("Comb")
    return names


def extract() -> dict[str, Any]:
    reader = PdfReader(str(PDF_PATH))
    if reader.is_encrypted:
        # The published form is AES-128 encrypted with an EMPTY user password.
        reader.decrypt("")

    # Map each widget annotation to its 1-indexed page.
    page_of: dict[int, int] = {}
    for page_index, page in enumerate(reader.pages, start=1):
        for annot in page.get("/Annots") or []:
            page_of[id(_resolve(annot))] = page_index

    root = reader.trailer["/Root"]
    acroform = _resolve(root.get("/AcroForm"))
    if acroform is None:
        raise SystemExit("No /AcroForm in this PDF -- it is not a fillable form.")

    fields: list[dict[str, Any]] = []
    stack: list[Any] = list(_resolve(acroform.get("/Fields")) or [])
    while stack:
        node = _resolve(stack.pop(0))
        if not isinstance(node, DictionaryObject):
            continue
        kids = _resolve(node.get("/Kids"))
        # A node with kids that themselves carry /T is an intermediate node.
        if isinstance(kids, ArrayObject):
            named_kids = [k for k in kids if "/T" in (_resolve(k) or {})]
            if named_kids:
                stack.extend(kids)
                continue

        field_type = _inherited(node, "/FT")
        if field_type is None:
            continue

        flags = int(_inherited(node, "/Ff") or 0)
        max_len = _inherited(node, "/MaxLen")
        tooltip = _inherited(node, "/TU")
        options = _inherited(node, "/Opt")

        entry: dict[str, Any] = {
            "name": _qualified_name(node),
            "type": str(field_type),
            "page": page_of.get(id(node)),
            "label": str(tooltip) if tooltip is not None else None,
        }
        if flags:
            entry["flags"] = _decode_flags(flags)
        if max_len is not None:
            entry["max_len"] = int(max_len)
        if str(field_type) == "/Btn":
            entry["on_value"] = _checkbox_on_value(node)
            entry["off_value"] = "/Off"
        if isinstance(options, ArrayObject):
            # /Opt entries are either a bare string or an [export, display] pair.
            # Only the export value is legal to write into /V.
            exports = []
            for opt in options:
                opt = _resolve(opt)
                exports.append(str(_resolve(opt[0])) if isinstance(opt, ArrayObject) else str(opt))
            entry["options"] = exports
        fields.append(entry)

    fields.sort(key=lambda f: (f["page"] or 0, f["name"]))
    return {
        "source": "USCIS Form I-765, Application for Employment Authorization",
        "edition": "08/21/25",
        "omb": "1615-0040",
        "expires": "08/31/2027",
        "pages": len(reader.pages),
        "encrypted": "AES-128, empty user password",
        "xfa_present": "/XFA" in acroform,
        "need_appearances": bool(acroform.get("/NeedAppearances", False)),
        "field_count": len(fields),
        "fields": fields,
    }


def main() -> None:
    data = extract()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {data['field_count']} fields -> {OUT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()

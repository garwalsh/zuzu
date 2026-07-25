"""The declarative schema must stay in sync with the real PDF.

These tests are the guardrail on the project's central claim -- that adding a
USCIS form means adding a schema file. A schema entry that points at a field
name the PDF does not have produces a form that silently drops the applicant's
answer, which in this domain means a rejected filing and months of delay.

Nothing here needs the service running, an API key, or a network connection.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INVENTORY_PATH = REPO_ROOT / "data" / "i765_acroform_fields.json"
SCHEMA_PATH = REPO_ROOT / "data" / "i765_form_schema.json"


@pytest.fixture(scope="module")
def inventory() -> dict[str, dict]:
    data = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    return {f["name"]: f for f in data["fields"]}


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _pdf_refs(field: dict) -> list[tuple[str, str | None]]:
    """Every (pdf_field_name, expected_on_value) this schema field writes to."""
    refs: list[tuple[str, str | None]] = []
    if field.get("pdf_field"):
        refs.append((field["pdf_field"], None))
    refs.extend((part, None) for part in field.get("pdf_field_parts", []))
    refs.extend((opt["pdf_field"], opt["pdf_value"]) for opt in field.get("options", []))
    return refs


def test_every_pdf_field_reference_exists(schema, inventory):
    """A schema pointing at a nonexistent field silently loses an answer."""
    missing = [
        f"{field['id']} -> {name}"
        for field in schema["fields"]
        for name, _ in _pdf_refs(field)
        if name not in inventory
    ]
    assert not missing, f"schema references fields absent from the PDF: {missing}"


def test_choice_export_values_match_the_pdf(schema, inventory):
    """Checkbox export values are irregular per-field (/1, /Y, /Single, / APT ).

    Writing a plausible-looking value such as /Yes leaves the box unchecked.
    """
    wrong = []
    for field in schema["fields"]:
        for name, expected in _pdf_refs(field):
            if expected is None:
                continue
            actual = inventory[name].get("on_value")
            if actual != expected:
                wrong.append(f"{field['id']}/{name}: schema {expected!r} != pdf {actual!r}")
    assert not wrong, f"checkbox export value mismatches: {wrong}"


def test_choice_fields_target_button_fields(schema, inventory):
    """An option must map to a /Btn; writing an export value into a text field
    puts a literal '/Y' on the printed form."""
    bad = [
        f"{field['id']} -> {name} is {inventory[name]['type']}"
        for field in schema["fields"]
        for name, expected in _pdf_refs(field)
        if expected is not None and inventory[name]["type"] != "/Btn"
    ]
    assert not bad, f"choice options must target /Btn fields: {bad}"


def test_declared_max_len_does_not_exceed_pdf_max_len(schema, inventory):
    """These fields are combed: overflowing MaxLen overflows the printed cells."""
    violations = []
    for field in schema["fields"]:
        declared = field.get("max_len")
        if not declared:
            continue
        for name, _ in _pdf_refs(field):
            actual = inventory[name].get("max_len")
            if actual is not None and declared > actual:
                violations.append(f"{field['id']}: declared {declared} > pdf {actual} on {name}")
    assert not violations, f"max_len exceeds the PDF's own limit: {violations}"


def test_state_field_is_a_closed_choice_list(schema, inventory):
    """State combos have no Edit flag, so only exact USPS export codes are
    writable. Free text such as 'California' is silently discarded."""
    state_fields = [f for f in schema["fields"] if f["type"] == "state"]
    assert state_fields, "expected at least one state-typed field in the schema"
    for field in state_fields:
        entry = inventory[field["pdf_field"]]
        assert entry["type"] == "/Ch", f"{field['id']} should map to a choice field"
        options = entry.get("options") or []
        assert len(options) > 50, f"{field['id']} should expose the full state list"
        assert "CA" in options and "NY" in options


def test_every_field_has_a_route_onto_the_form(schema):
    """A question with no destination wastes a real applicant's time."""
    orphans = [f["id"] for f in schema["fields"] if not _pdf_refs(f)]
    assert not orphans, f"schema fields with no PDF destination: {orphans}"


def test_field_ids_are_unique(schema):
    ids = [f["id"] for f in schema["fields"]]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate field ids: {dupes}"


def test_retired_items_are_not_referenced(schema):
    """Edition 08/21/25 removed the parents'-names and SSA-card-request items.

    Earlier planning docs still list them; a schema carrying them would be
    asking applicants questions this form has nowhere to put.
    """
    retired = ("father", "mother", "ssa", "wants_ssa")
    offenders = [
        f["id"]
        for f in schema["fields"]
        if any(term in f["id"].lower() or term in f["memory_key"].lower() for term in retired)
    ]
    assert not offenders, f"schema references items removed from this edition: {offenders}"


def test_schema_edition_matches_the_vendored_pdf(schema, inventory):
    del inventory  # edition lives on the inventory document, not a field
    data = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    assert schema["edition"] == data["edition"], (
        "schema edition and extracted PDF edition disagree -- regenerate "
        "data/i765_acroform_fields.json after replacing assets/i-765.pdf"
    )

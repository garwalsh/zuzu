"""Loader for the declarative I-765 form schema.

Spec: prompts/i765_schema_Python.prompt

The schema itself lives in data/i765_form_schema.json. This module only loads
and validates it, which is what makes "adding a form = adding a schema file"
literally true rather than aspirational.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA_PATH = REPO_ROOT / "data" / "i765_form_schema.json"

#: Stored sentinel meaning "asked, and the applicant could not or would not
#: answer". Distinct from "not yet asked" so the agent stops re-asking, and
#: distinct from a real value so the PDF engine leaves the field blank.
SKIP_SENTINEL = "__skip__"


class FieldOption(BaseModel):
    """One selectable choice, and the checkbox it ticks on the PDF."""

    model_config = ConfigDict(extra="forbid")

    value: str
    label: str
    pdf_field: str
    pdf_value: str


class FormField(BaseModel):
    """A single question, and where its answer lands on the form."""

    model_config = ConfigDict(extra="forbid")

    id: str
    question: str
    type: str
    group: str
    memory_key: str
    sensitive: bool
    required: bool
    read_back: bool = False
    label: str | None = None
    max_len: int | None = None
    depends_on: str | None = None
    help: str | None = None
    pdf_field: str | None = None
    pdf_field_parts: list[str] = Field(default_factory=list)
    options: list[FieldOption] = Field(default_factory=list)

    @model_validator(mode="after")
    def _must_reach_the_form(self) -> FormField:
        if not (self.pdf_field or self.pdf_field_parts or self.options):
            raise ValueError(
                f"field {self.id!r} has no route onto the PDF; asking an applicant "
                "a question whose answer has nowhere to go wastes their time"
            )
        return self

    def option_for(self, value: str) -> FieldOption | None:
        """Resolve a stored value to its option, case-insensitively."""
        target = value.strip().casefold()
        for option in self.options:
            if option.value.casefold() == target:
                return option
        return None


class FormSchema(BaseModel):
    """A whole form: its metadata and its ordered list of questions."""

    model_config = ConfigDict(extra="forbid")

    form_id: str
    title: str
    agency: str
    edition: str
    pdf: str
    notes: str | None = None
    fields: list[FormField]

    @model_validator(mode="after")
    def _ids_must_be_unique(self) -> FormSchema:
        seen: set[str] = set()
        for field in self.fields:
            if field.id in seen:
                raise ValueError(f"duplicate field id {field.id!r} in {self.form_id}")
            seen.add(field.id)
        return self

    @property
    def field_by_id(self) -> dict[str, FormField]:
        return {field.id: field for field in self.fields}

    def get_field(self, field_id: str) -> FormField | None:
        return self.field_by_id.get(field_id)

    def field_ids(self) -> list[str]:
        return [field.id for field in self.fields]

    def groups(self) -> dict[str, list[FormField]]:
        """Fields bucketed by mapping group.

        These buckets are independent of one another, which is what lets the
        mapping stage fan them out concurrently in a later milestone.
        """
        grouped: dict[str, list[FormField]] = {}
        for field in self.fields:
            grouped.setdefault(field.group, []).append(field)
        return grouped

    def pdf_path(self) -> Path:
        """Absolute path to this form's source PDF."""
        return REPO_ROOT / self.pdf


def load_form_schema(path: str | Path | None = None) -> FormSchema:
    """Read and validate a form schema from disk.

    The default path is resolved from this module's location rather than the
    process working directory, so uvicorn, pytest, and the worker all agree.
    """
    schema_path = Path(path) if path is not None else DEFAULT_SCHEMA_PATH
    if not schema_path.is_absolute():
        schema_path = REPO_ROOT / schema_path
    return FormSchema.model_validate_json(schema_path.read_text(encoding="utf-8"))


_cached_i765: FormSchema | None = None


def get_i765_schema() -> FormSchema:
    """The I-765 schema, parsed once per process."""
    global _cached_i765
    if _cached_i765 is None:
        _cached_i765 = load_form_schema()
    return _cached_i765

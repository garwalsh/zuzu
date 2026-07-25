"""Drop-in registry of supported forms.

Spec: prompts/form_registry_Python.prompt

Request handlers ask the registry for a form; they never name one. A second
USCIS form is an entry here plus a schema file.
"""

from __future__ import annotations

from collections.abc import Callable

from api.i765_schema import FormSchema, get_i765_schema

DEFAULT_FORM_ID = "I-765"

FormLoader = Callable[[], FormSchema]


class UnknownFormError(LookupError):
    """Raised when a caller asks for a form that is not registered."""

    def __init__(self, form_id: str, known: list[str]) -> None:
        self.form_id = form_id
        self.known = known
        super().__init__(f"unknown form {form_id!r}; known forms: {', '.join(known) or 'none'}")


_loaders: dict[str, FormLoader] = {DEFAULT_FORM_ID: get_i765_schema}
_cache: dict[str, FormSchema] = {}


def _normalize(form_id: str) -> str:
    """Fold the spellings a voice agent or a human might send.

    The agent may transcribe the form as "i765", "I-765", or "i 765"; they are
    all the same form and none of them should 404 mid-call.
    """
    compact = form_id.strip().upper().replace("-", "").replace(" ", "").replace("_", "")
    if compact.startswith("I") and compact[1:].isdigit():
        return f"I-{compact[1:]}"
    return form_id.strip().upper()


def register_form(form_id: str, loader: FormLoader) -> None:
    """Add or replace a form, without editing this module."""
    key = _normalize(form_id)
    _loaders[key] = loader
    _cache.pop(key, None)


def get_form(form_id: str) -> FormSchema:
    """Return a form's schema, parsing it on first use."""
    key = _normalize(form_id)
    loader = _loaders.get(key)
    if loader is None:
        raise UnknownFormError(form_id, list_forms())
    if key not in _cache:
        _cache[key] = loader()
    return _cache[key]


def list_forms() -> list[str]:
    return sorted(_loaders)

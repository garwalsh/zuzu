"""Drop-in registry of supported forms.

Spec: prompts/form_registry_Python.prompt

Request handlers ask the registry for a form; they never name one. A second
USCIS form is an entry here plus a schema file.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import partial

from api.i765_schema import REPO_ROOT, FormSchema, get_i765_schema, load_form_schema

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

#: Schemas dropped in here are picked up with no code change. That is the whole
#: "a form is data" claim, made literal: tools/onboard_form.py writes a file
#: here and the service serves the form.
FORMS_DIR = REPO_ROOT / "data" / "forms"


def _discover() -> None:
    """Register every schema file present under data/forms/."""
    if not FORMS_DIR.is_dir():
        return
    for path in sorted(FORMS_DIR.glob("*.json")):
        try:
            form_id = json.loads(path.read_text(encoding="utf-8"))["form_id"]
        except Exception:
            continue
        key = _normalize(form_id)
        if key in _loaders:
            continue
        _loaders[key] = partial(load_form_schema, path)


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
    _discover()
    key = _normalize(form_id)
    loader = _loaders.get(key)
    if loader is None:
        raise UnknownFormError(form_id, list_forms())
    if key not in _cache:
        _cache[key] = loader()
    return _cache[key]


def list_forms() -> list[str]:
    _discover()
    return sorted(_loaders)

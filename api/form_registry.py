"""Drop-in registry of supported forms.

Spec: prompts/form_registry_Python.prompt

Request handlers ask the registry for a form; they never name one. A second
USCIS form is an entry here plus a schema file.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Container
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


#: Paths already registered, so a rediscovery does not re-read them.
_seen_paths: set[str] = set()


def _discover() -> None:
    """Register every schema file present under data/forms/.

    The `key in _loaders` check used to come AFTER json.loads, so the cache
    prevented re-registration but never the read: every call parsed all eleven
    schema files -- 1.19 MB -- to learn what it already knew. get_form calls
    this unconditionally and save_field calls get_form, so that happened on the
    live voice path, on every single answer, inside an async handler with no
    thread offload. save_field's own docstring says "No LLM, no filesystem, no
    PDF work happens here."

    Skipping by path also means a form registered under a name that differs from
    its file's form_id is still only read once.
    """
    if not FORMS_DIR.is_dir():
        return
    for path in sorted(FORMS_DIR.glob("*.json")):
        name = str(path)
        if name in _seen_paths:
            continue
        try:
            form_id = json.loads(path.read_text(encoding="utf-8"))["form_id"]
        except Exception:
            # Not marked seen: a half-written file should be picked up once it
            # is complete, which is what /forms/onboard relies on.
            continue
        _seen_paths.add(name)
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


# ---------------------------------------------------------------------------
# Reconciling the id the voice agent sent with the id the form actually has.
# ---------------------------------------------------------------------------

#: Names a model reaches for instead of the schema's own. Each one was observed
#: in a real simulated call against the deployed agent -- it asked the right
#: question, got the right answer from the applicant, and then filed it under a
#: field that does not exist, so the answer was rejected and lost.
_ALIASES: dict[str, str] = {
    "gender": "sex",
    "alien_number": "a_number",
    "alien_registration_number": "a_number",
    "a_no": "a_number",
    "uscis_account_number": "uscis_online_account_number",
    "social_security_number": "ssn",
    "dob": "date_of_birth",
    "birth_date": "date_of_birth",
    "birthdate": "date_of_birth",
    "last_name": "family_name",
    "surname": "family_name",
    "first_name": "given_name",
    "phone": "daytime_phone",
    "phone_number": "daytime_phone",
    "telephone": "daytime_phone",
    "email_address": "email",
    "street": "mailing_street",
    "street_address": "mailing_street",
    "city": "mailing_city",
    "state": "mailing_state",
    "zip": "mailing_zip",
    "zip_code": "mailing_zip",
    "postal_code": "mailing_zip",
    "citizenship": "country_of_citizenship",
    "nationality": "country_of_citizenship",
    "passport_expiration": "passport_expiry",
    "i94": "i94_number",
    "sevis": "sevis_number",
    "eligibility": "eligibility_category",
    "category": "eligibility_category",
}


def _normalise(field_id: str) -> str:
    return (field_id or "").strip().lower().replace("-", "_").replace(" ", "_")


def resolve_field_id(
    schema: FormSchema,
    supplied: str,
    last_asked: str = "",
    answered: Container[str] = (),
) -> str | None:
    """The schema field an incoming answer belongs to, or None.

    A voice agent is told the field id by `get_missing_fields` and is supposed
    to send that exact id back. Models do not reliably do that -- a real
    simulated call produced `applicant_name`, `place_of_birth`, `gender` and
    `alien_number`, none of which exist on the I-765. Every one of those answers
    was correct and was thrown away with a 422.

    Three steps, most precise first:

        1. The id as given, if the form has it.
        2. A known alias -- `gender` is `sex`, `alien_number` is `a_number`.
        3. The field we most recently asked for, AND ONLY WHILE IT IS STILL
           UNANSWERED. An answer arriving right after a question is an answer to
           that question; that is what a conversation is. This is the step that
           generalises, because it needs no list of names anybody thought of in
           advance.

    The "still unanswered" half is not a detail. Without it the rule is "put it
    wherever we last asked", and three unrelated values in a row all land on the
    same field, each overwriting the last -- which is worse than refusing them,
    because it looks like it worked. Once the outstanding question has an
    answer, an unrecognised id has nowhere to go again.

    Returns None when none of those hold, and the caller still refuses -- a
    value with nowhere to go must not be pretended into the form.
    """
    if schema.get_field(supplied) is not None:
        return supplied

    normalised = _normalise(supplied)
    if schema.get_field(normalised) is not None:
        return normalised

    aliased = _ALIASES.get(normalised)
    if aliased and schema.get_field(aliased) is not None:
        return aliased

    if last_asked and last_asked not in answered and schema.get_field(last_asked) is not None:
        return last_asked
    return None

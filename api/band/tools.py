"""The only things an agent can actually do.

This is the boundary. Above it agents reason, delegate and argue in a Band room.
Below it nothing is a matter of opinion: these functions read and write real
state, and every one of them is ordinary deterministic Python that would behave
identically with no model involved at all.

An agent asks for a tool by name. If the name is not in that agent's whitelist
the call does not run. That is deliberately a lookup and not a prompt
instruction, because a prompt is a request and a dictionary is a rule.

Everything here is scoped by Principal. There is no way to call a tool for
"a session" -- only for a session belonging to a tenant and a user -- so an
agent cannot reach another organisation's applicant even if it were asked to.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from api.i765_schema import SKIP_SENTINEL
from api.tenancy import Principal

logger = logging.getLogger(__name__)


class ToolDenied(RuntimeError):
    """An agent asked for something outside its whitelist."""


class ToolFailed(RuntimeError):
    """The tool ran and could not do the thing."""


class SessionTools:
    """Deterministic operations over one session, for one principal.

    Constructed per collaboration, so a tool call can never be pointed at a
    different session or a different tenant than the one the agents are working.
    """

    def __init__(self, session_id: str, principal: Principal, out_dir: Path) -> None:
        self.session_id = session_id
        self.principal = principal
        self.out_dir = out_dir
        #: Populated as the collaboration proceeds; the Filler needs what the
        #: Validator found, and passing it through the room would mean trusting
        #: the model to relay it accurately.
        self._findings: list[dict[str, Any]] = []
        self._mapped: dict[str, str] | None = None

    # ---- helpers ---------------------------------------------------------

    async def _session(self):
        from api.session_store import get_session_store

        return await get_session_store().get(self.session_id)

    async def _schema(self):
        from api.form_registry import get_form

        session = await self._session()
        return get_form(session.form_id)

    # ---- the tools -------------------------------------------------------

    async def session_state(self) -> dict[str, Any]:
        """What form is being filled, and how far along it is."""
        from api.session_store import counts

        session = await self._session()
        schema = await self._schema()
        remaining, known = counts(session, schema)
        return {
            "form_id": schema.form_id,
            "form_title": schema.title,
            "answered": known,
            "remaining": remaining,
            "tenant": self.principal.tenant_id,
            "is_returning": session.is_returning,
        }

    async def next_question(self) -> dict[str, Any]:
        """The next thing the applicant should be asked, or None if finished."""
        from api.session_store import next_missing_field

        session = await self._session()
        schema = await self._schema()
        field = next_missing_field(session, schema)
        if field is None:
            return {"complete": True, "next": None}
        return {
            "complete": False,
            "next": {
                "id": field.id,
                "question": field.question,
                "type": field.type,
                "required": field.required,
                "sensitive": field.sensitive,
            },
        }

    async def collected_values(self) -> dict[str, Any]:
        """Every answer held, with where it came from.

        Sensitive values are masked. An agent reasons about whether a passport
        number is present, never about its digits, and the room transcript is
        kept forever.
        """
        session = await self._session()
        schema = await self._schema()
        out = []
        for field_id, stored in session.values.items():
            form_field = schema.get_field(field_id)
            sensitive = bool(form_field and form_field.sensitive)
            out.append(
                {
                    "field_id": field_id,
                    "source": stored.source,
                    "confidence": stored.confidence,
                    "skipped": stored.value == SKIP_SENTINEL,
                    "value": "[withheld]" if sensitive else stored.value,
                }
            )
        return {"count": len(out), "values": out}

    async def map_values(self) -> dict[str, Any]:
        """Place values on this form's fields; report anything with no home."""
        session = await self._session()
        schema = await self._schema()
        placed: dict[str, str] = {}
        homeless: list[str] = []
        for field_id, stored in session.values.items():
            if schema.get_field(field_id) is None:
                homeless.append(field_id)
                continue
            placed[field_id] = stored.value
        self._mapped = placed
        return {
            "mapped": len(placed),
            "no_destination": homeless,
            "form_id": schema.form_id,
        }

    async def cross_check(self) -> dict[str, Any]:
        """Run the Validator's checks and remember what they found."""
        from api.validation import cross_check

        schema = await self._schema()
        session = await self._session()
        values = self._mapped or {k: v.value for k, v in session.values.items()}
        self._findings = [f.as_dict() for f in cross_check(schema, values)]
        errors = [f for f in self._findings if f["severity"] == "error"]
        return {
            "findings": self._findings,
            "error_count": len(errors),
            "blocking": bool(errors),
        }

    async def write_form(self) -> dict[str, Any]:
        """Write the PDF, or refuse and say which answer stopped it."""
        from api.pdf_engine import fill_form

        schema = await self._schema()
        session = await self._session()
        values = self._mapped or {k: v.value for k, v in session.values.items()}

        if not self._findings:
            await self.cross_check()
        blocking = [f for f in self._findings if f["severity"] == "error"]
        if blocking:
            return {
                "written": False,
                "reason": "blocking findings",
                "fields": [f["field_id"] for f in blocking],
            }

        out_path = self.out_dir / f"{self.session_id}.pdf"
        report = fill_form(values, out_path, schema)
        if report.dropped:
            required = [
                fid
                for fid in report.dropped
                if (ff := schema.get_field(fid)) is not None and ff.required
            ]
            if required:
                out_path.unlink(missing_ok=True)
                return {
                    "written": False,
                    "reason": "a required answer would not fit the form",
                    "fields": required,
                    "detail": {k: report.dropped[k] for k in required},
                }

        from api.session_store import get_session_store

        await get_session_store().set_pdf_path(self.session_id, str(out_path))
        return {
            "written": True,
            "filled": len(report.filled),
            "discarded": list(report.dropped),
        }

    async def seal_record(self) -> dict[str, Any]:
        """Close the file for this collaboration."""
        session = await self._session()
        return {
            "session_id": self.session_id,
            "tenant": self.principal.tenant_id,
            "form_id": session.form_id,
            "answers": len(session.values),
            "findings": len(self._findings),
            "pdf": bool(session.pdf_path),
        }

    # ---- memory: the tiers, as things an agent does --------------------

    async def remember_fact(self, field_id: str = "", note: str = "") -> dict[str, Any]:
        """SEMANTIC. A stable fact about this applicant, for the next form.

        The value is read from the session rather than taken from the agent, so
        a model cannot write an applicant's date of birth by asserting one. It
        may decide *that* something is worth keeping; it never decides what it is.
        """
        from api.memory_store import Record, get_backend

        session = await self._session()
        schema = await self._schema()
        stored = session.values.get(field_id)
        form_field = schema.get_field(field_id)
        if stored is None or form_field is None:
            return {"stored": False, "reason": f"{field_id!r} is not an answer on this form"}
        if stored.value == SKIP_SENTINEL:
            return {"stored": False, "reason": "the applicant skipped this"}
        if form_field.sensitive and not self.principal.tenant.store_sensitive:
            return {
                "stored": False,
                "reason": "sensitive, and this organisation has not opted in to persisting those",
            }
        ok = await get_backend().put(
            self.principal.scope_key,
            Record(
                tier="semantic",
                key=field_id,
                value=stored.value,
                meta={"form_id": schema.form_id, "note": note, "source": stored.source},
            ),
        )
        return {"stored": ok, "field_id": field_id, "tier": "semantic"}

    async def recall_profile(self) -> dict[str, Any]:
        """All three tiers for this applicant, as they stand now."""
        from api.memory_store import get_backend

        schema = await self._schema()
        records = await get_backend().all(self.principal.scope_key)
        tiers: dict[str, list[dict[str, Any]]] = {"semantic": [], "episodic": [], "procedural": []}
        for r in records:
            if r.tier not in tiers:
                continue
            form_field = schema.get_field(r.key) if r.tier == "semantic" else None
            sensitive = bool(form_field and form_field.sensitive)
            tiers[r.tier].append(
                {"key": r.key, "value": "[withheld]" if sensitive else r.value, "meta": r.meta}
            )
        return {
            "is_returning": bool(records),
            "counts": {k: len(v) for k, v in tiers.items()},
            **tiers,
        }

    async def record_episode(self, outcome: str = "") -> dict[str, Any]:
        """EPISODIC. That this call happened, and how it went."""
        from api.memory_store import Record, get_backend

        session = await self._session()
        schema = await self._schema()
        ok = await get_backend().put(
            self.principal.scope_key,
            Record(
                tier="episodic",
                key=self.session_id,
                value=outcome or ("completed" if session.pdf_path else "did not finish"),
                meta={
                    "form_id": schema.form_id,
                    "answers": len(session.values),
                    "completed": bool(session.pdf_path),
                    "findings": len(self._findings),
                },
            ),
        )
        return {"stored": ok, "tier": "episodic", "session_id": self.session_id}

    async def learn_rule(self, rule: str = "", about: str = "applicant") -> dict[str, Any]:
        """PROCEDURAL. Something worth applying on every later call.

        "Speaks Spanish." "Has no SSN, stop asking." Unlike the other two this
        is the agent's own words, because a rule is a judgement rather than a
        fact -- so it is stored as a rule and never as an answer to a field.
        """
        from api.memory_store import Record, get_backend

        rule = (rule or "").strip()
        if not 4 <= len(rule) <= 240:
            return {"stored": False, "reason": "a rule must be a short sentence"}
        ok = await get_backend().put(
            self.principal.scope_key,
            Record(
                tier="procedural",
                key=rule.lower()[:60],
                value=rule,
                meta={"kind": about, "session_id": self.session_id},
            ),
        )
        return {"stored": ok, "tier": "procedural", "rule": rule}

    # ---- dispatch --------------------------------------------------------

    #: Name to bound method. An agent's whitelist is checked against this, so a
    #: tool that does not exist and a tool an agent may not use fail the same
    #: way -- there is nothing to learn from the difference.
    def _table(self) -> dict[str, Any]:
        return {
            "session_state": self.session_state,
            "next_question": self.next_question,
            "collected_values": self.collected_values,
            "map_values": self.map_values,
            "cross_check": self.cross_check,
            "write_form": self.write_form,
            "seal_record": self.seal_record,
            "remember_fact": self.remember_fact,
            "recall_profile": self.recall_profile,
            "record_episode": self.record_episode,
            "learn_rule": self.learn_rule,
        }

    async def call(self, name: str, allowed: tuple[str, ...], **kwargs: Any) -> dict[str, Any]:
        """Run a tool if this agent is allowed it. Never raises into the room."""
        if name not in allowed:
            raise ToolDenied(f"{name} is not available to this agent")
        fn = self._table().get(name)
        if fn is None:
            raise ToolDenied(f"no such tool: {name}")
        try:
            # Models pass plausible-but-wrong argument names. Dropping the ones
            # a tool does not take is kinder than failing the turn over it.
            import inspect

            accepted = set(inspect.signature(fn).parameters)
            return await fn(**{k: v for k, v in kwargs.items() if k in accepted})
        except Exception as exc:
            logger.warning("tool %s failed for %s: %s", name, self.principal.describe(), exc)
            raise ToolFailed(f"{name}: {type(exc).__name__}: {exc}") from exc


#: What the model is told it can call, in OpenAI tool-schema form. Descriptions
#: are written for the model, not for a developer: they say when to reach for
#: the tool, which is the part a model gets wrong.
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "session_state": {
        "description": (
            "Which form is being filled and how many answers are in. Call this first if "
            "you do not know what you are working on."
        ),
    },
    "next_question": {
        "description": (
            "The next question the applicant must be asked, or complete=true when the "
            "interview is over."
        ),
    },
    "collected_values": {
        "description": (
            "Every answer collected so far with its source and confidence. Sensitive "
            "values are withheld."
        ),
    },
    "map_values": {
        "description": (
            "Place the collected answers onto this form's real fields. Returns what "
            "mapped and what has no destination."
        ),
    },
    "cross_check": {
        "description": (
            "Run every cross-field validation. Returns findings by severity; "
            "blocking=true means the form must not be written."
        ),
    },
    "write_form": {
        "description": (
            "Write the official PDF. Refuses on any blocking finding or if a required "
            "answer will not fit the page."
        ),
    },
    "remember_fact": {
        "description": (
            "Keep one collected answer as a lasting fact about this applicant, so "
            "the next form does not ask for it again. Pass the field_id. The value "
            "is read from the session; you cannot supply one."
        ),
    },
    "recall_profile": {
        "description": (
            "Everything already remembered about this applicant across all three "
            "tiers: facts, past calls, and learned rules. Call this before deciding "
            "whether they are a returning caller."
        ),
    },
    "record_episode": {
        "description": (
            "Record that this call happened and how it ended, so a later call can "
            "refer to it rather than greeting them as a stranger."
        ),
    },
    "learn_rule": {
        "description": (
            "Store a rule worth applying on every future call, in your own words: "
            "'speaks Spanish', 'has no SSN, stop asking'. For judgements about how "
            "to serve this person, never for an answer to a form field."
        ),
    },
    "seal_record": {
        "description": ("Close the governance record for this filing and return its summary."),
    },
}


#: Arguments each tool takes. Everything absent here takes none, and a tool whose
#: parameters are not declared cannot be called correctly -- the model has no
#: other way to learn the argument exists.
TOOL_PARAMS: dict[str, dict[str, Any]] = {
    "remember_fact": {
        "type": "object",
        "properties": {
            "field_id": {
                "type": "string",
                "description": "The id of the collected answer to keep, from collected_values.",
            },
            "note": {"type": "string", "description": "Optional: why it is worth keeping."},
        },
        "required": ["field_id"],
    },
    "record_episode": {
        "type": "object",
        "properties": {
            "outcome": {
                "type": "string",
                "description": "One short phrase for how the call ended.",
            }
        },
    },
    "learn_rule": {
        "type": "object",
        "properties": {
            "rule": {
                "type": "string",
                "description": "The rule in one short sentence, e.g. 'speaks Spanish'.",
            },
            "about": {
                "type": "string",
                "description": "'applicant' for a personal rule, 'process' for one about the form.",
            },
        },
        "required": ["rule"],
    },
}


def schemas_for(allowed: tuple[str, ...]) -> list[dict[str, Any]]:
    """OpenAI-format tool schemas for one agent's whitelist."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": TOOL_SCHEMAS[name]["description"],
                "parameters": TOOL_PARAMS.get(name, {"type": "object", "properties": {}}),
            },
        }
        for name in allowed
        if name in TOOL_SCHEMAS
    ]

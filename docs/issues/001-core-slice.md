# Milestone 1 — Core vertical slice: schema, contract, and a filled I-765

## Goal

A scripted call drives the full HTTP contract end to end and produces a real,
openable, correctly filled USCIS I-765 PDF — in a single process, with no API
keys, no microphone, and no network egress.

This is the floor the rest of the build stands on. Voice, memory, and fast
inference are layered on top of this slice in later issues; none of them can be
demonstrated until this one works.

## Background

Zuzu is a voice-first assistant that fills USCIS immigration forms for people
the system already fails: disabled, elderly, low-literacy, and non-English
speaking applicants. An ElevenLabs conversational agent owns the call and calls
this orchestrator as a set of server tools. The orchestrator decides the next
question, stores answers, and produces the completed form.

The form itself is the hard part. `assets/i-765.pdf` (Edition 08/21/25) is
AES-128 encrypted, carries a stale static XFA overlay, and uses irregular
per-field checkbox export values. The naive implementation produces a PDF that
opens blank in Acrobat while reporting success.

## Acceptance criteria

Each is observable by running a command and looking at the result.

1. `GET /health` returns 200 with `{"status": "ok", "form_ids": ["I-765"]}`.
2. Every other endpoint returns 401 without a valid `X-Zuzu-Secret` header.
3. `POST /session/init` creates a session keyed by `conversation_id` and returns
   the `dynamic_variables` shape the ElevenLabs agent greets with.
4. `POST /tools/get_missing_fields` returns the first unanswered required field
   with a plain-language `question`, plus `remaining_count` and `known_count`.
5. `POST /tools/save_field` stores a value with provenance and returns
   `remaining_count` decremented. It returns in under 50 ms.
6. Repeating 4 and 5 until `next_field` is null terminates, and the number of
   round trips equals the number of required fields in the schema.
7. `POST /tools/generate_form` with fields still missing returns
   `status="incomplete"` and the list of missing ids, and writes no PDF.
8. `POST /tools/generate_form` with all required fields present returns
   `status="complete"` and a `pdf_url` that serves an `application/pdf` body.
9. The generated PDF opens with visible values in a standard viewer, and the
   values read back out of its AcroForm match what was submitted.
10. `python mocks/mock_voice_client.py` completes a full call against a locally
    running server and exits 0.

## Must not

These are the failure modes that matter in a legal-filing context, and each gets
an explicit negative test.

- **Must not fabricate a field value.** No default, placeholder, inferred, or
  model-invented value may ever reach the PDF. A field the applicant did not
  answer stays empty.
- **Must not log full sensitive values.** SSN, A-Number, passport number, I-94
  number, and USCIS account number must never appear in full in any log line.
  Field ids may be logged; values may not.
- **Must not accept an unknown `field_id`.** A value with nowhere to go on the
  form is rejected with 422, not stored.
- **Must not authorize on a blank secret.** If `ZUZU_SHARED_SECRET` is unset,
  requests fail closed with 500 rather than accepting a blank header.
- **Must not do LLM or PDF work on the `save_field` path.** That path is on the
  critical latency budget of a live human conversation.
- **Must not write a guessed eligibility category.** An unparseable code leaves
  all three boxes blank.

## Evidence

- `data/i765_acroform_fields.json` — generated inventory of all 161 AcroForm
  fields, produced by `tools/extract_i765_fields.py` from the official PDF.
- `data/i765_form_schema.json` — the declarative logical-field schema, whose
  every `pdf_field` reference is machine-checked against that inventory.
- `prompts/*.prompt` + `architecture.json` — the PDD source of truth for all
  nine modules.

## Validation

```bash
uv sync --extra dev
uv run pytest -q                                    # unit + negative tests
uv run uvicorn api.main:app --port 8000 &           # single process, no keys
uv run python mocks/mock_voice_client.py            # full scripted call
```

Then open the PDF written under `out/` and confirm the fields are visibly
populated.

## Done when

1. Acceptance criteria above are observable and satisfied.
2. Positive and negative paths are both tested, including every "must not".
3. Issue, code, tests, and docs agree.
4. No secrets or personal data in the diff or logs.
5. Project tests and CI pass on the PR head.
6. The demo flow in Validation works when run by hand.
7. The linked PR passes `pdd checkup --final-gate`.

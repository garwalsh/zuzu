<!-- pdd-story-prompts: form_registry_Python.prompt -->

# Ask for a form nobody hardcoded

## Story

As an applicant who needs a form Zuzu has never been asked for, I can name it or
paste its uscis.gov link, and Zuzu prepares it rather than telling me it is
unsupported.

## Why this is the architectural claim

A form is data, not code. If adding the thirteenth form means writing a
thirteenth module, the product does not scale past a demo. The voice agent knows
nothing about any specific form: it asks what the orchestrator hands it, which
is what lets one agent drive twelve forms and a thirteenth with no change to the
agent at all.

## Acceptance

- Plain speech resolves to a form. "I want to become a citizen" finds the N-400;
  "my work permit" finds the I-765. People do not speak in form numbers.
- A uscis.gov URL resolves to the same form the words would.
- An unknown form is fetched, its AcroForm inventory extracted, and its questions
  written from the document's own screen-reader labels.
- Field names are always extracted from the PDF, never recalled by a model. A
  hallucinated field name is an application that silently drops half the answers
  while reporting success.
- Adding a form requires no code change and no new module.
- Switching form mid-call carries the answers already given across.

## Evidence

Twelve forms, roughly 1,900 questions, none of them hand-written.

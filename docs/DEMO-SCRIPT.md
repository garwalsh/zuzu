# Zuzu — 3-minute demo script

Everything below is verified working on the live deploy. Nothing here is
aspirational; if a line is in this script, I have run it.

**Before you hit record**

1. Open `https://zuzu-orchestrator.onrender.com/dashboard?secret=zuzu-demo-2026-local`
   **two minutes early**. Render's free tier sleeps and cold-starts in ~50s.
2. Press **Start a sample application** once and let it finish, then reload.
   This seeds memory so the returning-caller moment has something to recall.
3. Close other tabs. The ElevenLabs widget sits bottom-right.
4. Have `data/i765_form_schema.json` open in a second window for the last beat.

---

## 0:00 — 0:20 · The problem

> "Forty percent of immigrants can't complete a USCIS form without paying a
> lawyer two thousand dollars. The forms are long, they're in English, and one
> wrong box costs you months.
>
> Zuzu is a phone call. If you can talk, you can file."

*On screen: the dashboard, idle.*

---

## 0:20 — 1:05 · A live call, in the caller's own words

Press **Start a call**. Say:

> "Hi, I need to renew my work permit."

Zuzu reads back *"Application for Employment Authorization, Form I-765"* and
waits for you to confirm. Say yes. Answer two or three questions.

**Say out loud while it fills:**

> "Notice I never said I-765. She said 'work permit'. Zuzu worked out the form,
> confirmed it, and started asking — one question at a time, in plain language."

*Point at the field grid lighting up, and the latency pill.*

> "Every answer lands in under a millisecond. That's deliberate — there's no
> model on the path where a human is waiting mid-sentence."

---

## 1:05 — 1:35 · The form switch (this is the moment)

Mid-call, say:

> "Actually, I also want to become a citizen."

Zuzu confirms *"Application for Naturalization, Form N-400"* and the **page
switches itself** — title, 273 fields, new questions — carrying the answers
already given.

> "Twelve USCIS forms, and the name and address she already gave came with her.
> The agent doesn't know what I-765 is. It asks what you need, and the
> orchestrator decides. A thirteenth form needs no code change."

---

## 1:35 — 2:05 · Memory — the moat

*Point at the Memory panel.*

> "Three kinds of memory, and they're different on purpose.
>
> **Semantic** — her passport number, her date of birth.
> **Episodic** — that she called on the 26th and finished her I-765.
> **Procedural** — that she speaks Spanish, and has no SSN, so stop asking.
>
> A cold caller answers 33 questions. She comes back and answers **four**."

*Her caller id is a SHA-256 hash — the store never holds a phone number.*

---

## 2:05 — 2:35 · The deliverable

Click **Download completed I-765**. Open the PDF.

> "This is the real USCIS form, edition 08/21/25, filled and ready to sign.
> Zuzu doesn't submit it and can't sign it — the signature fields are read-only
> by design. You review, you sign, you file."

*Then the checklist.*

> "And the documents to attach — pulled live from uscis.gov, narrowed to her
> eligibility category. A (c)(9) filer sees four items, a DACA filer sees five."

---

## 2:35 — 3:00 · Why it scales, and the close

*Show `data/i765_form_schema.json`.*

> "A form is data, not code. Adding a form is one request — Zuzu fetches the
> PDF, reads the fields, and writes the questions from the document's own
> screen-reader labels. Field names are extracted, never guessed by a model,
> because a hallucinated field name is an application that silently loses half
> your answers.
>
> Building against the real document, we found four errors in our own spec:
> Part 1 is Initial/**Replacement**/**Renewal**, not the order the docs said.
> Parents' names and the SSA request aren't on this edition at all. And the PDF
> is a hybrid XFA — fill it naively and the applicant opens a **blank form**
> that reported success.
>
> Immigration is the beachhead. Forms are the market."

**Name the sponsors:** ElevenLabs, mem0, TokenRouter, rtrvr.ai, RocketRide,
Band, Render.

---

## If the live call fails on stage

Press **Start a sample application**. It runs the identical contract with no
microphone and no network egress, fills 32 fields, and produces the same PDF.
Say: *"Same path, scripted — so a bad conference mic can't take the demo down."*

---

## Numbers you can quote, all measured

| Claim | Actual |
|---|---|
| Forms supported | 12 (~1,900 questions) |
| Returning caller | 33 questions → 4 |
| `save_field` latency | 0.03–0.13 ms |
| I-765 fields mapped | 32 logical → 43 PDF refs, machine-verified |
| Document checklist | 17 requirements read live from uscis.gov |
| Tests | 46, CI green |

## Do not claim

- **Cerebras.** 402 Payment Required; it never ran. Don't submit that track.
- **Band as an orchestrator.** Six agents are registered; Zuzu does not route
  work through them. Say "agent identities registered", not "multi-agent".
- **Email delivery.** The RocketRide pipeline runs, but `tool_gmail` has no
  Google credentials, so nothing is sent. Say "pipeline built", not "delivered".
- **All-generated code.** 2 of 10 modules came from `pdd --local generate`; the
  rest are hand-written to the same prompts. The README says so.

Being straight about these four is worth more than the tracks they'd win. A
judge who catches one overstatement re-examines everything else you said.

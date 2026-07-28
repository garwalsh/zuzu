# Zuzu

**Call a number, speak your language, and Zuzu fills your USCIS forms for you.**

USCIS forms are long, confusing, and high-stakes — one error costs months. They
are hardest for exactly the people the system already fails: disabled, elderly,
low-literacy, and non-English-speaking applicants. Zuzu turns the form into a
conversation. If you can talk, you can file.

**Live:** https://zuzu-orchestrator.onrender.com/dashboard

| | |
|---|---|
| **Dashboard** | [`/dashboard?secret=…`](https://zuzu-orchestrator.onrender.com/dashboard) — the live form filling in |
| **Presentation mode** | [`/dashboard?present=1`](https://zuzu-orchestrator.onrender.com/dashboard?present=1) — drives the whole demo end to end with captions, hands-off |
| **Slides** | [`/deck`](https://zuzu-orchestrator.onrender.com/deck) — arrow keys or click |

Presentation mode runs the full arc on its own: the problem, a call filling I-765,
a mid-call switch to N-400 carrying the answers across, the memory tiers, the
finished form, and the sponsor list. Open it two minutes early — Render's free
tier cold-starts in about fifty seconds — and give it a window at least 1440px
wide so the layout does not stack.

**12 USCIS forms**, ~1,900 questions, none of them hand-written.

---

## What it does

You say *"I need my work permit"* — not "I-765". Zuzu works out which form you
mean, confirms it, and starts asking one plain-language question at a time in
your language. Answers appear on screen as you speak. At the end you get a
completed, downloadable PDF plus the exact list of documents to attach.

Call back next month and it already knows you.

```
 Phone / voice widget
        │  speech, in the caller's own language
        ▼
 ElevenLabs Conversational AI ──► STT · language detection · TTS
        │  server tool calls
        ▼
 Orchestrator (FastAPI on Render)
        ├──► Supabase      three memory tiers, keyed by a hash of tenant+caller
        ├──► TokenRouter   spoken-value normalisation · translation · schema polish
        ├──► rtrvr.ai      reads uscis.gov for document requirements and new forms
        └──► RocketRide    declarative pipeline that delivers the finished packet
        ▼
 Form engine ──► fills the official AcroForm PDF ──► completed form + checklist
        ▼
 Live dashboard ──► questions, answers, memory, and fields filling in real time
```

---

## The sponsor stack, and what each one actually does

Every integration below executes. Where something is registered but not yet
driving the product, this README says so.

### ElevenLabs — the entire interface

The conversational agent owns the call: speech, language detection, turn-taking,
read-back confirmation. It is deliberately a **thin voice loop** — it asks
whatever question the orchestrator hands it and reports the answer back. All the
decisions live in one place.

`tools/create_elevenlabs_agent.py` registers the agent, its system prompt, and
five server tools pointed at the deployment — `identify_form`, `set_form`,
`get_missing_fields`, `save_field`, `generate_form`. `session_id` is bound to
`system__conversation_id`, so the id the dashboard subscribes to and the id the
agent sends are the same by construction.

### Memory — three tiers, in Postgres

A flat store is the wrong shape. Remembering a passport number, remembering that
someone called last Tuesday, and remembering that they need Spanish are three
different kinds of knowledge with three different lifetimes.

| Tier | Holds | Why it is separate |
|---|---|---|
| **Semantic** | Name, date of birth, passport number | Prefills the next form |
| **Episodic** | Which form, how many answers, whether a PDF came out, when | Lets Zuzu say *"last time we filed your renewal on the 25th"* |
| **Procedural** | *Speak French* · *no SSN, stop asking* · *category is (c)(3)(B)* | Learned once, applied on every later call |

Measured against the live service: I-765 has **32** fields, so a cold caller is
asked at most 32 questions. A returning caller in the same run was asked **7**,
with 25 recalled — the interview only covers what is genuinely new.

Each tier is a row kind in one table, so each is independently forgettable —
`POST /session/forget?caller_id=…&tier=episodic` drops call history while keeping
the profile that saves an hour. **User-level isolation:** caller ids are
SHA-256 hashed together with the tenant id before they reach the store, so it
never holds a raw phone number and one caller's memory is unreachable both from
another caller's session and from another organisation's.
Sensitive values are **not persisted at all** unless `ZUZU_MEMORY_STORE_SENSITIVE`
is explicitly set.

### TokenRouter (MiniMax-M3) — the inference lane

`api/inference.py` is provider-agnostic: base URL, model, and key are config, so
Cerebras or Groq is an env change rather than a patch. It carries the two jobs an
LLM genuinely earns on a voice call:

- **Spoken-value normalisation** — *"nineteen ninety-eight, April twelfth"* →
  `1998-04-12`; *"see three bee"* → `(c)(3)(B)`.
- **Question translation** — so an applicant who speaks Haitian Creole is not
  read English because nobody wrote a Creole schema.

It also polishes generated question wording and parses the document checklist out
of USCIS prose. **Nothing here touches the live `save_field` path**, which is why
saves land in **0.13 ms**.

### rtrvr.ai — reading the web that refuses to be read

`uscis.gov` returns **403 to scripted requests**. rtrvr drives a real browser, so
it gets the page. Two uses:

1. **Document requirements** — a live run pulled **17 real requirements** from
   `uscis.gov/i-765`, including the DACA worksheet, the DSO-endorsed I-20 for
   (c)(6), and the I-485 receipt for (c)(9). Narrowed per applicant: a (c)(9)
   filer sees 4 items, (c)(33) sees 5, a (c)(3)(B) renewal sees 6.
2. **Unknown forms** — paste any USCIS URL and rtrvr reads the page to identify
   which form it is, even one Zuzu has never seen.

### RocketRide — pipeline-as-JSON for form intake and model selection

RocketRide's model is **pipeline-as-JSON**, which is the same operating rule the
form schemas follow: config is the source of truth. Onboarding a form runs
through it.

```
webhook ──► question ──► llm_minimax ──► response      question wording
webhook ──► parse ─────────────────────► text          document intake
```

**The model is chosen in that JSON, not in Python.** `llm_*` components accept
`profile: "custom"` with an inline `apikey`, `serverbase` and `model`, so
TokenRouter drops in as an OpenAI-compatible gateway and swapping providers is
a config edit. `parse` needs no credentials at all, which is what makes document
intake work on the RocketRide key alone.

Verified improving real output:

| before | after |
|---|---|
| What is your family Name 1? | What is your family name? |
| What is your 9 Digit Alien Registration Number? | Could you tell me your nine-digit alien registration number? |

Two things worth knowing if you extend this: `llm_minimax` consumes a
`questions` lane and raw webhook text does not populate it, so wiring a model
straight to the webhook returns an empty result *with a 200* — the `question`
component is the converter. And `POST /task` only reserves a token; the run
happens at `POST /task/data`.

None of this is on the call path. Onboarding is a one-time background step, and
every failure here is silent by design: a pipeline that does not answer leaves
the deterministic wording in place rather than blocking a form from loading.

> **Delivery status:** the same machinery builds an email packet, but
> `tool_gmail` requires a Google service account or user OAuth and neither is
> configured, so `deliver_packet` returns `delivered: false` with that reason
> rather than a green tick nobody can find in an inbox.

### Band — six agents, actually talking to each other

Six agents are registered on Band with distinct, non-overlapping jobs. Each holds
its own WebSocket to Band under its own per-agent API key, and they address each
other by Band mention. They run as asyncio tasks inside the API process — one
service, one deploy — not as six separate processes:

```
Auditor opens the room
   └─► Intake ──► Extractor ──► Mapper ──► Validator ──► Filler ──► Auditor seals
```

That arrow chain is where work usually goes, not a loop it is stepped through.
Each agent decides for itself what to do and who to hand to — MiniMax-M3 through
TokenRouter, given Band's own tool schemas so it can send a message, list the
participants, or pull somebody else in. The Auditor opens the room because Band
rejects a message whose only mention is its own sender, and because owning the
record from before the first question is just true.

What the model may decide and what it may assert are deliberately different
things. It chooses **who acts next, what to say, and when the work is done**.
Every applicant value, every validation outcome, and every byte of the PDF comes
from a deterministic tool. A hallucinated hand-off wastes a round trip; a
hallucinated date of birth is a rejected filing months later.

Each turn is recorded with the tools it ran, who it addressed, why, and **which
of the two decided it** — the model, the deterministic fallback, or an answer
that came back unusable. A trail that blurs those is worse than no trail.

- `GET /sessions/{id}/audit` — Zuzu's record, durable in the memory store, so it
  survives the process that produced it
- `GET /sessions/{id}/room` — the same conversation read back out of **Band's**
  API, so "the agents really did talk over Band" is checkable without taking
  this service's word for it

Without `TOKENROUTER_API_KEY` the fleet still runs, on a deterministic hand-off
in the fixed order above — the orchestration loses its judgement, nobody loses
their filing. `/health` and every audit entry say which one is running.

The Validator catches what no single field can show, because these mistakes only
appear when you look at the answers together:

- arrival in the U.S. dated before the date of birth
- a `(c)(3)` student category with no SEVIS number, the most common avoidable
  I-765 rejection
- a value that will overflow its printed box — measured on what actually gets
  written, since the engine strips punctuation first, because a validator that
  cries wolf is worse than none

> **Status, stated precisely.** Band has two API surfaces. The Human API
> (`/api/v1/me/*`) answers `plan_required` on the free tier, which is what made
> this look impossible at first. The Agent API (`/api/v1/agent/*`) works on the
> free tier, and per-agent keys — issued once, at registration — are what the
> agents actually run on. So the transport is real.
>
> Two things are honestly partial. Free-tier Band has `ff_memory`,
> `ff_create_tools` and `ff_mcp_servers` off, so memory is Zuzu's own three-tier
> store rather than Band's. And routing is emergent up to a point: an agent picks
> who to address, but when it declares itself done, the fixed order decides who
> goes next rather than its choice.

### Render — one blueprint

`render.yaml` deploys the whole thing. Pinned to one worker until the Redis store
lands, because session state is in-process.

### Cerebras — not claimed

The playbook assigns Cerebras the speed lane. The account returns **402 Payment
Required**, and `llama-3.3-70b` — the model the docs name — **does not exist** on
it (404). Rather than claim an integration that never ran, the inference lane is
provider-agnostic and Cerebras is a config flip if credits arrive. **Zuzu does not
submit to the Cerebras track.**

---

## A form is data, not code

This is the product thesis, and it is tested rather than asserted.

`data/forms/*.json` declares every question, its plain-language phrasing, and the
exact AcroForm field it fills. Nothing in `api/` names a specific form.

Adding a form is one request:

```bash
# A form already in data/form_catalog.json:
curl -X POST "$BASE/forms/onboard?form_id=I-539" \
  -H "X-Zuzu-Secret: $SECRET" -H "X-Zuzu-Tenant-Key: $TENANT_KEY"

# Anything else needs the PDF url, and it must be a USCIS https url:
curl -X POST "$BASE/forms/onboard?form_id=I-129&pdf_url=https://www.uscis.gov/sites/default/files/document/forms/i-129.pdf" \
  -H "X-Zuzu-Secret: $SECRET" -H "X-Zuzu-Tenant-Key: $TENANT_KEY"
```

That fetches the fillable PDF, extracts its AcroForm inventory, derives questions
from the PDF's own screen-reader tooltips, and registers it — **no deploy, no code
change**. A form id not in the catalog and with no `pdf_url` answers 422 rather
than guessing at a URL.

**Field names are extracted, never recalled by a model.** Names, types, checkbox
export values, and length limits are facts; a model asked to remember them is how
you ship a form that silently drops half an applicant's answers. Only the
plain-language layer is generated, and every generated reference is validated back
against the real document.

Currently loaded: **I-765 · N-400 · I-130 · I-485 · I-131 · I-90 · I-751 · I-864 ·
I-821D · I-589 · G-28 · I-539**

---

## Things the real form disagreed with the spec about

Building against the actual USCIS document rather than a description of it turned
up four errors that would have produced bad filings:

1. **Part 1 is Initial / Replacement / Renewal.** The planning docs say
   Initial/Renewal/Replacement. Taking that literally checks "replacement" for
   every renewal applicant — the most common category.
2. **Parents' names and the SSA-card request are not on edition 08/21/25.** Zero
   hits for `Father`, `Mother`, or `SSA` in the field tree, the XFA template, or
   the page text. The profile schema was asking for three answers with nowhere
   to go.
3. **The PDF is a hybrid AcroForm + static XFA, AES-encrypted with an empty
   password.** Fill only the AcroForm and Acrobat shows a **blank form** while
   reporting success. The engine deletes `/XFA` before writing.
4. **There are no radio groups.** Every checkbox is an independent field with its
   own irregular export value — including unit selectors whose names carry
   leading *and* trailing spaces (`/ APT `, not `/APT`).

`tests/test_schema_matches_pdf.py` fails the build if the schema drifts from the
document. See [`docs/SPEC-CORRECTIONS.md`](docs/SPEC-CORRECTIONS.md).

---

## Design commitments, each with a test

- **Never fabricate.** A skipped field stays blank; an unparseable eligibility
  category writes nothing; an invalid state code is refused rather than silently
  dropped by the viewer.
- **Never log a sensitive value.** SSN, A-Number, passport, I-94, and USCIS
  account number are logged by field id only.
- **Fail closed.** An unset `ZUZU_SHARED_SECRET` returns 500 rather than
  accepting a blank header.
- **The call survives its dependencies.** A memory store that cannot be reached
  degrades to SQLite and says so in `/health`, rather than quietly answering
  "no history" for every caller. A missing conversation-init webhook opens the
  session lazily instead of 404-ing every tool call.
- **Human in the loop.** Zuzu fills to the review step. It does not submit, does
  not pay fees, and cannot sign — the signature fields are read-only by design.

---

## Run it

```bash
uv sync --extra dev
cp .env.example .env          # set ZUZU_SHARED_SECRET

# Nothing in the code reads .env -- no dotenv, and `uv run` does not source it.
# Export it, or every request answers 500 "ZUZU_SHARED_SECRET is not set".
set -a && . ./.env && set +a

uv run uvicorn api.main:app --port 8000
```

```bash
uv run pytest -q              # 42 tests
```

Dashboard at `/dashboard?secret=$ZUZU_SHARED_SECRET`.

> Run **one** worker until the Redis store lands — session state is in-process,
> so two workers means the call lands on one and the PDF download 404s from the
> other.

## HTTP contract

`session_id` is always the ElevenLabs `conversation_id`. Every endpoint that
touches a session or a caller requires `X-Zuzu-Secret`, and — once a tenant
registry is configured — `X-Zuzu-Tenant-Key` as well.

Public, by design: `/health`, `/forms`, `/forms/{id}/schema`, `/config`, `/`,
`/dashboard`, `/deck` and the OpenAPI pages. None of them names a session or a
caller; they describe what the deployment can do, not who used it. The
websocket takes the secret as a query parameter and the tenant key as a
subprotocol, because a browser cannot set request headers on one.

| Endpoint | Purpose |
|---|---|
| `POST /session/init` | Conversation-init webhook → greeting variables |
| `POST /tools/get_missing_fields` | Next question, or `null` when done |
| `POST /tools/save_field` | Store one confirmed answer |
| `POST /tools/generate_form` | Fill the PDF |
| `POST /tools/identify_form` | *"my work permit"* or a URL → the right form |
| `POST /session/set_form` | Switch forms mid-call, keeping answers |
| `POST /session/complete` | Post-call reconciliation, episode + rules learned |
| `GET /sessions/{id}/memory` | All three memory tiers |
| `GET /sessions/{id}/audit` | Which agent set which field, and every finding |
| `GET /sessions/{id}/checklist` | Documents to attach |
| `POST /sessions/{id}/deliver` | Email the packet via RocketRide |
| `POST /forms/onboard` | Teach Zuzu a new form at runtime |
| `GET /forms` | What is ready, and what can be learned |
| `GET /ws/{session_id}` | Live event stream |

## How this was built

Prompt Driven Development: `prompts/*.prompt` are the source of truth and
[`architecture.json`](architecture.json) declares the module graph. Code under
`api/` is the output.

**Provenance, stated plainly:** `api/contract.py` and `api/security.py` were
generated by `pdd --local generate`. The rest are hand-written against those same
prompts, after the generation backend hit an expired session. Regeneration was
attempted for the others with the test suite as the gate — modules whose generated
version failed were reverted rather than shipped.

`pdd checkup --final-gate` ran all 8 steps against PR #2 and found **zero code
findings**; its fixer caught a real test-isolation bug (a live `MEM0_API_KEY` in
the shell made the suite non-hermetic) and pushed the fix.

## Team

- Gar Walsh
- Bhargav Chintam

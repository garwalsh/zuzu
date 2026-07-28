# Screen-recording script — Zuzu on Band

For a 4–5 minute recording you narrate yourself. A 90-second cut is at the end.

The through-line, in one sentence: **most multi-agent demos are one model with six
prompts; this one has six registered Band identities, and Band itself can confirm
what they said to each other.** Everything you show should serve that.

---

## Before you hit record

**Wake the service.** Render's free tier cold-starts in ~50 seconds. Open
`zuzu-orchestrator.onrender.com/health` two minutes early and leave it until it
returns `"status":"ok"` with `"agents":6`.

**Tabs, left to right, in this order:**

1. `app.band.ai/agents` — logged in
2. `app.band.ai/chat/` — logged in, with a **Zuzu public-demo ·** room open
3. `zuzu-orchestrator.onrender.com/band`
4. `zuzu-orchestrator.onrender.com/dashboard`

**Three things to check on tab 1 before recording**, because I could not see that
page — it needs your session:

- All six `Zuzu-v2-*` agents are listed: Intake, Extractor, Mapper, Validator,
  Filler, Auditor.
- Three older ones are also on the account and will be visible:
  **Zuzu-Probe-Sender**, **Zuzu-Probe-Connect**, **Zuzu-Intake**. They are
  leftovers from working out the API. Either delete them first, or say "those
  three are from when I was figuring out your API" — do not leave them
  unexplained.
- If the room list on tab 2 is empty, the free plan may be gating that view. If
  so, skip tab 2 and use `/band`, which reads the same data through the Agent
  API. **Check this before recording, not during.**

**Have ready:** the room title `Zuzu public-demo · web_maria_…`, and the fact
that a fresh run takes about 60 seconds.

---

## The recording

### 0:00 — 0:30 · Open on the problem, not the architecture

*On screen: tab 4, the dashboard, nothing running yet.*

> "This is Zuzu. You call a number, speak your own language, and it fills a US
> immigration form with you — one plain question at a time.
>
> The reason this is hard isn't the information. Every applicant knows their own
> date of birth. It's the form. Naturalisation has 273 fields, one wrong box
> costs months, and a rejection isn't a retry — a status can lapse while you wait
> for the letter.
>
> I want to show you how I used Band, because the orchestration is the part I'd
> want you to look at."

### 0:30 — 1:15 · Six identities, not one bot

*Switch to tab 1: `app.band.ai/agents`. Scroll slowly through the six.*

> "First thing: these are six separately registered Band agents, not one model
> wearing six hats. Intake, Extractor, Mapper, Validator, Filler, Auditor.
>
> Each has its own API key, issued once at registration, and each opens its own
> WebSocket. In the room they're six distinct participants — and the agent id is
> what every entry in my audit trail is attributed to. That's why they're
> registered rather than simulated: I need the id to be real, months later, when
> somebody asks who set a field."

*Point at the handles, e.g. `bindubhargavareddy/zuzu-v2-auditor`.*

> "The names are namespaced — `zuzu-v2` — because Band won't delete an agent that
> has execution history. Which is correct, by the way: that id is in trails that
> have to stay resolvable. It just means a name, once used, is used forever, so I
> version them."

### 1:15 — 2:15 · The chatroom, and what the mention does

*Switch to tab 2: `app.band.ai/chat/`, with a `Zuzu public-demo ·` room open.
Scroll from the top of the conversation.*

> "Here's a real room. Every one of these is a filing.
>
> Read the flow: the **Auditor** opens it — it has to be somebody other than the
> first worker, because Band rejects a message whose only mention is its own
> sender. And honestly, the Auditor owning the record from before the first
> question is just true.
>
> Then Intake confirms the interview is complete. Extractor checks where every
> value came from. Mapper places them on real PDF fields. Validator runs the
> cross-checks. Filler writes the form. Auditor seals it."

*Pause on one message and point at the mention.*

> "This is the part I'd underline. **The mention is the routing.** There's no
> dispatcher deciding who goes next — each agent picks who to address, and Band
> delivers it. Your constraint that a message must carry at least one mention
> turned out to be the useful thing, not a nuisance: a message with no recipient
> would be an agent talking to nobody, and that's what a log line is for."

> "They're reasoning with MiniMax-M3 through TokenRouter — which is
> OpenAI-compatible, so I hand the model **your** tool schemas unchanged.
> `band_send_message`, `band_get_participants`, `band_lookup_peers`. It talks to
> the others in Band's vocabulary, not a translation layer of mine."

### 2:15 — 3:15 · Two independent records

*Switch to tab 3: `/band`. Scroll to the two-column section.*

> "This page is the thing I actually want to show you.
>
> Left column: what Zuzu says each agent did — the tools it ran, who it
> addressed, and which of three decided the turn: the model, the deterministic
> fallback, or an answer that came back unusable. Those are different claims and
> I don't blur them.
>
> Right column: **the same conversation read back out of your API.** Sender,
> mentions, per-recipient delivery status — all Band's, fetched with the agents'
> own credentials.
>
> That right column is the reason the left one is worth believing. In a
> legal-filing domain the trail *is* the product, and 'trust my service' isn't an
> audit trail."

*Optional, if you have the time: press **Run a new collaboration** and let it go.*

> "That's running six agents on Band live. Takes about a minute."

### 3:15 — 4:00 · The moment worth telling

*Stay on `/band`, or scroll the audit column.*

> "Two things happened while building this that I didn't design.
>
> The **Validator** refused to let a form be written until its cross-checks
> passed. That's its job — but it's doing it because the tool isn't in the other
> agents' lists, not because a prompt asked nicely. Five of the six physically
> cannot produce a filing.
>
> And the **Auditor** refused to seal a record where 32 collected answers had
> produced 29 filled fields. It would not accept 'three unaccounted' and spent
> the rest of its turn budget interrogating the Filler. It was right — those
> three were fields the applicant had declined — but no tool could tell it that.
> So I built one. Two agents arguing about arithmetic neither could see is a
> missing tool, not a missing prompt.
>
> I'd never have found that without a transcript I could read."

### 4:00 — 4:40 · What I'm not claiming

*Stay on `/band`, scroll to the last section — it's on the page.*

> "Three things I'm deliberately not claiming.
>
> Routing is emergent **up to a point**. An agent chooses who to address, over
> Band. When it declares its own part finished, my fixed role order picks who
> goes next.
>
> I'm not using Band's memory or hosted tools — `ff_memory` and
> `ff_create_tools` are off on the free plan, so the three memory tiers are mine
> in Postgres. Band is orchestration here, not storage.
>
> And the agents are asyncio tasks in one process, each with its own Band
> connection and key — not six deployments.
>
> One more, for your side: `/api/v1/agent/chats/{id}/messages` returns a 200 with
> an **empty list** for an agent key. No error. It looks exactly like a room where
> nothing was said, and I lost an evening to it. `/context` is the route that
> works — and because it's scoped to what each agent sent or was mentioned in, I
> union the six views to get the whole conversation."

### 4:40 — 5:00 · Close

*Switch to tab 4, the dashboard.*

> "The product itself is live and open — no login. Twelve USCIS forms, 2,281
> questions, none of them hand-written; the schemas are extracted from the real
> PDFs. Voice or typing, because nobody wants to say their A-number out loud in a
> waiting room.
>
> Band is what makes the inside of it inspectable. Thanks for building it —
> here's the link if you want to poke at the room yourself."

---

## The 90-second cut

If you only get ninety seconds, use these four beats and nothing else:

| Time | Screen | Point |
|---|---|---|
| 0:00–0:20 | `app.band.ai/agents` | Six registered agents, own keys, own sockets — not one bot with six prompts |
| 0:20–0:50 | `app.band.ai/chat/` | Read the hand-off chain. **The mention is the routing** — no dispatcher |
| 0:50–1:20 | `/band`, two columns | My record and **Band's** record, side by side. That's what makes it checkable |
| 1:20–1:30 | `/band`, last section | "Routing is emergent up to a point, and I'm not using Band's memory — here's exactly what I am using" |

---

## Delivery notes

- **Do not read this.** These are the points, in order, in roughly the words that
  work. Say them your way.
- Scroll slowly on the chatroom. The messages are the evidence; give them time to
  be read.
- The single strongest line is *"the mention is the routing"*. Land it, then stop
  talking for a beat.
- The Auditor story is the one a founder will remember. It's about their product
  surfacing something real, which is a better compliment than praise.
- Say the caveats in your own voice and without apology. Volunteering the limits
  is what makes the claims land.

## Facts you can state, all verified

| | |
|---|---|
| Agents registered on Band | 6, each with its own key and WebSocket |
| Rooms on the account | 8, titled `Zuzu public-demo · <session>` |
| A typical room | 8 turns, 12 messages, 6 participants, `delivered 1/1` |
| Band tools handed to the model | `band_send_message`, `band_get_participants`, `band_lookup_peers`, `band_add_participant` |
| Reasoner | MiniMax-M3 via TokenRouter |
| Forms / questions | 12 / 2,281, extracted from the real USCIS PDFs |
| Tests | 237 |
| Free-plan findings | Agent API works; Human API `403 plan_required`; `/messages` 200-empty, `/context` works; `ff_memory` off |

# Spec corrections

Findings from building against the real I-765 PDF and the real PDD CLI that
contradict the planning documents. Recorded here so the docs and the running
system do not quietly disagree.

## 1. Part 1 reason codes are Initial / Replacement / Renewal

The planning docs specify the enum `"Initial|Renewal|Replacement"`. On the
Edition 08/21/25 form the checkbox export values are:

| Export | Meaning |
|---|---|
| `/1` | Initial permission to accept employment |
| `/2` | **Replacement** of a lost, stolen, or incorrectly printed card |
| `/3` | **Renewal** of existing permission |

Positions 2 and 3 are swapped relative to the docs. Taking the doc ordering
literally checks "replacement" for every renewal applicant — the single most
common category — which is a substantively wrong filing, not a cosmetic bug.

Verified from the `/TU` tooltips on `form1[0].Page1[0].Part1_Checkbox[0..2]`.
Encoded in `data/i765_form_schema.json` and asserted by
`tests/test_schema_matches_pdf.py`.

## 2. Parents' names and the SSA-card request no longer exist on this form

The mem0 profile schema in the playbook (§5) carries:

```json
"ssn": { "has_ssn": false, "ssn": "", "wants_ssa_to_issue": null,
         "father_name": "", "mother_name": "" }
```

and §6 lists a "Part 2 — SSN section: do you have an SSN; do you want SSA to
issue one; parents' names (for SSN issuance)".

**None of those items are on Edition 08/21/25.** Searching the AcroForm field
tree, the raw XFA template, and the extracted page text for `Father`, `Mother`,
`SSA`, and `SocialSecurity` returns zero hits. The form now runs item 12
(previously filed I-765) → 13 (SSN, if known) → 14.a/14.b (country of
citizenship). The SSA request block is gone entirely.

Keeping them in the profile schema means asking applicants — by voice, one
question at a time — for three answers that have nowhere to go. Dropped from
the schema; `test_retired_items_are_not_referenced` prevents them coming back.

## 3. Endpoint paths differ between the two planning docs

`Zuzu_Voice_Orchestrator_Integration_Contract.md` §6 shows:

```
curl -X POST $BASE/get_missing_fields
```

`Zuzu_ClaudeCode_Build_Prompt.md` specifies `/tools/get_missing_fields`.

**This build uses the `/tools/*` form** (`/tools/get_missing_fields`,
`/tools/save_field`, `/tools/generate_form`), with `/session/init` and
`/session/complete` unprefixed, matching the build prompt and Appendix B.

> **Action for Gar:** confirm the ElevenLabs agent's server tools are pointed at
> `/tools/*`. If they are already registered against the unprefixed paths, say
> so and the orchestrator will serve both — but the two configs must not be left
> to drift, because the failure mode is a 404 mid-call in front of judges.

## 4. Other form facts worth knowing before mapping more fields

- The PDF is **AES-128 encrypted with an empty user password**. `pypdf` needs
  the `[crypto]` extra or it raises `DependencyError`.
- It is a **hybrid AcroForm + static XFA** file. Filling only AcroForm values
  leaves the XFA `datasets` packet stale, and Acrobat prefers XFA — so the
  applicant opens a "completed" form and sees a blank one. The engine deletes
  `/XFA` before writing; the XFA is static (`dynamicRender` is `forbidden`), so
  nothing is lost.
- **112 of 119 text fields ship with no appearance stream** and
  `NeedAppearances` is not set, so setting `/V` alone renders nothing in many
  viewers.
- **There are no radio groups.** Every checkbox is an independent field with its
  own irregular export value: `/1`, `/Y`, `/Single`, and unit selectors whose
  names carry leading *and* trailing spaces (`/ APT `, not `/APT`).
- **Field names do not track printed item numbers.** `Line7_AlienNumber` is item
  8, `Line19_DOB` is item 16, and `Line19_Checkbox` is a different field again.
  Map from the `/TU` tooltips, never by arithmetic on the name.
- **`Pt4`/`Pt5` prefixes are swapped** relative to the printed Parts in the
  interpreter and preparer blocks.
- **Signature fields have `MaxLen 1` and are read-only** — the form must be
  printed and signed in ink. Zuzu correctly stops at the review step.
- Sex is `Line9_Checkbox[0] = /N = Female`, `[1] = /Y = Male`. The Y/N here are
  legacy artifacts, not yes/no semantics.

## 5. PDD tooling: the `pdd-*` names are labels, not commands

The prize playbook describes a workflow of `pdd-generate` → `pdd-issue` /
`pdd-change` → `pdd-checkup`. In pdd-cli 0.0.308:

- `pdd issue` **is not a command** (`Error: No such command 'issue'`).
- `pdd-generate` appears nowhere in the package — not in the source, the docs,
  or any of the 472 bundled prompts.
- The `pdd-*` hyphenated names are **GitHub issue labels** consumed by a hosted
  PDD GitHub App, which dispatches hosted runs of the real commands.

The real local surface is `pdd generate <prompt-file|issue-url>`,
`pdd change <issue-url>`, `pdd sync <basename|issue-url>`, and
`pdd checkup --pr <url> --issue <url> --final-gate`.

Installing the GitHub App needs repo **admin**, which this account does not have
on `garwalsh/zuzu` (`push: true`, `admin: false`), so this build drives PDD from
the local CLI instead.

### Running pdd locally on this machine

`~/.pdd/llm_model.csv` shadows the packaged 36-provider catalog and contains only
GitHub Copilot and OpenAI ChatGPT rows — both `interactive_only`, both with no
API key. Copilot additionally needs an OAuth token at
`~/.config/litellm/github_copilot/api-key.json`, which does not exist. So:

```bash
export PDD_ALLOW_INTERACTIVE=1     # required: the only catalog rows are interactive_only
pdd --local --force --strength 0.5 generate prompts/<name>_Python.prompt
```

This routes through the ChatGPT/Codex subscription. Two caveats:

- The top-ranked models (`gpt-5.6-sol`, `gpt-5.5`) intermittently return a
  **Cloudflare bot challenge**; pdd falls through to the next model, so retries
  are worth building into any script.
- `--strength` drives model rank and overrides `PDD_MODEL_DEFAULT`.

To use a normal API key instead, move `~/.pdd/llm_model.csv` aside to restore the
packaged catalog, then set `OPENROUTER_API_KEY` or `ANTHROPIC_API_KEY`.

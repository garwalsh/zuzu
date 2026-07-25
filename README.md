# Zuzu

Voice-first immigration form assistant. Applicants call a phone number, speak in their native language, and the agent gathers their information to complete USCIS forms.

## How it works

1. Applicant connects using voice widget
2. Agent detects their language and greets them (by name if they've called before)
3. Agent ask which form they need to fill out
4. Agent checks the fields of this specific form and if the applicant has called before cross references with memory
5. Agent asks questions to collect missing information and updates memory where necessary
7. A form-filling agent maps the stored profile to the correct USCIS form fields and generates a completed PDF

## Architecture

- **Voice agent** — ElevenLabs conversational AI, handles the phone call and multilingual dialogue
- **Memory layer** — mem0 stores applicant profiles across sessions
- **Form engine** — Maps profile data to USCIS form fields and fills the PDF
- **Live dashboard** — Web UI showing the transcript and form fields populating in real time

## Sponsor tools used

- [ElevenLabs](https://elevenlabs.io) — Voice agent and multilingual speech
- [mem0](https://mem0.ai) — Applicant profile memory
- [Cerebras](https://cerebras.ai) — Fast inference for form mapping
- [Render](https://render.com) — Deployment

## Supported forms

- I-765 (Application for Employment Authorization)


## Team

- Gar Walsh
- Bhargav Chintam

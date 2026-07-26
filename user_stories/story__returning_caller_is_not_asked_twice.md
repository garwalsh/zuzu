<!-- pdd-story-prompts: memory_Python.prompt -->

# A returning caller is not asked the same thing twice

## Story

As someone who has called Zuzu before, my second call starts from what Zuzu
already knows about me, and I can delete that history without losing it all.

## Why three tiers and not one store

Remembering a passport number, remembering that a call happened last Tuesday,
and remembering that this applicant has no SSN are three different kinds of
knowledge with three different lifetimes and three different privacy postures.
Flattening them means an applicant who wants to drop their call history has to
drop the profile that saves them an hour, which is not a real choice.

## Acceptance

- SEMANTIC: facts given on a previous call are pre-filled and read back for
  confirmation, not asked again.
- EPISODIC: Zuzu knows which form I was working on and whether I finished it.
- PROCEDURAL: rules learned from past calls are applied -- the language I speak,
  and the fields I have already said do not apply to me.
- All three tiers are visible on screen with their counts, including when a tier
  is empty. An empty tier is shown as empty rather than hidden.
- I am identified by a hash of my caller id. The store never holds the raw
  number.
- Deleting one tier leaves the others intact.

## The failure this story exists to prevent

A recall outage must never be reported as an absence. mem0 meters reads and
writes separately, and when the read quota ran out every write still returned
200 while every recall returned 429 -- so a caller with a full history was
silently treated as a stranger and asked all thirty-three questions again.
"We cannot reach the memory" and "we have never met you" are different answers
and the system must say which one it means.

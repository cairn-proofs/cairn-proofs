---
name: "Capture"
description: "Capture an idea as a lightweight user story + acceptance criteria in a GitHub issue, in the background"
category: Workflow
tags: [workflow, backlog, capture, background, user-story]
argument-hint: "<idea / problem / feature in a sentence or two>"
---

Quick-capture an idea as a lightweight user story + acceptance criteria, filed as
a GitHub issue **in the background** so the current session is not interrupted.

This command is deliberately upstream of and independent from the OpenSpec flow:
it produces a non-canonical backlog item — a GitHub issue — that a later triage
step can promote via `/opsx:propose` (which cites the issue and closes it with
`Closes #<n>` on archive). GitHub Issues is the home of deferred work.

## Your job (orchestrator)

You are the orchestrator. **Do NOT write the user story yourself** — that would
muddy this session. Your entire job is:

1. **Determine the seed idea.**
   - If `$ARGUMENTS` is non-empty, that is the seed verbatim.
   - If `$ARGUMENTS` is empty, write a 2–4 sentence seed summarizing the idea
     currently under discussion in this conversation. (The background agent has
     **no** conversation context, so the seed must be self-contained — include
     any specifics, file paths, or constraints that matter.)

2. **Dispatch exactly one background Agent** (`Agent` tool, `run_in_background:
   true`, `subagent_type: "claude"`) with the prompt template below, substituting
   the seed idea. Do not wait on it.

3. **Report one line** to the user, e.g. `📥 Capturing "<short title>" in the
   background — I'll note when the issue lands.` Then continue whatever you
   were doing. When the background agent completes, relay its one-line summary
   (which includes the issue URL and the suggested `/opsx:propose` handoff).

Do not run the capture work inline. Do not commit anything.

## Background agent prompt (substitute SEED_IDEA)

```
You are capturing a single backlog idea as a lightweight user story + acceptance
criteria. Work fast and create exactly ONE GitHub issue. Do not ask questions.

SEED IDEA:
SEED_IDEA

Steps:
1. OPTIONAL, keep it quick (one command, ~5s budget): glance at the OpenSpec
   capability names to suggest where this belongs —
   `ls openspec/specs/` or `openspec list --json`. If unsure, write "unknown".
   Do NOT read spec bodies or go deep; this is a hint, not an analysis.
2. Compose the issue title and body using the TEMPLATE below. Infer a clear
   title, a crisp 2–3 sentence problem statement, one or more user stories in the
   form "As a <specific role>, I want <capability> so that <benefit>", and
   acceptance criteria in Given/When/Then (cover the happy path plus at least one
   edge or error case). Keep it tight — this is a capture, not a PRD. Leave a
   section empty with "- _none yet_" rather than padding it. If personas are
   available in project docs or specs, replace <specific role> with the
   appropriate persona instead of an arbitrary role name.
3. In the Suggested-capability link, substitute the actual capability (no literal
   "<capability>" placeholder should survive).
4. Create exactly one GitHub issue:
   gh issue create --title "<title>" --label backlog --body-file <tmpfile>
   (write the composed body to a temp file; create the `backlog` label first with
   `gh label create backlog` if it does not exist).
5. Return ONLY this one line as your final message:
   "📥 Captured: <issue-url>  —  promote with `/opsx:propose` (Closes that issue on archive)"

Hard guardrails:
- Create exactly ONE issue. Never modify openspec/ or any source/infra file.
- Read-only OpenSpec inspection (ls, openspec list/status) is fine; mutating
  openspec commands are forbidden.
- Use ONLY `gh issue create` (plus `gh label create backlog` if missing). Do NOT
  close/edit/comment on other issues, push code, or open a PR.

TEMPLATE (fill in; keep the headers exactly). The "<Title>" becomes the issue
title (passed via --title); the body is everything below it:

Title: <Title>

Body:
- **Suggested capability**: [<capability>](https://github.com/cairn-proofs/cairn-proofs/blob/main/openspec/specs/<capability>/spec.md) (or "unknown")

## Context / problem
<2–3 sentences: the problem, who hits it, why it matters.>

## User story
As a <specific role | persona>, I want <capability> so that <benefit>.
<add further stories, one per line, if the idea spans personas>

## Acceptance criteria
1. Given <precondition>, when <action>, then <expected outcome>.
2. Given <edge/error precondition>, when <action>, then <expected handling>.

## Out of scope
1. _none yet_

## Open questions
1. _none yet_

## Promotion
Ready to formalize? Run `/opsx:propose` and translate the Given/When/Then
acceptance criteria above into `#### Scenario:` blocks (4 hashtags exactly).
```

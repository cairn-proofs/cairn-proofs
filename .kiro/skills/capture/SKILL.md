---
name: capture
description: Quick-capture an idea as a lightweight user story + acceptance criteria in a GitHub issue. Follow the writing guidelines provided by the spec-simplifier SKILL. Use when the user wants to record a deferred idea, problem, or feature for later without interrupting current work — upstream of and independent from the OpenSpec flow. Triggers on "capture this", "jot this down for later", "file this for later", "stash this idea".
---

Quick-capture an idea as a lightweight user story + acceptance criteria, filed as exactly **one** GitHub issue.

This is deliberately upstream of and independent from the OpenSpec flow: it produces a **non-canonical** backlog item — a GitHub issue — that a later triage step can promote into a change via `/openspec-propose` (which cites the issue and closes it with `Closes #<n>` on archive). GitHub Issues is the home of deferred work.

## Input

The seed idea. Either:

- An explicit argument/task — the verbatim seed. This is the normal case when invoked via `/prompts capture <idea>` or backgrounded via `/spawn <trigger> <idea>`.
- If invoked inline with no argument, compose a self-contained 2–4 sentence seed summarizing the idea currently under discussion (include the specifics, file paths, and constraints that matter).

**Self-containment is mandatory.** When this runs in a spawned/parallel session (`/spawn`), it has **no** conversation context — everything it needs must be in the input. Never write a seed that says "the thing we just discussed"; state the idea concretely.

## Steps

1. **(Optional, ~5s budget) glance at capability names** to suggest where this belongs — `ls openspec/specs/` or `openspec list --json`. If unsure, write `unknown`. Do NOT read spec bodies or go deep; this is a hint, not an analysis.
2. **Compose the issue body** using the TEMPLATE below. Infer a clear title, a crisp 2–3 sentence problem statement, one or more user stories in the form "As a `<specific role>`, I want `<capability>` so that `<benefit>`", and acceptance criteria in Given/When/Then (cover the happy path plus at least one edge or error case). Keep it tight — this is a capture, not a PRD. Leave a section as `- _none yet_` rather than padding it. If personas are named in project docs or specs, use the appropriate persona instead of an arbitrary role.
   - **Link spec references.** When the capability is a known OpenSpec capability, link it to its spec with an absolute GitHub URL so it resolves in the rendered issue — `[<capability>](https://github.com/cairn-proofs/cairn-proofs/blob/main/openspec/specs/<capability>/spec.md)` (write a bare `unknown` if you can't place it). **Reference an individual requirement by its stable `[PREFIX-NNN]` identifier, never by a legacy `Req N.N` number, and link it to the spec heading** — `[*[PREFIX-NNN] Requirement Name*](https://github.com/cairn-proofs/cairn-proofs/blob/main/openspec/specs/<capability>/spec.md#requirement-<prefix>-<nnn>-<kebab-name>)` — so the reference is verifiably real, unambiguous, and survives a retitle (the `[PREFIX-NNN]` id is enforced by the `spec_ids` gate).
3. **Substitute the real capability** in the Suggested-capability link — no literal `<capability>` placeholder should survive into the issue body.
4. **Create exactly one GitHub issue** with `gh issue create`, passing the inferred title and the composed body. Apply the `backlog` label (create it if absent). Example:
   - `gh issue create --title "<title>" --label backlog --body "<body>"` (write the body to a temp file and pass `--body-file` if it is large or contains shell-hostile characters).
5. **Return ONLY this one line** as the final message:
   `📥 Captured: <issue-url>`

## Guardrails

- Create exactly ONE issue. Never modify `openspec/` or any source/infra file.
- Read-only OpenSpec inspection (`ls`, `openspec list`/`status`) is fine; mutating `openspec` commands are forbidden.
- Use ONLY `gh issue create` (plus `gh label create` if the `backlog` label is missing). Do NOT close, edit, or comment on other issues, and do NOT push code or open a PR.

## Invocation

- **Inline** — `/prompts capture <idea>`: runs in the current session. A capture is a single `gh issue create`, so the context cost is minimal.
- **Backgrounded (truly parallel)** — `/spawn <capture-trigger> <idea>`: runs in a fresh parallel session (monitor with `Ctrl+G`); the active session is left untouched. Because the spawned session has no conversation context, the `<idea>` you pass must be fully self-contained.

Either path runs this same skill — the skill is mechanism-agnostic and does not background itself; backgrounding is chosen by how it is invoked.

## TEMPLATE

Fill it in; keep the headers exactly. This is the issue BODY (the `# <Title>` becomes the issue title, passed via `--title`, not repeated in the body):

```
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
Run `/openspec-explore` to refine this item, then `/openspec-propose` to spec out the change (the proposal cites this issue and closes it with `Closes #<n>` on archive).
```

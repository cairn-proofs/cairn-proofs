---
name: spec-simplifier
description: Rewrite an EXISTING spec, requirements doc, or technical document to be more concise, clear, and direct using a fixed set of writing rules (active voice, positive language, no weasel words, data over adjectives, single-topic paragraphs). Aware of this project's OpenSpec format — preserves requirement/scenario structure, RFC 2119 keywords, IDs, and Capitalized_Underscore glossary terms. Use whenever the request is to tighten, clean up, condense, de-weasel, or improve the clarity of a document that already exists (including any file under openspec/), however phrased — for example "simplify this spec", "tighten this", "make this concise", "edit for clarity", "apply the writing guidelines", or sharing a doc and asking for a cleaner rewrite. NOT for authoring a new document from scratch — that is the spec-writer agent's job.
---

# Spec Simplifier

Rewrite an input spec so it states its purpose and fulfills it — nothing more, nothing less. Apply the writing rules in `references/writing-recommendations.md`. Preserve every requirement; change only the language.

## Workflow

1. **Read the full rules first.** Load `references/writing-recommendations.md` before editing. It is the source of truth for what "simplified" means here — do not rely on a general sense of "concise."
2. **Identify the document type.** A formal spec (OpenSpec, RFC, PRD) carries normative and structural content that MUST survive the rewrite — see "Preserve before you cut" below. Prose docs (memos, design docs) have fewer constraints.
3. **If this is an OpenSpec file, load the format reference.** When the file lives under `openspec/` (a `specs/<capability>/spec.md` or a `changes/<name>/specs/...` delta) or uses `### Requirement:` / `#### Scenario:` headers, read `references/openspec-format.md` before editing. It defines the structure to preserve and which surface you're touching.
4. **Rewrite pass.** Apply the rules clause by clause: cut redundant pairs and qualifiers, replace phrases with single words, convert passive to active, swap negative framing for positive, delete adverbs/adjectives that aren't carrying data, and make each paragraph a single topic with a topic sentence.
5. **Self-check.** For every remaining paragraph, sentence, and clause ask "why is this here?" and "how does it clarify the requirements?" Delete what fails. Confirm no requirement was dropped or weakened.
6. **Deliver and verify.** Output the rewritten spec. For an OpenSpec file, run `openspec validate --specs --strict` (or note that the user should) and confirm it passes. If the user wants to see what changed, offer a short list of the substantive edits (not a diff of every word).

## Preserve before you cut

The writing rules tell you to remove weasel words like "should", "must", "may". In a spec these can be **RFC 2119 normative keywords** (MUST, SHALL, SHOULD, SHOULD NOT, MAY, REQUIRED) with precise, load-bearing meaning. Do not delete or downgrade them. Distinguish:

1. Normative keyword defining a requirement level — keep verbatim (e.g., "The store SHALL reject unsigned bundles").
2. The same word used as vague hedging in prose — apply the rule (e.g., "this should mostly work" → state what it does).

Also preserve, unchanged in structure:

1. Requirement and scenario formatting — `### Requirement:` (3 hashtags) and `#### Scenario:` (exactly 4 hashtags; 3 fail validation **silently**), plus the `- **WHEN** … - **THEN** …` scenario shape.
2. IDs (`[CAP-001]`, `[CAP-001.01]`, `Source:` numbers), cross-references, delta headers (`## ADDED/MODIFIED/REMOVED/RENAMED Requirements`), and `Capitalized_Underscore` glossary terms (`Artifact_Store`, `Manifest_Digest`, …).
3. Numeric thresholds, units, media types, ARNs, and enums — never round, soften, or drop them. (Phase annotations are the exception — they generally should not exist; flag and confirm removal rather than preserving. See `references/openspec-format.md`.)

When a rule would change meaning, meaning wins. Conciseness serves clarity; it never overrides correctness.

## Quick reference

The rules in `references/writing-recommendations.md`, in order:

1. Formatting — prefer numbered lists, spell out acronyms on first use, short as possible. Break up run-on sentences and paragraphs for readability. 
2. Be concise — cut redundant pairs, unnecessary qualifiers, dead prepositional phrases, redundant modifiers; replace phrases with words; prefer positive form; remove repetition; just do it.
3. Paragraph as the unit of composition — one topic, topic sentence first.
4. Active voice over passive.
5. Simple, specific words over generalizations.
6. Remove indirect language (believe, would like to, planning on).
7. Remove vague language and weasel words — replace with data or a statement of intent.
8. Positive and inclusive language; reserve the negative for denial or antithesis.
9. Remove adverbs and adjectives; prefer data and actuals.

Read the reference for examples and the full rationale for each.

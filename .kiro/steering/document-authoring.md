---
inclusion: always
---

# Document authoring

These rules apply to every task, not only to code changes.

When the content of a document is settled and the document needs to be written or substantially expanded, delegate the writing to the `spec-writer` agent. Examples are runbooks, ADRs, design docs, narratives, plans, READMEs, and OpenSpec proposal and design prose. The `spec-writer` agent cannot run the validation gate. After it writes, the delegating session runs `mise run check`.

When the request is to tighten, condense, de-weasel, or clarify a document that already exists, including any file under `openspec/`, load the ASD-STE100 skill in the current session and apply it.

Do not delegate or rewrite a document while its content is still in negotiation with the user. Write it inline while it changes. Use `spec-writer` or the ASD-STE100 skill when the content settles or when the user asks. A document that `spec-writer` wrote gets an ASD-STE100 pass only when the user asks.

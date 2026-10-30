# ADR-0015: `/ns`-rooted URI namespace for Cairn-minted identifiers

## Status

Accepted (2026-06-11)

## Context

The producer's compliance claim is a standard in-toto Statement (reshaped from a Cairn-proprietary JSON object). An in-toto Statement requires a `predicateType`. This is an opaque **TypeURI** that identifies the schema of the `predicate` object. External tooling (in-toto verifiers, GUAC, cosign/policy engines, an auditor's consumer) uses it to route and parse the predicate.

This is the first Cairn-minted identifier that becomes a **durable external identity contract**. Once past alpha, it is written into WORM-immutable evidence and into consumers' policy configs, where it is expensive to change.

Two facts shape the choice of URI:

1. **A `predicateType` is an identifier, not an endpoint.** Per the in-toto spec, a TypeURI does **not** need to be resolvable. Nothing dereferences it. So the URI carries no operational dependency on `cairnproofs.dev` serving any content. It is purely a name in a namespace. (Cairn's own validate trigger keys on the OCI `artifactType` envelope label, not on `predicateType`. So `predicateType` is off the pipeline's critical path — see References.)

2. **This will not be the only such identifier.** Sibling efforts already in the backlog mint more opaque identifiers:
   1. the WORM-receipt SCITT adoption (a receipt/Signed-Statement type),
   2. the attestation-envelope seam (inbound third-party predicate discrimination).
   3. the Post-MVP auditor sign-off (potentially its own predicate).

   Without a shared convention, these would scatter across ad-hoc URI shapes.

The originating capture raised the narrow decision (`producer/` vs. flat `attestation/v1`) as open question 3: does the auditor sign-off share the producer's predicate machinery? This ADR does not need to *answer* that question now. It must only *not foreclose* it.

## Decision

Cairn-minted opaque identifiers (in-toto `predicateType`s, and future SCITT/seam identity URIs) follow a single rooted convention:

```
https://cairnproofs.dev/ns/<family>/<role>/<version>
```

- **`/ns`** — the single collection point under which every Cairn-defined namespaced identifier lives. `cairnproofs.dev/` proper stays free for actual web content. `/ns/` is the one prefix that means "this is a Cairn-defined identity (opaque, non-resolvable)."
- **`<family>`** — the identifier family (e.g. `attestation`). Grouping is **family-then-role**. So all attestation predicates share one `attestation/` subtree, and future families (`receipt/`, `provenance/`) sit beside it.
- **`<role>`** — the role within the family (e.g. `producer`). The convention plans for this segment even when no identifier exercises it yet. Minting `producer/` now gives a hypothetical auditor sign-off a clean `signoff/` sibling later. If sign-off ends up reusing the producer predicate verbatim, `signoff/` is simply never minted, and nothing is stranded.
- **`<version>`** — a **per-leaf** version (`v1`) that governs that specific predicate's schema contract. Versioning lives at the tail, not at the namespace root. So a breaking change to one predicate does not falsely bump unrelated identifiers.

The first concrete value minted under this convention is the producer-attestation predicate type:

```
https://cairnproofs.dev/ns/attestation/producer/v1
```

This cross-cutting naming/identity convention changes **no** Requirement. It governs the *value* of an identifier, not any behavior. Per the AGENTS.md ADR-vs-opsx boundary, this ADR therefore records it, not an opsx change.

The `artifact-ingestion` capability specifies the behavior that *uses* this predicateType: the in-toto envelope reshape, the read-path unwrap, and the optional `predicateType` assertion.

## Consequences

- The producer-attestation predicate type `https://cairnproofs.dev/ns/attestation/producer/v1` is minted under this convention. The `artifact-ingestion` spec references this ADR for the naming convention and does not re-derive it.
- Future identifier-minting changes (WORM-receipt SCITT, the attestation-envelope seam, auditor sign-off) inherit this tree: `/ns/<family>/<role>/<version>`. They cite this ADR and do not invent parallel shapes.
- The `producer/` segment is committed even though the sign-off-shares-predicate question (open question 3) is unresolved. Planning for the split costs one path segment and forecloses nothing.
- No spec deltas and no operational dependency. Nothing must serve content at any `/ns/...` URL. The strings are opaque TypeURIs.

## Revisit Triggers

- A future family needs a fundamentally different shape (e.g. a registry that *does* require resolvable schema URLs). Resolve it with a superseding ADR (`Status: Superseded by ADR-NNNN`), not by silently diverging one identifier.
- A breaking change to the producer predicate's `predicate` field shape. Bump the **leaf** version (`/v2`) per the per-leaf versioning rule. This needs no ADR change, because the convention already provides for it. It does ride an opsx change, because it changes the emitted/validated value.

## Alternatives Considered

- **Flat `https://cairnproofs.dev/attestation/v1` (no `/ns`, no role).** Rejected for three reasons:
  1. No single prefix marks Cairn-defined identifiers.
  2. Identity strings colonize the `cairnproofs.dev/` root.
  3. It forecloses the producer-vs-signoff split (open question 3). A later sign-off predicate would have no clean sibling.
- **`/ns`-rooted but namespace-level versioning (`/ns/v1/attestation/producer`).** Rejected: it couples unrelated identifiers to one version counter. A breaking change to one predicate would then falsely bump every identifier under the namespace. Per-leaf trailing version is the correct granularity.
- **Role-then-family (`/ns/producer/attestation/v1`).** Rejected: it scatters attestation predicates across role subtrees. Family-then-role keeps a coherent `attestation/` subtree as SCITT receipts and ingest predicates join `/ns`.
- **Reuse an existing public predicate vocabulary (SLSA, etc.).** Rejected for this change per the capture's out-of-scope item 3. No standard GRC predicate exists, so the predicate stays Cairn-defined. Interop is envelope-level (in-toto Statement), not semantic.

## References

- in-toto Statement spec — `predicateType` is a TypeURI and need not be resolvable.
- `/opsx:explore` session, 2026-06-11 (this convention and the discriminator/predicate-URI discussion).

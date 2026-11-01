# ADR-0023: Artifact is the genus; "evidence" is a reference role (OSCAL-grounded taxonomy)

## Status

Accepted (2026-11-01)

## Summary

`Artifact` is the genus for every stored, digest-addressed unit, and "evidence" is a reference role that an artifact plays when an observation cites it. Two named axes classify what the store holds: `Artifact_Type` for identity-bearing manifests and `Alias` for mutable tag pointers, and the producer-supplied classification is `Artifact_Kind`. Five names stay separate, each with one home and one mutability class, and the wire and identity authority is the format domain `cairnproofs.dev`.

Replaces ADR-0006 (deleted). Its text is in git history, not in the working tree.

## Context

The store holds artifacts that are not evidence:

- **System-minted artifacts.** Notation signatures, WORM receipts, and OCI image indexes. The validate stage skips them, and the `config.mediaType` discriminator identifies them.
- **Producer-supplied reference artifacts.** OSCAL catalogs, profiles, and policy documents. These are the things that evidence cites, not evidence.

A vocabulary that uses "evidence" as the genus therefore names a system that stores more than evidence. It forces coinages such as "non-evidence artifact" for things that have no other name.

The authoritative domain model agrees. In the NIST OSCAL Assessment Results model, evidence is not a type. It appears only as `relevant-evidence`, a leaf `href` reference from an `observation` to a resolvable resource. The only required field is a human `description`, and `href` is optional. The stored thing is a back-matter `resource` (`uuid`, `rlinks`, `base64`). Being cited confers evidence-ness. Without the citation, the same scan report is only a resource.

Two consequences follow for the producer-supplied classification:

- A classification named for evidence over-claims. It calls an artifact evidence before anything cites it.
- Eight of the fifteen `Artifact_Kind` values are OSCAL model root names, for example `catalog` and `assessment-results`. A catalog is what evidence cites. A tag segment named for evidence on a catalog inverts the OSCAL relationship.

ADR-0003 anticipated this case. The specs constrain the meaning of a term (an enum, a tag format). The glossary names the concept. Here the concept itself needed the correct name at the genus level.

## Decision

**D1.** `Artifact` is the genus for the stored, digest-addressed unit. It corresponds to an OSCAL back-matter `resource`. "Evidence" is a reference role, not a kind and not the type of the stored thing. An artifact is relevant evidence for an observation that cites it. This is a relation, not an intrinsic type.

**D2.** Two axes classify what the store holds:

```
Artifact_Type  (a manifest: has a digest, has identity)
   ├─ evidence       producer-supplied PAYLOAD
   ├─ reference      producer-supplied REFERENT (≈ OSCAL resource)
   ├─ signature      system-minted (referrer)
   ├─ worm-receipt   system-minted (referrer)
   └─ oci-index      system-minted
   discriminated by config.mediaType
   canonical tag: {artifact-kind}--_ref--{digest}

Alias  (a tag pointer: NO digest, a mutable VIEW over an Artifact)
   ├─ {artifact-kind}--_als--{name}       named alias
   └─ {artifact-kind}--_chn--{channel}    channel pointer
   NOT an Artifact_Type; created by operator action, never minted
```

The reserved tag tokens (`_ref` against `_als` and `_chn`) separate a canonical Artifact tag from an Alias.

**D3.** The producer-supplied classification of an artifact is `Artifact_Kind`, a closed controlled vocabulary. The leading tag segment is `artifact-kind`.

**D4.** ADR-0003's model (nominal-authoritative, normative-indexed) governs the genus terms. The glossary names each concept. The vocabulary and the tag-format constraints stay in their spec Requirements.

**D5.** Five names stay separate. Each name has one home and one mutability class:

1. The technical concept: `Provenance_System`.
2. The maker and the CLI: `cairn`.
3. The product brand: *Cairn Proofs*, on marketing surfaces only.
4. The wire and identity authority: `cairnproofs.dev`, frozen in stored bytes.
5. The per-deployment infrastructure naming token: `appName`, configurable, and never in stored bytes.

"Proof(s)" stays in the brand. It is safe there because it is not an audit term of art. Auditors use *evidence* and *reasonable assurance*, so a reader does not mistake "proof" for a governed type. The word reads as cryptographic proof of custody, which signing, WORM retention, and timestamping deliver. If "proof" became a genus, this decision would need a stronger word. The uniform resource-naming scheme `{appName}-{resource}-{qualifier}` lives in the infrastructure layer, not in this ADR.

**D6.** The wire and identity authority is the product and format domain `cairnproofs.dev`. Portable, externally verifiable attestation artifacts name their format authority, not a corporate umbrella. The supply-chain neighborhood does the same: Sigstore uses `dev.sigstore.*` and SLSA uses `slsa.dev`. Stored bytes embed wire identifiers and identity seeds, and these become immutable once a non-disposable deployment exists.

**D7.** The genus name is uniform across code, infrastructure, glossary, spec content, and wire identifiers. No seam exists between the concept name and the wire name:

- Annotations use the `dev.cairnproofs.artifact.*` prefix, for example `dev.cairnproofs.artifact.artifact-kind`.
- Media types use `vnd.cairnproofs.artifact.*`, for example `vnd.cairnproofs.artifact.config.v1` and `vnd.cairnproofs.artifact.worm-receipt.v1`. The producer attestation uses `vnd.cairnproofs.producer-attestation.v1`.
- ECR repositories sit under the `{appName}-artifact-{qualifier}` namespace.
- No `Evidence_Store`, `Evidence_Bundle`, `Evidence_Category`, `Evidence_Producer`, `Evidence_TUI`, or `EvidenceCategory` name occurs in `libs/`, `lambdas/`, `cli/`, `cdk/lib/`, `openspec/specs/`, or the glossary.

## Retired approach

Two earlier calls ended. The first call kept "Evidence Store" as the product name. It ended because the split between that brand and the wire identifiers would freeze permanently once stored bytes embedded wire identifiers. The disposable alpha, with no consumers, was the only cheap window to re-anchor, so the product became *Cairn Proofs* with the wire authority `cairnproofs.dev`. The second call deferred the full rename and staged a concept-only rename that left a seam between concept names and wire names. It ended because the rename happened as one hard cut while alpha was disposable, instead of as a permanent seam. This section records a rejected direction and gives no guidance.

## Consequences

**Positive**

- The vocabulary matches the domain authority (OSCAL). The stored unit is an artifact, and "evidence" is the reference relation it can play.
- Reference artifacts are coherent. The vocabulary does not label a catalog "evidence."
- The difference between an identity-bearing manifest (`Artifact_Type`) and a mutable view (`Alias`) has a name. The reserved tag-token registry already encoded the difference.
- The single source of truth holds. The vocabulary and the tag format stay in their spec Requirements, as ADR-0003 requires, and the glossary only names the concept.

**Negative**

- This ADR inherits the index-pointer fragility of ADR-0003. The anchor from a glossary entry to its governing Requirement is implicit, and `openspec validate` does not enforce it.

## Alternatives considered

- **Flatten "evidence" into a flat `Artifact_Type` enum with no special status.** Rejected. It hides the difference between a producer-supplied payload and a system-minted mechanism. OSCAL also models evidence as a relation, not as a peer value. The flat enum loses information that the specs need: only evidence is validated, and only evidence carries OSCAL content and controls.
- **Keep "evidence" as the genus and treat non-evidence as exceptions.** Rejected. It is false for what the store holds. It forces "non-evidence artifact" coinages and mislabels reference artifacts.

## References

- [ADR-0001](0001-adopt-architecture-decision-records.md) — the replace-and-delete rule, and the boundary that puts WHY in an ADR and WHAT in the spec.
- [ADR-0003](0003-glossary-authority-model.md) — the glossary authority model (nominal-authoritative, normative-indexed) that governs `Artifact` and `Artifact_Kind`.
- NIST OSCAL Assessment Results model — `relevant-evidence` (the observation leaf `href`) and the back-matter `resource`.
- [`openspec/GLOSSARY.md`](../../openspec/GLOSSARY.md) — the entries Artifact, Artifact_Kind, and Artifact_Type.
- [`openspec/specs/artifact-ingestion/spec.md`](../../openspec/specs/artifact-ingestion/spec.md) — Artifact Taxonomy Is Strictly Governed [INGEST-001], and Reject Invalid Artifact_Kind [INGEST-029].
- `AGENTS.md` — the single-source-of-truth convention.

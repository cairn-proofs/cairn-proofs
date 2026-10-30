# ADR-0020: Metadata-index core/projection boundary — system-bounded facts in the Metadata_Index record; producer-claim and format content live in their own modules

## Status

Accepted (2026-06-23). Amended 2026-06-22 — producer-claim content elevated from "a projection on the Metadata_Index record" to its own `producer-claims` capability module, symmetric with the format modules (see Decision §5). Immutable hereafter (the Draft→Accepted discipline of ADR-0019).

## Context

The `metadata-index` capability is the DynamoDB projection that makes artifacts discoverable without a scan of the archive. It never stated which data may live on the Metadata_Index record. So it conflates two kinds of data with opposite scaling characteristics:

1. **System-bounded facts** — the Manifest_Digest and identity coordinates, pipeline statuses, system timestamps, and storage locators. The system generates these, or constrains them to a fixed, bounded shape. So the Metadata_Index record's size and key cardinality stay bounded and predictable.
2. **Producer- and format-asserted content** — the producer's compliance claim and OSCAL extraction. We do not constrain the claim. It is the producer's signed assertion (ADR-0019 §4, "source-of-record, not source-of-truth"). A format module extracts open-ended content from producer documents. The system bounds neither.

Folding category 2 onto the Metadata_Index record makes the **base discoverability capability's scaling a function of what producers put in their claims**. That is the one thing the system does not control.

The rule prevents this failure mode. Suppose open-ended claim content (an arbitrarily long `controls` list, a producer-chosen control-citation fan-out) and open-ended format content (per-layer OSCAL enrichment) are queryable attributes on the one discoverability record. Then that record's worst-case item size, key cardinality, and write fan-out are a function of producer input rather than system design. The results:

1. A base "list bundles for a tenant" read drags a record whose size the producer sets.
2. A popular control concentrates every write and read onto one partition key.
3. A new compliance framework becomes a migration on the one table that must stay stable.

The claim is the producer's signed assertion (ADR-0019 §4, source-of-record not source-of-truth), so bounding it is not an option. The only lever left is to bound *where it lives*.

**Scope of this record.** This ADR records the standing architectural stance: the boundary rule and the residency model below. It changes **no** Requirement. The OpenSpec flow carries the per-Requirement moves that realign the `metadata-index` and `producer-claims` specs to this boundary. Per the ADR boundary in `AGENTS.md`, the scope-unbounded standing decision lives here, and the per-Requirement edits do not.

## Decision

The Metadata_Index record indexes only system-bounded facts about an artifact. The Metadata_Index record references everything a producer asserts (the compliance claim) or a format module extracts (OSCAL content). That content is materialized into purpose-built projections, which modules *on* the foundation (§5) own. The Metadata_Index record never carries it as open-ended queryable attributes.

**Load-bearing invariant: the Metadata_Index's worst-case item size and key cardinality are bounded by the system, independent of what a producer asserts in the claim.** This is the metadata-index analogue of ADR-0019's "integrity independent of the operator". There, integrity does not rest on the operator. Here, Metadata_Index scaling does not rest on the producer.

### 1. The residency rule

What governs a field's **shape and size** decides where it lives, not who types it.

| Category | Shape/size governed by | Representative fields | Lives in |
|----------|------------------------|-----------------------|----------|
| Identity & coordinates | System — generated, or producer-supplied but cross-checked exact-match to the IAM-enforced path (fixed, bounded cardinality) | Manifest_Digest, Derived_Identity, `{org-id}`/`{tenant-id}`/`{project-id}` | **Metadata_Index record** |
| Pipeline status & locators | System / pipeline — set by the stage, fixed enum or fixed-shape | `signingStatus`, `archiveStatus`, `wormStatus`, system timestamps, ECR/S3/Signing_Profile cross-references | **Metadata_Index record** |
| Producer claim | The producer — open-ended, system-of-record is the signed Producer_Attestation_Referrer | `controls`, `catalogOrProfileUuid`, `assessmentPeriod`, `assessor`, `assessmentId`, `artifactKind`, `supersedes` | **The `producer-claims` capability** (a module on the foundation, §5), referenced from the Metadata_Index record — a projection per designed access pattern within it |
| Format content | An interop module — open-ended, extracted from producer documents | OSCAL catalog/profile/control entities, per-layer OSCAL enrichment | **A projection** — the ADR-0018 enrichment seam / a separate OSCAL entity-graph plane |

The dividing question is never "who supplied the value" (the producer supplies `project-id` too). It is "**does the system bound its worst-case shape and size?**" Identity coordinates are producer-supplied yet system-bounded (path grammar + IAM exact-match cross-check), so they belong in the Metadata_Index record. A `controls` list is producer-supplied and producer-bounded, so it does not.

### 2. Why the Metadata_Index record must be producer-independent

If the Metadata_Index record admits open-ended claim/format content, scaling breaks in four producer-driven ways. All four are outside our control:

1. **Item-size (the 400 KB hard limit, ADR-0014).** The single Metadata_Index record already budgets pk/sk + claim projection + per-layer detail under 400 KB. Open-ended claim lists and per-layer format enrichment make that budget a function of claim verbosity. The same is true of the base "list bundles for a project" read, which drags the whole item. ADR-0014's own supersession trigger (moving per-layer detail off the single Metadata_Index record) is one instance of this rule. This ADR states the general case.
2. **Hot partitions.** The control-claims projection's `{catalogOrProfileUuid}#{controlId}` key ([PCLAIM-001]) concentrates every write and read for a popular control (e.g. NIST 800-53 `ac-2`) onto one partition-key value, tenant-wide. Partition throughput is capped per key, and a single key cannot be split. *Which controls are popular is producer-driven*.
3. **Unbounded write fan-out.** One bundle citing N controls writes N rows across ⌈N/25⌉ `BatchWriteItem` calls. N is producer-chosen and uncapped.
4. **Schema coupling.** Suppose claim fields and format vocabulary are baked into the Metadata_Index schema. Then every new compliance framework or claim field becomes a migration on the one table that must stay stable as the base discoverability substrate.

This reconciles two decisions we already made: *we will not constrain the claim* (ADR-0019 §4), and *we want predictable base-index scaling*. Both hold only if the claim never enters a fixed-schema, bounded-key Metadata_Index table raw. The projection layer is where **we** impose **our** bounds without constraining **their** claim. These bounds are fan-out caps, hot-key sharding, item-size budgets, and selected fields.

### 3. Projections are the correct home — and cheap, because the index is rebuildable

1. **A projection serves one designed access pattern.** It has explicit scaling guards and tenant scoping inherited from ADR-0019 (IAM isolation unit at the partition key, sub-hierarchy at the sort key, `Scan` denied). Control-coverage discovery — "everything backing AU-6," the headline auditor capability — *is* built, as a bounded projection, not as Metadata_Index attributes. The capability survives. Only its residency changes.
2. **A claim field with no designed access pattern is carried by reference, never denormalized on speculation.** `assessor`, `assessmentPeriod`, and `supersedes` have no query path today. They ride the Producer_Attestation_Referrer (their system of record). They earn a projection only when a real access pattern appears. The "write-only attribute nothing reads back" smell is the tell that an attribute was denormalized without a reader. This smell is already recognized in the per-layer OSCAL metadata sub-object and in a tolerated-but-unread Config_Blob schema-version field.
3. **Rebuildable-from-ECR+S3 makes projection topology free to evolve.** The index is a rebuildable projection of the canonical ECR + S3 sources (see the [metadata-index](../../openspec/specs/metadata-index/spec.md) capability Purpose). So adding, redesigning, or dropping a projection is a re-projection, not a data migration. The Metadata_Index record stays stable while projection planes change shape underneath it.

### 4. This unifies three existing decisions and conflicts with none

1. **ADR-0018 (format-agnostic foundation)** already makes format awareness a plug-in module, never a foundation attribute. The *format* half of this rule applies it directly. Per-layer OSCAL enrichment belongs in ADR-0018's enrichment seam / a separate OSCAL entity-graph plane, not the Metadata_Index record. This ADR extends the same logic to **producer-claim** content, which ADR-0018 did not cover.
2. **ADR-0014 (item budget governs the layer cap)** is the narrow sizing-math instance of the principle that this ADR states in general. This ADR does not supersede it. ADR-0014's executable per-layer invariant holds for as long as layers sit on the row.
3. **ADR-0019 (actor model)** supplies the substrate (deny-`Scan`, tenant at the partition key, project at the sort key). It also supplies the producer-source-of-record-not-truth stance, which makes "the claim is the producer's, not the system's" precise. Every projection this ADR mandates inherits ADR-0019's tenant scoping.

### 5. Producer-claim content is its own module, symmetric with format

ADR-0018 makes format awareness a plug-in module on the foundation. This ADR's first framing applied that to the *format* half (the OSCAL entity-graph plane is a real, separate capability). But it left the *producer-claim* half as "a projection". In practice, that meant a projection still living **inside** `metadata-index`. That is a logical relabel, not a separation: the foundation would still know about `controls`, `catalogOrProfileUuid`, and the claim. Real separation is a module boundary. So the producer-claim plane is its own capability, **`producer-claims`**, symmetric with the format modules:

| Layer | Capability | Owns |
|-------|-----------|------|
| Foundation | `metadata-index` | The system-bounded Metadata_Index record only — Manifest_Digest, identity coordinates, pipeline statuses, system timestamps, storage locators, Derived_Identity. Knows nothing of claims or formats. Correct with zero modules loaded. |
| Claim module | `producer-claims` (new) | The producer's compliance claim as a query plane — the control-claims citation index today, and any future claim-derived access path (assessment period, assessor, assessmentId). |
| Format module | `oscal-interop` | OSCAL **content** processing and interoperability — the documents inside the layers. |
| Format module | OSCAL entity graph | Canonical OSCAL entity **nodes** (catalog/profile/control) and their structural edges. |

The dependency is one-way for every module: `producer-claims` → `metadata-index` foundation, never the reverse. ADR-0018 §1 sets the same rule for format modules. The claim's **system of record stays the signed Producer_Attestation_Referrer** ([INGEST-005], artifact-ingestion). `producer-claims` only *projects* it for query, exactly as the index projects ECR + S3.

**The control-claims seam.** A bundle *cites* controls, which is a producer-claim fact. But a control *is* an OSCAL entity. The boundary has two sides. `producer-claims` owns the citation ("bundle B cited control-ids {X, Y}"), whole. The OSCAL entity-graph work owns the canonical entity *nodes* and traversal. Whether a claim's cited control-ids are later resolved against canonical OSCAL nodes (i.e. whether the claim is *OSCAL-formatted*) is `producer-claims`' own call. That call is deferred until a designed access pattern needs it. It follows that the bundle→control citation table is **not** the entity-graph work's to take. That work narrows to entity modeling. (Reconciling that is the entity-graph change's job when work on that change starts, not this boundary's.)

**Why stand up the capability now, when ADR-0018 §5 deferred the generic `interop` capability.** ADR-0018 declined to spec an `interop` framework because it would describe *unbuilt* architecture (N=1 module, no registration machinery). That would go against the current-state-only rule for `openspec/specs/`. That reasoning does not apply here: the control-claims projection is **already built and deployed**. Re-housing an as-built requirement into the capability that correctly names it is not speculation. It makes the spec say what the system already does. `producer-claims` is born carrying real, shipped content (the control-claims projection). It does not spec a future framework.

## Consequences

**Positive**

1. Base discoverability scales predictably and independent of the producer. The Metadata_Index record's worst-case size and key cardinality are system-bounded by construction, not by claim verbosity or control-citation popularity.
2. One rule now explains a set of otherwise-disconnected moves as facets of a single boundary rather than separate clean-ups. The moves are: moving per-layer detail off the Metadata_Index record, relocating OSCAL extraction into its own entity-graph plane, and moving producer-claim content into projections.
3. The format-agnostic guarantee (ADR-0018) reaches its conclusion: neither format content **nor** open-ended producer-claim content warps the foundation's Metadata_Index record.
4. Auditor control-coverage queries still work, re-housed as a bounded projection — relocated, not lost.

**Negative / obligations**

1. **A standing test going forward:** when a second interop module or a new claim-derived query arrives, the answer is "add a projection," never "add a Metadata_Index attribute." New `metadata-index` work must not add an open-ended producer/format attribute to the Metadata_Index record.

## Alternatives considered

1. **Leave the claim projection in the Metadata_Index record and rely on DynamoDB adaptive capacity / larger items.** Rejected: adaptive capacity does not split a single hot partition-key value, cannot raise the 400 KB item ceiling, and does nothing about schema coupling. It treats a boundary error as a capacity problem.
2. **Constrain the producer claim (cap control counts, fix the field set) so it fits the Metadata_Index record safely.** Rejected: it contradicts ADR-0019 §4. The claim is the producer's signed assertion, source-of-record not source-of-truth. A constraint would also reject legitimate attestations to protect an index-shape decision. We bound our projection, not their claim.
3. **Fold this into the ADR-0019 tenancy re-model as one big metadata-index change.** Rejected: that re-model is a BREAKING, hard cut. Merging an orthogonal boundary redraw muddies its diff and inverts the dependency. The boundary is better settled *before* the re-key, so the re-key lands once against a clean, minimal foundation.
4. **Record only per-change `design.md` notes, no ADR.** Rejected: the principle governs the tenancy re-model, the OSCAL entity-graph work, and future interop/claim work alike. That makes it a scope-unbounded standing standard, which is ADR territory by the `AGENTS.md` boundary. The implementing change is its first instance (the same ADR-stance-then-opsx-change shape as ADR-0018 and ADR-0019).

## References

- `AGENTS.md` — the ADR-vs-opsx boundary (WHY in the ADR, WHAT in the spec, never duplicated) and single-source-of-truth convention.
- `openspec/specs/metadata-index/spec.md` — the foundation capability this boundary governs (the system-bounded Metadata_Index record and its rebuildable-projection Purpose).
- `openspec/specs/producer-claims/spec.md` — the control-claims projection ([PCLAIM-001]) this boundary houses off the foundation.
- `openspec/specs/artifact-ingestion/spec.md` — [INGEST-004] (claim fields other than `projectId` are not in the content digest, but `projectId` is) and [INGEST-005] (the Producer_Attestation_Referrer is the claim's system of record), which the Metadata_Index record references rather than copies.
- ADR-0014 (metadata-index item budget governs the layer cap) — the narrower sizing-math instance of this rule. Not superseded.
- ADR-0018 (format-agnostic foundation; interop modules plug in) — owns the format half. This ADR applies its logic to producer-claim content.
- ADR-0019 (actor model — integrity independent of the operator) — the tenancy substrate (deny-`Scan`, tenant-pk, project-sk) and the producer source-of-record-not-truth stance. Also the precedent for an ADR stance whose per-Requirement moves land via a separate opsx change.
- The **`producer-claims`** capability — the module that owns the producer-compliance-claim query plane (control-claims citation index today, and future claim-derived access paths), depending one-way on the `metadata-index` foundation. Created by the implementing change that carries §5's WHAT.
- The OSCAL **entity-graph** plane — the separate capability that holds format-extracted OSCAL **entity nodes** (catalog/profile/control). It owns the canonical control *identity*, while the bundle→control *citation* is the `producer-claims` module's (the §5 seam). It owns the format half of this rule and leaves the producer-claim residue this ADR names.
- The planned **dedicated layer projection** — moving per-layer detail off the single Metadata_Index record. It is an instance of this rule and of ADR-0014's own supersession trigger.
- AWS docs — DynamoDB item-size limit (400 KB) and partition-key design / hot-key guidance: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ServiceQuotas.html and https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-partition-key-design.html . Also `dynamodb:LeadingKeys` exact-match and the single-partition-key `Query` constraint (the deny-`Scan` substrate): https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/specifying-conditions.html and https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Query.html

*Concurrent changes and backlog items are cited above by capability and concept, never by directory slug. This record is immutable once Accepted, and change slugs and backlog filenames rot as work archives, renames, or graduates. Durable cross-references anchor on peer ADR numbers, capability names, and Requirement IDs.*

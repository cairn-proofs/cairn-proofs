# metadata-index Specification

## Purpose
The DynamoDB-backed queryable index that makes evidence discoverable without scanning the archive. It defines:

1. Query access patterns — by project and by system-bounded pipeline status, plus direct lookup by Manifest_Digest.
2. Automatic pipeline-stage updates.
3. Conditional-write semantics that prevent race conditions across the pipeline.
4. Pagination behavior.
5. Per-layer descriptor projection.
6. Cross-reference storage between ECR, S3, and Signing_Profile.
7. The deterministic Derived_Identity.

The Metadata_Index record holds only system-bounded facts; the producer's compliance claim is sourced from the signed Producer_Attestation_Referrer and projected for query by the `producer-claims` capability (control-scoped discovery via [PCLAIM-001]). The index is a rebuildable projection of the canonical ECR + S3 sources. (Rebuild tooling deferred.)

*Domain terms (`Capitalized_Underscore`) used below are defined in [GLOSSARY.md](../../GLOSSARY.md), which names the governing requirement for each.*
## Requirements
### Requirement: [META-001] Queryable artifact index by core dimensions

The **Metadata_Index** is the DynamoDB table that indexes artifact bundles for query. THE Metadata_Index SHALL support querying the Metadata_Index record only by **system-bounded** dimensions — those the system sets or constrains: the project sub-hierarchy (the identity coordinate, addressed via the sort key), the pipeline-set statuses (signing status, archive status, WORM archive status), and direct lookup by Manifest_Digest. Each dimension SHALL be backed by the table key or an in-partition filter on a system-set attribute. Queries SHALL be scoped to the caller's **tenant** — the partition-key isolation boundary — which is the query scope, not a filter dimension; the IAM enforcement of that boundary (`dynamodb:LeadingKeys`, exact-match on the tenant partition key) is owned by the access-control capability.

THE physical key schema SHALL apply uniformly across every **tenant-PK plane** — the Metadata_Index record and each projection (the control-coverage projection, the **ControlClaims** table owned by the `producer-claims` capability via [PCLAIM-001], plus any further projection plane):

1. The `{tenant-id}` SHALL be the partition key, scoped exact-match via `dynamodb:LeadingKeys`. `LeadingKeys` is exact-match on the partition-key value, NOT a partition-key prefix; no plane SHALL rely on partition-key-prefix scoping.
2. The queryable sub-hierarchy SHALL be the sort key, addressed via `begins_with`: `{project-id}#{manifest-digest}` on the Metadata_Index record, and `{catalogOrProfileUuid}#{controlId}#{manifest-digest}` on the control-coverage projection — so a heavily-cited control is a sort-key band rather than a hot partition key.
3. Unscoped `Scan` SHALL be denied; every read SHALL supply an exact-match `{tenant-id}` partition key.
4. NO Local Secondary Index SHALL sit on any tenant-PK plane — an LSI pins a tenant's entire item collection to a single partition under DynamoDB's 10 GB per-partition cap, whereas a sort-key range plus the random `{manifest-digest}` suffix lets DynamoDB split the collection across partitions. Alternate access paths SHALL be Global Secondary Indexes only, and every such GSI on a tenant-PK plane SHALL itself lead with `{tenant-id}` as its partition key, so the `dynamodb:LeadingKeys` exact-match tenant scoping (item 1) binds a GSI query exactly as it binds the base table. A GSI whose partition key is not `{tenant-id}` is prohibited: because fine-grained `LeadingKeys` scoping binds to the queried index's own partition key, a non-tenant-keyed GSI would let a tenant-scoped principal's `Query` read across the tenant boundary.
5. `{org-id}` SHALL be carried as a DynamoDB attribute on the record and as the ECR/S3 path prefix; it SHALL NOT be a key component of any tenant-PK plane.

Producer-claim attributes — any field the Producer_Attestation_Referrer carries ([INGEST-005]), or anything derived from one such as the control family derived from `controls` — SHALL NOT be core query dimensions. Control-scoped discovery is served by the separate **control-claims projection** ([PCLAIM-001] in the `producer-claims` capability).

The query construction layer SHALL reject a query that references a non-core dimension, a malformed value, or an empty required parameter with a descriptive error, before issuing any DynamoDB call.

#### Scenario: [META-001.02] Invalid query parameter is rejected

- **WHEN** a caller submits a query that references a non-core dimension — for example a producer-claim attribute such as `assessor` or the `controls`-derived control family — or supplies a malformed value
- **THEN** the Metadata_Index rejects the query and returns an error identifying the offending parameter, without issuing any DynamoDB call (control-scoped discovery uses the control-claims projection, [PCLAIM-001])

#### Scenario: [META-001.03] Core query by project, filtered on a system-bounded status

- **WHEN** a caller queries the Metadata_Index for bundles in project `prj-01JYBV8X1MZ3K7QF9R2T6WYN4D` within tenant `tnt-01JABCD2EF3GH4JK5MN6PQ7RS8` with `signingStatus=signed`
- **THEN** the query reads the base table with the partition key set exact-match to the tenant `tnt-01JABCD2EF3GH4JK5MN6PQ7RS8` and a `begins_with('prj-01JYBV8X1MZ3K7QF9R2T6WYN4D#')` sort-key condition, applies an in-partition filter on the system-set `signingStatus`, and returns the matching bundles paginated according to the metadata-index pagination rules

#### Scenario: [META-001.04] No unscoped Scan and no Local Secondary Index on any tenant-PK plane

- **WHEN** the Metadata_Index record table and each projection (including the control-coverage projection) are provisioned and read
- **THEN** each plane keys on `{tenant-id}` at the partition key (exact-match via `dynamodb:LeadingKeys`, never a partition-key prefix), carries no Local Secondary Index, exposes any alternate access path as a Global Secondary Index that itself leads with `{tenant-id}` as its partition key (so `LeadingKeys` tenant scoping holds on the GSI), and rejects an unscoped `Scan`

### Requirement: [META-002] Automatic Metadata_Index updates from pipeline stages

WHEN an Artifact_Bundle is ingested, signed, or archived, THE Processing_Pipeline SHALL automatically update the Metadata_Index with the corresponding fields: signing status, WORM status, ingestion/sign/archive timestamps, and cross-references to ECR, S3, and the Signing_Profile. The responsible pipeline stage SHALL perform the update as part of its idempotent processing contract.

The Metadata_Index record SHALL carry only system-bounded facts — the identity coordinates, pipeline statuses, timestamps, storage cross-references, and Derived_Identity. It SHALL NOT carry the producer's compliance claim — the fields the Producer_Attestation_Referrer carries ([INGEST-005]): the claim is sourced from that referrer (its system of record) and projected only where a designed access path consumes it — control linkage into the `producer-claims` control-claims projection (see [PCLAIM-001]), the rest carried by reference. Whether a bundle cites controls is determined from its Producer_Attestation_Referrer and the presence of control-claims rows ([PCLAIM-001]).

The bundle is a heterogeneous multi-layer set with no single creation instant, and there is no synthetic producer creation time. Accordingly, THE Metadata_Index record's `createdAt` SHALL denote the bundle's **first-seen** time in the system, derived from `ecrPushedAt` (the EventBridge `eventTime` of the triggering PutImage, AWS-set and unmanipulable) of the first triggering event that created the record. `createdAt` SHALL NOT be derived from any producer-supplied or content-embedded timestamp, and SHALL remain stable under re-delivery of the same triggering event.

#### Scenario: [META-002.01] Signing stage updates Metadata_Index with signing status

- **WHEN** the `sign-bundle` Lambda successfully signs an Artifact_Bundle and emits an `artifact.signed` event
- **THEN** the metadata stage writes `signingStatus=signed`, the signing timestamp, the Signing_Profile ARN, and the per-signature `signatureDetails` entry into the Metadata_Index record for the bundle's Manifest_Digest

#### Scenario: [META-002.05] createdAt is the first-seen time, not a producer-supplied timestamp

- **WHEN** the update-metadata stage first creates the Metadata_Index record for a bundle in response to the triggering Producer_Attestation_Referrer PutImage event
- **THEN** the record's `createdAt` is set from `ecrPushedAt` (the AWS-set EventBridge `eventTime`) of that triggering event, is not derived from any content-embedded or producer-supplied timestamp, and remains unchanged if the same triggering event is re-delivered

#### Scenario: [META-002.06] Metadata_Index record carries no producer-claim attributes

- **WHEN** the update-metadata Lambda processes a bundle whose Producer_Attestation_Referrer carries a `controls` list, an `assessor`, and an `assessmentPeriod`
- **THEN** the Metadata_Index record stores none of those claim attributes — the control linkage is written to the `producer-claims` control-claims projection ([PCLAIM-001]) and the remaining claim fields stay in the Producer_Attestation_Referrer — and the Metadata_Index record holds only system-bounded facts

### Requirement: [META-005] Cross-reference storage between ECR, S3, and Signing_Profile

THE Metadata_Index SHALL store cross-references between the ECR artifact (repository, tag, Manifest_Digest), the S3 WORM copy (bucket, key prefix), and the Signing_Profile ARN(s) used to sign the bundle. These cross-references SHALL be sufficient to locate the bundle in either storage system and to identify the trust anchors that signed it.

For **every** archived bundle, the authoritative artifact reference travels with the artifact as an OCI Referrer of the bundle manifest (see the worm-archive *Artifact reference OCI Referrer attachment* Requirement); the cross-reference stored on the Metadata_Index record is a projection sufficient to locate the bundle and its referrers, not the system of record for the resource.

#### Scenario: [META-005.01] Metadata record carries ECR, S3, and signing-profile references

- **WHEN** an Artifact_Bundle has completed validation, signing, and WORM archival
- **THEN** its Metadata_Index record contains the ECR repository path, tag, and Manifest_Digest; the S3 bucket name and key prefix of the WORM copy; and the ARN(s) of every Signing_Profile that signed the bundle

#### Scenario: [META-005.02] Artifact reference cross-reference is a projection of the referrer, for any artifact

- **WHEN** any archived bundle's Metadata_Index record is written and the bundle's resource is attached as an OCI Referrer
- **THEN** the record's `artifactResourceReference` cross-reference is a projection of the referrer-borne resource — locating the bundle and its referrers — and the referrer remains the authoritative copy that travels with the artifact

### Requirement: [META-007] Conditional writes prevent concurrent update races

THE Metadata_Index SHALL prevent race conditions during concurrent updates from pipeline stages while guaranteeing that no stage's contribution to a record is silently lost. Specifically:

- **Stale-overwrite protection**: an update carrying a strictly-older triggering-event timestamp than the record's current state SHALL NOT overwrite newer data.
- **Row-completeness invariant**: WHEN two or more distinct pipeline stages write **complementary** (non-overlapping) field sets for the same Artifact_Bundle derived from the **same triggering event**, the persisted record SHALL converge to the **union** of those field sets regardless of which stage's write arrives first. A stage that finds the record already created by a peer stage SHALL **merge** its own attributes onto the existing record rather than discard them.
- **Idempotency**: re-delivery or re-processing of an already-recorded Artifact_Bundle (including a re-pushed identical Manifest_Digest) SHALL be a non-failing no-op; a stage SHALL NOT raise an error merely because the record, or its own contributed fields, already exist.

Implementations SHALL use DynamoDB conditional-write expressions (e.g., `attribute_not_exists` on first create, event-timestamp comparisons to reject strictly-older updates). A merge by a complementary peer stage sharing the winning create's triggering-event timestamp SHALL be admitted, not rejected as stale: it is additive and order-independent.

#### Scenario: [META-007.01] Complementary create-collision merges instead of discarding

- **WHEN** one pipeline stage has already created the Metadata_Index record for a Manifest_Digest with its own field set, and a second stage attempts to create the same record from the same triggering event with a complementary (non-overlapping) field set
- **THEN** the second stage's create-collision is resolved by merging its fields onto the existing record, the persisted record holds the union of both stages' fields, and no duplicate row is produced

#### Scenario: [META-007.02] Stale update is rejected via event-timestamp guard

- **WHEN** an update operation arrives whose triggering-event timestamp is strictly older than the record's current `updatedAt` value
- **THEN** the conditional write does not overwrite the newer data, preserving the most recent state in the Metadata_Index

#### Scenario: [META-007.03] Re-processing an already-recorded bundle is a non-failing no-op

- **WHEN** a pipeline stage re-processes an Artifact_Bundle whose Metadata_Index record (and that stage's contributed fields) already exist — for example a re-pushed identical Manifest_Digest whose record has since advanced through later pipeline stages
- **THEN** the stage completes successfully without raising an error and without regressing any field to an earlier value

### Requirement: [META-008] Per-layer descriptor projection for every bundle layer

THE Metadata_Index record SHALL project the descriptor detail of **every** layer in a bundle's content manifest — regardless of content format — as a single `layers` list, so that an auditor can enumerate those files (logs, screenshots, arbitrary documents, structured data, etc.) directly from the index without a separate ECR content-manifest fetch. Each `layers` entry SHALL carry the universal OCI descriptor fields `digest`, `size`, `mediaType`, and the `org.opencontainers.image.title` annotation (the pushed filename; omitted when the layer carries no title).

All layers SHALL be projected in the SAME list regardless of format. Any per-format enrichment for a layer SHALL be carried as an optional per-entry sub-object on that layer's `layers` row, so enrichment is a property of a layer rather than a distinct list. The projection SHALL NOT emit a separate per-format layer list.

The `layers` list SHALL cover the content manifest's layers only. Sibling referrer blobs (the Notary signature, the Producer_Attestation_Referrer, the artifact-reference resource referrer) are distinct manifests with their own identities and SHALL NOT be enumerated in this list.

#### Scenario: [META-008.02] A layer's enrichment is carried in-place, not in a separate list

- **WHEN** an indexed bundle's OSCAL layer is inspected in the `layers` projection
- **THEN** that layer's row carries its OSCAL enrichment (schema version, extracted OSCAL metadata) as a per-entry sub-object AND the same universal descriptor fields, and it appears in the single `layers` list alongside the bundle's other layers rather than in a separate per-format list

#### Scenario: [META-008.04] Referrer blobs are excluded from the layer list

- **WHEN** a bundle that carries a Notary signature, an Producer_Attestation_Referrer, and an artifact-reference resource referrer is indexed
- **THEN** the `layers` projection enumerates only the content manifest's layers and does NOT include the signature, attestation, or resource-referrer blobs (those are separate manifests with their own identities)

### Requirement: [META-009] Pagination with maximum page size

WHEN a query result set exceeds 100 items, THE Metadata_Index SHALL return results in pages of no more than 100 items each, along with a pagination token that the caller can use to retrieve the next page. Iterating through all pages using the pagination token SHALL yield exactly the full result set with no duplicates and no omissions.

#### Scenario: [META-009.01] Large result set is returned in 100-item pages with token

- **WHEN** a query matches 250 records
- **THEN** the first response contains 100 items and a non-null pagination token; subsequent calls using that token return the next 100 items and another token, then the final 50 items with a null token; the concatenated pages equal the full 250-record result set with no duplicates and no omissions

### Requirement: [META-010] Empty result set for no-match queries

IF a query specifies an Account_ID, project, control, or date range that matches no records, THEN THE Metadata_Index SHALL return an empty result set with zero items and no pagination token, rather than an error. Callers SHALL be able to distinguish "no matching records" from "invalid query" by the absence vs presence of an error.

#### Scenario: [META-010.01] No-match query returns empty page rather than error

- **WHEN** a caller queries for Account_ID `acme-prod`, project `nonexistent-project`
- **THEN** the Metadata_Index responds with an empty items array, no pagination token, and HTTP success (no error), letting the caller distinguish "no matches" from "invalid query"

### Requirement: [META-011] Reject invalid or malformed query parameters

IF a query contains an invalid or malformed parameter — including an unrecognized field name, an invalid date format, or an empty required field — THEN THE Metadata_Index SHALL reject the query and return an error indicating which parameter is invalid. Validation SHALL occur before any DynamoDB call is issued.

#### Scenario: [META-011.01] Unrecognized field name is rejected with descriptive error

- **WHEN** a caller submits a query containing the field `controlFamilyy` (typo) or an empty required `account` field
- **THEN** the Metadata_Index rejects the query before issuing any DynamoDB call and returns an error naming the offending parameter

### Requirement: [META-012] Deterministic derived identity over the canonical Manifest_Digest

THE system SHALL derive an identifier for any canonical artifact that carries none of its own (a **Derived_Identity** — e.g. the OSCAL back-matter `Resource` uuid that cites an Artifact_Bundle) as `uuid5(CAIRN_NAMESPACE, <canonical Manifest_Digest>)`. The name fed to the derivation is the **canonical prefixed** `sha256:<hex>` Manifest_Digest (NOT the bare hex form), and `CAIRN_NAMESPACE` is the fixed, frozen project namespace `uuid5(NAMESPACE_DNS, "cairnproofs.dev")`.

The content Manifest_Digest is itself reproducible — a pure function of content bytes, with no synthetic or ambient state (wall-clock `now()`, filesystem mtime, randomness) and no producer claim folded into it (the claim lives in the separately-signed Attestation referrer). Re-assembling the same content member set yields the identical Manifest_Digest. This gives the Derived_Identity a stable content-addressed foundation: because the digest the derivation is keyed on is reproducible, a Derived_Identity for byte-identical content is stable across re-pushes, not merely for a single already-archived manifest.

A Derived_Identity SHALL be a pure function of the Manifest_Digest: re-deriving it for the same bundle in any component, in any process, from the canonical prefixed digest alone SHALL byte-match the value minted onto the bundle's resource referrer — so the Metadata_Index stays rebuildable from ECR and S3 sources with no identity drift for already-archived artifacts. A borne identity (an identifier authored inside producer-supplied content, such as an OSCAL document root `uuid`) SHALL be preserved as a distinct identity and SHALL NOT overwrite the Derived_Identity.

The namespace string `"cairnproofs.dev"` is load-bearing: changing it re-bases every Derived_Identity and would dangle existing citations, so it SHALL be treated as a frozen constant.

#### Scenario: [META-012.01] Derived identity is uuid5 over the canonical prefixed digest

- **WHEN** the system derives an identity for a bundle whose canonical Manifest_Digest is `sha256:<hex>` and the content carries no identifier of its own
- **THEN** the derived identity equals `str(uuid5(CAIRN_NAMESPACE, "sha256:<hex>"))`, derived over the prefixed form — not over the bare 64-hex form

#### Scenario: [META-012.02] Re-derivation byte-matches the minted referrer

- **WHEN** any component re-derives a bundle's identity from its canonical prefixed Manifest_Digest alone, independently of the live Metadata_Index
- **THEN** the result byte-matches the Derived_Identity minted onto that bundle's resource referrer, with no drift for already-archived artifacts

#### Scenario: [META-012.03] Reproducible content digest yields a stable derived identity across re-pushes

- **WHEN** byte-identical content is assembled and pushed a second time, yielding the identical reproducible content Manifest_Digest
- **THEN** the Derived_Identity computed over that digest byte-matches the one from the first push, because identity carries no synthetic/ambient state and no producer claim — the claim residing in the separately-signed Producer_Attestation_Referrer

#### Scenario: [META-012.04] Borne identity is preserved alongside, not overwritten

- **WHEN** producer-supplied content carries its own identifier (e.g. an OSCAL document root `uuid`)
- **THEN** that borne identity is recorded as a distinct identity and the Derived_Identity is still computed as `uuid5(CAIRN_NAMESPACE, <prefixed Manifest_Digest>)`, the two never collapsing into one slot

### Requirement: [META-004] List all bundles for a project

THE Metadata_Index SHALL support listing all Artifact_Bundles for a given project within a single **tenant** using a `Query` whose partition key is the `{tenant-id}` (exact-match) and whose sort-key condition is `begins_with('{project-id}#')`. This access pattern SHALL be tenant-scoped: the exact-match `{tenant-id}` partition key bounds the result to one tenant, and the `{project-id}` sort-key prefix selects the project sub-hierarchy within it.

#### Scenario: [META-004.01] Project-scoped list returns every bundle

- **WHEN** a caller queries for all bundles in project `prj-01JYBV8X1MZ3K7QF9R2T6WYN4D` within tenant `tnt-01JABCD2EF3GH4JK5MN6PQ7RS8`
- **THEN** the Metadata_Index issues a `Query` against the base table with the partition key set exact-match to `tnt-01JABCD2EF3GH4JK5MN6PQ7RS8` and the sort-key condition `begins_with('prj-01JYBV8X1MZ3K7QF9R2T6WYN4D#')`, returning every bundle for the project, paginated

## Relocated and Retired IDs

| ID | Status | Resolves to |
|---|---|---|
| META-006 | relocated | RETAIN-002 |
| META-006.01 | relocated | RETAIN-002.01 |

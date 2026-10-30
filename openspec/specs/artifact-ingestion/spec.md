# artifact-ingestion Specification

## Purpose
The producer-facing surface of the Provenance_System. It defines:

1. how files are bundled into OCI manifests;
2. which annotations and Config_Blob fields are required;
3. how content layers are sized and counted;
4. how SHA-256 digests are computed and verified at ingestion;
5. how ECR repositories and immutable tags are organized, so an artifact is structurally addressable the moment it lands.

Producers — CI/CD pipelines, scanners, AI agents, human operators — all use this surface the same way.

*Domain terms (`Capitalized_Underscore`) used below are defined in [GLOSSARY.md](../../GLOSSARY.md), which names the governing requirement for each.*
## Requirements
### Requirement: [INGEST-001] Artifact Taxonomy Is Strictly Governed

THE Provenance_System SHALL model every object it holds as an **Artifact** — a digest-addressed OCI object identified by its Manifest_Digest — and SHALL classify each Artifact by an **Artifact_Type** drawn from the closed vocabulary `evidence | reference | signature | worm-receipt | oci-index`, discriminated physically by the manifest's `config.mediaType`. The genus `Artifact` is the single term for everything in the store; "evidence" is NOT an intrinsic Artifact_Type but the OSCAL `relevant-evidence` reference role conferred by an observation citing the Artifact.

An **Artifact_Bundle** is the content-bearing Artifact (Artifact_Type `evidence` or `reference`): an OCI manifest referencing one Config_Blob and 1–300 **Content_Layers**.

A **Content_Layer** is the unit of stored content — exactly one producer-supplied file per layer (1:1, never combined nor split) — carrying that file's own SHA-256 digest and `org.opencontainers.image.title`. A Content_Layer is content WITHIN an Artifact_Bundle; it is NOT itself an Artifact and carries no Artifact_Type.

Signatures, WORM_Receipts, and OCI image-indexes are Artifacts (referrer / index types), neither Content_Layers nor Artifact_Bundles.

THE Provenance_System SHALL NOT use the term "artifact" to denote a single file or Content_Layer in any normative surface (spec text, code symbol, or wire annotation): the genus (`Artifact`), the content-bearing subtype (`Artifact_Bundle`), and the content unit (`Content_Layer`) are three distinct concepts, each with exactly one referent.

#### Scenario: [INGEST-001.01] A signature is an Artifact but not a Bundle

- **WHEN** the pipeline attaches a Notation signature referrer to a bundle's Manifest_Digest
- **THEN** the signature is an Artifact of Artifact_Type `signature`, discriminated by its `config.mediaType`
- **AND** it is NOT an Artifact_Bundle and carries no Content_Layers

#### Scenario: [INGEST-001.02] A producer file is a Content_Layer, not an Artifact

- **WHEN** a producer submits N files in one Artifact_Bundle
- **THEN** the bundle contains exactly N Content_Layers, one per file, each carrying that file's bytes, SHA-256 digest, and title
- **AND** no Content_Layer is itself classified as an Artifact or assigned an Artifact_Type

#### Scenario: [INGEST-001.03] The genus spans every stored object

- **WHEN** the store holds bundles, signatures, WORM_Receipts, and image-indexes
- **THEN** every one of them is an Artifact addressed by its Manifest_Digest
- **AND** each is classified by exactly one Artifact_Type via its `config.mediaType`

### Requirement: [INGEST-002] Ingestion Interfaces

THE Provenance_System SHALL accept Artifact_Bundles via CLI (ORAS), SDK, and API ingestion interfaces, and SHALL support both manual (human-initiated) and automated (pipeline-initiated) ingestion workflows. All ingestion interfaces SHALL share the same validation, digest, and bundle semantics, so no interface is a privileged path.

As one such shared semantic, every supported ingestion path SHALL map each supplied file to exactly one OCI content layer — one file per layer, one layer per file — and SHALL NOT combine multiple files into a single content layer nor split one file across layers. This 1:1 file-to-layer mapping is a producer-path contract binding on every ingestion interface (CLI, SDK, API, and any future importer); the storage layer does NOT independently enforce it against arbitrarily-constructed manifests.

The contract preserves per-file addressability: each supplied file keeps its own SHA-256 digest, its own `org.opencontainers.image.title` annotation, and its own Metadata_Index projection.

#### Scenario: [INGEST-002.01] CLI push via ORAS

- **WHEN** an Artifact_Producer pushes a valid Artifact_Bundle to the Provenance_System using the ORAS CLI
- **THEN** the Provenance_System accepts the bundle through the same ingestion pipeline as SDK and API submissions

#### Scenario: [INGEST-002.02] SDK push from automated pipeline

- **WHEN** an automated pipeline submits a valid Artifact_Bundle via the SDK
- **THEN** the Provenance_System accepts the bundle and applies the same validation, digest, and bundling semantics as a human-initiated CLI push

#### Scenario: [INGEST-002.03] API push from external producer

- **WHEN** an external Artifact_Producer submits a valid Artifact_Bundle via the API
- **THEN** the Provenance_System accepts the bundle and processes it identically to CLI and SDK submissions

#### Scenario: [INGEST-002.04] One content layer per file holds across every ingestion path

- **WHEN** an Artifact_Producer submits N evidence files (N ≥ 1) through any supported ingestion interface (CLI, SDK, or API)
- **THEN** the resulting bundle contains exactly N content layers, one per supplied file, each carrying that file's bytes as a single blob and that file's name in its `org.opencontainers.image.title` annotation
- **AND** no ingestion interface combines multiple supplied files into one content layer or splits one supplied file across multiple layers

### Requirement: [INGEST-003] File Size Constraint

THE Provenance_System SHALL accept arbitrary file types with individual file sizes up to 10 GB. IF an individual file exceeds 10 GB, THEN THE Provenance_System SHALL reject the upload and return an error indicating the maximum file size limit.

THE Provenance_System SHALL enforce the [INGEST-003.04] bundle bounds — per-file size and content-layer count — rejecting any violating bundle with an error that names the violated constraint using the same `criterion`/`field`/`message` shape as the other [INGEST-017.07] criteria.

THE Provenance_System SHALL enforce the content-layer-count bound authoritatively at the `validate-bundle` trust boundary as a permanent validation failure (the layer count is the number of content-layer descriptors and cannot be misrepresented without changing the manifest).

THE Provenance_System SHALL enforce the per-file-size bound in two tiers:

1. an ADVISORY check at `validate-bundle` against the producer-asserted OCI descriptor `size` field already on each content-layer Descriptor, with NO blob fetch — a fast rejection of honestly-declared oversize files, NOT a trust boundary against the true byte length; and
2. the AUTHORITATIVE check against each content layer's TRUE fetched byte length where the pipeline already reads and digest-verifies the blob (archive), treating a layer whose true byte length exceeds 10 GB — or disagrees with its declared descriptor `size` — as a permanent integrity failure.

A client-side `cairn push` pre-flight SHALL mirror the validate-time bounds for fast local rejection but is NOT the trust boundary.

#### Scenario: [INGEST-003.01] File within size limit accepted

- **WHEN** an Artifact_Producer submits a file of any type with size at or below 10 GB
- **THEN** the Provenance_System accepts the file

#### Scenario: [INGEST-003.02] Oversized file rejected by advisory descriptor check

- **WHEN** an Artifact_Producer submits a file whose OCI descriptor `size` field is larger than 10 GB
- **THEN** the Provenance_System rejects the bundle at validate-bundle with an error indicating the maximum file size limit and identifying the offending content layer, deriving the size from the layer's OCI descriptor `size` field without fetching blob bytes

#### Scenario: [INGEST-003.03] Spoofed descriptor size caught by byte-true check

- **WHEN** a file's declared OCI descriptor `size` is at or below 10 GB but its true fetched byte length exceeds 10 GB (or otherwise disagrees with the declared `size`)
- **THEN** the Provenance_System rejects it as a permanent integrity failure at the point the blob's true bytes are read and digest-verified, identifying the offending content layer — the advisory descriptor check at validate-bundle is NOT relied upon as the trust boundary for true byte size

#### Scenario: [INGEST-003.04] Bundle constraints property — size and layer count enforced together

- **WHEN** an Artifact_Bundle submission is evaluated for any combination of individual file size (0 GB to 15 GB) and content layer count (0 to 400)
- **THEN** the Provenance_System accepts the bundle if and only if every individual file is at most 10 GB and the total number of content layers is between 1 and 300 inclusive, and rejects any violating bundle with an error indicating which constraint was violated

#### Scenario: [INGEST-003.05] Empty bundle rejected (too few layers)

- **WHEN** an Artifact_Bundle with 0 content layers is validated
- **THEN** validation fails permanently with an error naming the too-few-layers constraint (content-layer count must be at least 1)

#### Scenario: [INGEST-003.06] Layer-count maximum is inclusive at 300

- **WHEN** an Artifact_Bundle with exactly 300 content layers is validated, and separately one with exactly 301
- **THEN** the 300-layer bundle passes the layer-count bound and the 301-layer bundle fails permanently with an error naming the too-many-layers constraint

### Requirement: [INGEST-004] Bundle Identity Is Pure Content Addressing

THE Provenance_System SHALL compute the content Manifest_Digest as a deterministic function of the bundle's content bytes alone — the content-descriptive Config_Blob plus the content layers — and SHALL NOT fold synthetic or ambient state (wall-clock `now()`, filesystem mtime, randomness, monotonic counters, or any per-push value) into the bytes the digest is taken over.

Re-assembling the same content member set (the same Config_Blob fields and the same content-layer bytes) SHALL yield the identical Manifest_Digest, so an artifact pushed twice byte-for-byte is the same bundle with the same digest, the same canonical ECR tag, and the same Metadata_Index identity. Conversely, any change to the content bytes SHALL yield a distinct Manifest_Digest.

The producer's compliance claim fields other than `projectId` (`orgId`, `tenantId`, artifact-kind, controls, catalog-or-profile UUID, assessment-period, assessor, assessment-id, supersedes) SHALL NOT be part of the content digest — they ride in a separate signed Attestation referrer (see *Producer Claim Carried as Signed Attestation Referrer*).

`projectId` is the sole identity field that IS part of the content digest: the claim carries a copy too (it remains one of the Producer_Attestation_Referrer's ten claim fields), but it is also retained as a content-descriptive Config_Blob field (see *Config_Blob Media Type and Content-Descriptive Fields*) and is therefore hashed into the Manifest_Digest rather than carried claim-only. The opaque `orgId` and `tenantId` identity coordinates are claim-and-path scope fields cross-checked against the ECR repository path (see *Producer Claim Carried as Signed Attestation Referrer*); they are NOT content-descriptive Config_Blob fields and SHALL NOT be hashed into the Manifest_Digest.

#### Scenario: [INGEST-004.01] Same content member set re-pushed yields the identical digest

- **WHEN** the same content member set (identical content-descriptive Config_Blob fields and identical content-layer bytes) is assembled and pushed on two separate occasions
- **THEN** the Provenance_System computes the identical content Manifest_Digest both times, so the two pushes resolve to the same bundle identity, the same canonical ECR tag, and the same Metadata_Index row rather than to two distinct bundles

#### Scenario: [INGEST-004.02] Distinct content bytes yield a distinct digest

- **WHEN** two bundles are assembled whose content bytes differ — for example the same logical files in a different layer order, or one byte changed in any layer or in a content-descriptive Config_Blob field
- **THEN** the Provenance_System computes distinct content Manifest_Digests for the two bundles, so they are distinct identities

#### Scenario: [INGEST-004.03] Ambient state never enters identity

- **WHEN** the same content member set is pushed at two different wall-clock times, from two different filesystems with different file mtimes, or in two process runs
- **THEN** the computed content Manifest_Digest is identical across all of these, because no wall-clock timestamp, filesystem mtime, randomness, or counter is folded into the bytes the digest is taken over

### Requirement: [INGEST-005] Producer Claim Carried as Signed Attestation Referrer

THE Provenance_System SHALL carry the producer's compliance claim as an Producer_Attestation_Referrer — an OCI referrer whose envelope `subject` is the content Manifest_Digest — rather than as Config_Blob fields or manifest annotations on the content manifest.

The claim payload SHALL be a standard **in-toto Statement** carried in the referrer's single layer (layer `mediaType` `application/vnd.in-toto+json`), with:

1. `_type` = `https://in-toto.io/Statement/v1`;
2. a `subject` that is a single-element array binding the content Manifest_Digest;
3. `predicateType` = the Cairn-owned URI `https://cairnproofs.dev/ns/attestation/producer/v1` (naming convention per ADR-0015); and
4. a `predicate` carrying the claim fields `{orgId, tenantId, projectId, artifactKind, controls, catalogOrProfileUuid, assessmentPeriod, assessor, assessmentId, supersedes}`.

The Producer_Attestation_Referrer's OCI envelope `artifactType` SHALL remain `application/vnd.cairnproofs.producer-attestation.v1+json` — the role label and the resolution/trigger discriminator — independent of the in-toto layer `mediaType`.

The `assessmentPeriod` claim field SHALL be producer-supplied and SHALL NEVER be derived: for an evidence-form artifact it is REQUIRED in the Producer_Attestation_Referrer; for a reference-form artifact it is absent and never fabricated. The pipeline SHALL NOT resolve a period from any creation timestamp (none exists) and SHALL NOT write a period into any ECR tag segment — there is no assessment-period segment in the canonical tag grammar (see *ECR Tag Format*).

The producer chooses whether to cite a Catalog UUID (vanilla controls) or a Profile UUID (tailored/parameterized controls) in the `catalogOrProfileUuid` claim field; both are valid claims, and the pipeline does not dereference one to the other. When `catalogOrProfileUuid` is present it MUST be a well-formed OSCAL UUIDDatatype. Producers need NOT embed the cited Catalog or Profile document in the bundle as a layer — the UUID alone is the authoritative reference; Cairn trusts it.

The claim's **identity tuple** is the opaque `orgId` + `tenantId` + `projectId` triple — each a stable, opaque identifier (a logical id, never an AWS account number and never a human display name). Display names live only in the control-plane identity registry (owned by the metadata-index capability), keyed by the opaque id, and SHALL NOT ride the signed claim. Each of the three identity coordinates SHALL be cross-checked, exact-match, against the corresponding segment of the IAM-enforced ECR repository path (`{org-id}`/`{tenant-id}`/`{project-id}`); the cross-check is enforced by the processing pipeline (see processing-pipeline) and is not conditioned on any deployment mode.

The Producer_Attestation_Referrer SHALL have its own content-addressable digest, and SHALL be independently signed as its own Notation signature referrer, distinct from the content manifest's signature. Because the claim is a referrer keyed on the content subject and not part of the content digest, multiple Producer_Attestation_Referrers MAY share a single content subject — "one content, many attestations" — each independently signed and independently verifiable.

THE Provenance_System SHALL resolve the claim for a content manifest via the OCI Referrers API (filtering on the Producer_Attestation_Referrer `artifactType`), and SHALL read the claim fields from the in-toto Statement's `predicate`, never by reading claim fields back out of the content digest. Before trusting the `predicate`, THE Provenance_System SHALL verify that the Statement's `predicateType` equals the Cairn predicate URI; a mismatch SHALL be treated as a permanent validation failure.

The Producer_Attestation_Referrer's `PutImage` is the submission-complete trigger for the validate stage; a content manifest with no Producer_Attestation_Referrer is an incomplete submission and is never promoted (see *Rejection of Bundles Missing Required Fields*).

**Subject-scope binding (anti-hijack).** An Producer_Attestation_Referrer is a separable object that any authorized producer may push against any content digest. Before resolving or promoting one, THE Provenance_System SHALL verify that the referrer's resolved `subject` content manifest resides in the **same IAM-enforced ECR repository path** as the Producer_Attestation_Referrer itself — identical `{appName}-artifact-{qualifier}/{org-id}/{tenant-id}/{project-id}` segments. The Producer_Attestation_Referrer and its subject MUST live in one repository, which IAM push authorization already scopes to the producer's org/tenant/project. An Producer_Attestation_Referrer whose `subject` resolves to a content manifest in a different repository path SHALL be treated as a permanent validation failure (a cross-scope attestation hijack) and routed to the DLQ; it SHALL NOT be resolved, signed, archived, or indexed. This binds the "one content, many attestations" property to a single repository, so a producer cannot attach a claim to another org's, tenant's, or project's content (closes threats T-1 / T-5).

**Dual-subject consistency.** An in-toto Statement carries its own `subject` in addition to the OCI envelope `subject`. The OCI envelope `subject` SHALL remain the sole authority for the subject-scope anti-hijack binding above, and THE Provenance_System SHALL additionally verify that the Statement's `subject` digest equals the OCI envelope `subject` digest before resolving or promoting the referrer. The comparison SHALL normalize the OCI prefixed form (`sha256:<hex>`) against in-toto's algorithm-keyed DigestSet (`{"sha256": "<hex>"}`) and match by algorithm key, tolerating a DigestSet that carries additional algorithms (the shared algorithm MUST agree; map equality is NOT required).

The Statement's `subject` MUST be a single-element array; a Statement with zero or more than one subject SHALL be treated as a permanent validation failure. A Statement whose `subject` digest diverges from the envelope `subject` digest SHALL be treated as a permanent validation failure and routed to the DLQ; the Statement `subject` SHALL NOT be used independently of the envelope `subject`.

#### Scenario: [INGEST-005.01] Producer claim is carried as a valid in-toto Statement

- **WHEN** an Producer_Attestation_Referrer is constructed for a content manifest
- **THEN** its layer (mediaType `application/vnd.in-toto+json`) is a valid in-toto Statement with `_type` = `https://in-toto.io/Statement/v1`, a single-element `subject` array binding the content Manifest_Digest, `predicateType` = `https://cairnproofs.dev/ns/attestation/producer/v1`, and a `predicate` carrying `{orgId, tenantId, projectId, artifactKind, controls, catalogOrProfileUuid, assessmentPeriod, assessor, assessmentId, supersedes}`
- **AND** the referrer's OCI envelope `artifactType` is `application/vnd.cairnproofs.producer-attestation.v1+json` and the referrer is attached and Notation-signed as its own signature referrer

#### Scenario: [INGEST-005.02] Claim resolved via the Referrers API, read from the Statement predicate

- **WHEN** the pipeline needs the producer's compliance claim for a content manifest
- **THEN** it queries the OCI Referrers API for that content Manifest_Digest, selects the Producer_Attestation_Referrer by its `artifactType`, verifies the Statement `predicateType` equals the Cairn predicate URI, and reads `{orgId, tenantId, projectId, artifactKind, controls, catalogOrProfileUuid, assessmentPeriod, assessor, assessmentId, supersedes}` from the Statement's `predicate`
- **AND** it never reconstructs the claim from fields embedded in the content manifest or its digest

#### Scenario: [INGEST-005.03] Statement with a mismatched predicateType is rejected

- **WHEN** an Producer_Attestation_Referrer is processed whose layer is an in-toto Statement but whose `predicateType` is not the Cairn predicate URI
- **THEN** the Provenance_System treats it as a permanent validation failure and does NOT resolve, sign, archive, or index it

#### Scenario: [INGEST-005.04] Statement subject must agree with the envelope subject

- **WHEN** an Producer_Attestation_Referrer is processed whose in-toto Statement `subject` digest does not equal the OCI envelope `subject` digest (after normalizing the prefixed OCI form against in-toto's algorithm-keyed DigestSet and matching by algorithm key)
- **THEN** the Provenance_System treats it as a permanent validation failure, routes it to the DLQ, and does NOT resolve, sign, archive, or index it
- **AND** the OCI envelope `subject` remains the sole authority for the subject-scope anti-hijack binding

#### Scenario: [INGEST-005.05] Statement carrying not exactly one subject is rejected

- **WHEN** an Producer_Attestation_Referrer is processed whose in-toto Statement `subject` array has zero entries or more than one entry
- **THEN** the Provenance_System treats it as a permanent validation failure and does NOT resolve, sign, archive, or index it

#### Scenario: [INGEST-005.06] Attestation referrer whose subject is foreign-scope content is rejected

- **WHEN** an Producer_Attestation_Referrer is pushed whose resolved `subject` content manifest resides in a different ECR repository path (a different `{org-id}`/`{tenant-id}`/`{project-id}` than the Producer_Attestation_Referrer's own repository)
- **THEN** the Provenance_System treats it as a permanent validation failure (cross-scope attestation hijack), routes it to the DLQ, and does NOT resolve, sign, archive, or index it

#### Scenario: [INGEST-005.07] One content, many attestations

- **WHEN** two distinct Producer_Attestation_Referrers — for example two assessment periods, or a producer claim and a later additional claim — are pushed against the same content Manifest_Digest, each independently signed
- **THEN** the Provenance_System accepts both referrers against the single content subject, signs and verifies each independently, and the content manifest's identity (its digest) is unchanged by the number of attestations referencing it

#### Scenario: [INGEST-005.08] Content manifest and Attestation referrer are signed independently

- **WHEN** the signing stage processes a validated submission
- **THEN** it produces one Notation signature referrer over the content manifest AND a separate Notation signature referrer over the Producer_Attestation_Referrer, so the claim can be re-attested or co-signed in future without re-signing the content

#### Scenario: [INGEST-005.09] Assessment period is producer-supplied in the claim, never derived

- **WHEN** an evidence-form submission is processed
- **THEN** the assessment period is read from the `assessmentPeriod` field of the Producer_Attestation_Referrer (REQUIRED for evidence-form), is never resolved from a creation timestamp (none exists) and never written into any ECR tag segment, and a reference-form submission has no period resolved or fabricated

### Requirement: [INGEST-006] Manifest Digest Computation

WHEN an Artifact_Bundle is uploaded, THE Provenance_System SHALL compute and store a SHA-256 content-addressable Manifest_Digest for every bundle at ingestion time.

The digest computation SHALL be deterministic in two senses:

1. the SHA-256 hash function SHALL be deterministic, so repeated computation over the same byte sequence yields the same result and conforms to the standard SHA-256 algorithm; and
2. the **input** to the digest — the content manifest bytes — SHALL itself be a deterministic function of the bundle's content member set (content-descriptive Config_Blob plus content layers) and SHALL NOT incorporate synthetic or ambient state (wall-clock `now()`, filesystem mtime, randomness), so re-assembling the same content member set yields byte-identical manifest input and therefore the identical digest (see *Bundle Identity Is Pure Content Addressing*).

Where a packaging tool would otherwise inject ambient state on the producer's behalf — notably `oras push` setting `org.opencontainers.image.created` to wall-clock `now()` when the producer omits the key — the producer SHALL neutralize it by pinning that field to a fixed deterministic value, so the tool's auto-injection cannot enter the digest input. Declining to set the field is insufficient when the tool injects it by default.

#### Scenario: [INGEST-006.01] Digest computed at ingestion

- **WHEN** an Artifact_Producer uploads an Artifact_Bundle
- **THEN** the Provenance_System computes a SHA-256 Manifest_Digest at ingestion time and stores it alongside the bundle

#### Scenario: [INGEST-006.02] Digest computation determinism

- **WHEN** the SHA-256 digest of any given byte sequence is computed twice by the Provenance_System
- **THEN** the Provenance_System produces the same result both times, and the result matches the standard SHA-256 algorithm output for that byte sequence

#### Scenario: [INGEST-006.03] Manifest input is deterministic over the content member set

- **WHEN** the same content member set (identical content-descriptive Config_Blob fields and identical content-layer bytes) is assembled into a content manifest on two separate occasions, possibly at different times or on different hosts
- **THEN** the assembled content manifest bytes are byte-identical both times — no *wall-clock* `org.opencontainers.image.created` (any such field is pinned to a fixed deterministic value, never push-time `now()`), no mtime, no randomness is folded in — so the computed Manifest_Digest is identical

#### Scenario: [INGEST-006.04] Tool-injected creation timestamp is pinned, not wall-clock

- **WHEN** the producer pushes byte-identical content twice at different wall-clock times using a packaging tool that auto-injects `org.opencontainers.image.created` (e.g. `oras push`)
- **THEN** the producer pins `org.opencontainers.image.created` to a fixed deterministic value so the tool cannot stamp the differing push-time wall-clock into the manifest
- **AND** the two pushes therefore yield the identical content Manifest_Digest rather than two distinct bundles

### Requirement: [INGEST-007] Integrity Check on Ingestion

WHEN an artifact fails integrity checks on upload — the computed digest does not match the client-provided digest — THE Provenance_System SHALL reject the artifact and return an error indicating the expected digest, the computed digest, and the artifact identifier.

#### Scenario: [INGEST-007.01] Client-provided digest matches computed digest

- **WHEN** a client uploads an artifact with a client-provided SHA-256 digest that matches the digest computed by the Provenance_System on receipt
- **THEN** the Provenance_System accepts the artifact

#### Scenario: [INGEST-007.02] Client-provided digest does not match

- **WHEN** a client uploads an artifact whose client-provided SHA-256 digest does not match the digest the Provenance_System computes on receipt
- **THEN** the Provenance_System rejects the artifact and returns an error containing the expected digest, the computed digest, and the artifact identifier

#### Scenario: [INGEST-007.03] Digest verification correctness

- **WHEN** the digest verification function is invoked with any blob content and any expected digest pair
- **THEN** the function returns `true` if and only if the SHA-256 of the blob content equals the expected digest, and on failure returns an error that includes both the computed digest and the expected digest

### Requirement: [INGEST-008] Manifest Digest Returned on Successful Ingestion

WHEN an Artifact_Bundle is successfully ingested, THE Provenance_System SHALL return the Manifest_Digest to the caller as confirmation of successful ingestion.

#### Scenario: [INGEST-008.01] Successful ingestion confirmation

- **WHEN** an Artifact_Bundle is successfully ingested by the Provenance_System
- **THEN** the response to the caller includes the Manifest_Digest of the bundle as confirmation

### Requirement: [INGEST-009] Bundling Multiple Files

THE Provenance_System SHALL support bundling multiple related files into a single logical Artifact_Bundle represented as an OCI artifact with up to 300 content layers.

#### Scenario: [INGEST-009.01] Bundle with multiple related files accepted

- **WHEN** an Artifact_Producer submits an Artifact_Bundle composed of multiple related files represented as content layers in a single OCI artifact, with the total number of layers between 1 and 300 inclusive
- **THEN** the Provenance_System accepts the bundle as a single logical Artifact_Bundle

### Requirement: [INGEST-010] OCI Manifest Structure

THE Provenance_System SHALL represent each Artifact_Bundle as an OCI manifest referencing a Config_Blob and one or more content layers, up to a maximum of 300 content layers per bundle.

#### Scenario: [INGEST-010.01] Bundle represented as OCI manifest

- **WHEN** an Artifact_Bundle is ingested
- **THEN** the Provenance_System represents it as an OCI manifest that references one Config_Blob and between 1 and 300 content layers inclusive

### Requirement: [INGEST-011] OCI Media Type per Layer

WHEN a content layer is added to an Artifact_Bundle, THE Provenance_System SHALL tag the layer with an OCI media type that describes its content format (e.g., `application/oscal.assessment-results+json`, `application/pdf`, `image/png`).

#### Scenario: [INGEST-011.01] Layer tagged with descriptive OCI media type

- **WHEN** a content layer is added to an Artifact_Bundle
- **THEN** the Provenance_System tags the layer with an OCI media type that describes its content format

### Requirement: [INGEST-012] Scope of Stored Artifacts

THE Provenance_System SHALL store the complete chain of material artifacts produced by automated audit workflows — not only control-mapped evidence.

#### Scenario: [INGEST-012.01] Reference artifact with no control linkage is stored and retrievable

- **WHEN** an Artifact_Bundle is submitted with valid identity annotations (project-id, artifact-kind) but no `controls` or `control-family` annotations
- **THEN** the Provenance_System accepts it, runs the full sign/archive pipeline, writes a Metadata_Index record keyed by project-id and Manifest_Digest, and the artifact is retrievable by project-id and by Manifest_Digest

### Requirement: [INGEST-013] Manifest-Level Annotations Required and Optional (Including Supersedes)

THE Provenance_System SHALL require manifest-level annotations only for the irreducible identity set — project-id — and SHALL NOT require a creation timestamp: the synthetic `org.opencontainers.image.created` annotation is not set, required, or read, because it would pollute content identity (see *Bundle Identity Is Pure Content Addressing*).

The claim fields `artifact-kind`, `controls`, assessment-period, assessor, assessment-id, and supersedes SHALL NOT be manifest-level annotations at all — they live in the Producer_Attestation_Referrer (see *Producer Claim Carried as Signed Attestation Referrer*), not on the content manifest. In particular, artifact-kind is carried solely as the Producer_Attestation_Referrer's `artifactKind` claim field; the `dev.cairnproofs.artifact.artifact-kind` manifest annotation is not set, required, or read, so kind does not perturb content identity.

Control linkage is value-add indexing metadata, not a storage precondition (see *Scope of Stored Artifacts*). `control-family` SHALL NOT be a manifest annotation either: it is a derived projection of the control identifiers, computed on demand by downstream consumers and never carried in the bundle (see the metadata-index *Automatic Metadata_Index updates from pipeline stages* requirement).

When `controls` are supplied in the Producer_Attestation_Referrer, every control identifier MUST be well-formed — a non-empty string of lowercase-alphanumeric segments joined by single `.` or `-` separators (grammar `^[a-z0-9]+([.-][a-z0-9]+)*$`, accepting NIST-style `ac-2` / `ac-2.1` and ISO-style `5.1` while rejecting empty strings, separators like `#` or `/`, whitespace, and non-ASCII), so it cannot poison the derived family set or forge a `{catalogOrProfileUuid}#{controlId}` relationship-table key; a malformed control identifier is a permanent validation failure.

The `supersedes` claim field, when present, is a comma-separated list of ECR tags within the same repository that this bundle supersedes — a formal declaration that this bundle replaces one or more prior bundles for the same assessment period.

#### Scenario: [INGEST-013.01] Bundle with only the required identity annotations accepted

- **WHEN** a content manifest is submitted with the required identity annotation (project-id) and no control annotations, no `dev.cairnproofs.artifact.artifact-kind` annotation, and no `org.opencontainers.image.created` annotation
- **THEN** the Provenance_System accepts the content manifest's annotations as well-formed and does not require a creation timestamp or an artifact-kind annotation

#### Scenario: [INGEST-013.02] Bundle citing controls without an explicit control-family accepted

- **WHEN** a submission supplies its identity annotations plus a `controls` list in the Producer_Attestation_Referrer, but no `control-family`
- **THEN** the Provenance_System accepts it and the control-family set is derived from the control identifiers rather than required from the producer

#### Scenario: [INGEST-013.03] Supersedes claim declares replacement of prior bundles

- **WHEN** the Producer_Attestation_Referrer includes a `supersedes` field containing a comma-separated list of ECR tags within the same repository
- **THEN** the Provenance_System treats the new bundle as a formal declaration that it replaces the listed prior bundles for the same assessment period

#### Scenario: [INGEST-013.04] Bundle supersession protocol

- **WHEN** a submission whose Producer_Attestation_Referrer `supersedes` field contains a non-empty list of tags is processed by the `update-metadata` Lambda
- **THEN** the Lambda (a) for each referenced tag, locates the corresponding Metadata_Index record within the same ECR repository and sets `activeStatus=superseded` and `supersededBy={new-bundle-tag}` using a conditional write that fails if `activeStatus` is already `superseded`, (b) creates the new bundle's own record with `activeStatus=active`, and (c) if any referenced tag has no corresponding Metadata_Index record, logs a warning but does not fail the pipeline; a bundle with no `supersedes` claim is always created with `activeStatus=active`

#### Scenario: [INGEST-013.05] Malformed control identifier rejected

- **WHEN** an Producer_Attestation_Referrer supplies a `controls` list containing an identifier that does not match the control-id grammar `^[a-z0-9]+([.-][a-z0-9]+)*$` (for example an empty string, `ac/2`, `ac#2`, a value with whitespace, or a non-ASCII string)
- **THEN** the Provenance_System rejects the submission as permanently invalid and returns an error identifying the malformed control identifier, before any family is derived or any Controls relationship-table key is constructed from it

### Requirement: [INGEST-014] Layer-Level Annotations Required and Optional

THE Provenance_System SHALL require a layer-level annotation for original filename on every content layer, and SHALL support optional layer-level annotations for source tool and generation timestamp (RFC 3339 format).

#### Scenario: [INGEST-014.01] Layer with original filename annotation accepted

- **WHEN** a content layer is submitted with an original-filename layer-level annotation
- **THEN** the Provenance_System accepts the layer's annotations as well-formed

#### Scenario: [INGEST-014.02] Optional layer annotations accepted

- **WHEN** a content layer includes optional source-tool and RFC 3339 generation-timestamp layer-level annotations in addition to the required original filename
- **THEN** the Provenance_System accepts and preserves all of the layer's annotations

### Requirement: [INGEST-015] Content-Addressable Deduplication

THE Provenance_System SHALL provide content-addressable deduplication at two distinct layers:

- **Blob-level deduplication:** WHEN identical content is ingested across multiple bundles, THE Provenance_System SHALL deduplicate the content using content-addressable storage so that the same blob digest is stored only once and is referenced from each bundle by its digest.
- **Bundle-level reproducible identity:** WHEN the same content member set (content-descriptive Config_Blob plus content layers) is re-assembled and re-pushed, THE Provenance_System SHALL resolve it to the identical content Manifest_Digest — and therefore the identical bundle identity, canonical ECR tag, and Metadata_Index row — rather than minting a second distinct bundle, because identity is a pure function of content bytes (see *Bundle Identity Is Pure Content Addressing*) and carries no synthetic creation timestamp. The producer's compliance claim, being a separate Producer_Attestation_Referrer, does not perturb this bundle-level identity.

#### Scenario: [INGEST-015.01] Identical content across bundles stored once (blob-level)

- **WHEN** content with an identical SHA-256 digest is ingested across two or more bundles
- **THEN** the Provenance_System stores the content only once and references it from each bundle by its digest

#### Scenario: [INGEST-015.02] Re-pushed identical bundle resolves to one identity (bundle-level)

- **WHEN** a producer re-pushes the same content member set a second time
- **THEN** the content Manifest_Digest is identical to the first push, so the re-push resolves to the same bundle identity and canonical tag rather than creating a second distinct bundle, making re-push an idempotent CI step

### Requirement: [INGEST-016] Config_Blob Media Type and Content-Descriptive Fields

THE Provenance_System SHALL use a Config_Blob, with media type `application/vnd.cairnproofs.artifact.config.v1+json`, that carries only **content-descriptive** bundle-level metadata.

The Config_Blob SHALL include project ID as a content-descriptive identifier; it MAY carry content-descriptive references such as OSCAL document references. The Config_Blob SHALL NOT carry the producer's compliance claim — `controls`, `catalogOrProfileUuid`, `assessmentPeriod`, producer/assessor identity, assessment-id, and supersedes are claim fields of the Producer_Attestation_Referrer (see *Producer Claim Carried as Signed Attestation Referrer*), never Config_Blob fields. The Config_Blob SHALL NOT carry a `controlFamily` field — control-family is a derived projection of the `controls` list, never bundle data.

The Config_Blob SHALL NOT carry the authoritative OSCAL schema version: for an OSCAL-bearing bundle the version is derived from each layer's own document content, and any `oscalSchemaVersion` Config_Blob field is not read and is not an acceptance criterion (the content-derived schema-version model, the tolerate-and-ignore handling of a declared field, and rejection on a missing/unsupported embedded version are owned by the oscal-interop *OSCAL SDK-based extraction with content-derived schema version* Requirement).

#### Scenario: [INGEST-016.01] Content-descriptive Config_Blob with required media type accepted

- **WHEN** a content manifest's Config_Blob is submitted with media type `application/vnd.cairnproofs.artifact.config.v1+json` and includes project ID, carrying only content-descriptive fields and no compliance-claim fields
- **THEN** the Provenance_System accepts the Config_Blob as well-formed

### Requirement: [INGEST-017] Rejection of Bundles Missing Required Fields

IF a submission's content manifest is missing one or more required manifest-level annotations, required layer-level annotations, or a valid Config_Blob, THEN THE Provenance_System SHALL reject the bundle and return an error indicating which required fields are missing or invalid.

The required manifest-level annotation set is the identity set only — project-id (artifact-kind is not a manifest annotation; it is carried in the Producer_Attestation_Referrer's `artifactKind` claim); there is no creation-timestamp requirement (the `org.opencontainers.image.created` annotation is not set, required, or read). A bundle SHALL NOT be rejected for omitting control linkage or assessment-period.

A **missing Producer_Attestation_Referrer** is NOT a missing-required-field rejection but an incomplete submission that is never validated or promoted — validation triggers on the Producer_Attestation_Referrer push, so a content-only manifest is never validated and surfaces as a "did-not-land" entry. This trigger behavior is owned by *Producer Claim Carried as Signed Attestation Referrer* and *Pipeline-Internal Artifact Filtering Before Bundle Validation* (see [INGEST-030.02] and [INGEST-031.02]).

#### Scenario: [INGEST-017.01] Missing required manifest annotation rejected

- **WHEN** a content manifest is submitted without the required identity manifest-level annotation (project-id)
- **THEN** the Provenance_System rejects the bundle and returns an error identifying the missing annotation

#### Scenario: [INGEST-017.05] Missing required layer annotation rejected

- **WHEN** a content manifest is submitted with a content layer that lacks the required original-filename layer-level annotation
- **THEN** the Provenance_System rejects the bundle and returns an error identifying the missing layer-level annotation

#### Scenario: [INGEST-017.06] Invalid Config_Blob rejected

- **WHEN** a content manifest is submitted with a Config_Blob whose media type is not `application/vnd.cairnproofs.artifact.config.v1+json` or whose body lacks required fields
- **THEN** the Provenance_System rejects the bundle and returns an error identifying the invalid Config_Blob field(s)

#### Scenario: [INGEST-017.07] Bundle validation completeness

- **WHEN** a submission is validated (triggered by its Producer_Attestation_Referrer push, with the content manifest resolved via the referrer subject)
- **THEN** the Provenance_System accepts the bundle if and only if every acceptance criterion (a)–(e) below holds, and on failure the returned error identifies all failing criteria
- **AND** (a) the required identity manifest-level annotation is present (project-id)
- **AND** (b) every content layer has the required `org.opencontainers.image.title` annotation
- **AND** (c) the Config_Blob has media type `application/vnd.cairnproofs.artifact.config.v1+json` and contains valid JSON with the required project-id field, and — only when present in the claim — a well-formed `catalogOrProfileUuid` (the OSCAL UUIDDatatype rule defined in *Producer Claim Carried as Signed Attestation Referrer*)
- **AND** (d) the `artifactKind` value (sourced from the Producer_Attestation_Referrer claim, not from the content manifest) is in the Artifact_Kind controlled vocabulary
- **AND** (e) when `controls` are present in the claim, every control identifier matches the control-id grammar defined in *Manifest-Level Annotations Required and Optional (Including Supersedes)* ([INGEST-013])
- **AND** criterion (a) does NOT include a creation timestamp (no synthetic `created` annotation exists), an artifact-kind manifest annotation (kind is claim-only), or the presence of an Producer_Attestation_Referrer (a missing referrer is an incomplete submission that is never validated, not a failed one)
- **AND** control linkage (`controls`, `control-family`, `catalogOrProfileUuid`) and assessment-period are NOT part of criterion (a) and their absence SHALL NOT fail validation; criterion (e) applies only when `controls` are present

### Requirement: [INGEST-020] ECR Tag Immutability Enabled

THE Provenance_System SHALL create all evidence ECR repositories with tag mutability mode `IMMUTABLE_WITH_EXCLUSION`, where the exclusion filters are scoped exclusively to the reserved alias namespace (the `_als` and `_chn` reserved middle tokens). The single canonical tag form (`{artifact-kind}--_ref--{digest}`) SHALL remain immutable: a push that targets an existing canonical tag SHALL be rejected and cannot be overwritten. Only tags in the reserved alias namespace SHALL be mutable. The configured exclusion filters SHALL NOT match any canonical `_ref` tag.

#### Scenario: [INGEST-020.01] Repository created with alias-scoped tag mutability exclusion

- **WHEN** an evidence ECR repository is created by the Provenance_System
- **THEN** the repository is created with tag mutability mode `IMMUTABLE_WITH_EXCLUSION` and exclusion filters that match only tags in the reserved alias namespace (the `_als`/`_chn` middle-token forms) and no canonical `_ref` tag

#### Scenario: [INGEST-020.02] Canonical tags remain immutable

- **WHEN** a push targets an existing canonical `{artifact-kind}--_ref--{digest}` tag
- **THEN** the push is rejected because the tag is immutable and cannot be overwritten

#### Scenario: [INGEST-020.03] Exclusion filters never match a canonical tag

- **WHEN** the configured exclusion filters are evaluated against any canonical `_ref` tag
- **THEN** no canonical tag matches, so canonical immutability is preserved

### Requirement: [INGEST-021] ECR Tag Format

THE Provenance_System SHALL mint the canonical ECR tag in the pipeline **after validation**, derived solely from the validated artifact-kind claim and the bundle's content Manifest_Digest — never supplied by the pusher, never derived from any producer free text or content member. The tag is a fail-closed, immutable address parsed and emitted by a single authority (`build_ref_tag`/`parse_tag` in `libs/oci/cairn_oci/categories.py`); it is an address, not an identity (identity is the Manifest_Digest). There is a **single** canonical tag form:

```
{artifact-kind}--_ref--{digest}
```

where:

- `artifact-kind` is drawn from the `Artifact_Kind` controlled vocabulary (see *Reject Invalid Artifact_Kind*), forbids a leading underscore, and is **sourced from the Producer_Attestation_Referrer's `artifactKind` claim field**, validated against the controlled vocabulary at validate time before the tag is minted;
- `_ref` is the reserved middle token, dispatched via the reserved-token registry;
- `digest` is the leading lowercase-hex prefix (fixed short length) of the bundle's **content** Manifest_Digest — a reproducible pure-content digest (see *Bundle Identity Is Pure Content Addressing*), serving as both the human-scannable address and the terminal collision-breaker.

The `digest` segment SHALL match `\A[0-9a-f]+\Z` at the fixed prefix length. The `--` (double-hyphen) delimiter is unambiguous because every segment uses at most single internal hyphens and may never contain `_`. The assembled tag (controlled-vocabulary kind + reserved token + fixed-length hex prefix) SHALL NOT exceed the ECR 128-character tag limit.

There is NO `name` segment in the canonical tag: it carries NO producer-influenced free text, so it cannot misrepresent a multi-file bundle by naming it after one member, and it never reaches into content (a content layer's filename or an embedded document title) for a name that one content type would get and others would not. Memorable naming is provided by the mutable `{artifact-kind}--_als--{name}` named-alias form (see *Alias Tag Forms*), set deliberately, NOT auto-derived into the canonical address.

There is likewise NO assessment-period segment, NO control-scope segment, NO server-assigned sequence counter, and NO uuid in the canonical tag — period and control-scope are claim fields in the Producer_Attestation_Referrer, never encoded in any tag (see *Config_Blob Media Type and Content-Descriptive Fields*).

The content Manifest_Digest prefix is the collision-breaker: the canonical tag is a deterministic function of the validated artifact, and because the digest is a reproducible function of content bytes, the same artifact re-pushed derives the identical tag (idempotent under ECR tag immutability) and distinct content derives a distinct tag.

The tag applies to **every** validated artifact, identically: the form is a pure function of the validated artifact-kind and the content Manifest_Digest — both always present — so every validated artifact receives a canonical tag, with NO "untagged / digest-only" outcome and NO content-type-specific branch.

#### Scenario: [INGEST-021.01] Canonical tag carries the kind and digest-prefix segments

- **WHEN** the pipeline mints the canonical tag for a validated artifact
- **THEN** the tag SHALL have the form `{artifact-kind}--_ref--{digest}`
- **AND** `artifact-kind` SHALL be sourced from the Producer_Attestation_Referrer's `artifactKind` claim and validated against the controlled vocabulary
- **AND** `digest` SHALL be the leading lowercase-hex prefix of the bundle's reproducible content Manifest_Digest
- **AND** the tag SHALL contain no name, assessment-period, control-scope, sequence, or uuid segment

#### Scenario: [INGEST-021.02] Multi-file bundle is addressed without naming any single member

- **WHEN** the pipeline mints the canonical tag for a validated bundle whose content manifest carries more than one layer (more than one pushed file)
- **THEN** the tag SHALL be `{artifact-kind}--_ref--{digest}` exactly as for a single-file bundle
- **AND** NO layer's filename (`org.opencontainers.image.title`) and NO layer's embedded document title SHALL appear in the tag, so the address cannot misrepresent the bundle as any one of its members

#### Scenario: [INGEST-021.03] Bundles of any content format receive the identical tag form

- **WHEN** bundles of different content formats (for example an OSCAL-bearing bundle and a non-OSCAL bundle) of the same artifact-kind are each validated and tagged
- **THEN** each SHALL receive a tag of the form `{artifact-kind}--_ref--{digest}`
- **AND** the tag SHALL reach into no content for a name segment, for any format, so no bundle is addressed by the title of one of its members

#### Scenario: [INGEST-021.04] Reproducible digest makes re-push derive the identical tag

- **WHEN** the same content member set is validated and tagged on two separate pushes
- **THEN** because the content Manifest_Digest is reproducible (pure function of content bytes, no synthetic `created`), the trailing digest-prefix segment is identical both times
- **AND** the same artifact pushed twice SHALL derive the genuinely identical canonical tag, absorbed idempotently by ECR tag immutability
- **AND** two artifacts with distinct content SHALL derive distinct tags by virtue of distinct digests
- **AND** NO sequence counter and NO uuid SHALL be consulted or allocated

#### Scenario: [INGEST-021.05] Tag collision across distinct digests fails closed

- **WHEN** two manifests with different content Manifest_Digests resolve to the same `(artifact-kind, digest-prefix)` and the second is minted after the first
- **THEN** the second mint's tag application SHALL be rejected by ECR tag immutability (the tag already maps to a different digest)
- **AND** the condition SHALL be surfaced as a legible "tag already mapped to a different digest" failure that names the colliding digest
- **AND** the second artifact SHALL remain durable and digest-addressable

#### Scenario: [INGEST-021.06] Tag form is discriminated by a reserved token, not by arity

- **WHEN** `parse_tag` interprets a tag after splitting on `--`
- **THEN** if the middle segment is the registered `_ref` reserved token it SHALL dispatch to that token's parse handler and require the three-segment reference form `{artifact-kind}--_ref--{digest}`
- **AND** any other shape SHALL raise `InvalidTagError` rather than be silently re-interpreted

#### Scenario: [INGEST-021.07] Reserved-token namespace is leading-underscore and fail-closed on unallocated tokens

- **WHEN** the middle segment of a tag has a leading underscore but is not a registered reserved token
- **THEN** `parse_tag` SHALL raise `InvalidTagError` (reserved-but-unallocated), never a silent default
- **AND** because the segment grammars can never produce a leading underscore, a reserved token can never be forged by a real coordinate

#### Scenario: [INGEST-021.08] Tag is minted by the pipeline after validation, not by the pusher

- **WHEN** an artifact is admitted to the pipeline
- **THEN** the canonical tag SHALL be produced by the pipeline from the validated artifact-kind recorded at validate time plus the content Manifest_Digest
- **AND** `cairn push` SHALL NOT compute, supply, or own the tag

#### Scenario: [INGEST-021.09] Tag format round-trip

- **WHEN** a canonical tag is constructed from valid components and then parsed back
- **THEN** parsing yields the original components — artifact-kind and digest prefix — demonstrating the grammar is unambiguous and round-trip safe

### Requirement: [INGEST-022] Alias Tag Forms

THE Provenance_System SHALL support two mutable alias tag forms, each registered in the reserved-token registry alongside `_ref` (see *ECR Tag Format*), each carrying a reserved `_`+3-character token in the **middle** segment and the artifact-kind in the leading segment:

- a **named alias** `{artifact-kind}--_als--{name}` (3 segments), where `{name}` is a render-safe slug (lowercase alphanumeric, single internal hyphens); and
- a **channel pointer** `{artifact-kind}--_chn--{channel}` (3 segments), where `{channel}` is a render-safe slug, kind-scoped and enumerable as `{artifact-kind}--_chn--*`.

THE tag parser SHALL discriminate these forms by content — the reserved **middle** token, exactly as it discriminates `_ref` — not by arity, dispatching through the existing reserved-token registry with no change to the parse/build control flow, and SHALL fail closed with an invalid-tag error for any tag whose middle segment is a reserved token but whose shape does not match that token's registered form. Because the artifact-kind grammar forbids a leading underscore and the name/channel slug grammar forbids both a leading underscore and `--`, alias tags SHALL be provably disjoint from the canonical `_ref` form and from each other, and SHALL never collide with a canonical coordinate.

#### Scenario: [INGEST-022.01] Named alias parsed and round-tripped

- **WHEN** a named alias tag `{artifact-kind}--_als--{name}` is constructed and then parsed back
- **THEN** parsing yields the reserved `_als` form, the artifact-kind, and the original `{name}`, distinct from the canonical `_ref` form

#### Scenario: [INGEST-022.02] Channel pointer parsed and round-tripped

- **WHEN** a channel pointer `{artifact-kind}--_chn--{channel}` is constructed and then parsed back
- **THEN** parsing yields the reserved `_chn` form, the artifact-kind, and the channel, distinct from the canonical `_ref` form

#### Scenario: [INGEST-022.03] Malformed reserved-token tag fails closed

- **WHEN** a tag carries `_als` or `_chn` in its middle segment but does not match the registered form for that token
- **THEN** the parser rejects it with an invalid-tag error rather than silently defaulting to another form

#### Scenario: [INGEST-022.04] Alias tag cannot collide with a canonical tag

- **WHEN** any canonical `{artifact-kind}--_ref--{digest}` tag is parsed
- **THEN** it never matches an alias form, because it carries the `_ref` reserved token and its segment grammars forbid a leading underscore

### Requirement: [INGEST-023] Alias Set and Move Operation

THE Provenance_System SHALL provide an operator operation to set or move an alias tag, subject to all of the following constraints:

- the alias tag MUST be a well-formed `_als` or `_chn` tag (see *Alias Tag Forms*);
- the operation MUST resolve the alias only to an **already-filed** digest — a manifest that has been validated and carries a canonical `{artifact-kind}--_ref--{digest}` tag and a Metadata_Index record — and SHALL be rejected with a legible error if the target digest is not filed;
- a move onto an existing alias tag SHALL atomically repoint the alias to the target digest in a single registry operation;
- the operation SHALL be authorized under the alias-movement boundary defined in the access-control capability.

An alias SHALL be a view over an existing validated artifact and SHALL NOT introduce a new or un-validated address.

#### Scenario: [INGEST-023.01] Alias set to a filed digest succeeds

- **WHEN** an authorized operator sets `catalog--_als--nist-800-53-latest` to a digest that is already filed (validated, canonically tagged, and indexed)
- **THEN** the alias tag is created pointing at that digest

#### Scenario: [INGEST-023.02] Alias to an unfiled digest is rejected

- **WHEN** an operator attempts to set or move an alias to a digest that has not been validated and canonically tagged
- **THEN** the operation is rejected with an error indicating the digest is not filed

#### Scenario: [INGEST-023.03] Move atomically repoints an existing alias

- **WHEN** an authorized operator moves `catalog--_chn--latest` from one filed digest to another
- **THEN** the alias is repointed to the new digest in a single registry operation, and the previously pointed-to manifest retains its canonical immutable `_ref` tag

### Requirement: [INGEST-024] Alias Tags Do Not Carry the Verification Guarantee

WHEN an artifact is reached via an alias tag, THE Provenance_System retrieval and verification paths SHALL resolve the alias to its underlying Manifest_Digest and SHALL anchor verification on that digest, its signature, and its WORM_Receipt — never on the alias string. THE Provenance_System SHALL surface an alias as a mutable label and SHALL NOT render any verification verdict keyed on the alias itself.

#### Scenario: [INGEST-024.01] Verify resolves an alias to its digest

- **WHEN** a consumer verifies an artifact reached via an `_als` or `_chn` alias
- **THEN** verification is performed against the resolved Manifest_Digest, its Notation/AWS Signer signature, and its WORM_Receipt, and the alias is presented only as a mutable label, not as the verified identity

#### Scenario: [INGEST-024.02] Mutable alias is labeled as such on retrieval

- **WHEN** a consumer pulls or queries an artifact via an alias
- **THEN** the response indicates the artifact was reached via a mutable alias and shows the underlying immutable digest

### Requirement: [INGEST-026] Manifest_Digest as Canonical Identifier

THE Provenance_System SHALL use the Manifest_Digest (sha256) as the canonical identifier for all signing operations, WORM archive cross-references, Metadata_Index records, and OSCAL artifact references.

#### Scenario: [INGEST-026.01] Manifest_Digest used as canonical identifier across subsystems

- **WHEN** an Artifact_Bundle is referenced by signing, WORM archive cross-reference, Metadata_Index record, or OSCAL artifact reference
- **THEN** the reference uses the bundle's SHA-256 Manifest_Digest as the canonical identifier

### Requirement: [INGEST-027] ECR Lifecycle for Untagged Manifests

THE Provenance_System SHALL apply ECR lifecycle policies to remove untagged manifests after a configurable retention period (default: 90 days, minimum: 1 day, maximum: 365 days) while retaining all tagged immutable manifests.

#### Scenario: [INGEST-027.01] Untagged manifest removed after retention period

- **WHEN** an untagged ECR manifest exists in an artifact repository and its age exceeds the configured untagged-manifest retention period (default 90 days; bounded between 1 day and 365 days)
- **THEN** the Provenance_System's ECR lifecycle policy removes the untagged manifest while leaving all tagged immutable manifests in place

### Requirement: [INGEST-028] Reject Push Using Existing Immutable Tag

IF a push attempts to use a canonical `{artifact-kind}--_ref--{digest}` tag that already exists in a repository, THEN THE Provenance_System SHALL reject the push and return an error indicating that the tag is already in use and cannot be overwritten. This rejection SHALL apply to every immutable canonical tag and SHALL NOT apply to tags in the reserved alias namespace (the `_als`/`_chn` middle-token forms), which are mutable by an authorized alias move (see *ECR Tag Immutability Enabled* and *Alias Set and Move Operation*).

#### Scenario: [INGEST-028.01] Duplicate canonical tag push rejected

- **WHEN** a producer attempts to push to a repository using a canonical `_ref` tag that already exists in that repository
- **THEN** the Provenance_System rejects the push and returns an error indicating that the tag is already in use and cannot be overwritten

#### Scenario: [INGEST-028.02] Existing alias tag may be repointed by an authorized move

- **WHEN** an authorized alias move targets an existing `_als` or `_chn` alias tag in the repository
- **THEN** the operation is permitted and the alias is repointed, because alias tags are excluded from the immutability rejection

### Requirement: [INGEST-029] Reject Invalid Artifact_Kind

THE Provenance_System SHALL recognize an `Artifact_Kind` controlled vocabulary of fifteen canonical values, grouped as:

- **OSCAL document roles (8)** — one per GA OSCAL model, canonical spelling = the verbatim OSCAL schema root name: `catalog`, `profile`, `mapping-collection`, `component-definition`, `system-security-plan`, `assessment-plan`, `assessment-results`, `plan-of-action-and-milestones`.
- **Non-OSCAL document roles (7)** — canonical spelling = singular: `config-snapshot`, `compliance-report`, `attestation`, `system-documentation`, `governance-doc`, `tool-output`, `other`.

`Artifact_Kind` classifies a document's **role, not its format**. An OSCAL document role MAY be satisfied by either OSCAL content or a legacy / non-OSCAL counterpart (e.g. `assessment-results` may be an OSCAL SAR or a legacy PDF assessment report; `plan-of-action-and-milestones` may be an OSCAL POA&M or an Excel sheet). Vocabulary membership SHALL NOT require OSCAL content; any cross-check of a kind against extracted OSCAL model type is opportunistic and applies only when OSCAL content is present.

THE Provenance_System SHALL accept a set of **aliases** that normalize-on-input to a canonical value: a request MAY supply an alias and the Provenance_System SHALL record and operate on the resolved canonical value. The alias set SHALL include, at minimum:

- the natural plural of every singular Group-B canonical (`config-snapshots`, `compliance-reports`, `attestations`, `governance-docs`, `tool-outputs`);
- OSCAL short forms: `control-catalog`/`control-framework` → `catalog`, `baseline` → `profile`, `mapping` → `mapping-collection`, `cdef` → `component-definition`, `ssp` → `system-security-plan`, `ap`/`sap` → `assessment-plan`, `ar`/`sar` → `assessment-results`, `poam`/`mitigation-plan` → `plan-of-action-and-milestones`;
- governance sub-types that collapse to one kind: `policy`/`policies`/`standard`/`standards`/`guideline`/`guidelines` → `governance-doc`;
- output synonyms: `scan-report`/`scan-reports`/`api-output`/`api-outputs` → `tool-output`.

Every token across the combined canonical-and-alias set SHALL be globally unique — no token SHALL resolve to more than one canonical value. A single resolver SHALL perform normalization identically wherever a kind is read, and SHALL be **fail-closed**: a token that is neither a canonical value nor a known alias SHALL be rejected.

IF a repository creation or push request specifies an artifact-kind token that does not resolve (via the alias set) to a canonical vocabulary value, THEN THE Provenance_System SHALL reject the request and return an error indicating the invalid kind and listing the allowed **canonical** values. The artifact-kind subject to this check is read from the Producer_Attestation_Referrer's `artifactKind` claim field (see *Producer Claim Carried as Signed Attestation Referrer*).

#### Scenario: [INGEST-029.01] Invalid artifact-kind rejected with allowed canonical values

- **WHEN** a repository creation or push request specifies an artifact-kind token that resolves to no canonical value (e.g. `screenshot`)
- **THEN** the Provenance_System rejects the request and returns an error identifying the invalid kind and listing the allowed canonical values (`catalog`, `profile`, `mapping-collection`, `component-definition`, `system-security-plan`, `assessment-plan`, `assessment-results`, `plan-of-action-and-milestones`, `config-snapshot`, `compliance-report`, `attestation`, `system-documentation`, `governance-doc`, `tool-output`, `other`)

#### Scenario: [INGEST-029.02] A canonical value is accepted unchanged

- **WHEN** a request specifies `catalog`, `attestation`, or `plan-of-action-and-milestones`
- **THEN** the Provenance_System accepts it and records the same canonical value

#### Scenario: [INGEST-029.03] An alias normalizes on input to its canonical value

- **WHEN** a request specifies an alias such as `attestations`, `baseline`, `poam`, or `policies`
- **THEN** the Provenance_System accepts it and records the resolved canonical value (`attestation`, `profile`, `plan-of-action-and-milestones`, `governance-doc` respectively)

#### Scenario: [INGEST-029.05] Unknown token fails closed

- **WHEN** a request specifies a token that is neither a canonical value nor a known alias
- **THEN** the resolver raises rather than defaulting, and the request is rejected with the allowed-canonical-values error

#### Scenario: [INGEST-029.06] Artifact-kind vocabulary enforced as part of bundle validation completeness

- **WHEN** the bundle-validation function evaluates an Artifact_Bundle whose `artifactKind` claim value (read from the Producer_Attestation_Referrer) does not resolve to a canonical Artifact_Kind value
- **THEN** the function first normalizes the value via the single resolver, and on a non-resolving token rejects the bundle with an error identifying the invalid kind alongside any other failing criteria

#### Scenario: [INGEST-029.07] Alias-set global uniqueness is enforced

- **WHEN** the alias-to-canonical map is loaded
- **THEN** every token (canonical and alias) resolves to exactly one canonical value, and a map containing a token bound to two canonical values fails the uniqueness check rather than resolving ambiguously

### Requirement: [INGEST-030] Pipeline-Internal Artifact Filtering Before Bundle Validation

THE Provenance_System SHALL, before applying [INGEST-017.07] bundle validation (the *Rejection of Bundles Missing Required Fields* and *Config_Blob Media Type and Content-Descriptive Fields* Requirements), classify each pushed OCI artifact by its `PutImage` event and SHALL advance to bundle validation **only** an artifact positively identified as an Producer_Attestation_Referrer. Every other pushed artifact SHALL be skipped with a benign, non-failure disposition. Under pure content addressing the producer's claim — not the content — completes a submission, so the Producer_Attestation_Referrer's push is the single positive trigger and all other pushes (including the content manifest's own push) are skipped (see processing-pipeline *ECR PUSH event triggers validation stage*).

An artifact SHALL be advanced to bundle validation when, and only when, its `PutImage` is for a manifest whose `artifactType` is the Producer_Attestation_Referrer media type `application/vnd.cairnproofs.producer-attestation.v1+json` and whose `subject` references a content manifest digest. On that trigger THE Provenance_System SHALL resolve the content manifest via the Referrers API and apply [INGEST-017.07] to the (content manifest + claim) pair — the content manifest's structure (Config_Blob media type `application/vnd.cairnproofs.artifact.config.v1+json`, layer titles) and the claim fields read from the Producer_Attestation_Referrer.

An artifact SHALL be skipped — classified as not-a-submission-trigger — when any of the following holds:

- the manifest is a **content manifest** (its `config.mediaType` is `application/vnd.cairnproofs.artifact.config.v1+json`): a content manifest is never validated or promoted on its own push; it is validated only transitively when an Producer_Attestation_Referrer over it triggers validation. A content manifest with no Producer_Attestation_Referrer is an incomplete submission and is never promoted; or
- the manifest's top-level `mediaType` is an OCI image index (`application/vnd.oci.image.index.v1+json`), which carries no `config` descriptor; or
- the manifest's `config.mediaType` is the Notation signature media type (`application/vnd.cncf.notary.signature`); or
- the manifest's `config.mediaType` is the OCI empty/referrer config media type (`application/vnd.oci.empty.v1+json`) — covering the WORM_Receipt and artifact-reference resource referrers the archive stage attaches; or
- the manifest is otherwise not a positively-identified Producer_Attestation_Referrer.

These skipped artifacts come from ordinary ingestion (the content manifest push) and from the Provenance_System's own signing/archive flow (the Notation signatures, the WORM_Receipt, and the artifact-reference resource referrer the pipeline attaches — including the signature attached to the Producer_Attestation_Referrer itself).

The pipeline-attached pushes (signatures, WORM_Receipt, artifact-reference resource referrer, and the canonical tag) are issued under pipeline-stage roles and SHALL be dropped **upstream at the EventBridge trigger rule** by their push principal, so they never invoke `validate-bundle` (see processing-pipeline *ECR PUSH event triggers validation stage*). The content manifest push is issued under a **producer** principal — the same principal that pushes the Producer_Attestation_Referrer — so the rule cannot distinguish it by principal; it reaches the Lambda and SHALL be skipped here by its `config.mediaType`.

This in-handler classification is therefore authoritative and SHALL remain fail-closed regardless of any upstream rule filtering: it is the sole correctness gate for the content-manifest skip, and the defense-in-depth gate for any pipeline-internal or unrecognized artifact that reaches the Lambda (e.g. a future pipeline-stage role not yet excluded by the rule, or such a push by a non-pipeline principal).

When an artifact is skipped, THE Provenance_System SHALL NOT treat it as a permanent validation failure, SHALL NOT emit an `artifact.validation-failed` event, and SHALL NOT advance it to the signing stage; it SHALL instead record the skip observably (see the observability capability's `ValidationSkipped` metric) and return without error.

The classification SHALL be fail-closed in the promotion direction: nothing SHALL be promoted to signing, archive, or the Metadata_Index unless it is reached through a positively-identified Producer_Attestation_Referrer over a content manifest that passes [INGEST-017.07]. A content manifest SHALL NEVER be signed, archived, or indexed standalone; an unrecognized artifact SHALL be skipped and left inert, never promoted or silently advanced.

#### Scenario: [INGEST-030.01] Producer_Attestation_Referrer push triggers validation of content and claim

- **WHEN** an ECR `PutImage` event is received for a manifest whose `artifactType` is `application/vnd.cairnproofs.producer-attestation.v1+json` and whose `subject` is an already-pushed content manifest digest
- **THEN** the Provenance_System advances it to bundle validation, resolves the content manifest via the Referrers API, and applies [INGEST-017.07] to the content manifest structure plus the claim fields read from the Producer_Attestation_Referrer

#### Scenario: [INGEST-030.02] Content-only manifest push is skipped until its attestation arrives

- **WHEN** an ECR `PutImage` event is received for a content manifest whose `config.mediaType` is `application/vnd.cairnproofs.artifact.config.v1+json` and which is not yet referenced by any Producer_Attestation_Referrer
- **THEN** the Provenance_System skips it as not-a-submission-trigger, emits no `artifact.validation-failed` event, does not advance it to signing, records a `ValidationSkipped` metric, and leaves it un-promoted (an incomplete submission)

#### Scenario: [INGEST-030.03] Notation signature referrer push is skipped, not failed

- **WHEN** an ECR `PutImage` event is received for a manifest whose `config.mediaType` is `application/vnd.cncf.notary.signature`
- **THEN** the Provenance_System classifies it as a non-trigger artifact, skips it, emits no `artifact.validation-failed` event, does not advance it to signing, and records a `ValidationSkipped` metric

#### Scenario: [INGEST-030.04] OCI empty/referrer config manifest is skipped, not failed

- **WHEN** an ECR `PutImage` event is received for a manifest whose `config.mediaType` is `application/vnd.oci.empty.v1+json` (the WORM_Receipt or artifact-reference resource referrer the archive stage attaches)
- **THEN** the Provenance_System classifies it as a non-trigger artifact, skips it, and emits no `artifact.validation-failed` event

#### Scenario: [INGEST-030.05] OCI image index push is skipped, not failed

- **WHEN** an ECR `PutImage` event is received for a manifest whose top-level `mediaType` is `application/vnd.oci.image.index.v1+json` and which carries no `config` descriptor
- **THEN** the Provenance_System classifies it as a non-trigger artifact and skips it without raising an error or emitting an `artifact.validation-failed` event

#### Scenario: [INGEST-030.06] Unrecognized artifact is skipped and left inert

- **WHEN** an ECR `PutImage` event is received for a manifest that is neither an Producer_Attestation_Referrer nor a recognized content/internal artifact (e.g. a manifest whose `config.mediaType` is some other unexpected value)
- **THEN** the Provenance_System skips it, does not promote it to signing/archive/index, and records the skip — nothing is promoted absent a valid Producer_Attestation_Referrer over a valid content manifest

#### Scenario: [INGEST-030.07] Pipeline-stage pushes are dropped upstream; the in-handler classifier still gates whatever reaches it

- **WHEN** the signing/archive/mint stages attach their referrers, signatures, and canonical tag under pipeline-stage roles (dropped at the trigger rule by principal), AND a content manifest is pushed by a producer principal (reaches the Lambda)
- **THEN** the pipeline-stage pushes never invoke `validate-bundle`, while the content-manifest push is still classified in-handler by its `config.mediaType` and skipped as an incomplete submission — the in-handler classifier remaining the authoritative, fail-closed gate for everything that reaches it

### Requirement: [INGEST-031] Push By Digest (Untagged Admission)

THE Provenance_System SHALL admit a pushed artifact by its content digest, **untagged**: the pusher SHALL NOT mint or supply a canonical ECR tag, which the pipeline instead mints downstream after validation (see *ECR Tag Format*). The untagged content manifest's `PutImage` does NOT itself trigger validation — validation triggers only on the push of the Producer_Attestation_Referrer whose `subject` is the content manifest digest (see *Pipeline-Internal Artifact Filtering Before Bundle Validation* and processing-pipeline *ECR PUSH event triggers validation stage*). Untagged admission changes nothing about that trigger, because the trigger never keyed on a tag: the pipeline SHALL key on the manifest digest. `cairn push` SHALL return the Manifest_Digest synchronously; the human-readable canonical tag is therefore eventually consistent, existing only after the pipeline mints it, while the digest remains resolvable for the entire pushed-but-not-yet-tagged window.

#### Scenario: [INGEST-031.01] Successful push by digest, untagged

- **WHEN** the CLI pushes a fully-assembled Artifact_Bundle
- **THEN** the manifest, Config_Blob, and layer blobs SHALL be uploaded to the repository
- **AND** the manifest SHALL be referenced by its `sha256:` content digest
- **AND** no canonical tag SHALL be supplied by the client at push time

#### Scenario: [INGEST-031.02] Untagged admission leaves the validation trigger unchanged

- **WHEN** the untagged content manifest is pushed, followed by its Producer_Attestation_Referrer
- **THEN** the content manifest's own `PutImage` SHALL NOT trigger bundle validation (it is skipped as not-a-submission-trigger)
- **AND** the Producer_Attestation_Referrer's `PutImage` SHALL trigger the validate stage exactly as it does for any submission
- **AND** the pipeline SHALL key on the manifest digest, not on a tag

#### Scenario: [INGEST-031.03] Address is eventually consistent

- **WHEN** `cairn push` completes
- **THEN** it SHALL return the Manifest_Digest synchronously
- **AND** the canonical tag SHALL exist only after the pipeline mints it
- **AND** the digest SHALL remain resolvable for the entire pushed-but-not-yet-tagged window

### Requirement: [INGEST-032] Producer CLI Invocation Is Unambiguous and Self-Documenting

THE producer-facing `cairn push` command SHALL expose every non-file input as a named option (`--switch`) and SHALL treat the trailing file list as the sole positional argument, so the binding of operator-supplied tokens to bundle fields is unambiguous regardless of argument order or count. A required field that is omitted SHALL cause the command to fail loud with an error naming the missing option, and SHALL NEVER silently consume a file argument as a metadata field. Each producer-facing command SHALL be self-documenting: invoking it with `--help` SHALL render a description, its options, and at least one usage example.

THE producer SHALL supply the three identity coordinates that populate the claim's identity tuple — the opaque `{org-id}`, `{tenant-id}`, and `{project-id}`. THE `cairn` CLI SHALL resolve each coordinate from the first of the following sources that provides it, so a producer that has configured its target need NOT repeat any coordinate on each invocation:

1. an explicit command-line argument — the three named options `--org-id`, `--tenant-id`, `--project-id`, or a single fully-qualified `--target {org-id}/{tenant-id}/{project-id}`;
2. the environment variables `CAIRN_ORG_ID`, `CAIRN_TENANT_ID`, and `CAIRN_PROJECT_ID`;
3. a persisted CLI configuration — a project-local config file (e.g. `.cairn.toml`) overriding a per-user config (e.g. under `$XDG_CONFIG_HOME/cairn/`), written by a `cairn config set-target` command.

Explicit arguments SHALL override the environment, which SHALL override the configuration. A coordinate that no source supplies SHALL cause the command to fail loud with an error naming the unresolved coordinate(s), and the CLI SHALL NEVER silently consume a file argument as an identity coordinate.

#### Scenario: [INGEST-032.01] Named-switch push maps each file to exactly one layer

- **WHEN** an Artifact_Producer runs `cairn push --org-id O --tenant-id T --project-id P --assessment-period 2026-q2 FILE1 FILE2`
- **THEN** the Provenance_System assembles a single bundle with one content layer per supplied file (two layers), binds `org_id=O`, `tenant_id=T`, `project_id=P` and the assessment period to `2026-q2`, and applies the standard `ArtifactLocator` repo authority and push-by-digest semantics; the resulting bundle is independent of file order

#### Scenario: [INGEST-032.02] Missing required option fails loud

- **WHEN** an identity coordinate (for example `{project-id}`) is supplied by no source — no `--project-id`/`--target` argument, no `CAIRN_PROJECT_ID` environment variable, and no configured default
- **THEN** the command exits non-zero with an error naming the unresolved coordinate, and does not consume any file token as an identity coordinate

#### Scenario: [INGEST-032.03] No optional input is positionally interleaved with files

- **WHEN** an Artifact_Producer supplies only the required identity options and one or more files (e.g. `cairn push --org-id O --tenant-id T --project-id P report.json evidence.pdf`)
- **THEN** every supplied token after the options is treated as a file, no file token is reinterpreted as an assessment period or any other metadata field, and the layer count equals the number of supplied files

#### Scenario: [INGEST-032.04] Producer command is self-documenting

- **WHEN** an Artifact_Producer runs a producer-facing command with `--help` (e.g. `cairn push --help`)
- **THEN** the output includes a description of the command, its options, and at least one concrete usage example

#### Scenario: [INGEST-032.05] A single fully-qualified target populates all three identity coordinates

- **WHEN** an Artifact_Producer runs `cairn push --target O/T/P report.json`, supplying a single fully-qualified target in place of the three separate identity options
- **THEN** the Provenance_System populates the claim's identity tuple with `org_id=O`, `tenant_id=T`, and `project_id=P` from that target, with the result identical to supplying `--org-id O --tenant-id T --project-id P`

#### Scenario: [INGEST-032.06] Identity coordinates resolve from environment variables

- **WHEN** an Artifact_Producer (for example a CI pipeline) sets `CAIRN_ORG_ID=O`, `CAIRN_TENANT_ID=T`, and `CAIRN_PROJECT_ID=P` and runs `cairn push report.json` with no identity options
- **THEN** the CLI resolves `org_id=O`, `tenant_id=T`, and `project_id=P` from the environment, with the result identical to passing the three options explicitly

#### Scenario: [INGEST-032.07] A configured default target needs no per-command flags, and explicit arguments override it

- **WHEN** an Artifact_Producer has persisted `{org-id}/{tenant-id}/{project-id}` via `cairn config set-target` and runs `cairn push report.json` with no identity options, then separately runs `cairn push --project-id P2 report.json`
- **THEN** the first invocation resolves all three coordinates from the persisted configuration, and the second overrides only `{project-id}` with `P2` while resolving `{org-id}` and `{tenant-id}` from configuration — explicit arguments taking precedence over the environment, which takes precedence over configuration

### Requirement: [INGEST-018] Uniform ECR Repository Path Grammar

THE Provenance_System SHALL organize ECR repositories using a single uniform path-based naming convention with exactly one path shape:

```
{appName}-artifact-{regionCode}-{stage}/{org-id}/{tenant-id}/{project-id}
```

where `{regionCode}-{stage}` is the Deployment_Qualifier and `{org-id}`, `{tenant-id}`, and `{project-id}` are the three mandatory logical-hierarchy segments. No segment is optional, omitted, or conditioned on a deployment mode: there is exactly one path shape and nothing branches per segment.

Each of the three segments SHALL be a stable, **opaque**, immutable, allocate-once identifier — a lowercase alphanumeric string of 1–64 characters with single internal hyphens allowed and no leading or trailing hyphen. A segment SHALL carry no human-meaningful display name: the display name for an org, tenant, or project lives only in the control-plane identity registry (owned by the metadata-index capability), keyed by the opaque id, and SHALL NOT appear in the repository path.

`{tenant-id}` is a **logical tenant**, NOT an AWS account; `{org-id}` groups tenants and is a mandatory prefix on every path. The **producing identity** — the AWS account, other cloud, or on-prem host a bundle is pushed from — SHALL NOT be a path segment: a single workload MAY push to the same `{project-id}` from many AWS accounts, another cloud, or on-prem (colocation), and each artifact records its own producing principal through the signed claim, the CloudTrail push principal, and the signature (see processing-pipeline) rather than through the repository path.

A single repository SHALL hold every artifact-kind for a project; the artifact-kind is carried in the image tag, not the repository path.

#### Scenario: [INGEST-018.01] Uniform repository path constructed correctly

- **WHEN** a repository is provisioned for an `{org-id}`, `{tenant-id}`, and `{project-id}` that each satisfy the segment format constraints
- **THEN** the Provenance_System creates a single repository at the path `{appName}-artifact-{regionCode}-{stage}/{org-id}/{tenant-id}/{project-id}` with no artifact-kind segment, and that repository holds bundles of every artifact-kind for the project

#### Scenario: [INGEST-018.02] Repository locator construction property

- **WHEN** the `ArtifactLocator` construction function is invoked with any valid `{org-id}`, `{tenant-id}`, and `{project-id}`
- **THEN** it produces the single repository path `{appName}-artifact-{regionCode}-{stage}/{org-id}/{tenant-id}/{project-id}`, and it rejects inputs where any of `{org-id}`, `{tenant-id}`, or `{project-id}` violates its segment format constraints

#### Scenario: [INGEST-018.03] Producing identity is not a path segment (colocation)

- **WHEN** one workload pushes bundles to the same `{org-id}/{tenant-id}/{project-id}` from two different producing identities — for example two distinct AWS accounts, or an on-prem host and a non-AWS cloud
- **THEN** both bundles land in the identical repository path, because the producing identity is recorded per-artifact (signed claim, CloudTrail push principal, signature) and is never encoded as a path segment

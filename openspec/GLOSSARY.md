# Cairn Glossary

Canonical vocabulary for Cairn Proofs (the system concept: the Provenance_System).

**Authority model (see [ADR-0003](../docs/adr/0003-glossary-authority-model.md)).** This glossary is authoritative for the *nominal concept* of each term — the one-sentence "what is this thing." It is **not** authoritative for *normative constraints* (enums, formats, structures, behaviour): those live in `openspec/specs/` and are reached via the **Governed by** field on each entry. When a spec and this glossary disagree on a constraint, the spec wins. For the handful of **external** terms there is no in-repo authority, so the entry links to the owning standard. Two terms are *used-only* — specs use them but define no concept — and for those this glossary's nominal definition stands alone.

**Reading an entry.** Each term is a heading, followed by its nominal definition, then labeled fields. The first field is the authority: **Governed by** (a spec requirement is the normative source), **Authority** (this glossary or an ADR is the source — self-evident, used-only, or architecture terms), or **Standard** (external terms). Optional fields — **Constrained by**, **Carried as**, **Media type**, **Implemented in**, **Status**, **See also** — add detail.

> **Pointers verified 2026-06-21.** Each **Governed by** field names the requirement by its verbatim heading (`### Requirement: <name>`); the `Req N.N` identifier is the `Source:` number inside that requirement, where one exists. Maintained per the `rules.specs` upkeep rule in `openspec/config.yaml`: a change that introduces a new domain term adds its entry in the same delta.

---

## Domain Terms

Cairn-specific concepts, written `Capitalized_Underscore` — except **control plane** and **data plane**, two general-architecture terms Cairn adopts in their conventional lowercase spelling but with a Cairn-specific meaning fixed by ADR-0019 (see their entries below).

### Cairn

The maker name, carried by the CLI (`cairn`), the `cairn_*` Python distributions, and the `CAIRN_*` env prefix; the product brand is **Cairn Proofs**, and the system concept is the Provenance_System.

- **Authority** — this glossary (self-evident; no governing spec)

### Provenance_System

The overall system: the ECR operational layer, S3 WORM archive, DynamoDB Metadata_Index, and the EventBridge/Lambda pipeline.

- **Governed by** — [Customer_Hosted deployment mode](specs/deployment-modes/spec.md#requirement-deploy-001-customer_hosted-deployment-mode) ([DEPLOY-001])

### control plane

The **Operations** axis of the actor model: the operator that deploys, configures, and runs the Provenance_System substrate. Cairn's deliberate twist on the conventional control/data split is that the control plane keeps the data plane available but is **untrusted for integrity** — full operational reach cannot forge or tamper with an artifact undetectably, because integrity is anchored in the data plane, not conferred by the control plane.

- **Authority** — [ADR-0019](../docs/adr/0019-actor-model-four-axes-integrity-independent-of-operator.md)

### data plane

The **Production**, **Custody**, and **Consumption** axes of the actor model: who creates an artifact, who holds it, and who reads it (the artifact is the payload). Integrity is anchored **in** the data plane — the Manifest_Digest, signature, and WORM_Receipt — independent of whoever operates the control plane.

- **Authority** — [ADR-0019](../docs/adr/0019-actor-model-four-axes-integrity-independent-of-operator.md)

### Operator

The **Operations** axis principal: the least-privilege role that deploys, configures, and runs the Provenance_System substrate (in-house team, MSP, SI, or vendor-managed). Untrusted for integrity by construction — full operational reach cannot forge or tamper undetectably; it does not auto-carry the alias-mover grant, and its delete/DoS/read blast radius is bounded (WORM backstops delete).

- **Governed by** — [Least-Privilege Operator Principal](specs/access-control/spec.md#requirement-access-006-least-privilege-operator-principal) ([ACCESS-006])
- **See also** — [control plane](#control-plane)

### Custody

The actor-model axis of artifact ownership: the customer's own AWS account that holds and preserves artifacts, bounded by WORM even against itself. Custody is fixed in the customer's account and never moves to vendor-owned infrastructure; it maps to account ownership, not a persona.

- **Governed by** — [Customer_Hosted deployment mode](specs/deployment-modes/spec.md#requirement-deploy-001-customer_hosted-deployment-mode) ([DEPLOY-001])
- **See also** — [ADR-0019](../docs/adr/0019-actor-model-four-axes-integrity-independent-of-operator.md) (§3)

### Artifact_Bundle

The content-bearing Artifact (Artifact_Type `evidence` or `reference`) and the unit of ingestion: related files / Content_Layers bundled as one OCI artifact (manifest + Config_Blob + 1–300 Content_Layers). A bundle contains files (as Content_Layers), never "artifacts".

- **Governed by** — [Artifact Taxonomy Is Strictly Governed](specs/artifact-ingestion/spec.md#requirement-ingest-001-artifact-taxonomy-is-strictly-governed), [Ingestion Interfaces](specs/artifact-ingestion/spec.md#requirement-ingest-002-ingestion-interfaces) ([INGEST-002])

### Content_Layer

The unit of stored content inside an Artifact_Bundle: exactly one producer-supplied **File** per layer (1:1 — never combined, never split), carrying that file's own SHA-256 digest and `org.opencontainers.image.title` annotation. (A *File* is the producer-side object a Content_Layer stores; the two terms are 1:1 by contract.) A Content_Layer is content WITHIN a bundle — it is NOT itself an Artifact and carries no Artifact_Type.

- **Governed by** — [Ingestion Interfaces](specs/artifact-ingestion/spec.md#requirement-ingest-002-ingestion-interfaces) ([INGEST-002], the 1:1 file-to-layer contract), [Artifact Taxonomy Is Strictly Governed](specs/artifact-ingestion/spec.md#requirement-ingest-001-artifact-taxonomy-is-strictly-governed)

### Config_Blob

The JSON config object in an Artifact_Bundle carrying **content-descriptive** bundle-level metadata only (the producer's compliance claim lives in the Producer_Attestation_Referrer).

- **Governed by** — [Config_Blob Media Type and Content-Descriptive Fields](specs/artifact-ingestion/spec.md#requirement-ingest-016-config_blob-media-type-and-content-descriptive-fields) ([INGEST-016])
- **Media type** — `application/vnd.cairnproofs.artifact.config.v1+json`

### Manifest_Digest

The SHA-256 content-addressable digest of a bundle's OCI manifest, computed over content bytes only (pure content addressing — no producer claim, no synthetic/ambient state); the canonical, reproducible bundle identifier.

- **Governed by** — [Manifest Digest Computation](specs/artifact-ingestion/spec.md#requirement-ingest-006-manifest-digest-computation) ([INGEST-006])

### Producer_Attestation_Referrer

The signed OCI referrer carrying the producer's compliance claim (`projectId`, `artifactKind`, `controls`, `catalogOrProfileUuid`, `assessmentPeriod`, `assessor`, `assessmentId`, `supersedes`); its `subject` is the content Manifest_Digest, it has its own digest, and it is independently signed. Its push is the submission-complete validation trigger; many may share one content subject ("one content, many attestations").

- **Governed by** — [Producer Claim Carried as Signed Attestation Referrer](specs/artifact-ingestion/spec.md#requirement-ingest-005-producer-claim-carried-as-signed-attestation-referrer)
- **Media type** — `application/vnd.cairnproofs.producer-attestation.v1+json`

### Processing_Pipeline

The EventBridge-driven Lambda sequence validate → sign → archive → update-metadata → mint-tag.

- **Governed by** — [processing-pipeline](specs/processing-pipeline/spec.md) (whole capability; entry requirement [ECR PUSH event triggers validation stage](specs/processing-pipeline/spec.md#requirement-pipeline-001-ecr-push-event-triggers-validation-stage), [PIPELINE-001])

### Metadata_Index

The DynamoDB table indexing bundles for query (by project, control, date, status).

- **Governed by** — [Queryable artifact index by core dimensions](specs/metadata-index/spec.md#requirement-meta-001-queryable-artifact-index-by-core-dimensions)

### Identity_Registry

The control-plane DynamoDB table mapping an opaque id (`{org-id}`/`{tenant-id}`/`{project-id}`) to its display name and recording tenant→org parentage. Canonical mutable control-plane state, distinct from the rebuildable Metadata_Index — losing it costs display names, never identity or verifiability; display-name resolution is a read-time join by opaque id.

- **Governed by** — [Control-plane identity registry for opaque-id display-name resolution](specs/identity-registry/spec.md#requirement-idreg-001-control-plane-identity-registry-for-opaque-id-display-name-resolution)

### Signing_Profile

An AWS Signer resource defining signing parameters; Cairn uses `{appName}_artifact_pipeline_signing` and `{appName}_artifact_reviewer_signoff` (each suffixed with the Deployment_Qualifier; AWS Signer names are underscore-only).

- **Governed by** — [Pipeline Signing via Notation and AWS Signer](specs/signing/spec.md#requirement-sign-001-pipeline-signing-via-notation-and-aws-signer) ([SIGN-001])
- **See also** — also an external AWS Signer concept (see External Standards & Tools)

### WORM_Receipt

The JSON record of S3 archive location, timestamp, blob count, and integrity result, attached as an OCI Referrer.

- **Governed by** — [WORM_Receipt generation and OCI Referrer attachment](specs/worm-archive/spec.md#requirement-worm-005-worm_receipt-generation-and-oci-referrer-attachment) ([WORM-005])

### WORM_Enabled

Per-deployment boolean: `true` → S3 Object Lock Compliance + per-object retention headers; `false` → versioning only (non-production). Irreversible after provisioning.

- **Governed by** — [Archive workflow and conditional Object Lock application](specs/worm-archive/spec.md#requirement-worm-001-archive-workflow-and-conditional-object-lock-application) ([WORM-001])

### TimestampProvider

The signing-timestamp strategy configured in SSM: `system-clock` (built) or `rfc3161` (deferred — backlog `rfc3161-tsa-and-lta`).

- **Governed by** — [TimestampProvider Strategy](specs/signing/spec.md#requirement-sign-009-timestampprovider-strategy) ([SIGN-009])

### Artifact_Producer

Any principal that produces and pushes Artifact_Bundles (CI/CD pipelines, scanners, AI agents, human operators).

- **Authority** — this glossary (used-only — used by specs, defined by none)
- **Constrained by** — [access-control](specs/access-control/spec.md), [deployment-modes](specs/deployment-modes/spec.md)

### Artifact

The genus over everything in the store: any digest/manifest-addressed OCI object the Provenance_System holds — an Artifact_Bundle, a referrer (Signature / WORM_Receipt / Producer_Attestation_Referrer), or an oci-index — classified by Artifact_Type. A single *Content_Layer* ≈ an OSCAL `resource`; an *Artifact_Bundle* ≈ a set of them. "Evidence" is a reference *role* (OSCAL `relevant-evidence`, conferred by an observation citing the artifact), **not** an intrinsic kind — an artifact becomes evidence by being cited, so the store holds more than evidence.

- **Governed by** — [Artifact Taxonomy Is Strictly Governed](specs/artifact-ingestion/spec.md#requirement-ingest-001-artifact-taxonomy-is-strictly-governed)
- **See also** — [ADR-0023](../docs/adr/0023-artifact-genus-evidence-as-role.md) (nominal framing)

### Artifact_Kind

The producer-supplied controlled vocabulary classifying an artifact's type (values listed under *Literal Values & Enumerations*).

- **Governed by** — [Reject Invalid Artifact_Kind](specs/artifact-ingestion/spec.md#requirement-ingest-029-reject-invalid-artifact_kind) ([INGEST-029])

### Artifact_Type

The manifest-identity axis of an Artifact: `evidence | reference | signature | worm-receipt | oci-index`, discriminated physically by `config.mediaType`.

- **Governed by** — [Artifact Taxonomy Is Strictly Governed](specs/artifact-ingestion/spec.md#requirement-ingest-001-artifact-taxonomy-is-strictly-governed)
- **See also** — [ADR-0023](../docs/adr/0023-artifact-genus-evidence-as-role.md) (nominal framing)

### Assessment_Period

The time range during which evidence was collected for a control assessment.

- **Authority** — this glossary (used-only, nominal)
- **Carried as** — the `assessmentPeriod` claim field of the Producer_Attestation_Referrer; [Producer Claim Carried as Signed Attestation Referrer](specs/artifact-ingestion/spec.md#requirement-ingest-005-producer-claim-carried-as-signed-attestation-referrer)
- **See also** — *assessment-period* (Literal Values & Enumerations) for the wire format and its open validation gap

### Tenant

The logical isolation unit grouping a customer's projects: an immutable, opaque, allocate-once `{tenant-id}` path segment and the DynamoDB partition-key isolation unit — NOT an AWS account. Its display name lives in the Identity_Registry, never in the path, the signed claim, or an ECR resource tag.

- **Governed by** — [Producer Scoping by Project Within a Tenant](specs/access-control/spec.md#requirement-access-002-producer-scoping-by-project-within-a-tenant) ([ACCESS-002]), [Uniform ECR Repository Path Grammar](specs/artifact-ingestion/spec.md#requirement-ingest-018-uniform-ecr-repository-path-grammar) ([INGEST-018])

### Org

The mandatory top-level grouping of tenants: an immutable, opaque, allocate-once `{org-id}` path segment present on every path. Every Tenant belongs to exactly one Org. The org-grouping feature (org-scoped IAM roles, org-spanning queries) is deferred; the segment and per-tenant org assignment are not.

- **Governed by** — [Uniform ECR Repository Path Grammar](specs/artifact-ingestion/spec.md#requirement-ingest-018-uniform-ecr-repository-path-grammar) ([INGEST-018])
- **Status** — segment present; org-grouping feature deferred (backlog `org-level-isolation-tier`)

### Customer_Hosted

The deployment topology: the Provenance_System's data plane lives in the customer's own AWS account (custody is fixed there). Any operator — in-house, MSP, SI, or vendor-managed — may operate it by deploying into the customer's account without changing the trust model ("vendor-operated, customer-custodied").

- **Governed by** — [Customer_Hosted deployment mode](specs/deployment-modes/spec.md#requirement-deploy-001-customer_hosted-deployment-mode) ([DEPLOY-001])

### Customer_CMK

A customer-managed AWS KMS key used for encryption at rest, residing in the customer's own (custody) account — no cross-account grant (custody is fixed in that account).

- **Status** — Deferred; not yet built; requirements live in backlog `customer-cmk-support`

### Deployment_Qualifier

The immutable `{regionCode}-{stage}` suffix applied across all Cairn resource names.

- **Governed by** — [Region pinning at Cairn initialization](specs/deployment-modes/spec.md#requirement-deploy-002-region-pinning-at-cairn-initialization) ([DEPLOY-002])

### Derived_Identity

An identifier Cairn derives for a canonical artifact that carries none of its own, as `uuid5(CAIRN_NAMESPACE, <canonical prefixed Manifest_Digest>)`; a pure, rebuildable function of the digest, distinct from any borne identity in producer content.

- **Governed by** — [Deterministic derived identity over the canonical Manifest_Digest](specs/metadata-index/spec.md#requirement-meta-012-deterministic-derived-identity-over-the-canonical-manifest_digest)

### Artifact_TUI

The read-only terminal UI for listing bundles, controls, and pipeline health via the Metadata_Index.

- **Governed by** — [Bundle listing via sanctioned access paths](specs/artifact-tui/spec.md#requirement-tui-002-bundle-listing-via-sanctioned-access-paths)

### LTA

Long-Term Archival — the periodic re-timestamping pattern that keeps signatures verifiable beyond signing-certificate validity.

- **Status** — Deferred; not yet built; requirements live in backlog `rfc3161-tsa-and-lta`

## External Standards & Tools

No in-repo authority — each links to the owning standard; the gloss here is local convenience.

### OSCAL

NIST Open Security Controls Assessment Language (machine-readable GRC).

- **Standard** — NIST OSCAL

### OCI artifact

Open Container Initiative artifact: a manifest plus content-addressable layers. (The old glossary's `OCI_Artifact` underscore form does not appear in the specs.)

- **Standard** — OCI Image Spec

### ORAS

OCI Registry As Storage tooling for pushing/pulling arbitrary artifacts.

- **Standard** — oras.land

### Notation

CNCF/Notary-Project signing tool for OCI artifacts; used with the AWS Signer plugin.

- **Standard** — Notary Project

### Referrer

An OCI artifact linked to a subject artifact via the OCI Referrers API.

- **Standard** — OCI Image Spec (Referrers)

### TSA

Time-Stamping Authority; issues RFC 3161 TimeStampTokens.

- **Standard** — RFC 3161 / RFC 5816

### TST

TimeStampToken: a CMS `SignedData` structure from a TSA.

- **Standard** — RFC 3161

### AWS_Account_ID

The 12-digit AWS account number that owns AWS resources; distinct from `Account_ID`.

- **Standard** — AWS IAM

### WORM

Write Once Read Many (the storage property). In Cairn it is realized *optionally*, gated by `WORM_Enabled`.

- **Standard** — S3 Object Lock
- **See also** — WORM_Enabled (Domain Terms)

## Literal Values & Enumerations

Lowercase string values as they appear on the wire (tags, annotations, paths, SSM). Distinct by convention from the `Capitalized_Underscore` domain concepts above — this is the documented distinction, not drift.

### Artifact kinds

The `Artifact_Kind` vocabulary: the canonical value set plus its normalize-on-input aliases are defined authoritatively by [INGEST-029] — not re-enumerated here (single source of truth).

- **Governed by** — [Reject Invalid Artifact_Kind](specs/artifact-ingestion/spec.md#requirement-ingest-029-reject-invalid-artifact_kind) ([INGEST-029])
- **Implemented in** — `libs/oci/cairn_oci/categories.py`

### assessment-period

The wire form of Assessment_Period: the `assessmentPeriod` field of the Producer_Attestation_Referrer claim — producer-supplied, never derived, and optional at the validation gate. It appears only in the claim, never in tags, annotations, or paths.

- **Governed by** — [Producer Claim Carried as Signed Attestation Referrer](specs/artifact-ingestion/spec.md#requirement-ingest-005-producer-claim-carried-as-signed-attestation-referrer) (governs *carriage* — that the period rides in the referrer claim and is never tag-encoded; cf. [ECR Tag Format](specs/artifact-ingestion/spec.md#requirement-ingest-021-ecr-tag-format))
- **Format** — `YYYY-MM-DD` | `YYYY-qN` (N=1..4) | `YYYY`. This grammar is realized only in the `cairn push` CLI (`cli/cairn_cli/push.py`) and `libs/oci/cairn_oci/periods.py`; **no spec requirement defines or validates it** — the open period-format validation gap. Per ADR-0003 the glossary records the observed wire form but is not its authority.

### timestamp-provider values

`system-clock`, `rfc3161` (SSM `/{appName}/{stage}/signing/timestamp-provider`).

- **Governed by** — [TimestampProvider Strategy](specs/signing/spec.md#requirement-sign-009-timestampprovider-strategy) ([SIGN-009])

### RESTORE_IN_PROGRESS

Retrieval/metadata status returned while an S3 Glacier restore is in progress.

- **Status** — Deferred; not yet built; requirements live in backlog `worm-restore-tooling`

# worm-archive Specification

## Purpose
S3 archival of every successfully signed Artifact_Bundle. This spec defines the canonical S3 key structure, integrity verification on archive, the `WORM_Receipt` referrer attachment, and the `WORM_Enabled` per-deployment opt-in (restoration tooling is deferred). The archive workflow runs identically regardless of `WORM_Enabled`; only the S3 bucket's Object Lock Compliance mode and per-object retention headers differ. `WORM_Enabled=true` is the production posture; `WORM_Enabled=false` is for non-production deployments only.

*Domain terms (`Capitalized_Underscore`) used below are defined in [GLOSSARY.md](../../GLOSSARY.md), which names the governing requirement for each.*
## Requirements
### Requirement: [WORM-001] Archive workflow and conditional Object Lock application

The Processing_Pipeline SHALL create a copy in S3 of every successfully signed Artifact_Bundle under the canonical key structure, with S3 bucket versioning enabled. **WORM_Enabled** is a per-deployment boolean — `true` applies S3 Object Lock Compliance plus per-object retention headers, `false` is versioning-only (non-production); it is irreversible after provisioning. The archive workflow itself is identical regardless of the deployment's `WORM_Enabled` setting; only the bucket configuration and per-object headers differ.

WHILE the deployment operates with `WORM_Enabled=true`, the S3 bucket SHALL be configured with S3 Object Lock in Compliance mode, and each archived object SHALL be written with `ObjectLockMode=COMPLIANCE` and `ObjectLockRetainUntilDate` headers computed from the deployment's configured retention period. WHILE the deployment operates with `WORM_Enabled=false`, no Object Lock configuration or per-object retention headers SHALL be applied; bucket versioning SHALL remain enabled regardless.

#### Scenario: [WORM-001.01] Successful signed bundle archived with versioning regardless of WORM_Enabled
- **WHEN** an Artifact_Bundle is successfully signed in any deployment
- **THEN** the Processing_Pipeline writes a copy of the OCI manifest, Config_Blob, all content layers, and the signature artifact(s) to S3 under the canonical key structure with bucket versioning enabled

#### Scenario: [WORM-001.02] Object Lock headers applied when WORM_Enabled is true
- **WHEN** a signed Artifact_Bundle is archived in a deployment provisioned with `WORM_Enabled=true`
- **THEN** every PutObject for the bundle's artifacts includes `ObjectLockMode=COMPLIANCE` and `ObjectLockRetainUntilDate` derived from the deployment's configured retention period

#### Scenario: [WORM-001.03] No Object Lock applied when WORM_Enabled is false
- **WHEN** a signed Artifact_Bundle is archived in a deployment provisioned with `WORM_Enabled=false`
- **THEN** the archive copy is written without Object Lock headers and is mutable by authorized administrators, while bucket versioning remains in effect

### Requirement: [WORM-002] Canonical S3 archive key structure

The WORM copy SHALL be stored in the S3 bucket `{appName}-artifact-archive-{regionCode}-{stage}`. Archived objects SHALL be organized under the single key prefix `{org-id}/{tenant-id}/{project-id}/{manifest-digest}/`, composed of:

1. `{org-id}` — the mandatory organization segment;
2. `{tenant-id}` — the mandatory tenant segment;
3. `{project-id}` — the mandatory project segment;
4. `{manifest-digest}` — the content-addressed identity anchor.

The key prefix SHALL have exactly this shape: each of the three logical segments SHALL be present, and no segment SHALL be optional, omitted, or conditioned on deployment topology. The terminal `{manifest-digest}` segment SHALL be the key's sole identity anchor: distinct bundle content has a distinct manifest digest, so no two bundles collide regardless of `{org-id}`, `{tenant-id}`, or `{project-id}`. The archived set SHALL include the OCI manifest, the Config_Blob, all content layers (blobs), and all signature artifact(s) for the bundle.

The `WORM_Receipt` `s3KeyPrefix` value and any predicted S3 URI surfaced upstream (e.g. the validate stage's OSCAL artifact reference) SHALL use the same `{org-id}/{tenant-id}/{project-id}/{manifest-digest}/` shape, and all consumers SHALL treat it as an opaque string (never parsed back into segments).

#### Scenario: [WORM-002.01] Uniform key prefix construction
- **WHEN** archiving an Artifact_Bundle with valid org-id, tenant-id, project-id, and manifest-digest
- **THEN** the S3 key prefix construction function produces `{org-id}/{tenant-id}/{project-id}/{manifest-digest}/` exactly

#### Scenario: [WORM-002.02] All three identity segments are mandatory
- **WHEN** the key construction function is invoked with any of org-id, tenant-id, or project-id missing or empty
- **THEN** construction fails rather than producing a key with an omitted, defaulted, or fabricated segment, so every archived key carries the full `{org-id}/{tenant-id}/{project-id}/{manifest-digest}/` shape

#### Scenario: [WORM-002.04] Predicted S3 URI matches the archived key shape
- **WHEN** the validate stage predicts the S3 URI for an Artifact_Bundle and the archive stage later writes that bundle
- **THEN** the validate-predicted key prefix equals the archive-produced key prefix character-for-character for the same org-id, tenant-id, project-id, and manifest-digest, both in the `{org-id}/{tenant-id}/{project-id}/{manifest-digest}/` shape

#### Scenario: [WORM-002.05] S3 WORM key structure construction (for any valid inputs)
- **WHEN** the key construction function is invoked for any valid org-id, tenant-id, project-id, and manifest-digest
- **THEN** the produced key prefix matches the canonical `{org-id}/{tenant-id}/{project-id}/{manifest-digest}/` form character-for-character, and the produced object set covers the OCI manifest, Config_Blob, all content layer blobs, and the signature artifact(s)

### Requirement: [WORM-003] SHA-256 digest verification on archive

WHEN archiving an Artifact_Bundle, THE Processing_Pipeline SHALL verify the SHA-256 digest of each blob downloaded from ECR against the digest recorded in the OCI manifest, and SHALL write the blob to S3 only after digest verification succeeds.

#### Scenario: [WORM-003.01] Every blob digest verified before write
- **WHEN** the Processing_Pipeline downloads each blob referenced by the OCI manifest for an Artifact_Bundle
- **THEN** the pipeline computes the SHA-256 digest of the downloaded content and compares it byte-for-byte to the manifest digest before the blob is written to S3

#### Scenario: [WORM-003.02] Digest verification correctness (for any blob and expected digest)
- **WHEN** the digest verification function is invoked with any blob content and any expected digest
- **THEN** it returns `true` if and only if the computed SHA-256 of the blob equals the expected digest, and on failure the error includes both the computed digest and the expected digest

### Requirement: [WORM-004] Archive abort on digest mismatch

IF a SHA-256 digest mismatch is detected during archival, THEN THE Processing_Pipeline SHALL abort the archive operation for the affected bundle, SHALL emit an `artifact.archive-failed` event to EventBridge containing the mismatched digest and the expected digest, and SHALL NOT generate a WORM_Receipt for the bundle.

#### Scenario: [WORM-004.01] Mismatch aborts archive and emits failure event
- **WHEN** a digest computed during archival does not equal the digest recorded in the OCI manifest
- **THEN** the Processing_Pipeline aborts the archive operation, emits an `artifact.archive-failed` event to EventBridge containing the mismatched digest and the expected digest, and produces no WORM_Receipt

### Requirement: [WORM-005] WORM_Receipt generation and OCI Referrer attachment

WHEN archiving completes successfully, THE Processing_Pipeline SHALL generate a **WORM_Receipt** containing the S3 location, archive timestamp, blob count, and integrity verification result, and SHALL attach the receipt as an OCI Referrer to the original ECR artifact.

#### Scenario: [WORM-005.01] WORM_Receipt attached as OCI Referrer
- **WHEN** archive completes successfully for an Artifact_Bundle
- **THEN** the Processing_Pipeline writes a WORM_Receipt artifact recording the S3 location, the archive timestamp, the blob count, and the integrity verification result, and attaches it as an OCI Referrer to the original ECR artifact

### Requirement: [WORM-006] Artifact reference OCI Referrer attachment

WHEN archiving completes successfully for an Artifact_Bundle whose triggering `artifact.signed` event carries an `artifactResourceReference` — every bundle archived with an archive bucket configured at validate time — THE Processing_Pipeline SHALL attach that artifact reference (the reference pointing at the bundle, encoded per the owning interop module — see the oscal-interop *OSCAL encoding of the universal artifact reference* Requirement) as an OCI Referrer to the original ECR artifact — subject = the bundle Manifest_Digest — alongside the WORM_Receipt referrer.

The resource referrer's layer body SHALL be byte-identical to the `artifactResourceReference` carried on the event; THE Processing_Pipeline SHALL NOT re-derive the resource. The referrer manifest SHALL carry:

1. `artifactType = application/vnd.cairnproofs.artifact.resource.v1`;
2. a layer of media type `application/vnd.cairnproofs.artifact.resource.v1+json` holding the resource JSON;
3. a `config.mediaType` of `application/vnd.oci.empty.v1+json`, so the referrer's own `PutImage` is classified pipeline-internal (see the artifact-ingestion *Pipeline-Internal Artifact Filtering Before Bundle Validation* Requirement) and does not self-trigger the validate stage.

The attachment SHALL be idempotent: WHEN a resource referrer already exists for the subject (identified by `artifactType` via the Referrers API), THE Processing_Pipeline SHALL NOT mint a second one and SHALL proceed without error.

The only Artifact_Bundle that SHALL NOT carry a resource referrer is one with no artifact reference at all — archived when no archive bucket was configured at validate time, so no reference was generated. Every other archived bundle carries a resource referrer; its resource identity derives from `uuidv5(namespace, manifest_digest)`, with an OSCAL root `uuid` recorded as provenance only when an OSCAL layer is present.

#### Scenario: [WORM-006.01] Artifact reference attached as OCI Referrer on successful archive
- **WHEN** archive completes successfully for any Artifact_Bundle whose `artifact.signed` event carries an `artifactResourceReference`
- **THEN** the Processing_Pipeline attaches a resource referrer to the original ECR artifact with subject = the bundle Manifest_Digest and `artifactType` `application/vnd.cairnproofs.artifact.resource.v1`, discoverable via the OCI Referrers API alongside the signature and WORM_Receipt referrers

#### Scenario: [WORM-006.02] Resource referrer identity derives from the deterministic digest-uuid
- **WHEN** archive completes successfully for an Artifact_Bundle (for example a PDF, a scan output, or a markdown governance doc) archived with a bucket configured, whose `artifact.signed` event carries an `artifactResourceReference`
- **THEN** the Processing_Pipeline attaches a resource referrer whose resource identity derives from `uuidv5(namespace, manifest_digest)` and whose `document-ids[0]` derives from the same deterministic uuid when no OSCAL root `uuid` is present

#### Scenario: [WORM-006.03] Resource referrer body is byte-identical to the event reference
- **WHEN** the resource referrer is minted from an `artifact.signed` event carrying `artifactResourceReference`
- **THEN** the referrer's resource-JSON layer body equals the `artifactResourceReference` value byte-for-byte, with no re-derivation, so the referrer-borne resource and the event/DynamoDB projection cannot diverge

#### Scenario: [WORM-006.04] Resource referrer is discriminated pipeline-internal and does not self-trigger
- **WHEN** the resource-referrer `PutImage` lands in ECR
- **THEN** its manifest carries `config.mediaType` `application/vnd.oci.empty.v1+json`, so the validate stage classifies it pipeline-internal and skips it without emitting an `artifact.validation-failed` event or advancing it to signing

#### Scenario: [WORM-006.05] Re-archive does not mint a duplicate resource referrer
- **WHEN** the same bundle is archived again and a resource referrer already exists for its Manifest_Digest
- **THEN** the Processing_Pipeline detects the existing referrer by `artifactType` and does not mint a second one, completing without error

#### Scenario: [WORM-006.06] Bundle archived with no bucket configured gets no resource referrer
- **WHEN** archive completes for an Artifact_Bundle whose `artifact.signed` event carries no `artifactResourceReference` because no archive bucket was configured at validate time and no reference was generated
- **THEN** the Processing_Pipeline attaches no resource referrer, while still attaching the WORM_Receipt referrer

### Requirement: [WORM-007] Self-contained independently-retrievable archive

THE WORM copy SHALL be independently retrievable without access to ECR. The archived object set SHALL contain the OCI manifest, the Config_Blob, all content layer blobs, the signature artifact(s), and a metadata snapshot sufficient to reconstruct the original Artifact_Bundle from S3 alone.

#### Scenario: [WORM-007.01] Archive reconstructable without ECR
- **WHEN** a consumer holds only S3 credentials for the archive bucket and the canonical key prefix for a bundle
- **THEN** they can retrieve the OCI manifest, Config_Blob, all content layer blobs, signature artifacts, and metadata snapshot — and reconstruct the original Artifact_Bundle — without any ECR access

### Requirement: [WORM-008] Deletion prohibition during retention

WHILE operating with `WORM_Enabled=true`, THE Provenance_System SHALL NOT delete any artifact from the WORM archive before that object's Object Lock retention period expires; this prohibition SHALL be enforced both by S3 Object Lock Compliance mode at the storage layer and by an in-system policy that refuses delete operations. WHILE operating with `WORM_Enabled=false`, archived evidence MAY be deleted by an authorized administrator; deployments with `WORM_Enabled=false` SHALL be non-production only.

#### Scenario: [WORM-008.01] Delete denied under WORM_Enabled before retention expiry
- **WHEN** any principal — including an account administrator — attempts to delete an archived object before its `ObjectLockRetainUntilDate` in a deployment with `WORM_Enabled=true`
- **THEN** S3 Object Lock Compliance mode denies the delete and the in-system policy independently denies it; the object remains intact

#### Scenario: [WORM-008.02] Admin delete permitted only in non-production WORM-disabled deployments
- **WHEN** an authorized administrator deletes an archived object in a deployment with `WORM_Enabled=false`
- **THEN** the deletion succeeds, and the deployment is required to be a non-production deployment per the spec

### Requirement: [WORM-009] Lifecycle tiering with configurable transition ages

THE Provenance_System SHALL apply S3 lifecycle policies that transition archived objects through storage tiers. The transition schedule, default ages, configurability, and bounds validation are owned by the retention-lifecycle *S3 lifecycle tiering with configurable transition ages* Requirement. The WORM-specific invariant this Requirement owns: WHILE operating with `WORM_Enabled=true`, lifecycle tier transitions SHALL NOT affect Object Lock retention or the `WORM_Receipt`; S3 natively enforces the Object Lock invariant.

#### Scenario: [WORM-009.01] Lifecycle transition does not affect Object Lock retention under WORM_Enabled
- **WHEN** an archived object held under Object Lock Compliance mode reaches a lifecycle transition age in a `WORM_Enabled=true` deployment
- **THEN** the object transitions storage class without any change to its `ObjectLockRetainUntilDate` and remains undeletable until that date

### Requirement: [WORM-010] WORM_Enabled provisioning irreversibility

The `WORM_Enabled` setting SHALL be established at deployment provisioning time and SHALL NOT be changed thereafter for the lifetime of the deployment. This irreversibility is intrinsic: S3 Object Lock MUST be enabled at bucket creation and cannot be added to an existing bucket, so flipping `WORM_Enabled` from `false` to `true` would require a new bucket and therefore a new deployment. Provisioning a deployment with `WORM_Enabled=true` SHALL require explicit operator confirmation that names Object Lock Compliance mode and the configured retention period in the confirmation prompt or input. Deployments provisioned with `WORM_Enabled=false` SHALL be non-production only; production deployments SHALL be provisioned with `WORM_Enabled=true`.

#### Scenario: [WORM-010.01] WORM_Enabled set at provisioning and locked
- **WHEN** a deployment has been provisioned with a given `WORM_Enabled` value
- **THEN** the value is recorded in the deployment configuration and any subsequent attempt to change it via configuration update, console, or CDK redeploy of the existing stack fails or is rejected as a no-op

#### Scenario: [WORM-010.02] Provisioning WORM_Enabled true requires explicit confirmation
- **WHEN** an operator initiates provisioning of a deployment with `WORM_Enabled=true`
- **THEN** the provisioning workflow requires explicit operator confirmation that names Object Lock Compliance mode and the configured retention period before bucket creation proceeds

#### Scenario: [WORM-010.03] WORM_Enabled false rejected for production
- **WHEN** an operator attempts to provision a production-classified deployment with `WORM_Enabled=false`
- **THEN** the provisioning workflow rejects the request, citing the spec requirement that production deployments use `WORM_Enabled=true`

#### Scenario: [WORM-010.04] Flipping WORM_Enabled requires a new deployment
- **WHEN** an operator wishes to add WORM protection to a deployment originally provisioned with `WORM_Enabled=false`
- **THEN** the operator must provision a new deployment with `WORM_Enabled=true` and migrate evidence into it; the existing bucket cannot have Object Lock added after creation

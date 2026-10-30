# signing Specification

## Purpose
Cryptographic signing of artifact bundles via Notation with AWS Signer. Signing is the bridge between received bytes and tamper-evident evidence. This capability defines:

1. The signing profile model — `{appName}_artifact_pipeline_signing` for automation, `{appName}_artifact_reviewer_signoff` for human attestation. Deployed AWS Signer profile names are each suffixed with the Deployment_Qualifier, underscore-joined (AWS Signer names are underscore-only), e.g. `cairnproofs4af_artifact_pipeline_signing_cmh_alpha`.
2. The strict Notation trust policy.
3. Signature storage as OCI Referrer artifacts.
4. Per-signature provenance tracking.
5. Signing key custody in KMS HSM.
6. The `TimestampProvider` strategy. `system-clock` is the supported strategy: the AWS Signer `signingTime` is accurate and tamper-resistant within the AWS trust boundary but does not constitute cryptographic third-party proof of existence independent of the signer. `rfc3161` is a recognized but unsupported value; RFC 3161 / RFC 5816 timestamping is deferred.

*Domain terms (`Capitalized_Underscore`) used below are defined in [GLOSSARY.md](../../GLOSSARY.md), which names the governing requirement for each.*

## Requirements
### Requirement: [SIGN-001] Pipeline Signing via Notation and AWS Signer

A **Signing_Profile** is an AWS Signer resource that defines signing parameters. WHEN an Artifact_Bundle passes validation, the Processing_Pipeline SHALL sign the bundle using Notation with the AWS Signer plugin and the `{appName}_artifact_pipeline_signing` Signing_Profile. The pipeline SHALL produce **two independent signatures**:

1. One over the content manifest (the pure content-identity manifest). Its `subject` is the content manifest digest.
2. One over the Producer_Attestation_Referrer (the producer-claim referrer whose `subject` is the content manifest digest, per artifact-ingestion). Its `subject` is the Producer_Attestation_Referrer's digest.

Each signature SHALL be stored as its own OCI signature referrer (per *Signature Stored as OCI Referrer*). The two signatures are independent: signing the content manifest SHALL NOT require re-signing the Producer_Attestation_Referrer, and signing the Producer_Attestation_Referrer SHALL NOT require re-signing the content manifest.

Signing is the gating step between successful validation and WORM archival; an unsigned bundle MUST NOT proceed to archival. A bundle is considered signed for archival only when both the content manifest and the Producer_Attestation_Referrer carry a valid pipeline signature.

Independent signatures enable "same content, multiple attestations": a new Producer_Attestation_Referrer over an already-signed content manifest is signed on its own without re-signing content. They also admit a future human co-sign (the Attesting Reviewer, via `{appName}_artifact_reviewer_signoff`, per *Multiple Signatures per Bundle*) over a claim without re-signing content.

#### Scenario: [SIGN-001.01] Validated bundle is signed by the pipeline profile

- **WHEN** the `artifact.validated` event is emitted for a bundle that has passed all validation checks
- **THEN** the Processing_Pipeline invokes Notation with the AWS Signer plugin using the `{appName}_artifact_pipeline_signing` Signing_Profile and produces one signature whose `subject` is the content manifest's Manifest_Digest and one signature whose `subject` is the Producer_Attestation_Referrer's digest, each stored as an independent OCI signature referrer

#### Scenario: [SIGN-001.02] Content and attestation signatures are produced independently

- **WHEN** the pipeline signs a validated bundle comprising a content manifest and its Producer_Attestation_Referrer
- **THEN** signing the content manifest does not require re-signing the Producer_Attestation_Referrer and signing the Producer_Attestation_Referrer does not require re-signing the content manifest, and each signature verifies independently against the trust policy (per *Strict Notation Trust Policy*)

#### Scenario: [SIGN-001.03] A new attestation over existing content is signed without re-signing content

- **WHEN** a new Producer_Attestation_Referrer is pushed against a content manifest whose content signature already exists and is valid
- **THEN** the pipeline signs only the new Producer_Attestation_Referrer, the existing content signature is left untouched and remains valid, and both signatures are independently verifiable from the canonical ECR artifact

### Requirement: [SIGN-002] Signature Stored as OCI Referrer

WHEN a signature is created, the Provenance_System SHALL store the signature as an OCI Referrer artifact linked to the signed manifest via the OCI Referrers API.

#### Scenario: [SIGN-002.01] Signature attached via Referrers API

- **WHEN** a pipeline or reviewer signature is created for an Artifact_Bundle
- **THEN** the signature is written to ECR as an OCI artifact whose `subject` field references the signed bundle's manifest digest, discoverable via the OCI Referrers API

### Requirement: [SIGN-003] Multiple Signatures per Bundle

The Provenance_System SHALL support at least 10 signatures on a single Artifact_Bundle, including automated pipeline signatures using the `{appName}_artifact_pipeline_signing` Signing_Profile and manual reviewer sign-off attestations using the `{appName}_artifact_reviewer_signoff` Signing_Profile. Both Signing_Profiles SHALL be provisioned and SHALL be admitted by the Notation trust policy for the appropriate repository scope (verification level remains `strict`, per *Strict Notation Trust Policy*). Each signature SHALL be stored as an independent OCI Referrer artifact on the bundle manifest and SHALL be independently verifiable from the canonical ECR artifact without consulting the Metadata_Index (per artifact-verifiability *Index-Independent Re-Verification*). A reviewer sign-off SHALL be creatable with stock Notation tooling against the `{appName}_artifact_reviewer_signoff` profile — it is just another OCI signature referrer, requiring no Metadata_Index write to be valid.

#### Scenario: [SIGN-003.01] Pipeline and reviewer signatures coexist on the same bundle

- **WHEN** a bundle has been signed by `{appName}_artifact_pipeline_signing` and one or more reviewer principals subsequently sign it using `{appName}_artifact_reviewer_signoff`
- **THEN** all signatures are stored as independent OCI Referrer artifacts on the same manifest, and the Provenance_System accepts up to at least 10 such signatures without error

#### Scenario: [SIGN-003.02] Reviewer sign-off created with stock Notation tooling

- **WHEN** an authorized principal performs a manual sign-off on an Artifact_Bundle using the `{appName}_artifact_reviewer_signoff` profile (via `cairn sign` or a bare `notation sign` against that profile)
- **THEN** a new Notation signature referrer is attached to the bundle's manifest digest, the trust policy admits it under `strict` verification, and the sign-off is independently verifiable from the ECR artifact alone

#### Scenario: [SIGN-003.03] Each coexisting signature verifies independently

- **WHEN** a bundle carries both a pipeline signature and one or more reviewer sign-offs and a verifier checks the bundle
- **THEN** each signature is verified independently against the trust policy, and the per-signature `signatureDetails` (signing *Signature Status Reporting on Retrieval*) reflects each profile's status without one profile's status altering another's

### Requirement: [SIGN-004] Strict Notation Trust Policy

The Provenance_System SHALL enforce a Notation trust policy that defines which Signing_Profiles and identities are authorized for each ECR repository scope, with signature verification level set to strict.

In strict mode, any verification failure (untrusted identity, expired certificate, revoked signing version, missing signature when required) MUST result in a verification failure rather than a warning.

#### Scenario: [SIGN-004.01] Signature from unauthorized profile is rejected under strict verification

- **WHEN** verification is performed against a bundle whose signature was produced by a Signing_Profile not authorized for the bundle's repository scope in the trust policy
- **THEN** verification fails under strict mode and the signature is not treated as valid

### Requirement: [SIGN-005] Signature Status Reporting on Retrieval

WHEN an artifact bundle is retrieved, the Provenance_System SHALL verify all associated signatures and return both an overall `signingStatus` field and a per-signature `signatureDetails` list in the retrieval response.

The overall `signingStatus` SHALL be one of:

1. `signed` — all signatures valid, at least one present.
2. `unsigned` — no signatures present.
3. `partial` — at least one signature valid and at least one revoked.
4. `revoked` — all present signatures revoked.

The `signatureDetails` list SHALL include, for each signature, the Signing_Profile ARN, profile name, individual status (`valid` or `revoked`), signing timestamp, and — when the signature was timestamped under the `rfc3161` TimestampProvider strategy — the TSA endpoint used. Under the `system-clock` strategy no external TSA is contacted and the TSA endpoint field is null/absent.

Revoking one profile's signature SHALL NOT change the status of signatures made by other profiles — each profile's status is tracked independently.

#### Scenario: [SIGN-005.01] All signatures valid yields signed

- **WHEN** a bundle is retrieved that has at least one signature present and every present signature verifies successfully
- **THEN** the response carries `signingStatus: signed` and `signatureDetails` enumerates each signature with `valid` status, its Signing_Profile ARN, profile name, signing timestamp, and the TSA endpoint used when timestamped under the `rfc3161` strategy (null/absent under `system-clock`)

#### Scenario: [SIGN-005.02] No signatures present yields unsigned

- **WHEN** a bundle is retrieved that has no signature referrers attached
- **THEN** the response carries `signingStatus: unsigned` and an empty `signatureDetails` list

#### Scenario: [SIGN-005.03] Mixed valid and revoked signatures yield partial

- **WHEN** a bundle is retrieved that has at least one signature that verifies successfully and at least one signature whose Signing_Profile version has been revoked
- **THEN** the response carries `signingStatus: partial`, and `signatureDetails` records the surviving signatures as `valid` and the revoked signatures as `revoked` with no change in status to the surviving entries

#### Scenario: [SIGN-005.04] All present signatures revoked yields revoked

- **WHEN** a bundle is retrieved that has one or more signatures and every present signature has been revoked
- **THEN** the response carries `signingStatus: revoked` and every entry in `signatureDetails` shows `revoked`

#### Scenario: [SIGN-005.05] Revoking one profile does not flip status of other profiles

- **WHEN** a bundle carries signatures from two distinct Signing_Profiles A and B, both currently valid, and profile A's signing version is revoked
- **THEN** only profile A's `signatureDetails` entry changes to `revoked`, profile B's entry remains `valid`, and the overall `signingStatus` is recomputed (yielding `partial`) without mutating profile B's recorded status

### Requirement: [SIGN-006] CloudTrail Logging of Signing and Verification

The Provenance_System SHALL log all signing and verification events to CloudTrail.

#### Scenario: [SIGN-006.01] Signing and verification calls produce CloudTrail records

- **WHEN** the pipeline performs a signing operation or a caller performs a verification operation against an Artifact_Bundle
- **THEN** CloudTrail records the corresponding AWS Signer and KMS API events with the calling principal, the Signing_Profile referenced, and the timestamp

### Requirement: [SIGN-007] Signing Key Custody in KMS HSM

The Provenance_System SHALL ensure that signing key material never leaves AWS KMS HSM boundaries.

Private key material backing every Signing_Profile MUST remain inside the FIPS-validated KMS HSM at all times; no export, copy, or off-HSM cryptographic operation is permitted.

#### Scenario: [SIGN-007.01] Signing operations execute inside KMS HSM

- **WHEN** the pipeline (or any caller) signs an Artifact_Bundle via AWS Signer
- **THEN** the cryptographic signing operation is performed inside the KMS HSM and no private key bytes are exposed to the Lambda execution environment, the Signer service host, or any other component

### Requirement: [SIGN-008] Signing Failure Handling

IF signing of an Artifact_Bundle fails after the maximum retry attempts defined by the Processing_Pipeline, THEN the Provenance_System SHALL mark the bundle as `signingStatus: unsigned` in the Metadata_Index, route the failure event to the dead letter queue, and retain the unsigned bundle in ECR without blocking subsequent pipeline stages for other bundles.

#### Scenario: [SIGN-008.01] Exhausted signing retries leave the bundle unsigned but preserved

- **WHEN** the `sign-bundle` Lambda exhausts all retry attempts for a given Artifact_Bundle without producing a signature
- **THEN** the Metadata_Index entry for that bundle is set to `signingStatus: unsigned`, the failure event is routed to the dead letter queue, the bundle remains present in ECR, and the pipeline continues processing unrelated bundles without blocking

### Requirement: [SIGN-009] TimestampProvider Strategy

The **TimestampProvider** is the signing-timestamp strategy configured in SSM — `system-clock` (supported) or `rfc3161` (deferred). WHEN the `sign-bundle` Lambda signs an Artifact_Bundle using Notation, the Provenance_System SHALL record a signing timestamp using the `TimestampProvider` strategy configured in SSM Parameter Store at `/{appName}/{stage}/signing/timestamp-provider`. The strategy is resolved from SSM at signing time, so the configured value can change without a Lambda code change or redeployment.

The supported strategy is `system-clock`: the signing timestamp is the `signingTime` field embedded by AWS Signer in the CMS signature envelope, sourced from the Lambda execution environment's clock synchronized via Amazon Time Sync Service (an NTP-based service accurate to within microseconds within the AWS network). This timestamp is accurate and tamper-resistant within the AWS trust boundary but does not constitute cryptographic third-party proof of existence independent of the signer. No external network call is required.

`rfc3161` is a recognized `TimestampProvider` value but is NOT supported. WHEN the configured value is `rfc3161` (or any value outside the supported set), the `sign-bundle` Lambda SHALL raise an `UnsupportedTimestampProvider` error rather than sign under a substituted strategy. The `rfc3161` provider is not supported — it would obtain and embed an RFC 3161 / RFC 5816 TimeStampToken (TST) as a countersignature, including the primary/secondary TSA endpoint configuration and dual-timestamp behavior.

The Provenance_System SHALL record the `timestampProvider` value used at signing time in the Metadata_Index `signatureDetails` entry for that bundle.

#### Scenario: [SIGN-009.01] system-clock strategy stamps via AWS Signer signingTime

- **WHEN** `/{appName}/{stage}/signing/timestamp-provider` is set to `system-clock` and the `sign-bundle` Lambda signs a bundle
- **THEN** the signature carries the AWS Signer `signingTime` field sourced from the Amazon Time Sync Service-synchronized Lambda clock, no external TSA call is made, and the Metadata_Index `signatureDetails` entry records `timestampProvider: system-clock`

#### Scenario: [SIGN-009.04] Strategy switch requires no code change or redeployment

- **WHEN** an operator updates the `/{appName}/{stage}/signing/timestamp-provider` SSM parameter value
- **THEN** the next signing resolves the strategy from the updated SSM value at runtime — the value is read per signing invocation, not baked into the deployment — so changing the configured strategy requires no Lambda code change or redeployment

#### Scenario: [SIGN-009.05] rfc3161 selection is rejected as unsupported

- **WHEN** `/{appName}/{stage}/signing/timestamp-provider` is set to `rfc3161` (or any value outside the supported set) and the `sign-bundle` Lambda resolves the strategy to sign a bundle
- **THEN** the Lambda raises `UnsupportedTimestampProvider` and does not sign under a substituted strategy, because the `rfc3161` provider is not supported

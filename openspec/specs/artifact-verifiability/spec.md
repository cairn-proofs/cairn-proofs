# artifact-verifiability Specification

## Purpose
Independent, index-free, byte-exact re-verification of stored evidence. A third party can re-derive any artifact's authenticity, integrity, and WORM-archival status from the canonical ECR artifact and its OCI referrers alone — with stock OCI + Notation tooling, against a published (or self-supplied) trust policy, and **without trusting or reaching the Metadata_Index** (a rebuildable projection, never the source of the verdict).

OSCAL is the headline case. The standing assumption that machine-readable structured content is trivially PKI-verifiable ignores that OSCAL defines no signature format and JSON has no canonical byte form. This capability pins the chain that makes it verifiable:

1. Content-address the bytes.
2. Sign the OCI Manifest_Digest, which Merkle-commits to every layer.
3. Anchor identity in a strict trust policy.
4. Keep the index off the verification trust path.

The guarantee is content-agnostic. Verification is **byte-exact** — over the exact stored bytes, never a canonicalized or re-serialized form. The discovery↔verification boundary is deliberate: finding *which* artifact backs a control uses the index; *verifying* that artifact does not. Trust-anchor, timestamp, rebuildability, and scale facts are owned by `signing` / `metadata-index` / `runtime-slos` and referenced here, not restated.

*Domain terms (`Capitalized_Underscore`) used below are defined in [GLOSSARY.md](../../GLOSSARY.md), which names the governing requirement for each.*

## Requirements

### Requirement: [VERIFY-001] Index-Independent Re-Verification

THE Provenance_System SHALL allow any party holding read access to the canonical ECR repository to independently re-establish an Artifact_Bundle's authenticity, integrity, and WORM-archival status using ONLY (a) the bundle's OCI manifest and layer bytes, (b) its OCI Referrer artifacts (Notation signature(s) and the `WORM_Receipt`), and (c) a Notation trust policy. The verification verdict SHALL NOT depend on, read from, or require reachability to the Metadata_Index. The `signingStatus` returned on the retrieval path (signing *Signature Status Reporting on Retrieval*) is a convenience projection of this verdict and is NEVER its source of truth.

This independence is the auditor-facing core of the system. A verdict that required querying an operator-controlled, mutable, rebuildable index would be a verdict the operator could forge, so the trust path is anchored on cryptography the verifier re-checks, not on a database row. An automated regression test SHALL enforce index-independence by asserting the verify code path constructs no DynamoDB client.

#### Scenario: [VERIFY-001.01] Verification issues no Metadata_Index calls

- **WHEN** a verifier runs `cairn verify` (or a bare `notation verify` against the same trust policy) on an Artifact_Bundle reference
- **THEN** the verdict is derived solely from the ECR artifact, its signature/`WORM_Receipt` referrers, and the trust policy, and zero DynamoDB Metadata_Index API calls are issued during verification

#### Scenario: [VERIFY-001.02] Verification survives a destroyed or unreachable index

- **WHEN** the Metadata_Index is unreachable or has been destroyed
- **THEN** a complete authenticity + integrity verdict is still obtainable by verifying the canonical ECR artifact directly, because the index is a rebuildable projection of ECR + S3 state (rebuild tooling deferred) and is not on the verification trust path

#### Scenario: [VERIFY-001.03] Discovery requires the index, verification does not

- **WHEN** an auditor needs to locate which artifacts back a given control before verifying them
- **THEN** that discovery query uses the Metadata_Index, while the subsequent verification of each located artifact requires only the ECR artifact + referrers + trust policy — the index is on the discovery path, never on the verification trust path

### Requirement: [VERIFY-002] Byte-Exact Verification

Evidence verification SHALL be performed over the exact content-addressed bytes as stored and SHALL NOT depend on any canonicalized or re-serialized form of the logical document (verification is byte-exact: it depends on no canonicalization scheme). The signed subject SHALL be the Artifact_Bundle's OCI Manifest_Digest, which Merkle-commits to every layer's SHA-256 digest; verification therefore depends on no JSON or OSCAL canonicalization scheme. Two byte-distinct but semantically-equivalent documents SHALL be treated as distinct artifacts with distinct Manifest_Digests and distinct verdicts; a signature over one SHALL NOT verify the other.

OSCAL is the headline motivating case: the standing assumption that machine-readable structured content is trivially PKI-verifiable ignores that OSCAL defines no signature format and JSON has no canonical byte form. The guarantee is content-agnostic and applies to every artifact the store holds.

#### Scenario: [VERIFY-002.01] Byte-distinct but semantically-equivalent documents are distinct artifacts

- **WHEN** two OSCAL documents that are semantically equivalent but differ in byte serialization (for example key ordering or whitespace) are each pushed
- **THEN** each receives its own Manifest_Digest and its own signature, and a signature over one never verifies the other

#### Scenario: [VERIFY-002.02] Verification is content-type agnostic

- **WHEN** an Artifact_Bundle mixes OSCAL and non-OSCAL layers, or contains no OSCAL layer at all
- **THEN** the verification verdict is computed identically — over the Manifest_Digest and its committed layer digests — because the cryptographic guarantee is independent of payload semantics

#### Scenario: [VERIFY-002.03] Manifest commits to all layers

- **WHEN** any single layer's bytes are altered after signing
- **THEN** the layer's recomputed SHA-256 no longer matches the manifest's layer descriptor, the Manifest_Digest changes, and verification against the original signature fails

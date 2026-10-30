# identity-registry Specification

## Purpose
The control-plane **Identity_Registry**: the DynamoDB table that resolves the opaque `{org-id}` / `{tenant-id}` / `{project-id}` coordinates — carried throughout the ECR/S3 path, the signed producer claim, and the Metadata_Index — to their human-readable display names, and records each tenant's parentage (the one `{org-id}` a `{tenant-id}` belongs to). It is canonical, mutable control-plane state, encrypted and IAM-gated like every other table, and is a one-way module on the `metadata-index` foundation — DISTINCT from the rebuildable index: losing it costs only display-name resolution, never artifact identity, discoverability, or verifiability. Display names live ONLY here (never in the path, the signed claim, or an ECR resource tag), so renaming an org/tenant/project is a single-item write rather than an N-row rewrite, and consumers resolve names by a read-time join on the opaque id.

*Domain terms (`Capitalized_Underscore`) used below are defined in [GLOSSARY.md](../../GLOSSARY.md), which names the governing requirement for each.*

## Requirements
### Requirement: [IDREG-001] Control-plane identity registry for opaque-id display-name resolution

THE system SHALL maintain an **Identity_Registry** — a DynamoDB table, separate from the Metadata_Index, that resolves the opaque identity coordinates the system carries throughout the ECR/S3 path, the signed claim, and the index. The Identity_Registry SHALL:

1. Map each opaque `{org-id}`, `{tenant-id}`, and `{project-id}` to its human-readable display name, keyed by the opaque id.
2. Record each tenant's parentage — the `{org-id}` a `{tenant-id}` belongs to — so every tenant resolves to exactly one org.

THE Identity_Registry SHALL be canonical, mutable control-plane state — encrypted at rest and IAM-gated identically to every other DynamoDB table. It SHALL be DISTINCT from the rebuildable Metadata_Index projection (the `metadata-index` foundation): the Metadata_Index is a projection of the canonical ECR + S3 sources and SHALL carry no identity or verifiability dependency on the Identity_Registry, so loss of the Identity_Registry SHALL cost only display-name resolution — never artifact identity, discoverability, or verifiability. This capability is a module on the `metadata-index` foundation and depends on it one-way.

Display names SHALL live ONLY in the Identity_Registry — never in the ECR/S3 path, the signed producer claim, or an ECR resource tag. A path segment and an ECR resource tag are operator- and console-visible, and the signed claim carries the opaque identity coordinates exclusively.

A consumer of a plane that carries the opaque identity coordinates SHALL resolve a display name by joining against the Identity_Registry by opaque id at read time; per-bundle Metadata_Index records SHALL carry the opaque identity coordinates, never a denormalized display name. Renaming an org, tenant, or project is therefore a single-item write to the Identity_Registry rather than an N-row rewrite of Metadata_Index records. Because the path encodes the parentage, re-parenting a tenant to a different `{org-id}` is a path change and therefore a re-push, not a registry-only update.

#### Scenario: [IDREG-001.01] A rename is a single-item registry write, not an index rewrite

- **WHEN** an operator changes the display name of project `prj-01JYBV8X1MZ3K7QF9R2T6WYN4D`
- **THEN** the Identity_Registry record keyed by that opaque project id is updated in a single-item write, every Metadata_Index record continues to carry the unchanged opaque `{project-id}`, and subsequent reads resolve the new display name by joining against the Identity_Registry — no Metadata_Index record is rewritten

#### Scenario: [IDREG-001.02] Display names never appear outside the registry

- **WHEN** a bundle is ingested and indexed
- **THEN** its ECR/S3 path, its signed producer claim, its ECR resource tags, and its Metadata_Index record carry only the opaque `{org-id}`, `{tenant-id}`, and `{project-id}`, and the human-readable display name appears nowhere outside the Identity_Registry

#### Scenario: [IDREG-001.03] Losing the registry costs only display names

- **WHEN** the Identity_Registry is unavailable or lost
- **THEN** artifact identity (the Manifest_Digest and Derived_Identity), discoverability via the Metadata_Index, and cryptographic verifiability are unaffected — the Metadata_Index remains rebuildable from the canonical ECR + S3 sources and carries the opaque identity coordinates — and only display-name resolution is degraded


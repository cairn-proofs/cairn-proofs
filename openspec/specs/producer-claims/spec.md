# producer-claims Specification

## Purpose
`producer-claims` is the producer-compliance-claim query plane — a module on the `metadata-index` foundation (one-way dependency, ADR-0020 §5). It projects the signed Producer_Attestation_Referrer (its system of record, artifact-ingestion) into purpose-built, tenant-scoped projections: the control-claims citation index, and future claim-derived access paths as designed access patterns appear. It owns no canonical OSCAL entity identity — that is the OSCAL entity-graph work's; the bundle→control *citation* is the claim's.

*Domain terms (`Capitalized_Underscore`) used below are defined in [GLOSSARY.md](../../GLOSSARY.md), which names the governing requirement for each.*

## Requirements
### Requirement: [PCLAIM-001] Control-coverage projection — list all bundles for a control across projects within a tenant

THE `producer-claims` capability is a module on the `metadata-index` foundation and depends on it one-way (ADR-0020 §5). It SHALL project the producer's signed compliance claim — whose system of record is the Producer_Attestation_Referrer ([INGEST-005](../artifact-ingestion/spec.md), artifact-ingestion) — into tenant-scoped query planes, and SHALL support listing every Artifact_Bundle for a given control across all projects within a single **tenant** — the partition-key isolation boundary — through a dedicated **control-claims projection**. The query SHALL be scoped to the caller's tenant. The bundle→control **citation** is producer-claim data owned here; canonical control **nodes** belong to the OSCAL entity-graph work.

This projection holds the producer's control citations, which the Metadata_Index record does not carry (ADR-0020; see metadata-index [META-002](../metadata-index/spec.md#requirement-meta-002-automatic-metadata_index-updates-from-pipeline-stages)).

The control-claims projection is a **tenant-PK plane** and SHALL carry the uniform physical key schema the metadata-index [META-001](../metadata-index/spec.md#requirement-meta-001-queryable-artifact-index-by-core-dimensions) Requirement mandates for every such plane. Its primary key SHALL be:

1. The `{tenant-id}` as the partition key, scoped exact-match via `dynamodb:LeadingKeys` — the partition-key isolation boundary. The scoping is exact-match on the partition-key value, NOT a partition-key prefix; a control-scoped query against another tenant's partition SHALL return no rows (see access-control [ACCESS-003](../access-control/spec.md#requirement-access-003-read-only-auditor-access-scope)).
2. The composite `{catalogOrProfileUuid}#{controlId}#{manifest-digest}` as the sort key, addressed via a `begins_with('{catalogOrProfileUuid}#{controlId}#')` condition — so bundles citing the same control under different catalogs or profiles never collide, each query scopes to one (catalog/profile, control) pair within the caller's tenant partition, and a heavily-cited control is a sort-key band rather than a hot partition key.

NO Local Secondary Index SHALL sit on this plane, and any alternate access path SHALL be a Global Secondary Index that itself leads with `{tenant-id}` as its partition key (see [META-001](../metadata-index/spec.md#requirement-meta-001-queryable-artifact-index-by-core-dimensions) item 4). No GSI ships for this projection.

Each row SHALL carry:

1. The denormalized attribute that dereferences back to the Metadata_Index record: `projectId`.
2. The read state auditor queries need: `signingStatus`, `archiveStatus`, `wormStatus`.

Every denormalized attribute SHALL have a defined source — the Producer_Attestation_Referrer projection or a pipeline-stage status.

When a bundle's signed Producer_Attestation_Referrer declares a `controls` list, THE Processing_Pipeline SHALL fan out one control-claims row per control, keyed on the caller's `{tenant-id}` partition with sort key `{catalogOrProfileUuid}#{controlId}#{manifest-digest}` and sourced from the Producer_Attestation_Referrer projection. The write SHALL land all rows in one logical operation, and SHALL retry partial-write failures under the pipeline's idempotent retry contract (see processing-pipeline).

When the Producer_Attestation_Referrer declares no `controls` (or no `catalogOrProfileUuid`), THE Processing_Pipeline SHALL write zero control-claims rows for that bundle. The bundle remains reachable through the Metadata_Index, absent only from control-scoped queries.

#### Scenario: [PCLAIM-001.01] Auditor lists every bundle tagged with control ac-2 against a given catalog/profile

- **WHEN** an auditor scoped by `dynamodb:LeadingKeys` to tenant `tnt-01JABCD2EF3GH4JK5MN6PQ7RS8` queries the control-claims projection for `{catalogOrProfileUuid: <profile-uuid>, controlId: "ac-2"}` with no project filter
- **THEN** the `producer-claims` capability queries the control-claims projection with the partition key set exact-match to the tenant `tnt-01JABCD2EF3GH4JK5MN6PQ7RS8` and a `begins_with('<profile-uuid>#ac-2#')` sort-key condition, and returns one row per bundle in that tenant that cited `ac-2` under that catalog/profile — excluding other tenants and the same `controlId` under a different catalog/profile

#### Scenario: [PCLAIM-001.02] Multi-control bundle fans out one row per control

- **WHEN** an Artifact_Bundle's Producer_Attestation_Referrer declares `controls: ["ac-2", "ac-3", "au-4"]` and `catalogOrProfileUuid: <uuid>` under tenant `tnt-01JABCD2EF3GH4JK5MN6PQ7RS8` and the bundle reaches the `artifact.validated` event
- **THEN** the update-metadata stage writes one row to the control-claims projection per control — all under partition key `tnt-01JABCD2EF3GH4JK5MN6PQ7RS8` with sort keys `<uuid>#ac-2#<manifest-digest>`, `<uuid>#ac-3#<manifest-digest>`, `<uuid>#au-4#<manifest-digest>`, each pointing at the bundle's Manifest_Digest — in a single logical write

#### Scenario: [PCLAIM-001.03] Bundle with no controls writes zero rows

- **WHEN** an Artifact_Bundle whose Producer_Attestation_Referrer declares no `controls` reaches the `artifact.validated` event
- **THEN** the update-metadata stage writes no rows to the control-claims projection for that bundle, and the bundle stays recorded in the Metadata_Index and reachable by project-id and Manifest_Digest

#### Scenario: [PCLAIM-001.04] Every attribute on a written row has a defined source

- **WHEN** the update-metadata stage writes a control-claims row for a bundle citing a control
- **THEN** every attribute on the row traces to a defined source — `projectId` from the Producer_Attestation_Referrer projection, and `signingStatus`, `archiveStatus`, `wormStatus` from the corresponding pipeline-stage status

#### Scenario: [PCLAIM-001.05] Control-scoped query against another tenant's partition returns no rows

- **WHEN** an auditor whose `dynamodb:LeadingKeys` binds queries to partition-key value `{tenant-id}` = T issues a control-claims query naming a different tenant partition T', or attempts an unscoped `Scan` of the control-claims projection
- **THEN** the `producer-claims` capability returns no rows for both: the exact-match `dynamodb:LeadingKeys` scoping (T ≠ T') denies the cross-tenant partition read, and the unscoped `Scan` is denied — so no auditor reaches another tenant's control citations through this projection

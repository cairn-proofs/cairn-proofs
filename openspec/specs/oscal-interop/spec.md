# oscal-interop Specification

## Purpose
oscal-interop is the first interop module on Cairn's format-agnostic foundation. The foundation stores, signs, archives, indexes, and verifies every Artifact identically regardless of format; an interop module adds format-aware behavior for one content family at the foundation's seams. The dependency is one-way: oscal-interop depends on the foundation, never the reverse.

OSCAL support is first-class, expressed as **module ownership**: oscal-interop owns the OSCAL media-type family (`application/oscal.*` and `application/oscal+*`) and is fully responsible for:

1. the OSCAL media-type taxonomy (`application/oscal+json` for every OSCAL JSON layer);
2. the content-derived schema-version model — each OSCAL layer's version is read from the document's own `metadata.oscal-version`;
3. SDK-based metadata extraction;
4. the rule that extraction failure is a permanent validation failure;
5. the OSCAL encoding of the universal artifact reference (resource elements with rlinks to ECR and S3 — the foundation *generates* that reference, see processing-pipeline);
6. OSCAL UUID references carried in the Config_Blob `oscalReferences` object.

A declared-OSCAL bundle that cannot be processed as OSCAL is rejected; there is no silent-degrade path. OSCAL is the first of an anticipated family of interop modules (pdf / ocsf / cyclonedx); the foundation/interop extensibility model is recorded in ADR-0018.

*Domain terms (`Capitalized_Underscore`) used below are defined in [GLOSSARY.md](../../GLOSSARY.md), which names the governing requirement for each.*

## Requirements

### Requirement: [OSCAL-001] OSCAL SDK-based extraction with content-derived schema version

WHEN an Artifact_Bundle contains one or more layers whose media type is the OSCAL JSON media type `application/oscal+json` ([OSCAL-002]), then for each such layer the Processing_Pipeline SHALL:

1. parse the layer with the single shipped 1.x binding set;
2. derive the OSCAL model from the root key of the parsed document, not from the layer media type;
3. derive the OSCAL schema version from the parsed document's **own content** (`metadata.oscal-version`);
4. gate that the derived version is a supported OSCAL 1.x version string.

Before it parses an OSCAL layer, the Processing_Pipeline SHALL ignore one leading UTF-8 byte-order mark, as RFC 8259 section 8.1 permits. The stored layer bytes and the layer digest do not change.

IF a layer's bytes lack a readable `metadata.oscal-version` string, OR carry a version that is not a supported OSCAL 1.x string, OR the SDK cannot parse the layer, THEN the Processing_Pipeline SHALL treat this as a permanent validation failure. SDK parse success remains the gate; no separate conformance check against the NIST OSCAL JSON schema is performed.

The OSCAL layer size limit is a maximum byte count for each OSCAL layer. Each deployment SHALL be able to set the limit. A deployment that does not set it SHALL get a default limit. The Processing_Pipeline SHALL NOT parse an OSCAL layer that is larger than the limit, and SHALL NOT read more than the limit plus one byte of the layer. The Processing_Pipeline SHALL hold the bytes and the parsed document of at most one OSCAL layer at a time. IF the layer descriptor declares a size larger than the limit, OR the layer bytes are longer than the limit, THEN the Processing_Pipeline SHALL treat this as a permanent validation failure. The error SHALL state:

1. the layer digest;
2. the declared layer size, or for a layer whose bytes are longer than its declared size, that the bytes are longer than the limit;
3. the limit that applies, in bytes and in MiB;
4. that the limit applies to each OSCAL layer.

The version is read from the document the pipeline already parses model-agnostically. So a future OSCAL-2.0 document that is structurally parseable is rejected cleanly by the 1.x gate on its own embedded version, and one that is not parseable is rejected as a permanent parse failure. Either way the content read is the same seam at which a future multi-version SDK registry (OscalPythonBindings FR-6) would select a binding, so reading the version from content does not foreclose multi-version dispatch.

From the same parsed document, the Processing_Pipeline SHALL additionally read each OSCAL layer's in-band `metadata.last-modified` timestamp and, where the document carries it, `metadata.published`. These are content-derived per-layer timestamps read alongside `metadata.oscal-version`, `metadata.title`, and `metadata.version`; reading them adds no new gate — a layer that parses but carries no `metadata.last-modified` is not rejected on that basis. They are the canonical content-time source consumed by the per-OSCAL-layer temporal-consistency check (see the processing-pipeline temporal-consistency Requirement) and by the metadata projection via the *OSCAL metadata population* Requirement below.

#### Scenario: [OSCAL-001.01] OSCAL bundle parses successfully with content-derived schema version

- **WHEN** an Artifact_Bundle is submitted with an OSCAL layer of media type `application/oscal+json` whose document root key is `assessment-results`, whose document `metadata.oscal-version` is a supported 1.x value (e.g., `1.1.2`), and which the single 1.x binding successfully parses
- **THEN** the Processing_Pipeline derives the schema version from the layer's own content, accepts the OSCAL extraction, and proceeds to subsequent pipeline stages

#### Scenario: [OSCAL-001.02] SDK parse failure is permanent validation failure

- **WHEN** an Artifact_Bundle contains an `application/oscal+json` layer that the SDK cannot parse (malformed document or any SDK-reported parse error)
- **THEN** the Processing_Pipeline classifies the failure as permanent, does not retry, does not accept the layer as opaque content, and does not proceed to signing or archival

#### Scenario: [OSCAL-001.03] OSCAL layer with no readable embedded version is rejected

- **WHEN** an Artifact_Bundle contains a parseable `application/oscal+json` layer whose document carries no `metadata.oscal-version` string (absent or empty)
- **THEN** the Processing_Pipeline rejects the bundle as a permanent validation failure and the error identifies that the OSCAL layer carries no readable embedded schema version (a layer whose bytes are not parseable as an OSCAL document is rejected as a permanent parse failure)

#### Scenario: [OSCAL-001.04] Unsupported (non-1.x) embedded version is rejected

- **WHEN** an OSCAL-typed layer's embedded `metadata.oscal-version` is readable but is not a supported OSCAL 1.x string (for example a parseable 2.0 document)
- **THEN** the 1.x gate on the document's own embedded version rejects the bundle as a permanent validation failure identifying the unsupported version (a future document that is not parseable at all is rejected as a permanent parse failure)

#### Scenario: [OSCAL-001.05] Bundle with no OSCAL layers is not subject to OSCAL schema-version handling

- **WHEN** an Artifact_Bundle has no layers with the `application/oscal+json` media type and no layers with another media type in the OSCAL media-type family ([OSCAL-002])
- **THEN** the Processing_Pipeline does not perform OSCAL version extraction, gating, or parsing, and the bundle is accepted on the remaining criteria

#### Scenario: [OSCAL-001.06] Per-layer in-band timestamps are extracted alongside the schema version

- **WHEN** an OSCAL layer parses successfully and its document carries `metadata.last-modified` (and, optionally, `metadata.published`)
- **THEN** the Processing_Pipeline reads those timestamps from the same parsed document as content-derived per-layer values, makes them available to the per-OSCAL-layer temporal-consistency check and the metadata projection, and does not treat the absence of `metadata.published` (or, for a non-OSCAL layer, the absence of any embedded timestamp) as an extraction failure

#### Scenario: [OSCAL-001.07] OSCAL model comes from the document root key

- **WHEN** an Artifact_Bundle contains an `application/oscal+json` layer whose document root key is `mapping-collection` and whose embedded `metadata.oscal-version` is a supported 1.x value
- **THEN** the Processing_Pipeline parses the layer, records the OSCAL model of that layer as `mapping-collection` in the emitted `artifact.validated` event, and takes no part of the model from the layer media type

#### Scenario: [OSCAL-001.08] OSCAL layer declared larger than the size limit is rejected

- **WHEN** an Artifact_Bundle contains an `application/oscal+json` layer whose descriptor declares a size one byte larger than the OSCAL layer size limit
- **THEN** the Processing_Pipeline rejects the bundle as a permanent validation failure without a retry, and the `artifact.validation-failed` event states the layer digest, the declared size, and the limit that applies, in bytes and in MiB

#### Scenario: [OSCAL-001.09] OSCAL layer whose bytes exceed the size limit is rejected

- **WHEN** an Artifact_Bundle contains an `application/oscal+json` layer whose descriptor declares a size within the OSCAL layer size limit but whose bytes are longer than the limit
- **THEN** the Processing_Pipeline rejects the bundle as a permanent validation failure without a retry, and the `artifact.validation-failed` event states the layer digest, that the layer bytes are longer than the limit, and the limit that applies

#### Scenario: [OSCAL-001.10] OSCAL layer at the size limit is parsed

- **WHEN** an Artifact_Bundle contains an `application/oscal+json` layer whose size equals the OSCAL layer size limit
- **THEN** the Processing_Pipeline does not reject the layer on its size and sends it to SDK parsing

#### Scenario: [OSCAL-001.11] A deployment sets the OSCAL layer size limit

- **WHEN** a deployment sets an OSCAL layer size limit other than the default, and an Artifact_Bundle contains an `application/oscal+json` layer that is larger than the default but within the deployment's limit
- **THEN** the Processing_Pipeline does not reject the layer on its size, and a layer larger than the deployment's limit is rejected with an error that states the deployment's limit

#### Scenario: [OSCAL-001.12] OSCAL layer with a leading byte-order mark is parsed

- **WHEN** an Artifact_Bundle contains an `application/oscal+json` layer whose bytes are a UTF-8 byte-order mark followed by a valid OSCAL 1.x document
- **THEN** the Processing_Pipeline parses the document and accepts the OSCAL extraction, and the stored layer bytes and layer digest include the byte-order mark unchanged

### Requirement: [OSCAL-002] OSCAL-aligned media type taxonomy

THE Provenance_System SHALL use the media type `application/oscal+json` for every OSCAL JSON layer. This applies to each of the 8 GA OSCAL models: catalog, profile, mapping-collection, component-definition, system-security-plan, assessment-plan, assessment-results, plan-of-action-and-milestones. The NIST OSCAL metadata metaschema recommends this media type for OSCAL JSON resources. The media type does not name the OSCAL model. The Processing_Pipeline reads the model from the root key of the parsed document ([OSCAL-001]).

The OSCAL media-type family is the set of media types whose type and subtype begin with `application/oscal.` or `application/oscal+`. When the Processing_Pipeline classifies a layer, it SHALL compare the type and subtype case-insensitively and SHALL ignore media-type parameters. The Processing_Pipeline SHALL classify each layer as follows:

1. A layer whose media type is `application/oscal+json` is an OSCAL layer. This media type is the only signal that starts OSCAL-specific pipeline behavior: SDK extraction, content-derived schema-version gating, the per-OSCAL-layer temporal-consistency check, and OSCAL root-uuid provenance on the artifact reference ([OSCAL-007]).
2. A layer whose media type is in the OSCAL media-type family but is not `application/oscal+json` SHALL cause a permanent validation failure ([OSCAL-005]). The error SHALL name the layer digest and SHALL name `application/oscal+json` as the required media type. The error SHALL include the declared media type only in a length-bounded form in which control, format, and non-ASCII characters appear as escapes. This rule covers per-model forms such as `application/oscal.catalog+json` and the OSCAL XML and YAML forms `application/oscal+xml` and `application/oscal+yaml`.
3. A layer whose media type is not in the OSCAL media-type family is not an OSCAL layer.

#### Scenario: [OSCAL-002.01] Media type drives OSCAL pipeline branching

- **WHEN** a layer in an Artifact_Bundle declares media type `application/oscal+json`
- **THEN** the Processing_Pipeline routes that layer through the OSCAL SDK extraction path and treats the bundle as OSCAL-bearing for purposes of schema-version gating and OSCAL reference generation

#### Scenario: [OSCAL-002.02] Non-OSCAL media type bypasses OSCAL handling

- **WHEN** a layer declares a non-OSCAL media type (for example `application/json` or `application/pdf`)
- **THEN** the Processing_Pipeline does not attempt OSCAL SDK extraction on that layer and does not treat the bundle as OSCAL-bearing on the basis of that layer alone

#### Scenario: [OSCAL-002.03] Per-model OSCAL media type is rejected

- **WHEN** a layer in an Artifact_Bundle declares media type `application/oscal.catalog+json`
- **THEN** the Processing_Pipeline rejects the bundle as a permanent validation failure, the `artifact.validation-failed` event identifies the layer digest and names `application/oscal+json` as the required media type, the layer is not accepted as opaque content, and the bundle does not proceed to signing or archival

#### Scenario: [OSCAL-002.04] OSCAL XML or YAML media type is rejected

- **WHEN** a layer in an Artifact_Bundle declares media type `application/oscal+xml` or `application/oscal+yaml`
- **THEN** the Processing_Pipeline rejects the bundle as a permanent validation failure that names `application/oscal+json` as the required media type, and the layer is not accepted as opaque content

#### Scenario: [OSCAL-002.05] Media-type match ignores case and parameters

- **WHEN** a layer in an Artifact_Bundle declares media type `Application/OSCAL+JSON; charset=utf-8`
- **THEN** the Processing_Pipeline classifies the layer as an OSCAL layer and routes it through the OSCAL SDK extraction path

#### Scenario: [OSCAL-002.06] The CLI emits the one OSCAL media type for every GA model

- **WHEN** a producer runs `cairn push` with an OSCAL JSON file whose document root key is any of the 8 GA OSCAL models, including `mapping-collection`, and supplies no media-type override for that file
- **THEN** the CLI tags the layer with media type `application/oscal+json`

#### Scenario: [OSCAL-002.07] The CLI recognizes OSCAL JSON with a byte-order mark or a leading `$schema` key

- **WHEN** a producer runs `cairn push` with an OSCAL JSON file that starts with a UTF-8 byte-order mark, or whose first top-level key is `$schema` followed by an OSCAL root key, and supplies no media-type override for that file
- **THEN** the CLI tags the layer with media type `application/oscal+json`

#### Scenario: [OSCAL-002.08] The rejection error shows the declared media type only in escaped, bounded form

- **WHEN** a layer declares an OSCAL media-type family member other than `application/oscal+json` that is longer than 256 characters or contains control or non-ASCII characters
- **THEN** the rejection error names the layer digest and `application/oscal+json`, and shows the declared media type truncated, with its original length, and with control, format, and non-ASCII characters as escapes

### Requirement: [OSCAL-003] OSCAL metadata population

WHEN OSCAL extraction succeeds for all OSCAL layers in a bundle, the Processing_Pipeline SHALL populate the Metadata_Index with the extracted values:

1. project name;
2. control references;
3. assessment start date and assessment end date;
4. Assessment Results UUID and SSP UUID;
5. the OSCAL schema version, **extracted per layer from the document's own `metadata.oscal-version`**;
6. each OSCAL layer's in-band `metadata.last-modified` (and `metadata.published` where the document carries it).

The extracted version and the per-layer in-band timestamps SHALL be normalized into the emitted `artifact.validated` event per OSCAL layer, for downstream indexing and for the per-OSCAL-layer temporal-consistency check (see the processing-pipeline temporal-consistency Requirement), mirroring how the resolved `assessment-period` is carried forward.

The per-layer `metadata.last-modified`/`metadata.published` timestamps are content-derived from the document; `metadata.published` is optional and its absence SHALL NOT cause a validation failure.

#### Scenario: [OSCAL-003.01] Successful extraction populates Metadata_Index

- **WHEN** a valid OSCAL Assessment Results document is processed and its embedded `metadata.oscal-version` is a supported 1.x value
- **THEN** the metadata extraction function extracts system name, control references, assessment start date, assessment end date, Assessment Results UUID, SSP UUID, the document-embedded `oscal-version`, and the document-embedded `metadata.last-modified` (plus `metadata.published` when present), all extracted values match the values in the source document, and the Metadata_Index is populated with those values plus the content-extracted `oscalSchemaVersion`

#### Scenario: [OSCAL-003.03] In-band last-modified is carried forward for the temporal-consistency check

- **WHEN** a valid OSCAL document is processed whose `metadata.last-modified` is readable
- **THEN** the Processing_Pipeline normalizes that per-layer `metadata.last-modified` into the emitted `artifact.validated` event so the per-OSCAL-layer temporal-consistency check can compare it against `ecrPushedAt`, and includes it in the Metadata_Index projection

#### Scenario: [OSCAL-003.04] Missing optional published does not fail population

- **WHEN** a valid OSCAL document is processed that carries `metadata.last-modified` but no `metadata.published`
- **THEN** the Processing_Pipeline populates the Metadata_Index and emits `artifact.validated` with the per-layer `metadata.last-modified`, omits `metadata.published`, and does not reject the bundle on the absence of the optional `metadata.published` field

### Requirement: [OSCAL-005] Permanent validation failure on OSCAL extraction failure

IF OSCAL extraction fails for any layer in a bundle, THEN the Processing_Pipeline SHALL emit an `artifact.validation-failed` event indicating which layer digest failed and the extraction error detail, and SHALL NOT proceed with signing or archival for the bundle. The causes of an OSCAL extraction failure are:

1. a parse error;
2. an unsupported (non-1.x) OSCAL version;
3. a missing or unreadable embedded `oscal-version`;
4. a layer media type in the OSCAL media-type family other than `application/oscal+json` ([OSCAL-002]);
5. an OSCAL layer larger than the OSCAL layer size limit ([OSCAL-001]).

OSCAL extraction failure is a permanent validation failure; it is not retried, and the event is routed directly to the dead letter queue per the Processing_Pipeline's permanent-failure classification. The OSCAL-first-class stance is non-negotiable: a bundle that declares itself as carrying OSCAL content but cannot be processed as OSCAL is treated as a hard failure rather than degraded-mode acceptance.

#### Scenario: [OSCAL-005.01] Per-layer OSCAL extraction failure emits validation-failed event

- **WHEN** an Artifact_Bundle has two OSCAL layers and one fails SDK parsing
- **THEN** the Processing_Pipeline emits an `artifact.validation-failed` event identifying the failing layer's digest and the extraction error detail, does not proceed to signing for the bundle, does not proceed to archival for the bundle, and does not retry the failed extraction

#### Scenario: [OSCAL-005.02] OSCAL extraction failure is never silently degraded

- **WHEN** any OSCAL extraction failure occurs (parse error, unsupported version, missing/unreadable embedded schema version, an OSCAL media-type family member other than `application/oscal+json`, or an OSCAL layer larger than the OSCAL layer size limit)
- **THEN** the pipeline does not accept the bundle with a "best-effort metadata" record and does not fall back to treating the OSCAL layer as opaque content; the bundle is rejected as permanently invalid

### Requirement: [OSCAL-006] OSCAL UUID references in Config_Blob

THE Provenance_System SHALL support linking Artifact_Bundles to specific OSCAL assessment results, findings, and observations via UUID references stored in the Config_Blob `oscalReferences` object.

These UUID references are the linkage substrate that allows downstream tooling to navigate from an Artifact_Bundle to the OSCAL artifacts it relates to, and from OSCAL documents back to their supporting evidence.

#### Scenario: [OSCAL-006.01] Config_Blob carries OSCAL UUIDs

- **WHEN** a producer pushes an Artifact_Bundle with a Config_Blob whose `oscalReferences` object is populated with `arUuid` and `sspUuid`
- **THEN** the Processing_Pipeline preserves these UUID references in the Metadata_Index record for the bundle so that subsequent queries can resolve bundles by Assessment Results UUID or SSP UUID

### Requirement: [OSCAL-007] OSCAL encoding of the universal artifact reference

The universal artifact reference that the Processing_Pipeline generates for **every** validated bundle (see the processing-pipeline *Artifact reference generation for every validated bundle* Requirement) SHALL have its body encoded as an OSCAL back-matter `resource` element — Cairn's chosen interchange vocabulary for artifact references, applied to every artifact regardless of format. This is a vocabulary choice, not an OSCAL privilege: an OSCAL back-matter `resource` is the documented construct for pointing at *any* stored Artifact, and the referrer's wire `artifactType` is format-neutral.

The reference's resource identity is content-agnostic and deterministic: the resource `uuid` is `uuidv5(CAIRN_NAMESPACE, Manifest_Digest)`. The OSCAL root `uuid` is recorded as `document-ids[0]` **only as provenance enrichment when an OSCAL layer is present**; when the bundle carries no OSCAL content, `document-ids[0]` falls back to the same deterministic digest-derived uuid. The OSCAL root `uuid` is therefore the sole OSCAL-specific input to the encoding, and it degrades cleanly: it is NOT a gate on whether a reference is generated (that gate, and the ECR/S3 rlinks, belong to the foundation Requirement).

#### Scenario: [OSCAL-007.01] Reference body is encoded as an OSCAL back-matter resource

- **WHEN** the Processing_Pipeline generates the universal artifact reference for a validated bundle with an archive bucket configured
- **THEN** the reference body is a valid OSCAL back-matter `resource` element whose resource `uuid` is `uuidv5(CAIRN_NAMESPACE, Manifest_Digest)`, carrying the foundation-supplied ECR and S3 rlinks unchanged, for an OSCAL-bearing and a non-OSCAL bundle alike

#### Scenario: [OSCAL-007.02] OSCAL root uuid is recorded as provenance when present

- **WHEN** an OSCAL-bearing Artifact_Bundle is validated and its first OSCAL layer carries a root `uuid`
- **THEN** the encoded reference records that OSCAL root `uuid` as `document-ids[0]` (provenance), while the resource `uuid` remains the deterministic `uuidv5(CAIRN_NAMESPACE, Manifest_Digest)`

#### Scenario: [OSCAL-007.03] Non-OSCAL bundle's reference identity falls back to the deterministic uuid

- **WHEN** a non-OSCAL Artifact_Bundle (no `application/oscal+json` layer) is validated with an archive bucket configured
- **THEN** the encoded reference's resource `uuid` and `document-ids[0]` both derive from `uuidv5(CAIRN_NAMESPACE, Manifest_Digest)` (no OSCAL root `uuid` being present), and the body is otherwise identical in form to an OSCAL bundle's reference

## Relocated and Retired IDs

| ID | Status | Resolves to |
|---|---|---|
| OSCAL-003.02 | withdrawn | |

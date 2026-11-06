## MODIFIED Requirements

### Requirement: [OSCAL-001] OSCAL SDK-based extraction with content-derived schema version

WHEN an Artifact_Bundle contains one or more layers with an OSCAL media type matching `application/oscal.*`, then for each such layer the Processing_Pipeline SHALL:

1. parse the layer with the single shipped 1.x binding set;
2. derive the OSCAL schema version from the parsed document's **own content** (`metadata.oscal-version`);
3. gate that the derived version is a supported OSCAL 1.x version string.

IF a layer's bytes lack a readable `metadata.oscal-version` string, OR carry a version that is not a supported OSCAL 1.x string, OR the SDK cannot parse the layer, THEN the Processing_Pipeline SHALL treat this as a permanent validation failure. SDK parse success remains the gate; no separate conformance check against the NIST OSCAL JSON schema is performed.

The version is read from the document the pipeline already parses model-agnostically. So a future OSCAL-2.0 document that is structurally parseable is rejected cleanly by the 1.x gate on its own embedded version, and one that is not parseable is rejected as a permanent parse failure. Either way the content read is the same seam at which a future multi-version SDK registry (OscalPythonBindings FR-6) would select a binding, so reading the version from content does not foreclose multi-version dispatch.

From the same parsed document, the Processing_Pipeline SHALL additionally read each OSCAL layer's in-band `metadata.last-modified` timestamp and, where the document carries it, `metadata.published`. These are content-derived per-layer timestamps read alongside `metadata.oscal-version`, `metadata.title`, and `metadata.version`; reading them adds no new gate — a layer that parses but carries no `metadata.last-modified` is not rejected on that basis. They are the canonical content-time source consumed by the per-OSCAL-layer temporal-consistency check (see the processing-pipeline temporal-consistency Requirement) and by the metadata projection via the *OSCAL metadata population* Requirement below.

#### Scenario: [OSCAL-001.01] OSCAL bundle parses successfully with content-derived schema version

- **WHEN** an Artifact_Bundle is submitted with an OSCAL layer of media type `application/oscal.assessment-results+json` whose document `metadata.oscal-version` is a supported 1.x value (e.g., `1.1.2`) and the single 1.x binding successfully parses the document
- **THEN** the Processing_Pipeline derives the schema version from the layer's own content, accepts the OSCAL extraction, and proceeds to subsequent pipeline stages

#### Scenario: [OSCAL-001.02] SDK parse failure is permanent validation failure

- **WHEN** an Artifact_Bundle contains an `application/oscal.*` layer that the SDK cannot parse (malformed document or any SDK-reported parse error)
- **THEN** the Processing_Pipeline classifies the failure as permanent, does not retry, and does not proceed to signing or archival

#### Scenario: [OSCAL-001.03] OSCAL layer with no readable embedded version is rejected

- **WHEN** an Artifact_Bundle contains a parseable layer with media type matching `application/oscal.*` whose document carries no `metadata.oscal-version` string (absent or empty)
- **THEN** the Processing_Pipeline rejects the bundle as a permanent validation failure and the error identifies that the OSCAL layer carries no readable embedded schema version (a layer whose bytes are not parseable as an OSCAL document is rejected as a permanent parse failure)

#### Scenario: [OSCAL-001.04] Unsupported (non-1.x) embedded version is rejected

- **WHEN** an OSCAL-typed layer's embedded `metadata.oscal-version` is readable but is not a supported OSCAL 1.x string (for example a parseable 2.0 document)
- **THEN** the 1.x gate on the document's own embedded version rejects the bundle as a permanent validation failure identifying the unsupported version (a future document that is not parseable at all is rejected as a permanent parse failure)

#### Scenario: [OSCAL-001.05] Bundle with no OSCAL layers is not subject to OSCAL schema-version handling

- **WHEN** an Artifact_Bundle has no layers with an `application/oscal.*` media type
- **THEN** the Processing_Pipeline does not perform OSCAL version extraction, gating, or parsing, and the bundle is accepted on the remaining criteria

#### Scenario: [OSCAL-001.06] Per-layer in-band timestamps are extracted alongside the schema version

- **WHEN** an OSCAL layer parses successfully and its document carries `metadata.last-modified` (and, optionally, `metadata.published`)
- **THEN** the Processing_Pipeline reads those timestamps from the same parsed document as content-derived per-layer values, makes them available to the per-OSCAL-layer temporal-consistency check and the metadata projection, and does not treat the absence of `metadata.published` (or, for a non-OSCAL layer, the absence of any embedded timestamp) as an extraction failure

### Requirement: [OSCAL-002] OSCAL-aligned media type taxonomy

THE Provenance_System SHALL use OSCAL-aligned media types for OSCAL content following the pattern `application/oscal.{model-name}+json`, covering the OSCAL model set: catalog, profile, component-definition, system-security-plan, assessment-plan, assessment-results, plan-of-action-and-milestones.

These media types are the canonical signal that triggers OSCAL-specific pipeline behavior (SDK extraction, content-derived schema-version gating, OSCAL reference generation, OSCAL UUID annotation handling).

#### Scenario: [OSCAL-002.01] Media type drives OSCAL pipeline branching

- **WHEN** a layer in an Artifact_Bundle declares media type `application/oscal.system-security-plan+json`
- **THEN** the Processing_Pipeline routes that layer through the OSCAL SDK extraction path and treats the bundle as OSCAL-bearing for purposes of schema-version gating and OSCAL reference generation

#### Scenario: [OSCAL-002.02] Non-OSCAL media type bypasses OSCAL handling

- **WHEN** a layer declares a non-OSCAL media type (for example `application/json` or `application/pdf`)
- **THEN** the Processing_Pipeline does not attempt OSCAL SDK extraction on that layer and does not treat the bundle as OSCAL-bearing on the basis of that layer alone

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

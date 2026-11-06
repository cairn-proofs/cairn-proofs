## MODIFIED Requirements

### Requirement: [INGEST-011] OCI Media Type per Layer

WHEN a content layer is added to an Artifact_Bundle, THE Provenance_System SHALL tag the layer with an OCI media type that describes its content format (e.g., `application/oscal+json`, `application/pdf`, `image/png`).

#### Scenario: [INGEST-011.01] Layer tagged with descriptive OCI media type

- **WHEN** a content layer is added to an Artifact_Bundle
- **THEN** the Provenance_System tags the layer with an OCI media type that describes its content format

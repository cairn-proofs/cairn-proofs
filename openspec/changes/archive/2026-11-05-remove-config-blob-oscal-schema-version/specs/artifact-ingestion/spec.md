## MODIFIED Requirements

### Requirement: [INGEST-016] Config_Blob Media Type and Content-Descriptive Fields

THE Provenance_System SHALL use a Config_Blob, with media type `application/vnd.cairnproofs.artifact.config.v1+json`, that carries only **content-descriptive** bundle-level metadata.

The Config_Blob SHALL include project ID as a content-descriptive identifier; it MAY carry content-descriptive references such as OSCAL document references. The Config_Blob SHALL NOT carry the producer's compliance claim — `controls`, `catalogOrProfileUuid`, `assessmentPeriod`, producer/assessor identity, assessment-id, and supersedes are claim fields of the Producer_Attestation_Referrer (see *Producer Claim Carried as Signed Attestation Referrer*), never Config_Blob fields. The Config_Blob SHALL NOT carry a `controlFamily` field — control-family is a derived projection of the `controls` list, never bundle data.

#### Scenario: [INGEST-016.01] Content-descriptive Config_Blob with required media type accepted

- **WHEN** a content manifest's Config_Blob is submitted with media type `application/vnd.cairnproofs.artifact.config.v1+json` and includes project ID, carrying only content-descriptive fields and no compliance-claim fields
- **THEN** the Provenance_System accepts the Config_Blob as well-formed

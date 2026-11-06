Captured-from: #71

## Why

The `ConfigBlob` model still declares a typed `oscalSchemaVersion` field that no ingestion path reads, and the specs still describe a tolerate-and-ignore rule for it. The OSCAL schema version comes only from each OSCAL layer's `metadata.oscal-version`. The leftover field and the spec text about it make the bundle contract look like it has a version field that matters, when it does not.

After the typed field is removed, a producer-supplied `oscalSchemaVersion` key is an unknown Config_Blob key like any other. `ConfigBlob` accepts unknown keys (`extra="allow"`), so the key is neither stripped nor rejected. The Config_Blob bytes are covered by the content Manifest_Digest and the signature, so ECR and the archive keep them as pushed in any case. This change gives the key no special handling.

## Personas served

1. Priya — DevSecOps / Artifact Producer Engineer: "Conform to the bundle contract (OCI manifest, required annotations, Config_Blob) without re-reading the spec each time." The Config_Blob contract loses a field that looks meaningful but has no effect.

## What Changes

1. Remove the typed `oscal_schema_version` field from `ConfigBlob`, and remove the tolerate-and-ignore text from its docstring and comments.
2. Remove the `oscal-interop` spec text that describes a Config_Blob `oscalSchemaVersion` field or a producer-declared version, including scenario [OSCAL-003.02]. The requirements still state where the version comes from, how it is gated to 1.x, and how it is indexed.
3. Remove the [INGEST-016] paragraph that says the Config_Blob does not carry the authoritative OSCAL schema version. The requirement already lists what the Config_Blob carries.
4. In the requirements this change modifies, remove historical narration ("relocated", "making the version content-derived"), as the spec rules require.
5. Remove the tests, test fixtures, and docstrings that exist only to cover the tolerate-and-ignore rule. Tests that the version comes from `metadata.oscal-version`, is gated to 1.x, and is indexed per layer stay.
6. Correct the Sam (GRC Engineer) persona in `openspec/personas/personas.md`. Its goal and success metric describe a declared-versus-embedded version cross-check and a bundle-type / Config_Blob / ECR-path triple, neither of which exists. They now describe the 1.x gate on the embedded version and the identity-triple check against the repository path ([PIPELINE-013]).
7. Correct `runbooks/troubleshooting.md`. Its validate-failure classes describe an `accountId` Config_Blob check and an OSCAL parse "against the declared OSCAL schema version", and its log-field list names `bundleType` and `accountId`. They now describe the [PIPELINE-013] identity-triple check, the OSCAL `errorKind` values, and the current log fields.

Not changed:

1. `ConfigBlob`'s `extra="allow"` policy, and whether accepting unknown keys is part of the contract.
2. How the schema version is derived, gated, carried on the `artifact.validated` event (`layers[].oscal.oscalSchemaVersion`), or indexed. That event and index key has the same name as the removed Config_Blob field but is a different value, and it stays.
3. Already-archived bundles. Their Config_Blob bytes are immutable.
4. The `cairn` CLI's output. It already writes no `oscalSchemaVersion` key (`cli/cairn_cli/push.py` builds the Config_Blob from `projectId` only).

## Capabilities

### New Capabilities

None.

### Modified Capabilities

1. `oscal-interop`: [OSCAL-001], [OSCAL-002], and [OSCAL-003] stop describing a Config_Blob `oscalSchemaVersion` field. Scenario [OSCAL-003.02] is withdrawn.
2. `artifact-ingestion`: [INGEST-016] stops describing the OSCAL schema version.

## Impact

1. Code: `libs/oci/cairn_oci/config_blob.py` (field and docstring), `libs/oscal/cairn_oscal/dispatch.py` (module docstring).
2. Tests: `tests/unit/cairn_oci/test_config_blob.py`, `tests/unit/cairn_validation/test_bundle.py`, `tests/property/test_properties_artifact_ingestion.py`, `tests/property/test_properties_oscal_interop.py`, `tests/unit/test_validate_to_metadata_oscal_version_seam.py`, the `validate_bundle` and `update_metadata` unit-test fixtures, and the `cairn push` unit tests.
3. Runtime behavior: none. No stage reads the field. A Config_Blob that declares the key validates as before, and the key stays in the event's `configBlob` dump as an unknown key.
4. Producers: none. Direct-API and `oras` producers that still send the key are not affected.

## Context

`ConfigBlob` (`libs/oci/cairn_oci/config_blob.py`) declares `oscal_schema_version: str | None` (alias `oscalSchemaVersion`). No code reads it. The validate stage derives each OSCAL layer's version from `metadata.oscal-version` and carries it on the `artifact.validated` event as `layers[].oscal.oscalSchemaVersion` (`lambdas/validate_bundle/.../handler.py`, `_build_layers`). `update_metadata` indexes it from there. The `cairn` CLI builds its Config_Blob as `ConfigBlob(projectId=...)` with `exclude_none=True`, so it never writes the key.

`ConfigBlob` uses `extra="allow"`. An unknown key such as `fizbuzzJackalope` validates, rides in `__pydantic_extra__`, and appears in the `configBlob` dump on the `artifact.validated` event. `archive_to_s3` copies the raw Config_Blob bytes from ECR and checks them against the manifest digest ([WORM-003.02]), so the archive and ECR always hold the bytes as pushed.

GitHub issue #71 asked for the key to be stripped at ingestion. This design does not strip it. See Decision 1.

## Goals / Non-Goals

**Goals:**

1. Remove the typed field and every spec, docstring, comment, and test that exists only for the tolerate-and-ignore rule.
2. Keep every test of specified behavior: the version comes from `metadata.oscal-version`, is gated to 1.x, and is carried per layer on the event and in the index.

**Non-Goals:**

1. Changing `ConfigBlob`'s `extra` policy, or making unknown-key acceptance part of the contract.
2. Changing the event or index key `layers[].oscal.oscalSchemaVersion`.
3. Touching already-archived bundles.

Diagrams: N/A — removes an unread model field and spec text; no component, boundary, or runtime flow changes.

## Decisions

1. **Treat the key like any other unknown key; do not strip it.** After the typed field is removed, `oscalSchemaVersion` falls into `extra="allow"` with every other unknown key.
   - Alternative: strip the key before the `configBlob` dump on the event (#71's option a). Rejected. The only reader of the event's `configBlob` is `update_metadata`, and it reads only `oscalReferences`. Stripping cannot reach the raw bytes in ECR and the archive, because those bytes are covered by the Manifest_Digest and the signature. It would also add a one-key special case to an otherwise uniform unknown-key rule.
   - Alternative: reject Config_Blobs that declare the key. Rejected. It would break any direct-API or `oras` producer still sending the key, for no integrity gain.
   - Consequence: #71's precondition (producer telemetry showing no one sends the key) is not needed. No producer breaks and no stored value is lost.
2. **Remove the spec text instead of restating it in general terms.** Once a requirement says the version comes from `metadata.oscal-version`, a sentence saying a Config_Blob field is not read describes something the system does not do. [OSCAL-003.02] tests only that non-behavior, so it is withdrawn.
3. **Remove tests that only cover the removed rule. Do not add an unknown-key test.** Unknown-key acceptance is not in the contract, so a test that a Config_Blob declaring `oscalSchemaVersion` validates would pin unspecified behavior. The same goes for the CLI tests that assert the CLI emits no `oscalSchemaVersion` key. With the field removed, `ConfigBlob(projectId=...)` cannot produce it.
4. **Keep the event and index key name `oscalSchemaVersion`.** It names the content-derived per-layer value in `layers[].oscal` and in META-008's projection. It is a different value from the removed field, and renaming it would be a schema change to the index for no benefit.
5. **Remove historical narration in the modified requirements.** "Relocated" (temporal-consistency check) and "making the version content-derived" describe a past change. The spec rules require that wording to be replaced when found.

## Risks / Trade-offs

1. [A later change switches `ConfigBlob` to `extra="forbid"`, and no test flags that producers sending `oscalSchemaVersion` break.] → Accepted. The user chose not to put unknown-key acceptance in the contract. Whoever changes the `extra` policy owns that compatibility decision.
2. [Someone reads a stale `oscalSchemaVersion` from a raw Config_Blob and takes it as authoritative.] → The same risk applies to any unknown key. The specs name `metadata.oscal-version` as the only source, and the index carries the content-derived value.

## Migration Plan

No deploy-time steps. The change ships with the next Lambda bundle build.

OpenSpec deltas operate on whole requirements and have no way to remove one scenario. A MODIFIED block must contain every scenario the main spec has, so `openspec validate --strict` and `openspec archive` reject the [OSCAL-003] delta while the main spec still has [OSCAL-003.02]. That validation error is expected until archive. The archive flow makes these edits to `openspec/specs/oscal-interop/spec.md` by hand, in one step, immediately before it runs `openspec archive`:

1. Delete scenario [OSCAL-003.02] from [OSCAL-003].
2. Add a `## Relocated and Retired IDs` table at the end of the spec with the row `| OSCAL-003.02 | withdrawn | |`, in the format `artifact-ingestion` uses. The spec-id lint requires the row as soon as the scenario is gone.
3. Change Purpose item 2 to end at "read from the document's own `metadata.oscal-version`", dropping "never from a producer declaration". Deltas cannot carry Purpose edits.

After step 1, `openspec validate remove-config-blob-oscal-schema-version --strict` passes, and `openspec archive` applies the deltas.

Rollback is a revert of the commit.

## Threat model

Skipped with the user's approval. The change removes a model field that no code reads, and spec text about it. It adds no component and alters no data flow, trust boundary, or runtime behavior.

## Open Questions

None.

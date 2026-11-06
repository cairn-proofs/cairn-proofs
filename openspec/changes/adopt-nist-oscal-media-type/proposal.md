Captured-from: #101

## Why

[OSCAL-002](../../specs/oscal-interop/spec.md#requirement-oscal-002-oscal-aligned-media-type-taxonomy) makes the per-model form `application/oscal.{model-name}+json` the signal that starts OSCAL processing. NIST recommends the opposite. The remarks on the `media-type` flag in the OSCAL metadata metaschema ([`oscal_metadata_metaschema.xml`](https://github.com/usnistgov/OSCAL/blob/main/src/metaschema/oscal_metadata_metaschema.xml)) say that `application/oscal+json` SHOULD be used for OSCAL JSON. They also say that a media type that includes the model SHOULD be avoided, because a parser can detect the model from the content.

The current rule also causes a silent failure. The validate stage selects OSCAL layers by the prefix `application/oscal.`. A layer that has the NIST type `application/oscal+json` does not match that prefix. The pipeline accepts that layer as opaque content. It does no OSCAL parse, no version gate, and no [OSCAL-005](../../specs/oscal-interop/spec.md#requirement-oscal-005-permanent-validation-failure-on-oscal-extraction-failure) rejection. A producer that follows NIST gets no OSCAL validation and no error.

No OSCAL media type is registered with IANA (checked 2026-11-06). `application/oscal+json` is a NIST recommendation only.

## Personas served

1. Priya — DevSecOps / Artifact Producer Engineer: the frustration "Silent degradation when OSCAL or annotations are off." Today a NIST-typed OSCAL layer degrades to opaque content with no error.
2. Sam — GRC Engineer: the goal "Trust that OSCAL extraction failure is a *hard* failure, not a degrade", and the frustration "OSCAL flattened to opaque blobs." Sam merges external OSCAL. External producers that follow NIST use `application/oscal+json`.

## What Changes

1. **BREAKING** — The only OSCAL layer media type is `application/oscal+json`. The validate stage sends each layer that has this type through OSCAL SDK parsing and the content-derived version gate. The OSCAL model comes from the root key of the parsed document, not from the media type.
2. **BREAKING** — A layer whose media type is in the OSCAL media-type family but is not `application/oscal+json` causes a permanent validation failure. Examples are `application/oscal.catalog+json`, `application/oscal+xml`, and `application/oscal+yaml`. The error names `application/oscal+json` as the required type. There is no legacy alias. Issue open question 1 is resolved as "reject".
3. The media-type match is case-insensitive on the type and subtype, and ignores media-type parameters.
4. `cairn push` sniffs an OSCAL JSON file of any of the 8 GA models and emits `application/oscal+json`. The `mapping-collection` model is included.
5. **BREAKING** — The OSCAL layer size limit becomes a deployment setting (`cdk --context maxOscalLayerBytes=N`) with a default of 16 MiB per layer, down from a fixed 64 MiB. The validate stage checks the declared layer size before it fetches the layer, and it reads at most the limit plus one byte. A layer over the limit causes a permanent validation failure. The error states the layer digest, the size, and the limit that applies. The largest known OSCAL document, the NIST SP 800-53 rev5 catalog, is 10.0 MiB. A dense 16 MiB document peaked at 872 MiB of parse memory in a measurement, and the validate Lambda has 1,024 MB. Today an oversized OSCAL layer can stop the Lambda with an out-of-memory error and no rejection record.
6. The OSCAL media-type constant and the layer classification move into `libs/oscal/` (`cairn_oscal`), which is the oscal-interop module that owns the family.
7. Specs, tests, the CLI docstring, and `runbooks/troubleshooting.md` use `application/oscal+json`. No per-model OSCAL media type stays in `openspec/specs/`, `cli/`, `libs/`, `lambdas/`, `tests/`, or `runbooks/`. ADR-0018 is the one exception in `docs/`. See [design Decision 6](design.md#decisions).

## Capabilities

### New Capabilities

None.

### Modified Capabilities

1. `oscal-interop`: OSCAL-001 (trigger media type, the content-derived model, and the OSCAL layer size limit), OSCAL-002 (taxonomy rewritten to `application/oscal+json` with fail-closed rejection of other family members), OSCAL-005 (the media-type rejection is one of the permanent failure causes), and OSCAL-007 (scenario OSCAL-007.03 names the new type). The `## Purpose` text also changes. The delta flow does not carry Purpose text, so a task edits it. See [design Decision 7](design.md#decisions).
2. `artifact-ingestion`: the INGEST-011 example media type becomes `application/oscal+json`.

## Impact

1. Code: `lambdas/validate_bundle/cairn_lambda_validate_bundle/handler.py` (layer selection, the size check, and the failure-event field), `lambdas/validate_bundle/cairn_lambda_validate_bundle/ecr_client.py` (bounded blob read), `libs/oscal/cairn_oscal/` (new media-type module, the size-limit default and argument, and new error kinds), and `cli/cairn_cli/_media_type.py` (sniffer output).
2. Infrastructure: `cdk/bin/artifact-store.ts`, `cdk/lib/artifact-store-stack.ts`, and `cdk/lib/constructs/lambda-functions.ts` carry the optional `maxOscalLayerBytes` setting to the validate Lambda as an environment variable.
3. Documentation of the size limit: the OSCAL-001 delta states that the limit exists and can be set. The default value and the setting are in the `DEFAULT_MAX_OSCAL_LAYER_BYTES` docstring, `cli/README.md` (producer constraints), `runbooks/troubleshooting.md` (operator triage), `runbooks/deploy-alpha.md` (deploy settings), and `docs/architecture-as-deployed.md` (validate-bundle bounds). Today the 64 MiB limit is in code and code comments only.
4. Tests: unit tests under `tests/unit/cairn_oscal/`, `tests/unit/cairn_lambda_validate_bundle/`, `tests/unit/cairn_lambda_update_metadata/`, `tests/unit/cairn_validation/`, and `tests/unit/cairn_cli/`, CDK tests under `cdk/test/`, plus `tests/integration/test_pipeline_failure_paths.py`.
5. Producers: a producer that sets a per-model OSCAL media type with `oras` or the direct API gets a permanent validation failure after deploy. `cairn push` users get the new type after they update the CLI. An older CLI emits the per-model form, and its pushes fail until the user updates it. Under the default limit, an OSCAL layer between 16 MiB and 64 MiB that passed before now fails.
6. Stored data: no change. Archived bundles are immutable and keep their recorded layer media types. `cairn verify` does not branch on layer media type.
7. IAM and dependencies: no change. The validate Lambda keeps 1,024 MB.

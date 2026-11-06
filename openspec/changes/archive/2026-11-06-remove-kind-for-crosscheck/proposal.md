Captured-from: #45

## Why

`ArtifactLocator.kind_for_crosscheck` in `libs/oci/cairn_oci/categories.py` has no production caller. Its only consumer was the three-way bundle-type cross-check, and that cross-check no longer exists. The method suggests that the pipeline reads artifact-kind from the leading segment of the canonical ECR tag. It does not. Artifact-kind comes only from the signed claim (see [artifact-ingestion *Producer Claim Carried as Signed Attestation Referrer*](../../specs/artifact-ingestion/spec.md#requirement-ingest-005-producer-claim-carried-as-signed-attestation-referrer)). A security review flagged the method as a maintenance trap: a future contributor can use it to wire an unsigned kind source back into validation.

## Personas served

1. none. This change removes dead internal code and changes no behavior that a persona sees. The value is indirect. The change removes a ready-made path to read artifact-kind from the unsigned tag, which protects Marcus's goal to "trust the math, not the vendor's word". No persona models a code contributor, and this change does not add one.

## What Changes

1. Remove the `ArtifactLocator.kind_for_crosscheck` method from `libs/oci/cairn_oci/categories.py`.
2. Remove the `kind_for_crosscheck` reference from the `normalize_artifact_kind` docstring in the same file.
3. Remove the `kind_for_crosscheck` tests: one test in `tests/unit/cairn_oci/test_categories.py` and three tests in `tests/unit/cairn_oci/test_locator.py`.
4. Keep `parse_tag` and all its helpers. `mint_tag`, `alias_tag_of`, and the `cairn alias` CLI use `parse_tag`. [INGEST-021](../../specs/artifact-ingestion/spec.md#requirement-ingest-021-ecr-tag-format) names it as the single tag parser.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

None. No requirement in `openspec/specs/` refers to `kind_for_crosscheck` or to a tag-derived kind check. The change sets `skip_specs: true`.

## Impact

1. Code: one method and one docstring line in `libs/oci/cairn_oci/categories.py`. No other module, Lambda, or CLI command refers to the method.
2. Tests: four test functions removed. The out-of-vocabulary tag case stays covered by `test_parse_tag_fail_closed` in `tests/unit/cairn_oci/test_locator.py`.
3. Specs, docs, infrastructure, and dependencies: no change. Archived changes under `openspec/changes/archive/` keep their references as history.

## Context

The [proposal](proposal.md#why) gives the motivation. This section records the audit that [issue #45](https://github.com/cairn-proofs/cairn-proofs/issues/45) acceptance criterion 1 requires before any removal.

Audit result (a repository-wide search for `kind_for_crosscheck`, outside `openspec/changes/archive/`):

1. `libs/oci/cairn_oci/categories.py`: the method definition, and one mention in the `normalize_artifact_kind` docstring.
2. `tests/unit/cairn_oci/test_categories.py`: `test_kind_for_crosscheck_reads_canonical_form`.
3. `tests/unit/cairn_oci/test_locator.py`: `test_kind_for_crosscheck_reads_tag`, `test_kind_for_crosscheck_fail_closed_on_bad_tag`, and `test_kind_for_crosscheck_takes_no_repo_name_argument`.

No module under `lambdas/`, `libs/`, `cli/`, or `cdk/` calls the method. No file under `openspec/specs/` or `docs/` names it.

The method body is one line: `return self.parse_tag(image_tag).artifact_kind`. It has no private helper of its own.

## Goals / Non-Goals

**Goals:**

1. Remove `kind_for_crosscheck` and every reference to it outside the change archive.
2. Keep the behavior of every live caller of `parse_tag` the same.

**Non-Goals:**

1. Change where artifact-kind comes from or how the pipeline checks it.
2. Change the canonical ECR tag format or the tag parser.
3. Fix the TUI test-isolation flake (`test_project_row_drilldown_lands_and_stays_on_detail`).

## Architecture

Diagrams: N/A. The change removes one unused library method. It adds no component and changes no runtime flow.

## Decisions

1. **The dead code is the one method.** The method delegates to `parse_tag` and owns no helper or constant. `parse_tag` and its helpers stay, because these callers use it:
   1. `lambdas/mint_tag/cairn_lambda_mint_tag/handler.py` reads back canonical tags for idempotency ([issue #45](https://github.com/cairn-proofs/cairn-proofs/issues/45) acceptance criterion 4).
   2. `alias_tag_of` in `categories.py`, which `cli/cairn_cli/verify.py`, `pull.py`, `query.py`, and `tui/app.py` call.
   3. `cli/cairn_cli/alias.py`.

   This answers the issue's open question 1.
2. **Archived changes stay as they are.** Files under `openspec/changes/archive/` are history. The project rule "no history in specs, docs, or code comments" applies to current-state files, not to the archive. No current-state file cites the method. This answers the issue's open question 2.
3. **Remove the tests. Do not move them to `parse_tag`.** The removed tests check behavior that existing `parse_tag` tests already check:
   1. Leading-kind extraction: `test_property6_canonical_round_trip` in `test_categories.py` and the [INGEST-021.09] round-trip tests in `test_locator.py`.
   2. Fail-closed on an out-of-vocabulary kind: `test_parse_tag_fail_closed` in `test_locator.py` uses the same `not-a-category--_ref--…` input.

   `test_kind_for_crosscheck_takes_no_repo_name_argument` checks a signature of the removed method. It has no successor.
4. **Use `skip_specs: true`.** No requirement changes. Adding a requirement such as "the locator has no kind-from-tag method" would state an absence that no persona observes.

## Risks / Trade-offs

1. [An unlisted caller exists, for example a dynamic `getattr`] → `mise run check` runs the full test suite and catches an `AttributeError`. A search for the string `kind_for_crosscheck` also finds a dynamic name that the code spells as a literal.
2. [A contributor later needs artifact-kind from a tag] → `parse_tag(tag).artifact_kind` gives the same value. The removal makes that reader visible at each call site, and a reviewer can check it against [INGEST-005](../../specs/artifact-ingestion/spec.md#requirement-ingest-005-producer-claim-carried-as-signed-attestation-referrer).

## Migration Plan

No deployment step. The method is not in a Lambda code path. Rollback is a revert of the commit.

## Threat model

The user approved a skip of the threat-model step for this change. The change removes one library method that no code calls. It adds or changes no component, data flow, trust boundary, or runtime behavior. This change has no `threat-model-report.md`.

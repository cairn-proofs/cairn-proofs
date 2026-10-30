# Design

## Root Cause Analysis

<!-- Walk from the Current Behavior clauses in bugfix.md to the underlying
     defect in code. Cite file paths and line numbers
     (e.g., libs/oscal/dispatch.py:142). Name the precise mechanism:
     off-by-one, missing await, wrong KMS key id, race on shared
     module-level state, etc. List each contributing file if multi-site. -->

## Why It Wasn't Caught

<!-- One paragraph. Which test was supposed to cover this and why didn't it?
     Missing case, mocked dependency that diverged from prod, test only ran
     serially, etc. This shapes the regression tests below. -->

## Proposed Fix

<!-- The change. Be specific:
     - Which functions, which lines
     - What behavior changes
     - Minimal code sketch if it clarifies

     Call out alternatives considered and why this approach wins.
     Surgical > comprehensive. If you're proposing a refactor, ask
     whether that belongs in a follow-up change instead. -->

## Blast Radius

<!-- Numbered list of files/modules touched, with a one-line "who else
     calls this" note for each modified function. Number them so a
     regression-guard property can cite "blast-radius #2". This bounds the
     regression-test scope. -->

1. `<path/to/file.py>` — modifies `<function>`; callers: `<...>`

## Test Properties

<!-- Three categories. Frame each as a property-based assertion
     (input shape → invariant), not a single example.
     The project uses Hypothesis; reference existing
     tests/unit/cairn_*/ patterns. -->

### (a) Bug Reproduction (RED)

<!-- Properties that FAIL on main and PASS on the fix branch.
     Each must trace to a specific Expected Behavior clause in bugfix.md. -->

1. Property: <!-- assertion --> — covers bugfix.md Expected Behavior clause #<n>

### (b) Fix Verification (GREEN)

<!-- Properties that PASS on the fix branch.
     Often the same set as (a) - explicitly note any divergence
     (e.g., fix-only invariants that don't make sense to assert
     against unfixed code). -->

1. <!-- Same as (a) unless noted -->

### (c) Regression Guard

<!-- Properties that PASS on main AND MUST CONTINUE TO PASS on the fix
     branch. Encode the Unchanged Behavior clauses from bugfix.md plus
     any behavior identified in Blast Radius that the fix could
     plausibly disturb. Err on the side of more guards. -->

1. Property: <!-- assertion --> — covers bugfix.md Unchanged Behavior clause #<n>
2. Property: <!-- assertion --> — guards blast-radius caller `<path:line>`

## Spec Alignment

<!-- If bugfix.md flagged a spec amendment:
     1. Describe the delta
     2. Create the delta spec at openspec/changes/<name>/specs/<capability>/spec.md
        following project conventions (MODIFIED Requirements with full
        updated content, exact header match, NO phase annotations — specs
        describe current state, so spec deltas MUST NOT introduce `*Phase:*` lines)

     If no spec change needed: write "Spec already mandates the Expected
     Behavior; no delta needed." -->

## Rollback

<!-- One paragraph. How to revert if the fix introduces a worse bug.
     For most fixes: "revert the commit, no state to unwind."
     Call out migrations, schema changes, or operational steps that
     make rollback non-trivial. -->

## 1. Bug Reproduction (RED)

<!-- Failing tests FIRST. One task per property from design.md
     "Test Properties (a) Bug Reproduction". Must fail against main. -->

- [ ] 1.1 <!-- Add failing property test for <Expected Behavior clause N> in tests/unit/cairn_<lib>/test_<thing>.py -->
- [ ] 1.2 <!-- Confirm test fails against main; record observed failure mode in commit message or wave summary -->

## 2. Fix

<!-- Implement the change from design.md "Proposed Fix".
     Touch only files in "Blast Radius". -->

- [ ] 2.1 <!-- Modify <file:line> to <change> per design.md "Proposed Fix" -->
- [ ] 2.2 <!-- Confirm Group 1 tests now pass -->

## 3. Regression Guard

<!-- Tests from design.md "Test Properties (c) Regression Guard".
     Pass before AND after the fix. -->

- [ ] 3.1 <!-- Add property test for <Unchanged Behavior clause N> -->
- [ ] 3.2 <!-- Add property test for <blast-radius caller from design.md> -->

## 4. Spec Sync (only if Spec Impact flagged amendment)

<!-- Skip this group if bugfix.md "Spec Impact" said "None". -->

- [ ] 4.1 <!-- Create or update delta spec at openspec/changes/<name>/specs/<capability>/spec.md -->
- [ ] 4.2 <!-- Run `openspec validate --specs --strict` -->

## 5. Verification

- [ ] 5.1 Run `mise run check` and confirm green
- [ ] 5.2 <!-- Run smoke suite if the fix touches Lambda code or CDK -->

## Task Dependency Graph
```json
{
    "waves": [
        { "id": 0, "dependsOn": [],  "tasks": ["1.1", "1.2"] },
        { "id": 1, "dependsOn": [0], "tasks": ["2.1", "2.2"] },
        { "id": 2, "dependsOn": [1], "tasks": ["3.1", "3.2"] },
        { "id": 3, "dependsOn": [2], "tasks": ["4.1", "4.2"] },
        { "id": 4, "dependsOn": [2,3], "tasks": ["5.1", "5.2"] }
    ]
}
```

Wave 0 (RED) MUST complete before wave 1 (fix) so the failing-test
contract is recorded first. Wave 3 (spec sync) is skipped entirely if
bugfix.md's Spec Impact said "None"; in that case wave 4 depends only
on wave 2. For small fixes, waves 2-4 can fold into a single wave.

Tests SHIP IN THE SAME WAVE as the code they cover - except the RED
reproduction tests in wave 0, which intentionally ship before the fix
so we can prove they fail first. No two waves may edit the same file.

`##` headings are organizational, not wave boundaries — wave membership
is driven by file ownership + shared test surface. To parallelize, split
file-disjoint independent work into **sibling waves** sharing a
`dependsOn`, not into multiple tasks inside one wave; but keep tasks that
share a file or test together. (The RED→fix edge holds regardless.)

## 1. Bug Reproduction (RED)

<!-- Failing tests FIRST. One task per property from design.md
     "Test Properties (a) Bug Reproduction". Must fail against main. -->

- [ ] 1.1 <!-- Add failing property test for <Expected Behavior clause N> in tests/unit/cairn_<lib>/test_<thing>.py -->
- [ ] 1.2 <!-- Confirm test fails against main; record the observed failure mode in the task report -->

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
- [ ] 4.2 <!-- Run `mise run spec-validate` -->

<!-- Order tasks so each task follows its prerequisites. Name the files each task touches. Ship tests in the same task as the code. -->

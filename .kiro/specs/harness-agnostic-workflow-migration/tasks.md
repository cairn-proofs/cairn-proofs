# Implementation Plan: Harness-Agnostic Workflow Migration

## Overview

This plan follows the design section "Feasibility checks". The feasibility checks run first. Three of them are stop gates. Then the plan creates the recovery tag, edits the Kiro surfaces and the portable surfaces, removes the retired files, updates the reference docs, replaces the ADRs, verifies the repository state, commits, and files the follow-up issues.

The Task Dependency Graph at the end of this file sets the execution order. Each task comes after its prerequisites. Tasks in the same wave are independent and can run at the same time.

Shell rule for every task that runs a command: never prefix a shell command with `cd`. Run commands from the repository root. If a command needs another directory, use the tool's `cwd` parameter.

Baseline commit for every "pre-change value" and byte-equality check: `37af994`. Read a baseline file with `git show 37af994:<path>`.

## Tasks

- [x] 1. Run the feasibility checks F1 to F6
  - Run these checks before any other edit lands, except the edits that a check itself needs.
  - Shell rule: never prefix a shell command with `cd`. Use the tool's `cwd` parameter if needed.

  - [x] 1.1 Add the run-artifact ignore rule
    - Add the two lines from design section C15 to `.gitignore`: the comment line and `/openspec/workflow-runs/`.
    - The probes in this task group write under `openspec/workflow-runs/probe/`. This rule keeps those files out of git.
    - Files: `.gitignore`
    - _Requirements: 9.7, 10.4_

  - [x] 1.2 F1 (stop gate): validate the recipe structure
    - Replace the untracked `.kiro/workflows/opsx-apply.workflow.json` with the skeleton in design section C5. Keep every node type, id, agent, `joinPolicy`, repeat bound, `stopCondition`, and `artifacts` map exactly as the skeleton shows.
    - For this check, each `prompt` can be a short inline placeholder string. Each placeholder must contain the same `{{...}}` template references that its prompt contract in C5 uses. The `reconcile-plan` placeholder references `{{implement-slot-1.output}}`, `{{implement-slot-2.output}}`, and `{{implement-slot-3.output}}`. The `adjudicate` placeholder references `{{code-review.output}}`, `{{security-review.output}}`, and `{{test-gaps.output}}`.
    - Run `validate_workflow` on `.kiro/workflows/opsx-apply.workflow.json`.
    - Pass condition: no error, and no happens-before error for the `reconcile-plan` and `adjudicate` references.
    - On an error, fix the recipe and validate again.
    - STOP GATE: if no static mechanism for the fan-out design of that time validates, stop. Do not continue to task 1.3. Report to the user. Do not ship a sequential implement step without the user's approval.
    - Files: `.kiro/workflows/opsx-apply.workflow.json`
    - _Requirements: 9.16_

  - [x] 1.3 F2 (stop gate): probe the default-prompt assumption
    - Remove the top-level `"prompt"` key from `.kiro/agents/orchestrator.json` and from `.kiro/agents/implementer.json`. Change no other key in this task.
    - Create `.kiro/steering/document-authoring.md` with the exact text in design section C4 (front matter `inclusion: always`). This is the final content of the file.
    - Write a scratch probe recipe `.kiro/workflows/probe-default-prompt.workflow.json`. It has one step with agent `orchestrator` and one step with agent `implementer`. Each step asks the agent to quote the first heading of `AGENTS.md` and one rule from `.kiro/steering/document-authoring.md`. Do not commit the probe recipe.
    - Run the probe recipe.
    - Pass condition: both agents quote the heading and the rule correctly.
    - STOP GATE: if either agent cannot do this, stop. Do not continue to task 1.4. Report the blocking finding to the user with the two options in design section C1: keep a minimal neutral `prompt` file, or move the content to a different layer. Leave the edits in place for the user's decision.
    - Files: `.kiro/agents/orchestrator.json`, `.kiro/agents/implementer.json`, `.kiro/steering/document-authoring.md`, `.kiro/workflows/probe-default-prompt.workflow.json` (scratch)
    - _Requirements: 1.3, 1.4, 2.6, 3.2, 3.6, 12.5, 12.8_

  - [x] 1.4 F3: probe the round loop and the cross-slot write check
    - Write a scratch probe recipe `.kiro/workflows/probe-fanout.workflow.json`. It has a repeat with 2 iterations, a `parallel` node with 3 implementer slot branches and `joinPolicy` `allSettled`, and a reconcile step after the parallel node. Use the scratch `run_dir` `openspec/workflow-runs/probe/`.
    - Seed a plan file in the scratch `run_dir`. Make 2 of the 3 slots empty in round 1.
    - In one iteration, seed a cross-slot write: one slot reports a write to a file that another slot declares.
    - Run the probe recipe and confirm each item:
      - The empty slots end at once.
      - The reconcile step receives all three slot outputs.
      - A `fileCheck` on `done` stops the loop.
      - A `send_message` with severity `error` in a step fails the run.
      - The seeded cross-slot write makes the reconcile step pause with `send_message` severity `warning`.
    - Record what `allSettled` does when one branch fails. Report the result to the user in the task summary.
    - On a failure, adjust the recipe in `.kiro/workflows/opsx-apply.workflow.json`, then run task 1.2 again. If the loop cannot carry branch outputs to the reconcile step, stop and report to the user.
    - Files: `.kiro/workflows/probe-fanout.workflow.json` (scratch), files under `openspec/workflow-runs/probe/` (ignored)
    - _Requirements: 5.4, 5.5_

  - [x] 1.5 F4 (stop gate): probe the `wf-review-aggregator` write
    - Write a scratch probe recipe `.kiro/workflows/probe-adjudicate.workflow.json` with one `adjudicate` step that uses agent `wf-review-aggregator`. The step writes `{"verdict": "APPROVED"}` to `openspec/workflow-runs/probe/review-verdict.json`.
    - Run the probe recipe.
    - Pass condition: `openspec/workflow-runs/probe/review-verdict.json` exists and holds valid JSON.
    - STOP GATE: if the file does not exist or does not parse, stop. Do not continue to task 1.6. Ask the user to choose a fallback from design "Open items for the user" item 2: `stopWhen` on `{{adjudicate.output}}` in place of `fileCheck`, or an orchestrator step after `adjudicate` that writes the file from the aggregator's output. Each fallback needs a requirement change and the user's approval.
    - Files: `.kiro/workflows/probe-adjudicate.workflow.json` (scratch), `openspec/workflow-runs/probe/review-verdict.json` (ignored)
    - _Requirements: 9.5_

  - [x] 1.6 F5: probe the run directory location
    - As the orchestrator, write `openspec/workflow-runs/probe/x.json` with a file-write tool.
    - Run `openspec list --json`, `openspec validate --specs`, and `python3 openspec/tools/spec_ids.py`. Confirm that each tool ignores `openspec/workflow-runs/`.
    - Run `git check-ignore openspec/workflow-runs/probe/x.json`. Confirm that git ignores the path.
    - On a failure, choose another directory under `openspec/` with no leading dot. Update `.gitignore` and the `run_dir` convention, then repeat this task.
    - Files: `openspec/workflow-runs/probe/x.json` (ignored)
    - _Requirements: 9.7_

  - [x] 1.7 F6: probe the archive flow under the orchestrator config
    - Create a throwaway git worktree outside the repository with `git worktree add`. In the worktree, create a scratch change under `openspec/changes/`.
    - Run the `/openspec-archive-change` flow on the scratch change as the orchestrator agent. Use the tool's `cwd` parameter to run commands in the worktree. Do not prefix commands with `cd`.
    - Pass condition: the flow completes with `.kiro/agents/orchestrator.json` unchanged.
    - On a failure, report the operation that failed to the user. Use the operation table in design section C14.
    - Remove the worktree with `git worktree remove` when the check completes.
    - Files: none in the main working tree
    - _Requirements: 13.2, 13.3_

  - [x] 1.8 Remove the probe artifacts
    - Delete `.kiro/workflows/probe-default-prompt.workflow.json`, `.kiro/workflows/probe-fanout.workflow.json`, and `.kiro/workflows/probe-adjudicate.workflow.json`.
    - Delete the directory `openspec/workflow-runs/probe/`.
    - Report the F1 to F6 results to the user. Ask the user if questions arise before task 2 starts.
    - Files: the three probe recipes, `openspec/workflow-runs/probe/`
    - _Requirements: 9.16_

- [x] 2. Create the recovery point
  - Shell rule: never prefix a shell command with `cd`. Use the tool's `cwd` parameter if needed.

  - [x] 2.1 Create the tag `pre-wave-retirement`
    - Run `git tag -a pre-wave-retirement 37af994 -m "Recovery point before ADR-0021 replaces ADR-0004, ADR-0005, ADR-0007, and ADR-0016"`.
    - Confirm the tag with `git tag -l pre-wave-retirement` and `git rev-list -n 1 pre-wave-retirement`. The second command must print the full hash of `37af994`.
    - Do not push the tag. Pushing the tag is the user's decision.
    - Do this task before task 7.3 deletes any ADR.
    - Files: none (git ref only)
    - _Requirements: 8.11_

- [x] 3. Update the Kiro surfaces
  - Shell rule: never prefix a shell command with `cd`. Use the tool's `cwd` parameter if needed.

  - [x] 3.1 Write the recipe and its prompts
    - Replace the content of `.kiro/workflows/opsx-apply.workflow.json` with the skeleton in design section C5. The root `steps` are, in order: the `implement` step (agent `implementer`), the `quality-gate-loop` repeat, and the `full-gate` step (agent `orchestrator`).
    - `quality-gate-loop` has `maxIterations` 3, `onMaxIterations` `pause`, and `"stopWhen": "{{adjudicate.output}} contains VERDICT: APPROVED"`. It has no `stopCondition` and no `fileCheck`. Its body is, in order: `simplify` (agent `code-simplifier`), the `reviews` parallel node with `joinPolicy` `all` and exactly the branches `code-review` (agent `code-reviewer`), `security-review` (agent `security-reviewer`), and `test-gaps` (agent `tester`), then `adjudicate` (agent `wf-review-aggregator`, `artifacts` `{"verdict": "{{run_dir}}/review-verdict.json"}`), then `apply-fixes` (agent `implementer`).
    - The recipe has 8 `step` nodes. It has no plan file, no implementer slot, no planner or reconcile step, and no baseline file (Requirement 9.19).
    - Write each prompt as a short inline string from its contract in design section C5: P-IMPLEMENT, P-SIMPLIFY, P-REVIEW, P-SECURITY, P-TESTER, P-ADJUDICATE, P-FIX, and P-FULLGATE. Each prompt points at the `openspec-apply-change` skill, `AGENTS.md`, and the agent configs that it relies on, and restates none of them. No prompt references a `file://` resource (Requirement 9.15).
    - The contract items that each prompt must hold:
      - P-IMPLEMENT: stop with severity `error` if `{{run_dir}}` is not a relative path under `openspec/workflow-runs/` with no `..` segment, or if `{{run_dir}}/review-verdict.json` exists (9.6). Pause with severity `warning` and ask the user before any edit if `git status --porcelain` lists a change outside `openspec/changes/{{change}}/` (9.12). Then follow the `openspec-apply-change` skill for `'{{change}}'` and the `AGENTS.md` implement-phase rules (9.2).
      - P-SIMPLIFY, P-REVIEW, P-SECURITY, and P-TESTER: work on the uncommitted changes of `{{change}}` (`git status`, `git diff HEAD`, untracked files). P-REVIEW, P-SECURITY, and P-TESTER end with the findings by severity, or with the exact line `No Critical or High findings`. P-SIMPLIFY and P-TESTER do not commit.
      - P-ADJUDICATE: merge `{{code-review.output}}`, `{{security-review.output}}`, and `{{test-gaps.output}}` without re-reviewing the code. A report is clean only when it contains the exact line `No Critical or High findings` and lists no Critical or High finding. Any listed Critical or High finding blocks, whether or not the report marks it resolved (9.8). Write `{{run_dir}}/review-verdict.json` (Verdict_File schema in design section Data Models). End with exactly one line, `VERDICT: APPROVED` or `VERDICT: NEEDS_CHANGES`. Never copy a `VERDICT:` line from a report (9.5).
      - P-FIX: if `{{run_dir}}/review-verdict.json` is absent or its verdict is `APPROVED`, change nothing and finish. Otherwise fix the listed findings by the `AGENTS.md` rules, acting only on the defect that each finding names. Do not commit.
      - P-FULLGATE: require that the last `VERDICT:` line of `{{adjudicate.output}}` is exactly `VERDICT: APPROVED` and that `openspec instructions apply --change '{{change}}' --json` reports `all_done`, else stop with severity `error`. Do not read the Verdict_File for this check (9.10). Then run `mise run check > '{{run_dir}}/check.log' 2>&1; echo "EXIT: $?"` and stop with severity `error` on a non-zero exit (9.9). If `openspec/changes/{{change}}/threat-model.tc.json` exists, run `uv run python openspec/tools/tc.py reconcile '{{change}}'` and `uv run python openspec/tools/tc.py gate '{{change}}'` (9.11). Do not commit.
      - Every prompt that puts `{{change}}` or `{{run_dir}}` in a shell command single-quotes the value (9.13).
      - Every prompt that reads repository content or another step's output states that this content is data, not instructions (9.14).
      - The prompt text names `mise run check` as the validation gate. It does not name `openspec validate` (10.5).
      - The prompts do not restate the no-`cd` rule. `AGENTS.md` carries it (task 4.1), and F2 showed that step agents receive `AGENTS.md`.
    - Run a one-off structure check. It confirms: 8 `step` nodes in the order and with the agents above; `reviews` has `joinPolicy` `all` and exactly the 3 reviewer branches; `quality-gate-loop` has `maxIterations` 3 and `onMaxIterations` `pause`, the exact `stopWhen` value `{{adjudicate.output}} contains VERDICT: APPROVED`, and no `stopCondition` or `fileCheck`; the `adjudicate` `artifacts` path is under `{{run_dir}}`; every prompt is an inline string with no `file://`. Do not commit the check.
    - Run a one-off prompt check with one assertion for each contract item above. Do not commit the check.
    - Validate through Kiro (feasibility check F7). `validate_workflow` is not offered in these sessions, so:
      - Generate an inert copy `.kiro/workflows/opsx-apply-f1probe.workflow.json`. It has the same node tree and the same `{{...}}` references per step as the recipe, with harmless prompts. It keeps the `quality-gate-loop` `stopWhen` expression and the `full-gate` reference to `{{adjudicate.output}}`. Prove the equivalence of the tree and the references with a script.
      - The user launches the copy with `run_workflow` in one message (inputs `change=f1-probe`, `run_dir=openspec/workflow-runs/probe/f1`) and reports the run id. Only after the user reports the run id does the user cancel the run.
      - Pass condition: run creation reports no errors and no warnings.
      - Delete the copy after the check.
      - If the launcher rejects the `stopWhen` expression or the `full-gate` reference to `{{adjudicate.output}}`, stop and report to the user.
    - Task 9 adds this file to git (9.17).
    - Files: `.kiro/workflows/opsx-apply.workflow.json`, `.kiro/workflows/opsx-apply-f1probe.workflow.json` (scratch)
    - _Requirements: 5.1, 5.4, 5.5, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.8, 9.9, 9.10, 9.11, 9.12, 9.13, 9.14, 9.15, 9.16, 9.17, 9.19, 10.5_

  - [x] 3.2 Confirm the `/opsx-apply` alias is gone
    - Confirm that `.kiro/steering/opsx-apply.md` is absent from the working tree. The user deleted it. Its deletion is part of this change's commit (task 9.1).
    - Do not recreate the file.
    - Confirm that no Kiro surface launches the bare Apply_Skill as a gated apply. The single gated apply entry point is the `opsx-apply` workflow (design section C4, Apply_Entry_Point).
    - Files: `.kiro/steering/opsx-apply.md` (deleted)
    - _Requirements: 9.18_

  - [x] 3.3 Edit the orchestrator config
    - Confirm that task 1.3 removed the top-level `"prompt"` key.
    - Replace `"description"` with exactly this text from design section C1: "OpenSpec change orchestrator for Cairn Proofs. The main session for the opsx-* flows (explore, propose, apply, verify, archive). In the opsx-apply workflow it runs the full-gate step: it confirms the review verdict and the all_done task state, runs mise run check, and runs the threat-model reconcile and gate. Spawns the code-reviewer, security-reviewer, tester, code-simplifier, threat-model, and spec-writer specialists without a confirmation prompt."
    - Confirm that the working tree already removed `file://.kiro/skills/spec-simplifier/references/writing-recommendations.md` from `"resources"`, and that the other entries are unchanged.
    - Add no archive denial to this file.
    - Change no other key.
    - Files: `.kiro/agents/orchestrator.json`
    - _Requirements: 1.3, 1.5, 1.7, 1.9, 2.4, 2.5, 12.2, 13.2_

  - [x] 3.4 Edit the implementer config
    - Confirm that task 1.3 removed the top-level `"prompt"` key.
    - Replace `"description"` with exactly this text from design section C1: "Implements an OpenSpec change: follows the openspec-apply-change skill over the change's tasks.md, writes the production code and the tests that cover it, and runs those scoped tests to green. Runs as the implement and apply-fixes steps of the opsx-apply workflow, or when dispatched by the orchestrator. Never commits, never runs the full mise run check, and never spawns subagents. Triggers on "implement these tasks", "write the code for these tasks", "build out tasks N-M"."
    - Add `openspec/changes/archive/**` at the end of `toolsSettings.read.deniedPaths` and at the end of the `match` list of the existing `fs_read` deny rule in `permissions.rules`. Use the same form as the `.claude/**` entry.
    - Append `"pyproject.toml"`, `"uv.lock"`, and `"openspec/changes/*/tasks.md"` at the end of `toolsSettings.write.allowedPaths` and at the end of the `match` list of the existing `fs_write` allow rule in `permissions.rules`. Add no other new path.
    - Change no other key or entry.
    - Files: `.kiro/agents/implementer.json`
    - _Requirements: 1.4, 1.6, 1.8, 1.9, 2.1, 2.2, 2.3, 2.7, 13.1, 13.7_

  - [x] 3.5 Add the archive read denials to the other six agent configs
    - In each file, add `openspec/changes/archive/**` at the end of `toolsSettings.read.deniedPaths` and at the end of the `match` list of the existing `fs_read` deny rule in `permissions.rules`. Use the same form as the `.claude/**` entry.
    - Change no existing entry.
    - Keep the working-tree `spec-writer.json` changes (the removed `resources` entry and the new `description`). Do not revert them.
    - Confirm that each file still parses as JSON.
    - Files: `.kiro/agents/code-reviewer.json`, `.kiro/agents/code-simplifier.json`, `.kiro/agents/security-reviewer.json`, `.kiro/agents/spec-writer.json`, `.kiro/agents/tester.json`, `.kiro/agents/threat-model.json`
    - _Requirements: 12.2, 12.7, 13.1, 13.7_

  - [x] 3.6 Delete the two retired prompt files
    - Confirm that every non-blank, non-heading line of each file at `37af994` has its relocated text in place, as the Line_Mapping_Record in `design.md` states. The targets are `AGENTS.md` (task 4.1), `.kiro/steering/document-authoring.md` (task 1.3), and the recipe prompts (task 3.1). The graph runs task 4.1 before this task. Task 8.1 confirms the `AGENTS.md` targets again.
    - Delete `.kiro/agent-prompts/orchestrator.md` and `.kiro/agent-prompts/implementer.md`.
    - Files: `.kiro/agent-prompts/orchestrator.md`, `.kiro/agent-prompts/implementer.md`
    - _Requirements: 1.1, 1.2, 3.3, 3.4, 3.5, 3.7_

  - [x] 3.7 Remove the stale gate phrase from two agent prompts
    - In `.kiro/agent-prompts/tester.md`, replace line 34 with the exact text in design section C3. Keep the `mise run check` and `mise run semgrep` denial and the scoped-test discipline.
    - In `.kiro/agent-prompts/code-reviewer.md` line 9, replace the clause from "are the" through "either." with the exact text in design section C3: "are the aggregate gate, which runs once after implementation completes. Your config denies them to you, so do not attempt them, and do not re-run `uv run ruff`/`uv run pytest` either." Leave the rest of the bullet unchanged. The bullet then begins as design section C3 shows.
    - Files: `.kiro/agent-prompts/tester.md`, `.kiro/agent-prompts/code-reviewer.md`
    - _Requirements: 10.4, 11.3_

- [x] 4. Update the portable surfaces
  - Shell rule: never prefix a shell command with `cd`. Use the tool's `cwd` parameter if needed.

  - [x] 4.1 Edit `AGENTS.md`
    - Replace the whole "Code-change workflow" section with the exact text in design section "AGENTS.md text".
    - Confirm that phase 2 ends with the sentence "A harness without delegation runs this phase in a separate session from the one that runs the full gate and the commit." (Requirement 4.11).
    - Confirm that phase 3b ends with the sentence "A harness without concurrency runs them one after another." (Requirement 4.12).
    - Remove the bullet that starts "**The wave-status ledger**" from "Concurrency-safe ambient state".
    - In the `docs/adr/` bullet, replace the sentence "Append-only and immutable once Accepted; a later ADR supersedes an earlier one (`Status: Superseded by ADR-NNNN`) rather than editing it." with the replacement sentence in the design.
    - Add the archive sentences from the design to the `openspec/changes/` item of "Spec authoring model".
    - Add the two "Conventions" bullets from the design: no history in specs, docs, or code comments, and run shell commands from the repository root.
    - Use plain operating-manual prose. Use no `SHALL`, no `MUST`, no `#### Scenario:` block, and no Capitalized_Underscore term.
    - Files: `AGENTS.md`
    - _Requirements: 3.1, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 4.10, 4.11, 4.12, 8.8, 8.12, 10.1, 10.2, 10.3, 10.5, 13.4_

  - [x] 4.2 Rewrite the `cairn` schema instructions
    - In the `tasks` instruction of `openspec/schemas/cairn/schema.yaml`, keep the opening (checkbox format and the `- [ ] X.Y` rule). Delete the text from "After generating tasks, create a task dependency graph" through the example graph and its explanation. Insert the replacement bullets in design section C8.
    - In the `apply` instruction, replace the text from "The main session is the **orchestrator**" through the end of the aggregate-gate paragraph with the replacement text in design section C8. Remove the WHY paragraph about sub-agent spawning.
    - Keep the threat-model block from "Then, BEFORE committing" unchanged, except "Once the gate passes, commit." becomes "Once the gate passes, the change is ready to commit when the user asks." Keep the final "Pause and surface" line.
    - Keep the threat-model instruction and its Tc_Gate block unchanged.
    - Name no harness, no spawn primitive, no parallel-dispatch primitive, and no Kiro surface. Name `mise run check` as the validation gate, not `openspec validate`.
    - Files: `openspec/schemas/cairn/schema.yaml`
    - _Requirements: 5.1, 5.2, 6.1, 6.4, 6.5, 6.8, 6.10, 10.3, 10.5_

  - [x] 4.3 Rewrite the `bugfix` schema instructions
    - Make the `tasks` instruction edit of task 4.2 in `openspec/schemas/bugfix/schema.yaml`. Add the bugfix RED-order sentence from design section C8.
    - In the bugfix replacement bullets, the guideline "Ship each task's tests in the same task as the code they cover." becomes "Ship each task's tests in the same task as the code they cover, except the RED reproduction tests, which ship in Group 1 before the fix."
    - Group 4 (Spec Sync): "Run `openspec validate --specs --strict`." becomes "Run `mise run spec-validate`." (Requirement 6.12).
    - Group 5 (Verification): remove the whole Group 5 bullet, as design section C8 states. The instruction then has Groups 1 to 4 only and no smoke-suite task (Requirement 6.13).
    - Add the guideline from design section C8 after the guideline "There is no review or simplify pass during implementation. …": "No checklist task runs `mise run check`. The aggregate gate runs it once after every task is complete, and it confirms that the Group 1 RED tests now pass and the Group 3 regression guards still pass." (Requirement 6.11).
    - Change the `tasks` `description` from "Implementation checklist with wave DAG, structured around RED→GREEN→Regression test flow" to "Implementation checklist, ordered RED→GREEN→Regression".
    - Make the `apply` instruction edit of task 4.2. Add the RED rule sentence from design section C8 to the per-task paragraph.
    - Name no harness, no spawn primitive, no parallel-dispatch primitive, and no Kiro surface. Name `mise run check` as the validation gate, not `openspec validate`.
    - Files: `openspec/schemas/bugfix/schema.yaml`
    - _Requirements: 5.1, 5.2, 6.2, 6.3, 6.4, 6.8, 6.10, 6.11, 6.12, 6.13, 10.3, 10.5_

  - [x] 4.4 Rewrite both `tasks.md` templates
    - In each template, delete the `## Task Dependency Graph` block and every paragraph after it.
    - Add the HTML comment from design section C8 under the last group of each template.
    - In the bugfix template, change task 1.2 from "record observed failure mode in commit message or wave summary" to "record the observed failure mode in the task report".
    - In the bugfix template, task 4.2 becomes `- [ ] 4.2 <!-- Run \`mise run spec-validate\` -->` (Requirement 6.12).
    - In the bugfix template, remove the heading `## 5. Verification` and every task under it, including the `mise run check` task and the smoke-suite task (Requirement 6.11, 6.13). The template ends with Group 4, followed by the ordering HTML comment (design section C8).
    - Files: `openspec/schemas/cairn/templates/tasks.md`, `openspec/schemas/bugfix/templates/tasks.md`
    - _Requirements: 6.3, 6.6, 6.7, 6.11, 6.12, 6.13, 10.3_

  - [x] 4.5 Validate both schemas
    - Run `openspec schema validate cairn` and `openspec schema validate bugfix`. Each command must exit with status 0.
    - On a failure, fix the schema file and run the command again.
    - Files: none
    - _Requirements: 6.9_

- [x] 5. Remove the retired tooling and skill
  - Shell rule: never prefix a shell command with `cd`. Use the tool's `cwd` parameter if needed.

  - [x] 5.1 Delete the ledger tool and its tests
    - Delete `openspec/tools/wave_ledger.py` and `tests/unit/openspec_tools/test_wave_ledger.py`.
    - Keep `tests/unit/openspec_tools/test_tc.py` and `tests/unit/openspec_tools/test_tc_render.py`.
    - Keep the orchestrator shell allow pattern `uv run python openspec/tools/.*` in `.kiro/agents/orchestrator.json`.
    - Run `uv run pytest tests/unit/openspec_tools/`. It must pass.
    - Files: `openspec/tools/wave_ledger.py`, `tests/unit/openspec_tools/test_wave_ledger.py`
    - _Requirements: 7.1, 7.2, 7.4_

  - [x] 5.2 Confirm the `spec-simplifier` skill deletion
    - Run `git status --porcelain .kiro/skills/spec-simplifier/`. Confirm that it lists `SKILL.md`, `references/openspec-format.md`, and `references/writing-recommendations.md` as deleted.
    - Confirm that the directory `.kiro/skills/spec-simplifier/` is absent from the working tree.
    - Task 9 includes the three deletions in the commit.
    - Files: `.kiro/skills/spec-simplifier/SKILL.md`, `.kiro/skills/spec-simplifier/references/openspec-format.md`, `.kiro/skills/spec-simplifier/references/writing-recommendations.md`
    - _Requirements: 12.1_

- [x] 6. Update the reference docs and the bugfix surfaces
  - Shell rule: never prefix a shell command with `cd`. Use the tool's `cwd` parameter if needed.

  - [x] 6.1 Rewrite `openspec/README.md`
    - Make the edits in design section C10 with its exact text:
      - Line 43, the `/openspec-apply-change` row: "Implement the `tasks.md` checklist, run the aggregate review/test gate, reconcile+gate the threat model. On Kiro, the saved `opsx-apply` workflow runs this."
      - Line 54, the `apply` prose: replace "implements via the wave DAG at the bottom of `tasks.md` and runs a single post-wave simplify/review/test gate" with "implements the ordered `tasks.md` checklist and then runs the single simplify/review/test gate on the cumulative diff".
      - Line 63, the WHAT row: "Artifacts and their order, the ordered task checklist (the sequential floor), the single aggregate simplify/review/test gate, the threat-model reconcile/gate".
      - Line 64, the HOW row: the C10 text that names the saved `opsx-apply` workflow (implement, then the aggregate gate loop, then the full gate), says that Kiro generates a workflow from `tasks.md` for parallel implementation and that nothing in the repository schedules it, and says that `AGENTS.md` stays harness-agnostic. The location column is "each harness's own configuration (Kiro: `.kiro/workflows/`, `.kiro/steering/`, `.kiro/agents/`)". The "Per-harness?" cell is "**Yes** — each harness keeps its own execution detail; `AGENTS.md` stays harness-agnostic".
    - Replace the two prose paragraphs under the table, the one that starts "The load-bearing rule:" and the one that starts "Why this needs no branches:", with the exact text in design section C10. The "load-bearing rule" paragraph names, for Kiro, the saved `opsx-apply` workflow, Kiro-generated workflows for parallel implementation, the steering files, and the agent configs.
    - Confirm that the HOW row and the prose name no deleted orchestrator prompt file and no `loop_to` in an orchestrator definition.
    - If the file links to `docs/adr/0016-harness-agnostic-workflow-schemas.md`, change each link to `docs/adr/0021-checklist-floor-and-native-orchestration.md`.
    - Files: `openspec/README.md`
    - _Requirements: 8.8, 11.1, 11.8_

  - [x] 6.2 Rewrite the workflow section of `docs/summary/workflows.md`
    - Replace the section 7 prose and its Mermaid diagram with the text and the diagram in design section C10. The prose describes the saved `opsx-apply` workflow (one implement step over the `tasks.md` checklist, one simplify, review, and test gate loop on the uncommitted changes, then the full gate that runs `mise run check`) and says that Kiro generates a workflow from `tasks.md` for parallel implementation. The diagram shows the implement step, the gate loop with adjudication and fixes, the full gate, the commit, and the archive, with the Kiro-generated workflow as a dotted alternative path into the gate.
    - Files: `docs/summary/workflows.md`
    - _Requirements: 11.2_

  - [x] 6.3 Rewrite the bugfix skill
    - Make the line edits in design section C11 to lines 85 to 89, 110, 111, and 132, with its exact text:
      - Lines 85 to 87: "Group 1 (Bug Reproduction RED) comes first. Every Group 2 (Fix) task comes after every Group 1 task, so the RED tests exist and fail before the fix lands. Group 4 (Spec Sync) is dropped from the checklist if `bugfix.md`'s Spec Impact said None."
      - Line 88: "The checklist has no `mise run check` task; the aggregate gate runs it."
      - Line 89: "The checklist is in RED → fix → regression order and has no dependency-graph block."
      - Line 110: "Task count and the RED-before-fix ordering".
      - Line 111 prompt: the RED reproduction tasks run first, and if those tests pass against main, the workflow pauses so the user can revisit `bugfix.md`. This file names the saved `opsx-apply` workflow, not a `/opsx-apply` alias.
      - Line 132: the apply command implements the checklist generated here, and the RED tasks have the same special pause.
    - Remove every other reference to Group 5. The checklist has Groups 1 to 4 only (design section C8), so no line of the file names Group 5.
    - Files: `.kiro/skills/bugfix/SKILL.md`
    - _Requirements: 6.13, 11.4_

  - [x] 6.4 Rewrite the Claude bugfix command
    - Make the same line edits as task 6.3 (design section C11) to lines 85 to 89, 110, 111, and 132, with one difference: the line 111 prompt keeps `/opsx:apply`.
    - Remove every other reference to Group 5. The checklist has Groups 1 to 4 only (design section C8), so no line of the file names Group 5.
    - Change no other file under `.claude/`.
    - Files: `.claude/commands/bugfix.md`
    - _Requirements: 6.13, 10.6, 11.5_

  - [x] 6.5 Confirm the capture skill description
    - Confirm that the `description` of `.kiro/skills/capture/SKILL.md` in the working tree names the ASD-STE100 skill and does not name `spec-simplifier`.
    - Task 9 includes the file in the commit.
    - Files: `.kiro/skills/capture/SKILL.md`
    - _Requirements: 12.4_

- [x] 7. Replace the ADRs
  - Shell rule: never prefix a shell command with `cd`. Use the tool's `cwd` parameter if needed.

  - [x] 7.1 Write ADR-0021
    - Create `docs/adr/0021-checklist-floor-and-native-orchestration.md` from design section "ADR-0021 outline".
    - Use the fixed first 11 lines from the design. Set the Status date to the date the change is applied.
    - Write the sections Context, Decision, Retired approach, Consequences, Alternatives considered, and References, as the outline states:
      - Context: the evidence from the requirements Introduction in short form, and the harness evolution (Claude Code agent nesting, Kiro native DAG and loop primitives, and workflows that Kiro generates for the task at hand).
      - Decision: D1 to D17 from the Decision_Mapping_Record, each stated as this ADR's own decision. D17 states that on Kiro the saved `opsx-apply` recipe runs one implement step, the review-loop gate, and the full gate; that the gate verdict is read from the adjudicator's captured output, not from a file; and that parallel implementation comes from Kiro-generated workflows, with no scheduling machinery in the repository.
      - Retired approach: one short section that describes the wave model once, as a rejected direction. It states what the model was and why it ended, and that keeping concurrent tasks on different files is now the job of whichever harness runs tasks in parallel (D4, D14). It gives no guidance.
      - Consequences: the positive and negative items in the outline. The negative items are: the saved recipe implements in one step, so a large checklist runs in one session unless the user asks Kiro for a generated parallel workflow; a generated workflow is not reviewed in the repository before it runs, but Kiro shows its plan for approval and the same aggregate gate runs on its result; and the text of a replaced ADR leaves the working tree and stays only in git history.
      - Alternatives considered: the fixed recipe that schedules checklist tasks into parallel implementer slots through planner and reconcile steps, a plan file, and a baseline file, rejected because it rebuilt the retired task-management machinery and kept producing new edge cases. Also the per-harness branch, per-tool branching in the schema, translation by each harness, and per-wave review.
      - References: the list in the outline, with no Markdown link to a deleted file.
    - State that the old ADR text is in git history and not in the working tree. Do not name the tag or the commit.
    - Files: `docs/adr/0021-checklist-floor-and-native-orchestration.md`
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.7, 8.11_

  - [x] 7.2 Amend ADR-0001
    - Make the four edits in design section "ADR-0001 amendment text" to `docs/adr/0001-adopt-architecture-decision-records.md`: the Status section, the first Decision sentence, the immutability paragraph, and the first positive Consequences bullet.
    - Add the section "Why superseded ADRs are deleted" after the Decision section, with the exact text in the design.
    - Set the amendment date to the date the change is applied.
    - Make no other edit to the body of any accepted ADR. Leave `docs/adr/0006-artifact-genus-evidence-as-role.md` unchanged.
    - Files: `docs/adr/0001-adopt-architecture-decision-records.md`
    - _Requirements: 8.9, 8.10, 8.11, 8.13_

  - [x] 7.3 Delete the four replaced ADRs
    - Confirm that task 2.1 created the tag: `git tag -l pre-wave-retirement` prints the tag name.
    - Delete `docs/adr/0004-single-post-wave-review-gate.md`, `docs/adr/0005-wave-granularity-as-parallelism-unit.md`, `docs/adr/0007-cairn-test-scoping-and-template-forcing-functions.md`, and `docs/adr/0016-harness-agnostic-workflow-schemas.md`.
    - Add no stub, marker, redirect, or archive copy.
    - Files: the four ADR files
    - _Requirements: 8.6, 8.11_

  - [x] 7.4 Fix every live citation of the deleted ADRs
    - Search the tracked files: `git grep -n -i -E "ADR-0004|ADR-0005|ADR-0007|ADR-0016|0004-single-post-wave-review-gate|0005-wave-granularity-as-parallelism-unit|0007-cairn-test-scoping-and-template-forcing-functions|0016-harness-agnostic-workflow-schemas" -- . ':!.kiro/specs/harness-agnostic-workflow-migration' ':!openspec/changes/archive'`.
    - The one permitted match is the "Replaces ADR-0004, ADR-0005, ADR-0007, and ADR-0016 (deleted)" line in ADR-0021.
    - Change each other match in a live document to cite ADR-0021.
    - If a match is in the body of an accepted ADR other than ADR-0001 and ADR-0021, or in ADR-0006, do not edit it. Stop and report it to the user. Requirement 8.9 permits one ADR body edit only, and Requirement 8.13 keeps ADR-0006 unchanged.
    - Files: each file with a live citation
    - _Requirements: 4.7, 8.8_

- [x] 8. Verify the repository state and run the gates
  - These checks are one-off scripts and commands. They are not permanent tests. Do not commit the scripts.
  - Shell rule: never prefix a shell command with `cd`. Use the tool's `cwd` parameter if needed.

  - [x] 8.1 Write and run the one-off verification script
    - Write the script outside the repository, for example `/tmp/verify_migration.py`. Run it with `uv run python /tmp/verify_migration.py > /tmp/verify.log 2>&1; echo "EXIT: $?"`. Read `/tmp/verify.log` for the details.
    - The script follows design section Testing Strategy, "Repository-state checks". It checks:
      - File presence and absence for Requirement 1.1, 1.2, 7.1, 7.2, 8.6, and 12.1.
      - The agent configs, for Requirement 1.3 to 1.9, 2.1 to 2.5, 2.7, 12.2, 13.1, 13.2, and 13.7:
        - Each file in `.kiro/agents/` parses as JSON.
        - `orchestrator.json` and `implementer.json` have no top-level `"prompt"` key.
        - Each new `"description"` equals the C1 text, has at most 600 characters, and contains none of "wave", "task dependency graph", "cohort", or "ledger", matched case-insensitively.
        - Each guardrail key (`tools`, `allowedTools`, `toolsSettings`, `permissions` including the `subagent` rules, `resources`, and `shell`) is byte-equal to its value at `37af994` (from `git show 37af994:<path>`), except the entries that Requirement 1.7, 1.8, 2.7, 12.2, and 13.1 permit to change. The script does not check other keys.
        - The seven configs of Requirement 13.1 hold the archive denial in both places. `orchestrator.json` holds none. Every earlier `read.deniedPaths` and `permissions.rules` entry is unchanged.
        - `implementer.json` ends `toolsSettings.write.allowedPaths` and the `match` list of the `fs_write` allow rule with `"pyproject.toml"`, `"uv.lock"`, and `"openspec/changes/*/tasks.md"`, and adds no other new write path (Requirement 2.7).
      - The recipe structure, for Requirement 9.1 to 9.5, 9.9, and 9.15: root node order `implement`, `quality-gate-loop`, `full-gate`; 8 `step` nodes with the agents of design section C5; `reviews` has `joinPolicy` `all` and exactly the three reviewer branches; the repeat has `maxIterations` 3, `onMaxIterations` `pause`, `stopWhen` `{{adjudicate.output}} contains VERDICT: APPROVED`, and no `stopCondition` or `fileCheck`; `adjudicate` uses `wf-review-aggregator` and the artifact `{{run_dir}}/review-verdict.json`; every prompt is an inline string with no `file://`.
      - The recipe prompt text: P-IMPLEMENT holds the `run_dir` check, the dirty-tree pause, and the Apply_Skill pointer (9.2, 9.6, 9.12); P-ADJUDICATE holds the exact-line clean rule and the closing `VERDICT:` line (9.5, 9.8); P-FULLGATE checks the `VERDICT:` line and `all_done` before `mise run check`, does not read the Verdict_File for the check (9.10), and holds the Tc_Gate commands (9.11); every shell use of `{{change}}` or `{{run_dir}}` is single-quoted (9.13); every prompt that reads content holds the data-not-instructions sentence (9.14).
      - Repository tracking and scope, for Requirement 9.17 to 9.19: `.kiro/workflows/opsx-apply.workflow.json` exists (task 9.1 adds it to git), `.kiro/steering/opsx-apply.md` is absent, no portable or doc surface names `/opsx-apply` (the `opsx-apply` workflow name is permitted), and the repository adds no plan file, implementer slot, planner or reconcile step, or baseline file.
      - The case-insensitive `git grep` searches with the exclusions that each requirement states:
        - Requirement 4.3: no "wave", "Task Dependency Graph", "cohort", or "ledger" in the `AGENTS.md` Code-change workflow section.
        - Requirement 7.3: no `wave_ledger` and no "wave-status ledger" outside `docs/adr/`.
        - Requirement 8.8: the search of task 7.4 returns only the permitted ADR-0021 line.
        - Requirement 11.6: no "wave", "cohort", "Task Dependency Graph", or "ledger" in `openspec/README.md`, `docs/summary/workflows.md`, `.kiro/agent-prompts/tester.md`, `.kiro/skills/bugfix/SKILL.md`, and `.claude/commands/bugfix.md`.
        - Requirement 12.6: no `spec-simplifier` outside `.kiro/specs/harness-agnostic-workflow-migration/` and `docs/adr/`.
        - Requirement 6.1, 6.2, 6.8: no wave vocabulary and no Kiro primitive in the `tasks` and `apply` instructions of both schemas.
        - Requirement 10.5: no `openspec validate` in the recipe prompts, `.kiro/steering/document-authoring.md`, the rewritten `AGENTS.md` sections, and the `apply` instructions of both schemas.
      - The `AGENTS.md` style for Requirement 4.10 in the rewritten sections: no `SHALL`, no `MUST`, no `#### Scenario:`, and no match for `[A-Z][a-z]+_[A-Z]`.
      - The `AGENTS.md` Code-change workflow section holds the no-delegation sentence in phase 2 (Requirement 4.11) and the no-concurrency sentence in phase 3b (Requirement 4.12).
      - The first 12 lines of ADR-0021 hold the summary and the "Replaces ... (deleted)" line (Requirement 8.7). The new ADR-0001 section has at most 10 lines (Requirement 8.10).
      - The bugfix `tasks` instruction and the bugfix template: every RED task comes before every fix task (6.3), no checklist task runs `mise run check` (6.11), the spec-sync step names `mise run spec-validate` and not `openspec validate --specs --strict` (6.12), and there is no smoke-suite task and no Group 5, and the template ends with Group 4 (6.13).
      - `.kiro/skills/bugfix/SKILL.md` and `.claude/commands/bugfix.md` each contain the line "The checklist has no `mise run check` task; the aggregate gate runs it." and name no Group 5 (Requirement 11.4, 11.5).
      - The `openspec/README.md` HOW row "Per-harness?" cell and the two prose paragraphs under the table match design section C10, and name no deleted orchestrator prompt file and no `loop_to` in an orchestrator definition (Requirement 11.8).
      - The Line_Mapping_Record row count against the non-blank, non-heading line numbers of both retired prompts at `37af994` (Requirement 3.7), and the presence of each `AGENTS.md` target in the record.
      - `git diff --name-only 37af994` and the untracked files of this change against the allowed paths of Requirement 10.4. `DEVELOPING.md` and `mise.toml` are outside the allowed paths. Report them as unrelated edits that task 9 keeps out of the commit.
      - `git diff --quiet 37af994 -- .kiro/skills/openspec-apply-change/SKILL.md docs/adr/0006-artifact-genus-evidence-as-role.md` exits with status 0 (Requirement 5.3, 8.13).
    - On a failure, fix the file that the failed check names, then run the script again.
    - Files: `/tmp/verify_migration.py` (outside the repository)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 2.1, 2.2, 2.3, 2.4, 2.5, 2.7, 3.7, 4.3, 4.10, 4.11, 4.12, 5.3, 6.1, 6.2, 6.3, 6.8, 6.11, 6.12, 6.13, 7.1, 7.2, 7.3, 8.6, 8.7, 8.8, 8.10, 8.13, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.8, 9.9, 9.10, 9.11, 9.12, 9.13, 9.14, 9.15, 9.17, 9.18, 9.19, 10.4, 10.5, 11.4, 11.5, 11.6, 11.8, 12.1, 12.2, 12.6, 13.1, 13.2, 13.7_

  - [x] 8.2 Confirm F7 passed (task 3.1)
    - Confirm that task 3.1 recorded the F7 result: the user launched the inert copy, reported the run id, and then cancelled the run, and run creation reported no errors and no warnings.
    - Confirm that `.kiro/workflows/opsx-apply-f1probe.workflow.json` is absent.
    - If F7 did not pass, or the recipe changed after F7 ran, run F7 again as task 3.1 describes.
    - No review sub-agents run for this change.
    - Files: none
    - _Requirements: 9.16_

  - [x] 8.3 Run the schema and tool-test commands
    - Run `openspec schema validate cairn`. It must exit with status 0.
    - Run `openspec schema validate bugfix`. It must exit with status 0.
    - Run `uv run pytest tests/unit/openspec_tools/ > /tmp/openspec_tools.log 2>&1; echo "EXIT: $?"`. The exit code must be 0. Read `/tmp/openspec_tools.log` for the details.
    - Do not run `mise run check` for this change.
    - On a failure, fix the cause, then run the failed command again.
    - Files: none
    - _Requirements: 6.9, 7.5_

- [x] 9. Commit the change
  - The main session does this task, and only when the user asks.
  - Shell rule: never prefix a shell command with `cd`. Use the tool's `cwd` parameter if needed.

  - [x] 9.1 Commit with named-file pathspec commits
    - Run `git status --porcelain`. Note any pre-staged file that is not part of this change.
    - For each new file of this change, run `git add <named file>`. The new files include `.kiro/workflows/opsx-apply.workflow.json`, `.kiro/steering/document-authoring.md`, `docs/adr/0021-checklist-floor-and-native-orchestration.md`, and the files under `.kiro/specs/harness-agnostic-workflow-migration/`. Never run `git add -A` or `git add .`.
    - Commit with an explicit pathspec: `git commit <path> <path> ... -m "<message>"`. The pathspec lists every changed, deleted, and new path of this change, and no other path. A pathspec commit leaves other staged work out of the commit.
    - Include the deletion of `.kiro/steering/opsx-apply.md` in the commit that carries the Kiro surfaces. Either run `git rm .kiro/steering/opsx-apply.md` first, or list the path in that commit's pathspec.
    - Use a Conventional Commits message. Add no Co-Authored-By trailer.
    - Keep `DEVELOPING.md` and `mise.toml` out of this commit. Their ASD-STE100 install edits are unrelated to this change. Tell the user that those edits need a separate pathspec commit.
    - Confirm with `git show --stat HEAD` that the commit holds only the paths of this change.
    - Files: every path of this change
    - _Requirements: 9.17, 9.18, 10.4, 12.1, 12.7_

- [ ] 10. File the follow-up GitHub issues
  - Start this task only after every earlier task is complete.

  - [ ] 10.1 File the two issues with the capture workflow
    - Load the `capture` skill. File one issue for each item:
      - Convert ADR-0006 from "superseded in part" to the replace-and-delete rule of the amended ADR-0001.
      - All deferred Claude-side work: the remaining `.claude/` references to the retired vocabulary, the port of the Kiro HOW content and the `spec-writer` delegation policy to the Claude tree (after which the policy can move into `AGENTS.md`), and the `openspec/changes/archive/**` read denial for the `.claude/` agent definitions.
    - Write the issue text from the implemented result.
    - If the capture workflow runs shell commands, never prefix them with `cd`. Use the tool's `cwd` parameter if needed.
    - Report the two issue URLs to the user.
    - Files: none in the repository
    - _Requirements: 10.7, 13.6_

## Notes

- The stop gates are task 1.2 (F1), task 1.3 (F2), and task 1.5 (F4). When a stop gate fails, no later task starts until the user decides.
- This spec has no property-based tests. The design has no Correctness Properties section. Task 8 uses one-off scripted checks, the schema validations, and the scoped `openspec_tools` test run.
- No task is optional.
- The probe recipes and the probe run directory are scratch files. Task 1.8 deletes them. Git ignores `openspec/workflow-runs/`.
- Pushing the tag `pre-wave-retirement` is the user's decision.
- Some identifiers contain retired terms, for example `wave_ledger.py`, the four deleted ADR file names, and the tag name. The tasks name them only to delete, search for, or replace them.
- The Task Dependency Graph below is part of the Kiro spec format. This change retires dependency graphs only in OpenSpec `tasks.md` files under `openspec/`. It does not retire this graph.
- In the graph, no two tasks in the same wave edit the same file.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.3"] },
    { "id": 3, "tasks": ["1.4"] },
    { "id": 4, "tasks": ["1.5"] },
    { "id": 5, "tasks": ["1.6", "1.7"] },
    { "id": 6, "tasks": ["1.8"] },
    { "id": 7, "tasks": ["2.1", "3.1", "3.2", "3.3", "3.4", "3.5", "3.7", "4.1", "4.2", "4.3", "4.4", "5.1", "5.2", "6.1", "6.2", "6.3", "6.4", "6.5", "7.1", "7.2"] },
    { "id": 8, "tasks": ["3.6", "4.5", "7.3"] },
    { "id": 9, "tasks": ["7.4"] },
    { "id": 10, "tasks": ["8.1"] },
    { "id": 11, "tasks": ["8.2"] },
    { "id": 12, "tasks": ["8.3"] },
    { "id": 13, "tasks": ["9.1"] },
    { "id": 14, "tasks": ["10.1"] }
  ]
}
```

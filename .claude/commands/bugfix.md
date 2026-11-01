---
name: "Bugfix"
description: Start a Kiro-style bugfix change - defect/expected/unchanged behavior + RED→GREEN→regression tasks, all in one go
category: Workflow
tags: [workflow, artifacts, bugfix, experimental]
---

Start a bugfix change using the `bugfix` schema (Kiro-style). Generates `bugfix.md`, `design.md`, and `tasks.md` in one pass so the change is ready for `/opsx:apply`.

Use this instead of `/opsx:propose` when:
- You are fixing a defect (not adding capability)
- The spec is already correct OR needs at most a clarifying amendment
- You want explicit defect/expected/unchanged contracts and a RED→GREEN→regression test flow

If the change is mostly new capability and the bug is a side effect, use `/opsx:propose` instead — it carries the full proposal/specs/design/threat-model artifact chain.

---

**Input**: The argument after `/bugfix` is the change name (kebab-case), OR a description of the bug.

**Steps**

1. **If no input provided, ask what the bug is**

   Use the **AskUserQuestion tool** (open-ended, no preset options) to ask:
   > "What bug are we fixing? Describe the defect — what's happening, what should happen, and any reproduction details you have."

   From the description, derive a kebab-case name starting with `fix-` (e.g., "validate Lambda crashes on empty manifest" → `fix-validate-empty-manifest`).

   **IMPORTANT**: Do NOT proceed without understanding the bug. If reproduction steps or the wrong behavior are unclear, ask one focused follow-up before scaffolding — the bugfix.md is the contract for everything downstream and a vague defect statement contaminates the whole change.

2. **Create the change directory using the bugfix schema**
   ```bash
   openspec new change "<name>" --schema bugfix
   ```
   This scaffolds `openspec/changes/<name>/` with `.openspec.yaml` pointing at the `bugfix` schema.

3. **Get the artifact build order**
   ```bash
   openspec status --change "<name>" --json
   ```
   Parse the JSON to confirm:
   - `schemaName: "bugfix"`
   - `applyRequires`: should contain `["tasks"]`
   - `artifacts`: `bugfix-analysis`, `design`, `tasks` (in dependency order)

4. **Create artifacts in sequence until apply-ready**

   Use the **TodoWrite tool** to track progress through the three artifacts.

   For each artifact in dependency order (`bugfix-analysis` → `design` → `tasks`):

   a. Get instructions:
      ```bash
      openspec instructions <artifact-id> --change "<name>" --json
      ```

   b. The instructions JSON includes:
      - `context`: project background (constraints for YOU — do NOT include in output)
      - `rules`: artifact-specific rules (constraints for YOU — do NOT include in output)
      - `template`: the structure to use for the output file
      - `instruction`: schema-specific guidance for this artifact type
      - `outputPath`: where to write the artifact
      - `dependencies`: completed artifacts to read for context

   c. Read any completed dependency artifacts for context.

   d. Create the artifact file using `template` as the structure, filling in real content for the project. Apply `context` and `rules` as constraints — but do NOT copy them into the file.

   e. **Schema-specific quality bars** — before moving to the next artifact:

      - **bugfix-analysis (`bugfix.md`)**:
        - Every clause under "Current Behavior" / "Expected Behavior" / "Unchanged Behavior" uses the exact bolded keywords (**WHEN**, **THEN**, **SHALL**, **SHALL CONTINUE TO**).
        - One clause per bullet — no compounds joined with "and".
        - "Spec Impact" is filled in with one of the two stated forms (None vs. amendment needed).
        - If reproduction steps reference data you don't have (a specific fixture, a real customer ticket), ask the user before guessing.

      - **design (`design.md`)**:
        - Root Cause Analysis cites file paths and line numbers from the actual repo (verify with Read/Grep — don't invent line numbers).
        - "Test Properties (a) Bug Reproduction" properties each trace back to a specific Expected Behavior clause from `bugfix.md`.
        - "Test Properties (c) Regression Guard" properties each trace back to a specific Unchanged Behavior clause OR a named blast-radius caller.
        - "Spec Alignment" matches the decision from `bugfix.md`'s "Spec Impact" — if amendment is needed, create the delta spec file alongside `design.md` at `openspec/changes/<name>/specs/<capability>/spec.md`.

      - **tasks (`tasks.md`)**:
        - Group 1 (Bug Reproduction RED) comes first.
        - Every Group 2 (Fix) task comes after every Group 1 task, so the RED tests exist and fail before the fix lands.
        - Group 4 (Spec Sync) is dropped from the checklist if `bugfix.md`'s Spec Impact said None.
        - The checklist has no `mise run check` task; the aggregate gate runs it.
        - The checklist is in RED → fix → regression order and has no dependency-graph block.

   f. Re-run `openspec status --change "<name>" --json` after each artifact. Stop when every artifact in `applyRequires` has `status: "done"`.

5. **Validate before handoff**
   ```bash
   openspec validate "<name>" --strict 2>&1
   ```
   If validation fails, fix the artifact and re-validate before reporting completion.

6. **Show final status**
   ```bash
   openspec status --change "<name>"
   ```

**Output**

After completing all artifacts, summarize:
- Change name and location (`openspec/changes/<name>/`)
- Schema: `bugfix`
- Artifacts created: `bugfix.md`, `design.md`, `tasks.md` (and any spec deltas)
- Task count and the RED-before-fix ordering
- Prompt: "Run `/opsx:apply <name>` to start implementing. The RED reproduction tasks run first. If those tests pass against main, the workflow pauses so you can revisit `bugfix.md`."

**Artifact Creation Guidelines**

- Follow the `instruction` field from `openspec instructions` for each artifact — it carries schema-specific guidance the template comments don't.
- **bugfix.md is the contract.** Time spent making the WHEN/THEN clauses concrete pays back many times over in the design and test phases. Resist hand-waving here.
- **Cite real code, not plausible-looking code.** When writing `design.md`, use Read/Grep to confirm file paths and line numbers. Inventing `libs/foo/bar.py:142` when the file doesn't have that function makes the change unreviewable.
- **`context` and `rules` are constraints for YOU, not content for the file.** Do NOT copy `<context>`, `<rules>`, `<project_context>` blocks into the artifact.

**Guardrails**

- Create all three artifacts before reporting completion — partial bugfix scaffolds are worse than none because they bypass the RED-first discipline.
- Always read dependency artifacts before creating a new one (design reads bugfix; tasks reads both).
- If a change with that name already exists, ask whether the user wants to continue it (`/opsx:continue <name>`) or pick a new name.
- If reproduction is genuinely unknown (the bug was reported as "it sometimes fails"), say so in `bugfix.md`'s Reproduction section and add a Group 0 task: "Capture a reliable repro before writing RED tests." Do not invent reproduction steps.
- If during `design.md` the root cause turns out to require a refactor larger than the bug warrants, stop and surface this — the right move is usually to ship a minimal surgical fix here and file a follow-up change for the refactor, not to expand scope.
- Verify each artifact file exists after writing before proceeding to the next.

**Relationship to other opsx commands**

- `/opsx:explore` — use this first if the bug is poorly understood; come back to `/bugfix` once you can articulate Current vs. Expected behavior.
- `/opsx:apply` — implements the checklist generated here. The RED tasks have a special pause: if the failing tests pass against main, the run stops and surfaces to the user instead of advancing to the fix tasks.
- `/opsx:archive` — same as for any change; runs spec sync if a delta was authored.

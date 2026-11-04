---
inclusion: always
name: feedback-capture-full-output-to-file
description: "Run a slow or expensive command once with its full output kept. Never pipe the first run through tail/head/grep and then re-run it."
---

Run a slow or expensive command once, and keep its full output. This applies to `mise run check`, a full `pytest` run, builds, `deploy.sh`, and CDK synth. Do not pipe the first run through `tail`, `head`, or `grep`:

```
mise run check
```

The harness keeps the full output and reports the exit code. Short output comes back inline. Output over the harness limit is saved to the session's `tool-outputs/` directory, and the result names the saved file. Read that file, or `grep` it, for each part you need: summary lines, failures, a single subtask. Reading the saved output again costs nothing. Running the command again costs a full run.

**Why:** A pipe keeps only the slice it passes on, and the harness sees nothing else. For example, `mise run check | tail -40` shows the end of the test output but drops the ruff, spec-validate, and cdk subtask results. It also hides the command's exit code, because the exit code reported is `tail`'s. On `correlate-pipeline-traces`, this made `mise run check` run three times (about 20 s each) to collect what one plain run would have kept. The same thing happened with scoped pytest runs.

**How to apply:**
- Run the command without a pipe, then read the parts you need from the inline result or the saved file.
- Pipe-and-discard (`| tail`) only when you are certain that one slice is all you will ever need from that run.
- Redirect to a file only when the harness truncates output without saving it. Write the log inside the workspace, for example `openspec/workflow-runs/<change>/<run>/check.log`, and capture the exit code on the same line: `cmd > <log> 2>&1; echo "EXIT: $?"`. Paths outside the workspace can raise a read-approval prompt that blocks unattended workflow steps.

Related: [[feedback_openspec_json_piping]] covers the narrower question of which `openspec --json` outputs to filter and which to show raw.

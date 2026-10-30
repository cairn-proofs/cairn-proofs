---
inclusion: always
name: feedback-capture-full-output-to-file
description: "For long/expensive commands, redirect full output + exit code to a file in ONE run, then inspect the file — never page-then-rerun."
---

For any slow or expensive command whose output you might need to inspect more than one slice of — `mise run check`, full `pytest`, builds, `deploy.sh`, CDK synth — redirect the FULL output and capture the exit code in a single invocation, then read the file as many times as needed:

```
mise run check > /tmp/check.log 2>&1; echo "EXIT: $?"
```

Then use Read / `grep` against `/tmp/check.log` for whatever slices you need (summary lines, failures, a specific subtask). Re-inspecting the file is free; re-running the command is not.

**Why:** Piping an expensive command's first run through `tail`/`head`/`grep` discards the parts you'll discover you need (e.g. `mise run check | tail -40` shows the test tail but cuts the ruff/spec-validate/cdk subtask results and the exit code), forcing a second and third full re-run. On `correlate-pipeline-traces` this happened repeatedly — `mise run check` ran 3× (~20s each) to assemble what one redirect would have given. Same anti-pattern on scoped pytest runs.

**How to apply:** Default to `cmd > /tmp/<name>.log 2>&1; echo "EXIT: $?"` for anything that takes more than a few seconds and whose output you may need to examine in parts. Capture the exit code in that same line (don't re-run just to learn pass/fail). Only pipe-and-discard (`| tail`) when you are certain a single tail slice is all you will ever need from that run. Relates to [[feedback_openspec_json_piping]] (which governs the narrower case of which `openspec --json` outputs to filter vs. show raw).

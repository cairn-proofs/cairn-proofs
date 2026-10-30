---
inclusion: always
name: feedback_commit_via_pathspec_when_index_dirty
description: "When unrelated files are already staged, commit via pathspec (git commit <paths>) — a bare git commit bundles them"
---

A bare `git commit` commits everything in the index, so named `git add` of only your files is NOT enough when the working tree already has *other* pre-staged changes (e.g. a graduation `git mv` for a different OpenSpec change left staged from before the session). Both my commits this session wrongly bundled a pre-staged rename for a different OpenSpec change until I switched to a pathspec commit.

**Why:** project rule is one logical commit per change unit, named-file add (never `git add -A`). The index can carry unrelated staged work that a bare commit silently sweeps in.

**How to apply:** when `git status` shows pre-staged files outside your change, commit with an explicit pathspec — `git commit <file1> <file2> ... -m "..."` — which commits ONLY those paths and leaves the rest staged as-is. If you already bundled, `git reset --soft HEAD~1` then re-commit via pathspec. Watch for staged `git mv`s from an OpenSpec change graduation, which often sit in the index across sessions.

---
inclusion: fileMatch
fileMatchPattern: ["**/*.sh", "lambda-layers/**"]
name: feedback-macos-bash-portability
description: "Build scripts in this project must work on macOS's default bash 3.2 — avoid bash 4+ features like `mapfile`, associative arrays, `${var,,}` case conversion."
---

Build scripts intended to run on contributor machines (e.g., `lambda-layers/*/build.sh`) must be compatible with macOS's default `/bin/bash` (3.2.x — Apple frozen at the last GPLv2 release). Bash 4+ features fail with `command not found` or syntax errors on a stock macOS even with `#!/usr/bin/env bash` (which finds `/bin/bash` first unless the contributor has Homebrew bash explicitly earlier on PATH).

Features to avoid:
- `mapfile` / `readarray` — use `while IFS= read -r line; do arr+=("$line"); done < <(cmd)` instead
- Associative arrays `declare -A` — use parallel indexed arrays or external lookups
- `${var,,}` / `${var^^}` case conversion — use `tr` instead
- `&>` redirection — use `>file 2>&1`
- `[[ $var =~ regex ]]` with `BASH_REMATCH` — works in 3.2 but capture groups behave subtly differently

**Why:** Caught when `mapfile -t key_fprs` failed in `lambda-layers/notation/build.sh` on the dev machine. Requiring contributors to install Homebrew bash and edit their PATH is high friction; portability wins.

**How to apply:** Any new `*.sh` script that contributors run locally. CI scripts can assume bash 4+ if the CI image bundles it, but err toward portability anyway — moves freely between local and CI.

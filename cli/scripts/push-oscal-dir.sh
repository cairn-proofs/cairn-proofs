#!/usr/bin/env bash
# Push every OSCAL bundle in a directory to Cairn Proofs.
#
# Walks DIRECTORY for files ending in `oscal.json` and runs `cairn push` once
# per file. PROJECT_ID is forwarded as the required `--project-id` switch and
# any EXTRA_ARGS are forwarded verbatim to each push; the matched file is
# appended as the final `files` positional. Extra args are `cairn push` flags
# (e.g. --assessment-period 2026-q2, --artifact-kind catalog). `--artifact-kind` is an
# Artifact_Kind (catalog, profile, system-security-plan, …); it defaults to
# `other` when omitted. "evidence" is NOT a kind — whether a bundle is
# addressed as evidence vs. reference is derived server-side from whether it
# carries a control-scope/assessment-period, not from an artifact-kind value.
#
# Usage:
#   push-oscal-dir.sh [--depth N] DIRECTORY PROJECT_ID [EXTRA_ARGS...]
#
# --depth N bounds how deep `find` descends (find -maxdepth). Defaults to 1
# (top level only); use 0 for unbounded recursion.
#
# Examples:
#   push-oscal-dir.sh ./bundles acme-corp
#   push-oscal-dir.sh --depth 0 ./bundles acme-corp
#   push-oscal-dir.sh ./bundles acme-corp --assessment-period 2026-q2 --artifact-kind catalog
#
# Override the CLI entrypoint with CAIRN (default: `cairn` on PATH), e.g.
# `CAIRN="uv run cairn"` to run from the workspace without an install.
#
# bash 3.2-safe (macOS default shell): no mapfile / associative arrays.

set -euo pipefail

usage() {
  echo "usage: $(basename "$0") [--depth N] DIRECTORY PROJECT_ID [EXTRA_ARGS...]" >&2
}

depth=1
while [ "$#" -gt 0 ]; do
  case "$1" in
    --depth)
      [ "$#" -ge 2 ] || { echo "error: --depth requires a value" >&2; usage; exit 2; }
      depth="$2"
      shift 2
      ;;
    --depth=*)
      depth="${1#--depth=}"
      shift
      ;;
    --)
      shift
      break
      ;;
    *)
      break
      ;;
  esac
done

case "$depth" in
  ''|*[!0-9]*)
    echo "error: --depth must be a non-negative integer: $depth" >&2
    exit 2
    ;;
esac

if [ "$#" -lt 2 ]; then
  usage
  exit 2
fi

directory="$1"
project_id="$2"
shift 2
# Remaining "$@" are the EXTRA_ARGS forwarded to every `cairn push`.

if [ ! -d "$directory" ]; then
  echo "error: not a directory: $directory" >&2
  exit 1
fi

cairn_cmd="${CAIRN:-cairn}"

pushed=0
failed=0

# -print0 / read -d '' keeps paths with spaces or newlines intact.
while IFS= read -r -d '' file; do
  echo ">> cairn push --project-id $project_id $* $file"
  if $cairn_cmd push --project-id "$project_id" "$@" "$file"; then
    pushed=$((pushed + 1))
  else
    echo "error: push failed for $file" >&2
    failed=$((failed + 1))
  fi
done < <(
  if [ "$depth" -eq 0 ]; then
    find "$directory" -type f -name '*oscal.json' -print0
  else
    find "$directory" -maxdepth "$depth" -type f -name '*oscal.json' -print0
  fi
)

echo "done: $pushed pushed, $failed failed"
[ "$failed" -eq 0 ]

#!/usr/bin/env bash
#
# Bootstraps a private checkout of this repo's threat model into
# ./threat-models. Generic: this script names nothing about where the store
# actually is, so it's safe to keep in a public repo. The store's URL comes
# from the environment -- see internal onboarding for its value.
#
# Each product's threat model lives on its own branch in the store, named
# after the product, with the model file(s) at that branch's root -- so a
# plain single-branch clone gives a flat ./threat-models directory, no
# nested subdirectory.
#
# Usage (from this repo's root):
#   THREAT_MODEL_STORE_URL=<url> scripts/setup-threat-model.sh
set -euo pipefail

if [ -z "${THREAT_MODEL_STORE_URL:-}" ]; then
    echo "THREAT_MODEL_STORE_URL is not set. See internal onboarding for the value." >&2
    exit 1
fi

TARGET_DIR="threat-models"
# Defaults to this repo's own name, which is also the convention for the
# matching branch name in the store. Override with THREAT_MODEL_BRANCH if
# this repo's branch there is named differently.
BRANCH="${THREAT_MODEL_BRANCH:-$(basename "$(git rev-parse --show-toplevel)")}"

if [ -d "$TARGET_DIR/.git" ]; then
    echo "Already set up at ./${TARGET_DIR} (branch: ${BRANCH})" >&2
    exit 0
fi

echo "Cloning branch '${BRANCH}' into ./${TARGET_DIR}" >&2
if ! git clone --branch "$BRANCH" --single-branch "$THREAT_MODEL_STORE_URL" "$TARGET_DIR"; then
    echo "error: branch '${BRANCH}' not found in the store." >&2
    echo "Create it there first (see that repo's README), or set" >&2
    echo "THREAT_MODEL_BRANCH if this repo's branch there is named differently." >&2
    exit 1
fi

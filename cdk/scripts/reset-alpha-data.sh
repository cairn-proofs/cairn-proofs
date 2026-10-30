#!/usr/bin/env bash
#
# reset-alpha-data.sh — DESTRUCTIVE dev/alpha-only data wipe.
#
# Deletes EVERY image from the evidence ECR repo, empties both DynamoDB
# tables (Metadata_Index + Controls), and purges all five pipeline DLQs in the
# pinned alpha account/region. The repo, tables, and queues themselves are kept
# (they are CDK-managed) — only their contents are removed. Purging the DLQs is
# part of a true reset: otherwise stale pre-reset failures (referencing the
# now-deleted digests) linger and trip DLQ-depth alarms / confuse triage.
#
# NOT for production. Object-Lock / WORM is disabled in alpha (WORM_Enabled=false),
# so ECR images are deletable; in a WORM-enabled deployment archived objects are
# retention-locked and this script does NOT touch S3.
#
# Usage:
#   ./cdk/scripts/reset-alpha-data.sh          # interactive confirmation
#   ./cdk/scripts/reset-alpha-data.sh --yes    # skip the prompt (CI/scripted)
#
set -euo pipefail

# Expected account is operator-asserted via the mise-loaded root .env
# (CAIRN_ALPHA_ACCOUNT) — NOT derived from the caller's own credentials
# (aws sts get-caller-identity / CDK_DEFAULT_ACCOUNT), which would degrade the
# guard below to a tautology (caller == caller, always true). Fail fast if it
# is unset/empty so the destructive wipe can never run against an unasserted
# target.
ACCOUNT_ID="${CAIRN_ALPHA_ACCOUNT:-}"
if [ -z "$ACCOUNT_ID" ]; then
  echo "ERROR: CAIRN_ALPHA_ACCOUNT is unset or empty — refusing to run a destructive reset" >&2
  echo "       without an operator-asserted expected account. Set it in the root .env" >&2
  echo "       (see .env.example) and ensure mise has loaded it." >&2
  exit 1
fi
REGION="us-east-2"
ECR_REPO="cairnproofs4af-artifact-cmh-alpha/mvp-tenant"
TABLES="cairnproofs4af-ArtifactMetadata-cmh-alpha cairnproofs4af-Controls-cmh-alpha"
DLQS="cairnproofs4af-validate-bundle-dlq-cmh-alpha cairnproofs4af-sign-bundle-dlq-cmh-alpha cairnproofs4af-archive-to-s3-dlq-cmh-alpha cairnproofs4af-update-metadata-dlq-cmh-alpha cairnproofs4af-mint-tag-dlq-cmh-alpha"

# ── Guard: only ever run against the pinned alpha account ──────────────────
caller_account="$(aws sts get-caller-identity --query Account --output text)"
if [ "$caller_account" != "$ACCOUNT_ID" ]; then
  echo "ERROR: caller account $caller_account != pinned alpha account $ACCOUNT_ID — refusing." >&2
  exit 1
fi

echo "About to PERMANENTLY DELETE in account $ACCOUNT_ID / $REGION:"
echo "  • all images in ECR repo: $ECR_REPO"
for t in $TABLES; do echo "  • all items in DynamoDB table: $t"; done
for q in $DLQS; do echo "  • all messages in DLQ: $q"; done
echo

if [ "${1:-}" != "--yes" ]; then
  printf "Type 'DELETE' to proceed: "
  read -r confirm
  if [ "$confirm" != "DELETE" ]; then
    echo "Aborted." >&2
    exit 1
  fi
fi

# ── ECR: batch-delete all images (100 imageIds per call) ───────────────────
#
# Manifest lists / OCI image indexes (and referrer artifacts such as Notation
# signatures) reference child manifests. ECR refuses to delete a child while a
# parent manifest list still points at it (ImageReferencedByManifestList). So we
# delete iteratively: each pass removes the now-unreferenced parents, freeing
# their children for the next pass. A no-progress guard prevents an infinite
# loop if something is genuinely undeletable.
echo "==> Emptying ECR repo $ECR_REPO ..."
REGION="$REGION" ECR_REPO="$ECR_REPO" python3 - <<'PY'
import json, os, subprocess, sys

region = os.environ["REGION"]
repo = os.environ["ECR_REPO"]


def aws_json(*args):
    out = subprocess.run(
        ["aws", "ecr", *args, "--region", region,
         "--repository-name", repo, "--output", "json"],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out) if out.strip() else {}


def list_digests():
    # No --max-items/--no-paginate: the CLI auto-paginates and aggregates every
    # imageId, so one call returns the full repo contents.
    resp = aws_json("list-images")
    return [img["imageDigest"] for img in resp.get("imageIds", [])]


prev = -1
while True:
    digests = list_digests()
    n = len(digests)
    if n == 0:
        print("    repo is empty.")
        break
    if n == prev:
        print(f"ERROR: {n} image(s) remain but a full delete pass made no progress.",
              file=sys.stderr)
        print("       Remaining digests: " + ", ".join(digests), file=sys.stderr)
        sys.exit(1)
    print(f"    {n} image(s) remain; deleting (parents first, children freed next pass)...")
    for i in range(0, n, 100):
        ids = [{"imageDigest": d} for d in digests[i:i + 100]]
        aws_json("batch-delete-image", "--image-ids", json.dumps(ids))
    prev = n
PY

# ── DynamoDB: scan keys + batch-delete (25/req, retry UnprocessedItems) ────
for table in $TABLES; do
  echo "==> Emptying DynamoDB table $table ..."
  REGION="$REGION" TABLE="$table" python3 - <<'PY'
import json, os, subprocess, sys

region = os.environ["REGION"]
table = os.environ["TABLE"]


def aws(*args, payload=None):
    return json.loads(
        subprocess.run(
            ["aws", "dynamodb", *args, "--region", region, "--output", "json"]
            + (payload or []),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )


def scan_keys():
    keys, start = [], None
    while True:
        args = ["scan", "--table-name", table,
                "--projection-expression", "pk,sk", "--no-paginate"]
        if start:
            args += ["--exclusive-start-key", json.dumps(start)]
        page = aws(*args)
        keys.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            return keys


keys = scan_keys()
print(f"    {len(keys)} item(s) to delete.")
for i in range(0, len(keys), 25):
    chunk = keys[i:i + 25]
    request = {table: [{"DeleteRequest": {"Key": k}} for k in chunk]}
    # Retry UnprocessedItems (throttling / partial batches).
    for _ in range(8):
        resp = aws("batch-write-item", payload=["--request-items", json.dumps(request)])
        unprocessed = resp.get("UnprocessedItems") or {}
        if not unprocessed:
            break
        request = unprocessed
    else:
        print(f"    WARNING: gave up with unprocessed items at offset {i}", file=sys.stderr)
        sys.exit(1)
print("    done.")
PY
done

# ── SQS: purge the pipeline DLQs ───────────────────────────────────────────
#
# PurgeQueue deletes all messages in one call; it is asynchronous (can take up
# to 60s to fully drain) and may be invoked at most once per 60s per queue. A
# missing queue is warned-and-skipped rather than aborting the whole reset.
echo "==> Purging DLQs ..."
for q in $DLQS; do
  url="$(aws sqs get-queue-url --queue-name "$q" --region "$REGION" --query QueueUrl --output text 2>/dev/null || true)"
  if [ -z "$url" ] || [ "$url" = "None" ]; then
    echo "    WARNING: DLQ $q not found — skipping." >&2
    continue
  fi
  aws sqs purge-queue --queue-url "$url" --region "$REGION"
  echo "    purged $q (async; drains within ~60s)."
done

echo
echo "Reset complete: ECR repo $ECR_REPO emptied, tables [$TABLES] emptied, DLQs purged."

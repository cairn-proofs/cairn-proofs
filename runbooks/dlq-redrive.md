# Runbook: redrive / triage the pipeline Lambda DLQs

Four pipeline Lambdas run **asynchronously**: `validate-bundle`, `sign-bundle`, `archive-to-s3`, and `mint-tag`. The `{appName}-ecr-push-cmh-alpha` EventBridge rule triggers `validate-bundle` (ECR `PutImage` via CloudTrail). `artifact.*` events on the custom bus trigger the rest. Each Lambda has an SQS DLQ (`cairn-<fn>-dlq-cmh-alpha`) as its **async-invoke** dead-letter target.

These are async-invoke DLQs, not SQS event-source DLQs. There is **no one-click "redrive to source"**. To reprocess, re-invoke the function with each DLQ message's body. The body *is* the original event the handler expects.

## When to use

- DLQ depth climbing / a `DeadLetterErrors` alarm fired.
- After you fix a bug that caused a Lambda to fail (e.g. a packaging/import error). Reprocess the events that piled up while it was broken.

## 1. Diagnose first — what's actually failing

Each DLQ message carries an `ErrorMessage` message-attribute (the function error) and, in its body, the original event (incl. `detail.requestParameters.repositoryName`).

```bash
Q=https://sqs.us-east-2.amazonaws.com/123456789012/{appName}-validate-bundle-dlq-cmh-alpha
R=us-east-2
aws sqs get-queue-attributes --queue-url "$Q" --region "$R" \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
# sample error strings + repos without consuming (visibility 0 → reappears):
aws sqs receive-message --queue-url "$Q" --region "$R" --max-number-of-messages 10 \
  --visibility-timeout 0 --message-attribute-names All \
  --query "Messages[].MessageAttributes.ErrorMessage.StringValue"
```

Classify before acting:
- **Transient / now-fixed failures** (e.g. `ImportModuleError`, throttling) → redrive.
- **Permanent validation failures** (`N permanent validation failure(s)`, `Config_Blob invalid`) → the bundle was genuinely invalid. Do **not** redrive blindly.
- **Dead test repos** (`RepositoryNotFoundException` for `…/int-*` repos) → stale integration-test residue. Delete it.
- **Self-trigger events** (`missing required field(s): ['imageDigest']`) → the pipeline's own signature/index/tag pushes re-triggering validate (the config.mediaType self-trigger class) → not redrivable as bundles.

## 2. Redrive (re-invoke the function per message)

Deletes a message only on a clean `StatusCode 200` / no `FunctionError`, so anything that re-fails stays safely in the DLQ. Skips dead `int-*` test repos.

```bash
#!/usr/bin/env bash
set -euo pipefail
Q=https://sqs.us-east-2.amazonaws.com/123456789012/{appName}-validate-bundle-dlq-cmh-alpha
FN={appName}-validate-bundle-cmh-alpha
R=us-east-2
LIMIT=${1:-1}          # verify with 1; pass a big N to drain
done=0
while (( done < LIMIT )); do
  msg=$(aws sqs receive-message --queue-url "$Q" --region "$R" \
        --max-number-of-messages 1 --visibility-timeout 30 \
        --query 'Messages[0]' --output json)
  [[ "$msg" == "null" || -z "$msg" ]] && { echo "queue drained"; break; }
  rh=$(jq -r '.ReceiptHandle' <<<"$msg")
  body=$(jq -r '.Body' <<<"$msg")
  repo=$(jq -r '.detail.requestParameters.repositoryName // "?"' <<<"$body")
  if [[ "$repo" == */int-* ]]; then echo "SKIP stale: $repo"; continue; fi
  printf '%s' "$body" > /tmp/ev.json
  out=$(aws lambda invoke --function-name "$FN" --region "$R" \
        --payload fileb:///tmp/ev.json /tmp/out.json \
        --query '{S:StatusCode,E:FunctionError}' --output json)
  if [[ "$(jq -r '.E // "none"' <<<"$out")" == "none" && "$(jq -r '.S' <<<"$out")" == "200" ]]; then
    aws sqs delete-message --queue-url "$Q" --region "$R" --receipt-handle "$rh"
    echo "OK   redriven+deleted: $repo"
  else
    echo "FAIL left in DLQ: $repo  ($out)  $(cat /tmp/out.json)"
  fi
  ((done++))
done
```

Always run with `LIMIT=1` first and confirm `OK redriven+deleted` before draining.

## 3. Delete only the unambiguously-stale (dead test repos)

```bash
# loop receive (visibility 300 so the run terminates), delete messages whose
# target repo matches /int- ; KEEP everything else untouched.
# ... (same receive loop; delete-message when repo == */int-* )
```

Never broaden the delete criterion beyond a provable stale signal (dead `int-*` repo). Deleting "everything except project X" risks destroying legitimate failed-validation events.

## 4. Hard reset (stop-the-bleed) — when the DLQ is actively growing

If depth climbs on its own (e.g. the validate self-trigger loop), reprocessing just feeds it. Reset instead:

```bash
aws events disable-rule --name {appName}-ecr-push-cmh-alpha --event-bus-name default --region us-east-2
aws sqs purge-queue   --queue-url "$Q" --region us-east-2   # irreversible; ~60s to drain
aws events enable-rule  --name {appName}-ecr-push-cmh-alpha --event-bus-name default --region us-east-2
# then re-push cleanly: bash cli/scripts/push-oscal-dir.sh <args>
```

`purge-queue` destroys all DLQ messages. Only do this when the events are reproducible (e.g. a bulk push you can re-run). The artifacts remain in ECR by digest.

## Reference incident (2026-06-01)

`validate-bundle` crash-looped with `Runtime.ImportModuleError: No module named 'cairn_metadata'`. Its `pyproject.toml` imported `cairn_metadata` but never declared it. The bundle therefore shipped without it (masked in dev by the editable workspace install). Every ECR push DLQ'd. Fix: declare `cairn-metadata` in `lambdas/validate_bundle/pyproject.toml`, rebuild, and redeploy. Then reset (§4) and re-push. To guard against recurrence, import-check a built bundle in isolation. The editable install hides undeclared-but-imported workspace deps.

# Runbook — The dedup/drift digest pin

**You are probably here because** `mise run integ` failed on
`tests/integration/test_dedup_and_drift.py::test_observable_outputs_pinned_against_drift`
with a `DRIFT:` assertion — *"stable fixture produced content Manifest_Digest
`<NEW>`, pinned expectation is `<OLD>`"*. This runbook explains what that pin is.
It explains why the pin trips, how to tell an intended change from a regression,
and exactly how to re-pin.

## 30-second decision

Did you (or a change you are building on) intentionally change how a bundle is
**built**? Look at its OCI manifest, the `Config_Blob`, the layer set, the media
types, and the manifest annotation keys. A wire-identity rename like
`cairnsecurity` → `cairnproofs` counts. Adding a *claim* field does NOT — claims
live in the referrer, see below.

- **Yes** → the pin is stale. Re-pin (steps below). Normal maintenance.
- **No** → STOP. This is real, unintended drift — a regression in bundle
  identity. Do NOT bump the pin to silence it. Find what changed the manifest.

## What the pin is

`tests/integration/fixtures/dedup_drift_fixture.expected-digest.txt` holds the
EXACT content `Manifest_Digest` that `cairn push` emits for the fixed-bytes
fixture `dedup_drift_fixture.json`, pushed under the default project
`mvp-tenant`. The drift test pushes that fixture and asserts the emitted digest
equals the pin. It is a **tripwire**: a known input must always produce the same
bundle identity. If it does not, something changed it. You should be made to
decide whether that was on purpose.

## The mental model people get wrong

The content `Manifest_Digest` is `sha256` of the OCI **manifest** that
`cairn push` assembles ([cli/cairn_cli/push.py](../cli/cairn_cli/push.py)). It is
a pure function of:

1. the **content layer bytes** (the fixture),
2. the **Config_Blob** = `{projectId}` — content-descriptive identity
   (it carries no `bundleType`),
3. the **manifest annotations** — `project-id`, a PINNED `created` (epoch 0),
   and `source-tool` (the same change removed the `artifact-kind` annotation —
   kind is claim-only now),
4. the **code constants** baked into 2–3 — the media types
   (`application/vnd.cairnproofs.artifact.config.v1+json`) and the annotation
   keys (`dev.cairnproofs.artifact.*`).

It is **NOT** a function of:

- the **deployment** — account, region, ECR repo, DynamoDB, and S3 appear
  nowhere in the manifest. **Wiping the repo/DB does not change it. A fresh
  deployment does not change it.** Same code + same content + same project ⇒ same
  digest, anywhere. (This is why you commit the pin once, and do not regenerate
  it per environment.)
- the **wall clock** — `created` stays at `1970-01-01T00:00:00Z` on purpose
  (otherwise `oras` would fold push-time `now()` into the manifest).
- the producer **claim** — artifact-kind, controls, catalog/profile UUID,
  assessment period, assessor, assessment id, supersedes, and account id. These
  live in the separately-attached **Producer_Attestation_Referrer**, a different
  OCI object with its own digest. They are NOT in the content manifest.

Two things people (including the author of this runbook) get wrong:

1. **"Deterministic" ≠ "constant forever."** It means *same inputs AND same
   manifest-building code ⇒ same digest*. Change the code that builds the
   manifest (e.g. a media-type / annotation-key rename) and the digest
   deterministically becomes a NEW value. That is a code change moving an
   identity — not flakiness, and not a deployment effect. The clincher: the
   `cmh-alpha` deployment produced one digest in June and a different one later
   with the *same* deployment — only the code changed.
2. **`projectId` IS in the digest.** It is a `Config_Blob` field AND a manifest
   annotation. The digest is therefore **project-dependent**, which is why we
   scope the pin to `mvp-tenant`. The producer *claim* fields — now including
   `artifactKind` — live in the referrer. Only `projectId` stays in the content
   manifest as identity (the content manifest carries no `bundleType` and no
   `artifact-kind` annotation). (The spec historically said otherwise for
   `projectId` — see "Spec reconciliation".)

## When the docs disagree, look at a real bundle

This is how the `projectId` question above was actually settled — do this, not a
code read, when documentation contradicts itself:

```bash
REPO=cairnproofs4af-artifact-cmh-alpha/mvp-tenant

# 1. find a CONTENT manifest (its artifactType is the cairnproofs config type):
aws ecr describe-images --repository-name "$REPO" --region us-east-2 \
  --query "imageDetails[?artifactMediaType=='application/vnd.cairnproofs.artifact.config.v1+json'].imageDigest"

# 2. fetch its manifest — annotations are inline:
aws ecr batch-get-image --repository-name "$REPO" --region us-east-2 \
  --image-ids imageDigest=<DIGEST> --query 'images[0].imageManifest' --output text | python3 -m json.tool

# 3. download its Config_Blob (the manifest's config.digest):
URL=$(aws ecr get-download-url-for-layer --repository-name "$REPO" --region us-east-2 \
  --layer-digest <CONFIG_DIGEST> --query downloadUrl --output text)
curl -s "$URL" | python3 -m json.tool
```

A real `mvp-tenant` content manifest shows `{"projectId":"mvp-tenant"}` in the
Config_Blob and `dev.cairnproofs.artifact.project-id` in the annotations — both
hashed into the digest. (The Config_Blob carries no `bundleType` and the
manifest no `artifact-kind` annotation. Only the Producer_Attestation_Referrer
`artifactKind` claim carries kind, so it does not appear here.)

## Why this is an integration test at all (and the part that is NOT)

`cairn push` computes the digest **client-side** before anything reaches the
pipeline. The exact-digest pin therefore does not need a deployment to *confirm*
it — only (today) to *observe* it. It rides in the integration suite for a historical
reason: capturing the expected value required a live run. The *other* two things
the same test pins ARE genuinely live — the **archive S3 key shape** and the
**metadata projection shape**. Only the real pipeline produces those two shapes.
The `sk == digest` match shows that the pipeline preserved the identity the
client computed. So one client-side identity pin rides alongside two real
end-to-end assertions. Canonical lifecycle: the sequence diagram in
[docs/architecture-as-deployed.md](../docs/architecture-as-deployed.md).

## How to re-pin (once you are sure you INTENDED the change)

1. Run the drift test against a real deployment: `mise run integ` (or target just
   this test).
2. Read the new digest from the failure message: `DRIFT: stable fixture produced
   content Manifest_Digest sha256:<NEW>, pinned expectation is sha256:<OLD>`.
3. Run it a **second time** and confirm the same `<NEW>`. The convention is two
   agreeing runs before arming. The fixture is deterministic, so they should
   match. If they do not, something folds per-push state into the manifest, which
   is a bug, not a re-pin.
4. Replace the digest line in
   `tests/integration/fixtures/dedup_drift_fixture.expected-digest.txt`.
5. Add a `History:` entry in that file: date, prior value, and a one-line WHY
   (which intended change moved it).
6. Commit the re-pin **in the same change** that made the identity change, with
   the justification in the message. The pin moving is a reviewable event — you
   are formally accepting a new bundle identity.

## When NOT to re-pin

If you did not intend to change bundle identity, the failure is a real
regression: something changed the manifest. Look for a new or renamed annotation,
a Config_Blob field, a media type, a layer-ordering change, or a non-pinned
`created`. Re-pinning would silently bless it. Diff what touched
`libs/oci/cairn_oci/` (`config_blob`, `annotations`, `categories`) or
`cli/cairn_cli/push.py`. Then decide whether the identity change is acceptable —
that is the whole point of the tripwire.

## Spec reconciliation

Only the Producer_Attestation_Referrer `artifactKind` claim carries
artifact-kind. The content manifest does not carry it (no Config_Blob
`bundleType`, no `dev.cairnproofs.artifact.artifact-kind` annotation). This
matches the `artifact-ingestion` spec's "kind is a referrer claim, not part of
the content digest." `controls`, `catalogOrProfileUuid`, `assessmentPeriod`,
`assessor`, `assessmentId`, `supersedes`, and `accountId` are likewise
referrer-only / not in the content digest. The one remaining content-manifest
identity field is `projectId`, which stays in the Config_Blob and the digest
deliberately (Req 318). The spec historically described `projectId` as
referrer-only. Reconciling that wording to the built behavior is a
Requirement-level decision that goes through the OpenSpec flow, not this runbook.

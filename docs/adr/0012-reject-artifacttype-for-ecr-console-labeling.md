# ADR-0012: Reject OCI manifest `artifactType` for ECR-console evidence-bundle labeling

## Status

Accepted (2026-06-05)

> **Clarifying note — artifact-kind carriers.** The content manifest does not carry artifact-kind, so kind does not ride in the content `Manifest_Digest`. Two things carry kind: the canonical ECR tag's leading segment (the `artifact-ingestion` tag grammar) and, authoritatively, the Attestation_Referrer's signed `artifactKind` claim (`producer-claims`). The Context and Alternatives sections below list an earlier carrier arrangement that no longer holds: a Config_Blob `bundleType` field and a `dev.cairnproofs.artifact.artifact-kind` (`ARTIFACT_KIND`) content-manifest annotation. This does **not** change this ADR's decision (Cairn still sets no top-level `artifactType` for ECR-console labeling). The Revisit Trigger still holds: a per-kind `artifactMediaType` value would derive from `normalize_artifact_kind` on the claimed kind.

## Context

Cairn pushes each Evidence_Bundle as a standard OCI image manifest (`application/vnd.oci.image.manifest.v1+json`). The manifest has a custom Config_Blob media type (`application/vnd.cairnsecurity.artifact.config.v1+json`) and no top-level `artifactType` field (`libs/oci/cairn_oci/manifest.py`). A deferred proposal would set a per-kind top-level `artifactType` on the manifest. Its aim was to make the **ECR console's "Artifact type" column** label bundles by kind (assessment-results, catalog, profile, …), instead of showing every bundle as "Other". That label would let an operator distinguish bundle kinds at a glance without inspecting the manifest.

We tested the operator-facing premise empirically against live ECR (`artifact-cmh-alpha/mvp-tenant`, `us-east-2`). The test used manual `oras push --artifact-type application/vnd.cairnsecurity.artifact.assessment-results.v1` pushes. We cross-checked the results against the AWS docs (`AmazonECR/latest/APIReference/API_ImageDetail`, `userguide/image-info`). Two independent facts emerged, and they point in opposite directions.

**1. ECR's API/CLI *does* honor a custom top-level `artifactType` — even over a real, non-empty Config_Blob.** `DescribeImages` returns an `artifactMediaType` field. Its value is the manifest's `artifactType` if present, else a fallback to `config.mediaType`. Measured:

```
artifact (oras push)                config.mediaType        DescribeImages artifactMediaType
smoke-real-cfg   (artifactType set) cairn config (real)  →  application/vnd.cairnsecurity.artifact.assessment-results.v1
smoke-empty-cfg  (artifactType set) oci.empty            →  application/vnd.cairnsecurity.artifact.assessment-results.v1
catalog (today, no artifactType)    cairn config (real)  →  application/vnd.cairnsecurity.artifact.config.v1+json   (fallback)
pipeline referrers (sig/resource/worm) oci.empty         →  their artifactType
```

This **disproves** the hypothesis that `artifactType` requires an empty config (the OCI 1.1 artifact-manifest shape). Therefore **no manifest-shape conflict exists, and no conflict with the validate self-trigger discriminator exists**. That discriminator keys on `config.mediaType` (`classify_pipeline_internal`, `libs/oci/cairn_oci/config_blob.py`). `artifactType` is an independent field, and `config.mediaType` stays untouched.

**2. The ECR *console* never renders custom vendor media types.** For both `smoke-real-cfg` and `smoke-empty-cfg`, the console showed **"Other"** in two places (user-verified): the repository **list** column and the artifact **detail** "Artifact media type" field. This was true although `DescribeImages` returned the custom value. The console gives friendly labels only to types that AWS recognizes (Image, Index, Helm, signatures, SBOMs, …). A custom `application/vnd.cairnsecurity.*` value renders "Other", regardless of how the manifest carries it.

Fact 2 defeats the user story exactly as written — *"distinguish kinds at a glance **in the ECR console**"*. The console is precisely the surface that cannot display the value. Fact 1 means that the only residual benefit of setting `artifactType` is at the API level: `DescribeImages.artifactMediaType` would carry a per-kind value. Today every bundle reports the identical `…config.v1+json`.

That residual benefit is marginal. Any API consumer can **already** get the artifact-kind in three other ways:

1. the leading segment of the canonical image tag (`{artifact-kind}--_ref--…`, `libs/oci/cairn_oci/categories.py`),
2. the Config_Blob `bundleType`, and
3. the `ARTIFACT_KIND` manifest annotation.

`artifactType` would add only ECR's *native* `artifactMediaType` filter field. No current consumer needs that convenience.

## Decision

Do **not** set a top-level `artifactType` on Evidence_Bundle manifests for the purpose of ECR-console labeling. The backlog item is closed WON'T DO and deleted from the inbox (its full evidence lives in git history and in this ADR).

This is a technology evaluation that changes **no** Requirement. The manifest's `artifactType` is below the Requirement layer. Bundle identity remains the Manifest_Digest, and the kind vocabulary in `ARTIFACT_KINDS` does not change. Therefore this ADR records it, rather than an opsx change, per the AGENTS.md ADR-vs-opsx boundary.

## Consequences

- The Evidence_Bundle manifest keeps its current shape: image manifest + real Config_Blob in `config`, no top-level `artifactType`. `DescribeImages` continues to report `artifactMediaType = …config.v1+json` for every bundle.
- The ECR console "Artifact type"/"Artifact media type" surfaces continue to show "Other" for evidence bundles. Operators distinguish kinds via the **Image tag** column (kind is the canonical tag's leading segment). They can also use `cairn tui`, which classifies artifacts directly and shows more detail.
- This decision introduced or risked no conflict. ECR honors `artifactType` independently of `config.mediaType`, so this decision is a "don't bother," not a "can't". The option remains cleanly available if a future driver appears.
- No spec deltas. The validate self-trigger discriminator is unaffected.

## Revisit Triggers

Reconsider only if **API-level kind filtering on ECR's native `artifactMediaType` field** becomes wanted (e.g. tooling or a runbook that must `describe-images --query` by kind without parsing the tag/annotation). If so, the successor is a clean, conflict-free one-kwarg change. Thread `--artifact-type` through `cli/cairn_cli/_oras.py:push()` with a per-kind value `application/vnd.cairnsecurity.artifact.{canonical-kind}.v1`. Derive the value from the already-canonical `normalize_artifact_kind(bundle_type)`. It changes no Requirement, so it would be a superseding ADR (`Status: Superseded by ADR-NNNN`), not an opsx change. The exception is if it is paired with a Requirement change for some other reason.

Do **not** revisit on the original (console-labeling) premise. The ECR console limitation is a fixed AWS behavior, and no manifest change can fix it.

## Alternatives Considered

- **Per-kind top-level `artifactType` (`…artifact.{kind}.v1`).** Rejected for the console goal (console shows "Other"). Take this path *only* under the Revisit Trigger above.
- **Single constant `artifactType` (`…artifact.bundle.v1`).** Rejected outright. Even without the console limitation, one constant renders identically for every bundle. So it never *distinguishes* kinds, and it fails the user story on its own terms.
- **Empty-config OCI 1.1 artifact manifest** (move Config_Blob out of `config`). Rejected. It is unnecessary, because Fact 1 shows that a real config carries `artifactType` fine. It is also actively harmful: the validate trigger classifies an empty config as `OCI_EMPTY` and skips it, so the bundle bypasses validation/signing entirely.
- **Status quo — no `artifactType`.** Accepted. The image tag, `bundleType`, and the `ARTIFACT_KIND` annotation already carry the kind.

## References

- The deferred proposal to set a per-kind top-level `artifactType` on the manifest — the originating capture that this ADR rejects (tracked as future work).
- `libs/oci/cairn_oci/manifest.py` — `OciManifest` (no `artifactType` field).
- `libs/oci/cairn_oci/config_blob.py` — `CONFIG_BLOB_MEDIA_TYPE`, `classify_pipeline_internal` (the `config.mediaType`-keyed discriminator), pipeline referrer `artifactType` constants.
- `libs/oci/cairn_oci/categories.py` — `ARTIFACT_KINDS`, `normalize_artifact_kind` (the canonical kind vocabulary from which a per-kind value would derive).
- AWS docs: `AmazonECR/latest/APIReference/API_ImageDetail` (`artifactMediaType`, `imageManifestMediaType`); `userguide/image-info` (console "Artifact media type" detail field).
- `/opsx:explore` session, 2026-06-05 (this evaluation and the live-ECR spike).

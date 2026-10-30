# ADR-0011: Defer Lambda packaging consolidation; keep per-function pre-built bundles

## Status

Accepted (2026-06-04)

## Context

`cdk/scripts/build-lambdas.sh` bundles each pipeline Lambda into its own `Code.fromAsset` directory under `lambda-bundles/<name>/`. The question: would one shared artifact cut build duplication? The candidates are a common Lambda layer, or a single container image with handler dispatch. Any candidate must not regress cold-start, breach size limits, or violate the no-Docker-on-the-build-path convention.

The duplicated bulk is uniform third-party dependencies. That uniformity is the single most important fact here:

```
Present in ALL 5 bundles (~38 MB of each ~46 MB bundle):
  botocore .............. 24.0 MB   ← the elephant
  pydantic_core .......... 4.2 MB
  aws_lambda_powertools .. 3.9 MB
  opentelemetry .......... 2.7 MB
  google ................. 2.4 MB
  pydantic ............... 1.9 MB
  (+ boto3, requests, urllib3, certifi, wrapt, idna, packaging…)

Only in validate_evidence (+~2 MB): dns, oscal_bindings, cairn_validation, cairn_oscal
```

`botocore` alone is >50% of every bundle. The build ships it **deliberately** rather than use the runtime's boto3. A comment in `build-lambdas.sh` records why. The Lambda runtime's boto3 lags the ECR OCI Referrers API surface (`list_image_referrers`) that sign / archive / mint depend on. So the bundle carries a pinned `boto3>=1.34` + `botocore`. That one decision drives the duplication. Therefore the highest-leverage consolidation is not "all libs into a layer" but "stop shipping `botocore` 5×."

**Sizing is not the constraint, and this changes the whole question.** The obvious worry is the 250 MB unzipped limit (zip + layers). Each bundle is ~46–48 MB unzipped. The Notation layer is 8.7 MB zipped (~22 MB unzipped) and attaches only to `sign_evidence`. The worst case for a shared-common-layer layout is sign_evidence: common layer + notation layer + thin function code. That case is ~60 MB unzipped, well under 250 MB. The container option's limit is 10 GB. **Neither option is anywhere near a size wall.**

So limits do not force consolidation. It is a discretionary build-cost / simplicity play. The real decision axes are build time, deploy coupling, and toolchain/convention fit. They are *not* size or cold-start. Both are ≈ neutral: total bytes loaded is similar whether deps live in `/opt` via a layer or `/var/task` via a bundle.

**What duplication actually costs today.** CDK asset hashing already dedups *uploads*. CDK does not re-push an unchanged bundle, so duplication does not cost deploy bandwidth on no-op deploys. The felt costs are:

1. local build time — 5× `uv pip install` full resolves on every `deploy.sh`, and
2. ~230 MB of local bundle dirs (cheap).

The win of consolidation is essentially "one layer build instead of 5 botocore installs."

### The two options

**Option A — shared common-deps layer.** Move the uniform core (boto3/botocore, powertools, opentelemetry, pydantic, pydantic_core, google, requests, urllib3, certifi, …) into one layer attached to all 5 functions. Each function bundle shrinks to its handler + the `libs/` it imports + function-specific deps (validate keeps the OSCAL stack). `uv` builds the layer once. **No container runtime is necessary, and the build stays PATH-only.** It is fully compatible with the existing pre-build convention. Cost: a layer version becomes a blast radius. A bump to a common dep republishes the layer and rolls all 5 functions atomically. For a 5-function pipeline this is acceptable, arguably desirable.

**Option B — single container image + handler dispatch.** One image, with the handler selected via `CMD`/env. The 10 GB image limit removes any size concern. The objection is *operational, not doctrinal*: a container build reintroduces a mandatory container runtime on the deploy path.

Option B is feasible. `CDK_DOCKER=finch cdk deploy` makes Finch (Apache-2.0, free at any scale) a true drop-in for the bundling step. Image assets land in CDK bootstrap's dedicated `cdk-hnb659fds-container-assets-*` ECR repo, **never** the `artifact-<qualifier>/*` evidence repos. So a separate repo, not a container ban, satisfies "don't pollute the evidence registry". But Finch is still a VM-backed runtime (a Lima VM on macOS), not a `mise`-pinnable PATH binary. Any contributor or CI runner without the VM running could no longer deploy. **Option A achieves the same dedup with zero new runtime dependency**, so B is heavier than the problem needs.

### On the no-Docker convention

AGENTS.md's "stack assets via local pre-build, not Docker bundling" conflates two concerns under one absolute rule.

1. Its **core is sound**: keep a mandatory container runtime off the routine deploy path. Then `mise install` remains the whole setup story, and every contributor/CI runner can deploy with PATH tools alone.
2. Its **letter is mis-scoped**: it names a product and an era (Docker Desktop, with org-size licensing) that the project has already avoided. Finch removes the licensing concern entirely.

Container-image Lambdas are not doctrinally forbidden. They are simply the wrong tool for *this* problem, because they re-add a runtime that a pure-`uv` layer makes unnecessary. A more accurate phrasing of the convention is "no mandatory container-runtime dependency on the common build path." This ADR records that clarification, so that a future reader does not cite "no Docker" as a blanket ban on container assets.

The technical reason the convention is viable at all: for a Python Lambda, the one problem that Docker bundling normally solves is native wheels. `pydantic_core` ships platform-specific binaries. An install on a macOS host gives `No module named 'pydantic_core._pydantic_core'`. `build-lambdas.sh` avoids the problem with `uv pip install --python-platform aarch64-manylinux_2_17 --python-version 3.12`. That command selects the manylinux/arm64 wheels cross-platform without a container.

## Decision

Defer Lambda packaging consolidation. Keep the current per-function pre-built bundles (`lambda-bundles/<name>/`, one `Code.fromAsset` each).

If/when build-time pain becomes felt (see Revisit Triggers), pursue **Option A in its narrow form** as the preferred path. That form is a single common-deps layer that targets the uniform botocore + pydantic + powertools + opentelemetry core. It dedups the actual mass, stays a pure-`uv` PATH-only build, and adds no container runtime.

**Option B is not rejected on principle** (Finch + a separate ECR repo make it feasible), but it is not preferred. It reintroduces a mandatory VM-backed runtime to solve a problem that a layer already solves. Pursue B only if some *other* driver appears (a dep that cannot be cross-installed, or a >250 MB closure).

This decision changes no Requirement, because packaging shape is below the Requirement layer. Therefore it is an ADR rather than an opsx change (per the AGENTS.md ADR-vs-opsx boundary).

## Consequences

- The 5 pipeline Lambdas keep independent, hermetic, asset-hashed bundles. Each one has its own version and upload. No shared-layer blast radius exists today.
- `botocore` (24 MB) continues to ship in all 5 bundles. This is a deliberate, recorded cost (pinned boto3 for the ECR Referrers API surface), not an accident. Its consolidation is the main prize whenever the project takes up Option A.
- `deploy.sh` continues to run 5× `uv pip install`. Build time scales linearly with function count. This cost grew when `mint_tag` made the count 5, and it is the signal to watch.
- The no-Docker convention stands, but this ADR clarifies its scope. It bars a *mandatory* container runtime on the common build path, not container assets as a category. A future one-line sharpening of the AGENTS.md wording is a nicety, not required by this ADR.
- No spec deltas: neither the packaging shape nor the deferral touches a capability Requirement.

## Revisit Triggers

Reconsider when any of the following becomes true:

1. **`deploy.sh` build time becomes a felt pain** — it measurably slows the inner deploy loop or CI. This is the load-bearing trigger and points directly at Option A (one layer build replaces 5 botocore installs).
2. **Function count grows further** (a 6th+ pipeline Lambda). Each addition multiplies the duplicated botocore install. The linear cost that Option A collapses gets larger with every function.
3. **A bundle approaches the 250 MB unzipped limit** — e.g. validate's OSCAL stack grows, or a new heavy dep lands in all functions. Sizing is a non-issue today (~46 MB). If that stops being true, a layer becomes a necessity rather than a convenience.
4. **A driver for container images appears independently of consolidation** — a dependency that cannot be cross-installed with `uv --python-platform`, or a deployment closure that exceeds the 250 MB zip+layers limit but fits the 10 GB image limit. Only then does Option B's cost (a mandatory container runtime) buy something a layer cannot.

If a trigger fires, the successor work is an opsx change only if it touches a Requirement (it likely does not). Otherwise it is a superseding ADR (`Status: Superseded by ADR-NNNN`).

## Alternatives Considered

- **A. Shared common-deps layer.** Deferred, not rejected. It is the *preferred* path when a trigger fires. It dedups the real mass (botocore + common core) with a pure-`uv` PATH-only build and no container runtime. Its cost is layer-version blast radius across the 5 functions, which is acceptable at this scale.
- **B. Single container image + handler dispatch.** Deferred and not preferred. It is feasible via `CDK_DOCKER=finch` + the CDK bootstrap container-assets ECR repo, which keeps images out of the evidence registry. But it reintroduces a mandatory VM-backed runtime to solve a problem that Option A solves with none. Pursue only under trigger 4.
- **Status quo — per-function pre-built bundles.** Accepted. Sizing is not a constraint, and asset hashing already dedups uploads. The only growing cost (5× `uv` install) is not yet painful.

## References

- `cdk/scripts/build-lambdas.sh` — the per-function bundling, the deliberate `botocore` retention, and the `uv --python-platform` cross-install that makes the no-Docker path viable.
- `cdk/lib/constructs/lambda-functions.ts` — the 5 `Code.fromAsset` functions, the Notation layer (sign only), and the external ADOT collector layer.
- `lambda-layers/notation/build.sh` — the layer that the consolidation must coexist with on `sign_evidence`.
- ADR-0002 — retains the Notation layer. That layer is the source of the `notation`-binary dependency that, like this ADR's analysis, keeps the toolchain non-trivial.
- AGENTS.md — "Stack assets via local pre-build, not Docker bundling" and the ADR-vs-opsx boundary.
- `/opsx:explore` session, 2026-06-04 (this evaluation).

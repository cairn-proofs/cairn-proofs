# Runbook — Notation Lambda Layer

Build, version-pinning, and CDK-consumption notes for the Lambda layer that ships the [Notation CLI](https://notaryproject.dev/) and the [AWS Signer notation plugin](https://docs.aws.amazon.com/signer/latest/developerguide/notation-aws-signer-plugin.html) to the `sign-bundle` Lambda.

## What the layer contains

| File | Source | Purpose |
|---|---|---|
| `bin/notation` | GitHub Releases — `notaryproject/notation` | CLI binary. Lambda Runtime adds `/opt/bin` to PATH, so `notation` is invocable as `notation` from the Lambda handler. |
| `notation/plugins/com.amazonaws.signer.notation.plugin/notation-com.amazonaws.signer.notation.plugin` | AWS CloudFront — `d2hvyiie56hcat.cloudfront.net` | Notation plugin that delegates signing to AWS Signer. Loaded by `notation` when `XDG_CONFIG_HOME=/opt` is set. |
| `notation/plugins/com.amazonaws.signer.notation.plugin/LICENSE` | Plugin zip | Plugin license. |
| `notation/plugins/com.amazonaws.signer.notation.plugin/THIRD_PARTY_LICENSES` | Plugin zip | Plugin third-party attributions. |

Architecture: `arm64 / linux`. Both binaries are Go-built, statically linked.

## Pinned versions

| Component | Pinned version | SHA-256 (full file) |
|---|---|---|
| Notation CLI | `v1.3.2` (April 2025) | `34143c7b816a50104824278080a5cf12bbf855488e003f62e6acf27ed25c423c` |
| AWS Signer plugin (zip) | `v1.0.2292` (June 2025) | `b416954ac98a20d71f968c4f7cd7b13905328352f43298a405a8261016a8be51` |
| AWS Signer plugin (binary, inside zip) | bundled with above | `c65940608ff6d5c7532915ce917365c0b6edb2dd7e7e4504eac6b3170488ec77` |

AWS Signer's GPG signing key is pinned to fingerprint `E84AF8A2A9B52F1F4435AE71A3B52DA65461CF90` (key id `A3B52DA65461CF90`, owner `AWS Signer Notation`). The `public.key` file itself is pinned by SHA-256 alongside the binaries.

The pin pair is **the contract**. `build.sh` enforces it. Any drift in the upstream artifact or in the AWS public key fails the build with an explicit error. **Never silently update the pin to make a failing build pass** — investigate first.

## Trust model — what each pin actually defends

The script's two verification layers (SHA-256 pins + GPG signatures) are **not independent provenance** for the AWS Signer plugin. The plugin zip, both detached `.sig` files, and the `public.key` all come from the same CloudFront origin (`d2hvyiie56hcat.cloudfront.net`). A CloudFront or origin compromise serves coordinated artifacts — poisoned binary, matching `.sig`, attacker-controlled key. SHA-256 catches in-flight tampering between pin time and build time. GPG sigs catch the same plus key-rotation drift. They're complementary, not orthogonal.

The **load-bearing trust root is the GPG fingerprint pin** in `build.sh` (`AWS_SIGNER_KEY_FINGERPRINT`). It's sourced from `docs.aws.amazon.com` — a different AWS property on a different CDN path than the artifact origin — and the script enforces it three ways:

1. `public.key`'s SHA-256 must match the pin (catches bytewise rotation).
2. The fingerprint of every key in `public.key` must equal the pinned fingerprint. The file must also contain exactly one key. This defends against a multi-key file where an attacker key follows the pinned key. The second key would otherwise become a valid signer once `gpg` dearmors the keyring.
3. Each `gpgv` invocation parses `--status-fd` output. It asserts that the signing key's `VALIDSIG` line names the pinned fingerprint. This defends against the keyring containing other keys for any reason.

The **Notation CLI has no upstream signature**. The Notary Project does not publish detached `.sig` files or Sigstore attestations alongside the v1.3.x release tarballs. The CLI is pinned by SHA-256 only — adequate for in-flight integrity, weaker than the plugin's protection for pin-time tampering.

## Prerequisites

| Tool | Why | Install (macOS) | Install (Debian/Ubuntu) |
|---|---|---|---|
| `bash`, `curl`, `tar`, `unzip`, `shasum`, `zip` | Build mechanics | preinstalled | preinstalled |
| `gpg`, `gpgv` | Signature verification | `brew install gnupg` | `apt-get install gnupg` |

The build needs GPG by default. `gpg` must be version 2.2 or newer, because older versions lack `--show-keys`. The script probes the version and refuses to run on older releases.

The escape hatch for emergency local builds is `SKIP_GPG=1 I_ACKNOWLEDGE_SUPPLY_CHAIN_DOWNGRADE=1 ./build.sh`. Both env vars are required. The script rejects `SKIP_GPG` alone, so the downgrade is always intentional. Builds in CI environments (`CI` or `GITHUB_ACTIONS` env vars set) refuse the downgrade entirely. The downgrade makes the resulting layer zip carry a `.SKIP_GPG_USED` sentinel file at the root with a build-host timestamp. Downstream reviewers can then detect the weaker artifact post-hoc.

## Build

```bash
./lambda-layers/notation/build.sh
```

What it does, in order:

1. Download Notation CLI tarball from GitHub Releases. Verify SHA-256 against the pin.
2. Download AWS Signer plugin zip from `d2hvyiie56hcat.cloudfront.net`. Verify SHA-256 against the pin.
3. Download both detached `.sig` files and the AWS public key.
4. Extract the AWS public key fingerprint via `gpg --show-keys` (no agent needed) and verify it matches the pinned fingerprint.
5. Convert the public key to a standalone keyring with `gpg --dearmor`.
6. Verify the plugin-zip signature with `gpgv` against that keyring.
7. Extract both binaries. Verify the inner plugin binary's SHA-256.
8. Verify the inner binary's own detached signature with `gpgv`.
9. Assemble `build/notation-layer.zip` with the layer layout.

The build is fully isolated. It never touches the contributor's default GPG keyring or starts a `gpg-agent`. The `--dearmor` + `gpgv` path is agent-free, which avoids the macOS Homebrew IPC-socket fragility.

Build output:

```
lambda-layers/notation/
├── build/
│   ├── download/    # raw download artifacts (gitignored)
│   ├── stage/       # assembled layer contents (gitignored)
│   └── notation-layer.zip  # the deliverable (gitignored)
├── build.sh
└── .gitignore       # excludes build/
```

Typical layer size: **~9 MiB** (well under Lambda's 50 MiB zip / 250 MiB unzipped limits).

## CDK consumption

The CDK stack will declare the layer via a `LayerVersion` construct pointing at the asset path:

```typescript
const notationLayer = new lambda.LayerVersion(this, "NotationLayer", {
  layerVersionName: `notation-${stage}`,
  code: lambda.Code.fromAsset(
    path.join(__dirname, "../../lambda-layers/notation/build/notation-layer.zip"),
  ),
  compatibleArchitectures: [lambda.Architecture.ARM_64],
  compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
  description: `Notation CLI v1.3.2 + AWS Signer plugin v1.0.2292 (arm64)`,
});
```

The consuming Lambda **MUST** be configured with:

- `architecture: lambda.Architecture.ARM_64`
- `runtime: lambda.Runtime.PYTHON_3_12`
- `environment: { XDG_CONFIG_HOME: "/opt" }` — so notation discovers the plugin at `/opt/notation/plugins/com.amazonaws.signer.notation.plugin/notation-com.amazonaws.signer.notation.plugin`
- `layers: [notationLayer]`

The `cdk synth` step assumes `build/notation-layer.zip` exists. Contributors run `./lambda-layers/notation/build.sh` once before `cdk deploy`. CI does the same. The asset hashing makes CDK re-upload the layer only when the zip bytes change, so cold rebuilds don't churn the deployment.

## Version-bump policy

Upstream sometimes ships a new Notation CLI or AWS Signer plugin version that we want to adopt. The new pin requires **independent verification before commit**. Re-running `./build.sh` only proves "the SHA I just typed matches what CloudFront serves right now," which is circular if CloudFront is the attacker. The acceptable verification paths are:

- **Re-download from a clean network/host** (e.g., a different machine on a different ISP) and verify that the SHA matches.
- **Compare against an AWS-published source**. Use an AWS Security Bulletin, the AWS Signer release notes, a SHA published in the [AWS Signer developer guide](https://docs.aws.amazon.com/signer/latest/developerguide/image-signing-prerequisites.html), or an AWS support case.
- For Notation CLI, compare against the GitHub release page's `notation_X.Y.Z_checksums.txt`. GitHub serves that file over HTTPS from a different origin than CloudFront, so it is itself integrity-checkable.

Procedure:

1. Update the version + SHA-256 pin pair in `lambda-layers/notation/build.sh` at the `─── Pinned versions ───` block.
2. Update the same versions + SHAs in the **Pinned versions** table above.
3. Re-run `./build.sh`. It MUST succeed. If it fails, the upstream artifact rotated between pin lookup and verification — pause and investigate, don't paper over.
4. **Complete the independent-verification step above** before committing.
5. Commit the build-script change AND this runbook change in one commit (`chore(layers): bump notation to vX.Y.Z`).
6. Re-deploy via CDK. The new asset hash triggers a new `LayerVersion`, and Lambdas start to use it.

The AWS public key fingerprint should not change. If `build.sh` reports a fingerprint mismatch, do not adjust the pin — that's a credible signal of a supply-chain incident. The recovery path is:

1. Open an AWS support case.
2. Wait for AWS to acknowledge legitimate key rotation in a Security Bulletin.
3. Bump the fingerprint pin AND the `public.key` SHA-256 in one commit that cites the bulletin.

## Known gaps

| Gap | Risk | Possible mitigation |
|---|---|---|
| Notation CLI has no upstream signature | Pin-time tampering on a fresh release would land an unsigned malicious binary if SHA-256 is taken from the same compromised release | Build Notation from source at a pinned commit. Or adopt upstream Sigstore attestations once published |
| Build runs on contributor's machine with whatever `gpg`/`gpgv` is on PATH | Hijacked verification tools = no defense | Run the build inside a pinned, hash-verified container image |
| `/latest/` URL on CloudFront has no versioned alternative | A new plugin release silently lands in `/latest/` and our pin stays on the older version forever (freshness gap, not tampering) | A periodic job that diffs the `/latest/` SHA against the pin and files an issue when they diverge |
| Both defense layers (SHA + GPG) share a single CloudFront origin | Origin compromise serves a coordinated artifact + matching sig + attacker key | The out-of-band fingerprint pin mitigates this today. Dual-source the plugin from an AWS S3 mirror if AWS exposes one |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `SHA-256 mismatch for notation.tar.gz` | Upstream Notation release rotated bits at the same version (unlikely) OR the pin is stale (more likely) | Verify that the upstream version is the one you think. Refresh the pin per the version-bump policy |
| `SHA-256 mismatch for plugin.zip` | AWS rotated the `/latest/` artifact (likely on every plugin release since the URL has no versioned path) | Pull the new SHA from `shasum -a 256 build/download/plugin.zip` after a manual download, update the pin |
| `imported GPG key fingerprint mismatch` | AWS rotated their signing key — treat as a security incident | Do NOT bump the pin. Read the AWS Signer docs for an announcement. If legitimate, the new fingerprint should be documented |
| `gpgv: BAD signature` | The downloaded artifact and signature don't match — likely a transient CDN issue, possibly a supply-chain attack | Re-run the build. If it persists, investigate before bumping anything |
| `gpg-agent: IPC connect call failed` | Should not happen with the current script (uses `--show-keys` + `--dearmor` + `gpgv`, no agent). If it does, gpg version is too old (< 2.1.x doesn't support `--show-keys`) | Upgrade gnupg |

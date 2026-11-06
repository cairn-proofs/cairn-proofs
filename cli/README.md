# cairn CLI

Producer / auditor CLI for Cairn Proofs. Wraps the four end-user flows — `push`, `pull`, `verify`, `query` — around the shared libraries under `libs/` and the `oras` binary pinned by `mise.toml`.

## Install

The CLI is a `uv` workspace member. For local development, the root `uv sync` already places it on `PATH` as `cairn`:

```bash
uv run cairn --help
```

For installation onto an operator's laptop, use `pipx` from the workspace root so dependencies on sibling libraries resolve cleanly:

```bash
pipx install ./cli
```

We deliberately do **not** ship a single-file binary (PyInstaller, shiv, etc.); `pipx` is the documented install path and keeps the distribution surface tiny.

## Prerequisites

- **AWS credentials** — the CLI reads from the standard AWS SDK default chain (env vars, profile, instance metadata). No CLI-specific configuration is required. The IAM principal needs:
  - `ssm:GetParameter` on `/cairnproofs/{regionCode}-{stage}/app-name` (appName discovery), `/{appName}/{stage}/registry/ecr-host`, `/{appName}/{stage}/notation/trust-policy`, and `/{appName}/{stage}/tables/{metadata,control-claims,identity-registry}` (the CLI/TUI resolve the auto-named DynamoDB tables from these)
  - `ecr:GetAuthorizationToken`, `ecr:BatchCheckLayerAvailability`, `ecr:PutImage`, etc. (covered by the `{appName}-producer` managed policy provisioned by `cdk/lib/constructs/iam-roles.ts`)
  - `dynamodb:Query` on the Metadata_Index and ControlClaims tables (covered by the `{appName}-auditor` managed policy)
- **`oras` CLI** — pinned to v1.x in `mise.toml`. Verify via `oras version`.
- **`notation` CLI** — required for `cairn verify` and `cairn pull`. Install via `brew install notation` or download from https://notaryproject.dev/.
- **Notation trust policy** — `cairn verify` fails-closed if `~/.config/notation/trustpolicy.json` is missing. Install via (`<appName>` is the per-deployment prefix, e.g. `cairnproofs4af` — discoverable via the `/cairnproofs/{regionCode}-{stage}/app-name` SSM parameter or pinned with `CAIRN_APP_NAME`):
  ```bash
  aws ssm get-parameter --name /<appName>/alpha/notation/trust-policy \
    --query 'Parameter.Value' --output text > ~/.config/notation/trustpolicy.json
  ```

## Commands

```text
cairn push (--target <org>/<tenant>/<project> | --org-id ID --tenant-id ID --project-id ID) \
    [--assessment-period PERIOD] \
    <file> [<file> ...] \
    --artifact-kind <artifact-kind> \
    --catalog-or-profile-uuid <uuid> \
    --control <controlId> [--control <controlId> ...] \
    [--source-tool NAME] [--assessor NAME] [--assessment-id ID] \
    [--supersedes TAG,TAG] [--media-type FILENAME=MEDIATYPE]

cairn config set-target <org>/<tenant>/<project>   # persist a default target; `cairn config show`

cairn pull <reference> [--output-dir DIR]

cairn verify <reference>

cairn query --project <project-id> --tenant-id <tenant-id> [--limit N] [--page-token TOKEN]
cairn query --control <controlId> --catalog-or-profile-uuid <uuid> \
            [--limit N] [--page-token TOKEN]
```

The three identity coordinates (`{org-id}/{tenant-id}/{project-id}`) are resolved via a precedence chain: explicit `--org-id`/`--tenant-id`/`--project-id` (or a single `--target {org}/{tenant}/{project}`) > the `CAIRN_ORG_ID`/`CAIRN_TENANT_ID`/`CAIRN_PROJECT_ID` env vars > a persisted default (`cairn config set-target`; a project-local `.cairn.toml` over `$XDG_CONFIG_HOME/cairn/config.toml`). `cairn query --project` requires `--tenant-id` (the tenant is the exact-match partition key); the `--control` path keys on `{catalogOrProfileUuid}#{controlId}` and does not.

A reference is either a long-form ECR URI (`{registry}/{repo}:{tag}` or `...@sha256:...`) or the short form `<project-id>/<evidence-category>:{tag}` which the CLI expands using `--stage` (default `alpha`) and `--region-code` (default `cmh`).

### One push = one bundle (multiple files become layers)

`cairn push` accepts one or more files and assembles them into a **single Artifact_Bundle**, not one bundle per file. Each file becomes **one OCI layer** of the same manifest:

- the layer's media type is sniffed from the filename (override per file with `--media-type FILENAME=MEDIATYPE`), and its `org.opencontainers.image.title` layer annotation is set to the filename;
- the bundle-level metadata is **shared across all layers** — a single Config_Blob and one set of manifest annotations (`--artifact-kind`, `--control`, `--catalog-or-profile-uuid`, assessment-period, …) describe the whole bundle, so all files in one push share the same artifact-kind and control linkage;
- the whole set is validated once, pushed once (by digest), and returns a **single Manifest_Digest**.

```bash
# assessment-results + the assessment-plan and SSP it references,
# bundled together as ONE artifact:
cairn push --target acme-co/acme/corp-webapp --assessment-period 2026-q1 \
    assessment-results.oscal.json assessment-plan.oscal.json ssp.oscal.json \
    --artifact-kind assessment-results --control ac-2 --control ac-2.1
```

> **Identity is the `{org-id}/{tenant-id}/{project-id}` triple, supplied as named switches** (or a persisted default). Resolution precedence: explicit `--org-id`/`--tenant-id`/`--project-id` (or `--target {org}/{tenant}/{project}`) > the `CAIRN_ORG_ID`/`CAIRN_TENANT_ID`/`CAIRN_PROJECT_ID` env vars > `cairn config set-target` / a project-local `.cairn.toml`. An unresolved coordinate fails loud; a file token is never silently consumed as a coordinate. Every trailing token is a file (one content layer per file).
>
> - old (positional, or single `--project-id`): `cairn push acme 2026-q2 report.json` / `cairn push --project-id acme report.json`
> - new (full triple, or a persisted default): `cairn push --target acme-co/acme/payments --assessment-period 2026-q2 report.json`

Constraints: a bundle holds **1–300 layers**. OSCAL JSON layers use the media type `application/oscal+json` for every OSCAL model; `cairn push` sets it from the document root key. The pipeline rejects any other OSCAL media type, such as `application/oscal.catalog+json`. Each OSCAL layer has a size limit, **16 MiB by default**, that the deployment can change; the pipeline rejects a larger OSCAL layer with an error that states the limit that applies. Use distinct filenames within a push — the filename is the layer title and becomes the output filename on `cairn pull`, which refuses to overwrite, so two layers sharing a name would collide on extraction.

`cairn query` emits matching records as JSONL on stdout. When a pagination token is present, it is written to STDERR as `__next_token__: <token>` so stdout stays a clean JSONL stream for downstream `jq` pipelines.

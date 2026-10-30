# ADR-0009: CLI read paths (`pull` / `verify`) self-authenticate to ECR

## Status

Accepted (2026-06-02)

## Context

The `cairn` CLI's write path (`push`) and read paths (`pull` / `verify`) both reach ECR through external binaries: `oras` for byte transfer and `notation` for signature verification (see ADR-0008). Each of those round-trips needs a registry credential. The credential has the same shape in both directions. It is an ECR authorization token from `ecr:GetAuthorizationToken`, given to `oras login`. `oras login` writes the shared docker credential store (`~/.docker/config.json`). **Both** `oras` and `notation` read that store.

`push` already gets that token and runs `oras login` itself (`cli/cairn_cli/push.py`), so a producer needs no ambient `docker login`. The read paths did **not**. `pull` and `verify` assumed that the operator had already authenticated their local credential store out-of-band. Consider a first-time consumer, e.g. an auditor who runs `cairn pull <project>/<category>@sha256:...` to fetch a published OSCAL control catalog. That consumer got an opaque registry auth error at the `notation verify` / `oras manifest fetch` step. The error gave no hint that a prior `docker login` was the missing precondition.

This asymmetry was an **implementation accident, not a designed boundary.** The canonical specs constrain only the authorization layer below login:

- `openspec/specs/access-control/spec.md` — all ECR access goes through IAM, no shared service-internal credentials. Auditors may `pull` / `verify` / `query` within their scope.
- `openspec/specs/deployment-modes/spec.md` — cross-account push (Service_Hosted, Post-MVP) requires the `ecr:GetAuthorizationToken` *permission*.

No spec states the *"the CLI performs the login for you"* behavior for either side. `push`'s auto-login is a convenience that exists only in code. The read paths' lack of it was the same code-level choice, applied inconsistently. To close the gap changes **no Requirement**. That is why this is an ADR (a cross-cutting ergonomics/consistency decision) rather than an `/opsx:propose` change. Per the ADR/opsx boundary in `AGENTS.md`: WHY here, no spec WHAT to record.

## Decision

`pull` and `verify` self-authenticate to ECR before any registry round-trip, exactly as `push` does. A single helper holds the shared mechanism: `cairn_cli._aws.ecr_login(ecr_client, oras_module, *, registry_host)`. It gets the token via the existing `get_ecr_basic_auth` and runs `oras login`. All three commands call it. The operator needs no ambient `docker login`.

Key properties:

- **One login covers both binaries.** `oras login` writes the docker credential store that `notation` also reads. So a single login authenticates manifest fetch, layer pull, *and* signature verify. No per-tool credential plumbing.
- **Placed after reference resolution, before the first registry call.** An invalid reference still fails fast (exit 2) without a wasted login. On `verify`, a missing trust policy still short-circuits (exit 4) before login, because that is a purely local check.
- **Login failure is surfaced legibly** as exit 4 (`ECR login failed: ...`), distinct from a signature-verification failure. All three commands catch the same pair of errors:
  1. a failed `oras login` (`OrasCommandError`), *and*
  2. an unusable token payload (`EcrAuthError`, e.g. empty/malformed `authorizationData`).

  As a result, a credential problem can never escape as a raw traceback. This change aligned `push`'s handler to the same label and shape. Before, it caught only `OrasCommandError` and used a different label. The three handlers are now identical.
- **No new spec surface.** Behavior does not change for an operator who *was* already logged in (the login is idempotent). The change only removes a precondition.

## Consequences

- A consumer can run `cairn pull` / `cairn verify` in a fresh environment with only AWS credentials present. No `aws ecr get-login-password | docker login` prelude is necessary. This is the headline ergonomics win. It removes the rough edge that the 2026-06-02 read-path walkthrough identified.
- The three commands now share one credential path (`ecr_login`). A future change to how the CLI authenticates has a single edit site instead of three divergent inline blocks. Examples of such a change: token caching, a credential-helper shell-out, or the Option-C boto3 unification that ADR-0008 mentions.
- `pull` / `verify` now require `ecr:GetAuthorizationToken` on the caller's IAM identity. Any principal that could pull at all already needed this implicitly. It is also consistent with the push-side permission in `deployment-modes`.
- Suppose the CLI ever needs the *guarantee* (not just the behavior) that read paths authenticate, e.g. for a conformance test or a Service_Hosted onboarding contract. Then that guarantee becomes a net-new Requirement in `access-control` or `artifact-ingestion`. That Requirement would cross-link this ADR.

## Alternatives Considered

- **Document the `docker login` precondition instead of automating it.** Rejected. It keeps the push/read asymmetry. Also, the precondition is exactly the kind of undocumented tribal knowledge that makes a first auditor pull fail. The token round-trip is cheap, and push already implements it.
- **Require the operator to pass credentials explicitly to `pull` / `verify`.** Rejected. It diverges from `push` and adds surface. It offers nothing over the standard AWS credential chain that boto3 already resolves.
- **Make read-path login a net-new spec Requirement now.** Rejected as premature. No Requirement changes, and no consumer of a *guarantee* exists yet. This ADR records the decision. It can become a spec Requirement if a contract later needs it.
- **Self-authenticate on read paths via `ecr_login`, factored shared.** Accepted.

## References

- `cli/cairn_cli/_aws.py` — `ecr_login` helper (the shared credential path).
- `cli/cairn_cli/pull.py`, `cli/cairn_cli/verify.py` — read-path login call sites.
- `cli/cairn_cli/push.py` — the write-path precedent this mirrors.
- ADR-0008 — retains the `oras` + `notation` binaries whose credential store this login populates. That is the reason one `oras login` is sufficient for both tools.
- `openspec/specs/access-control/spec.md`, `openspec/specs/deployment-modes/spec.md` — the IAM/authorization layer below this decision. Neither specifies CLI auto-login, which is why this is an ADR.
- Read-path walkthrough, 2026-06-02 (this evaluation).

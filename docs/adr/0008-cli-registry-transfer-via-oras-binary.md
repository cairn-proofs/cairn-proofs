# ADR-0008: Retain the `oras` CLI binary for client-side registry transfer

## Status

Accepted (2026-06-02)

## Context

The `cairn` CLI's `push` / `pull` / `verify` commands move bytes to and from ECR. Today the registry-transfer leg runs the `oras` CLI binary as a subprocess through a thin wrapper (`cli/cairn_cli/_oras.py`). `mise.toml` pins `oras`. The question: should the CLI use a "proper" Python ORAS library (`oras-py`) instead of wrapping the binary?

The framing matters. The usual implied payoff of "library over CLI wrapper" is to *remove the external binary dependency*. That payoff does not occur here. The question also misses a third option. The full landscape follows.

**Two registry surfaces exist, and they are NOT symmetric.**

- **Lambda pipeline (server-side)** — `validate` / `sign` / `mint_tag` reach ECR via **boto3 directly** (`batch_get_image`, `get_download_url_for_layer`, `put_image`). No `oras`, no `oras-py`. The rationale is inline at `lambdas/validate_evidence/cairn_lambda_validate_evidence/ecr_client.py`: ship no binary, and keep boto3's transparent SigV4 + IAM-role credential chain. Note that the Lambdas only *download* blobs and `put_image` an existing digest. They **never upload new blobs**, because producers do.
- **`cairn` CLI (client-side)** — builds the OCI manifest + Config_Blob **in-process** via `cairn-oci`. It then runs `oras` as a subprocess for the wire transfer only. Verification on `pull` runs the **`notation`** binary as a subprocess via `cairn-signing`.

**The binary count never reaches zero on the client.** The CLI requires the `notation` binary on every `pull` / `verify`. No pure-Python Notation + AWS Signer plugin equivalent exists (see ADR-0002, which keeps the Notation layer for exactly this reason). A swap from `oras → oras-py` removes one binary but leaves `notation` (and therefore mise) required. The swap does not cross the "zero external binaries" threshold. It halves a count that stays ≥ 1, and this removes the primary reason for the swap.

**Three options on the table:**

| | Transfer mechanism | Client binaries | Code owned |
|---|---|---|---|
| **A. status quo** | `oras` CLI | oras + notation | thin subprocess wrapper |
| **B. oras-py** | `oras-py` library | notation only | hand-built manifest dict + config/annotation glue |
| **C. boto3 everywhere** | ECR blob-upload APIs | notation only | full chunked upload protocol |

- **B (`oras-py`)** is a lateral move at best. The high-level `push` in `oras-py` does not cleanly accept a *custom config media type + body* together with *both* manifest-level and per-layer annotations. To use it, we must construct the manifest dict by hand and bypass the convenience method. That repeats exactly what the binary exposes as first-class flags (`--config foo:type`, `--annotation-file`). It trades a clean subprocess wrapper for messier in-process glue, and it still ships `notation`. The `_oras.py` "Why not oras-py" docstring records the same reasoning.
- **C (boto3 everywhere)** is the interesting option that the library-vs-wrapper framing skips. The real problem is not "wrapper vs library". It is the **asymmetry**: Lambda talks to ECR via boto3 and the CLI via `oras`, although boto3 is *already* a CLI dependency. Unification on boto3 would retire `oras` entirely and leave `notation` as the sole client binary. The cost is ECR's chunked blob-**upload** protocol (`initiate-layer-upload` → `upload-layer-part` → `complete-layer-upload` → `put_image`). Because the Lambdas never upload, the CLI would be the *first* place that hand-rolls that protocol. That protocol is exactly the part that `oras` gets right for free. Option C is a real project with its own correctness risk, not a swap.

## Decision

Retain the `oras` CLI binary (Option A) for the `cairn` CLI's registry-transfer leg. Reject `oras-py` (Option B). It is a lateral move that does not remove the binary-dependency class, and it forces hand-built manifest construction. Do not pursue boto3-unified registry access (Option C) now. Record it as the correct path *if* removal of `oras` ever becomes worthwhile, gated on the triggers below.

This decision changes no Requirement. It is a client-side technology choice for how the CLI does the wire transfer. Therefore it is an ADR rather than an opsx change (per the AGENTS.md ADR-vs-opsx boundary).

## Consequences

- The `cairn` CLI continues to depend on the `oras` binary (pinned in `mise.toml`) plus the `notation` binary. Contributors and producers already run `mise install`, so this adds no new setup step.
- Manifest + Config_Blob construction stays in-process in `cairn-oci`. `oras` owns only the byte transfer. The seam (in-process build, shell-out transfer) stays, and `cli/pyproject.toml` documents it.
- ECR immutability-rejection handling continues to rely on a match against `oras` stderr text in `OrasCommandError` (e.g. "tag already exists" → bump `--tag-sequence`). A future `oras` release that changes that text is a low-likelihood maintenance risk. The CLI's push tests catch it.
- The Lambda ↔ CLI asymmetry (boto3 vs `oras`) remains. It is now a *recorded, deliberate* asymmetry rather than an accident. A follow-up nicety, which this ADR does not require, is a one-line note in `_oras.py`. The note would distinguish "why not oras-py" (already there) from "why not boto3 like the Lambdas" (this ADR). Then a future reader sees the choice as intentional.
- No spec deltas: the transfer mechanism is below the Requirement layer.

## Revisit Triggers

Reconsider this decision when any of the following becomes true. The first two point toward Option C (boto3). The rest are independent signals.

1. **`notation` gains a pure-Python (or boto3-native) equivalent** with AWS Signer plugin support, or signing/verification otherwise stops requiring an external binary on the client. This is the load-bearing trigger. Only then does retirement of `oras` actually reach "zero client binaries". At that point Option C's payoff becomes real, and the upload-protocol cost may be worth paying. (Track alongside ADR-0002, which holds the Notation-layer dependency.)
2. **Service_Hosted multi-account onboarding** ships a CLI that must run in locked-down or minimal environments. In those environments, per-client installation/pinning of `oras` causes friction (e.g. customer CI runners without mise). A boto3-only client that needs nothing but Python + AWS credentials would be a materially better distribution story.
3. **`oras-py` ships a first-class API** for push-with-custom-config-media-type *and* combined manifest + per-layer annotations (i.e. the awkwardness in Option B disappears). If oras-py can express the push as cleanly as the binary flags do, Option B's "messier glue" objection no longer applies. The in-process library then becomes attractive on its own merits (structured errors, no argv/stdin credential plumbing, mock at the Python boundary).
4. **The `oras` stderr contract breaks** — a release changes the immutability- or auth-rejection message text, and `OrasCommandError` classification becomes fragile or unmaintainable as a result. Structured exceptions from oras-py or boto3 would then be a correctness improvement, not just ergonomics.
5. **The CLI starts to upload blobs that the pipeline also needs to upload.** The chunked-upload protocol then becomes a shared concern worth one implementation in a shared lib. That would spread Option C's main cost across both surfaces.

If a trigger fires, the successor work is an opsx change only if it touches a Requirement (it likely does not). Otherwise it is a superseding ADR (`Status: Superseded by ADR-NNNN`).

## Alternatives Considered

- **B. Swap to `oras-py`.** Rejected. It does not remove the binary-dependency class (`notation` stays). It also forces hand-built manifest construction that repeats what the binary's flags provide. A lateral move.
- **C. boto3-unified registry access (retire `oras`).** Deferred, not rejected. It is the *correct* path to a minimal-binary client. But trigger 1 (notation becomes binary-free) gates its payoff, and it costs a hand-rolled ECR chunked-upload protocol that the Lambdas never needed. Premature now.
- **A. Status quo — keep the `oras` CLI wrapper.** Accepted.

## References

- `cli/cairn_cli/_oras.py` — the wrapper and its "Why not oras-py" docstring.
- `cli/pyproject.toml` — records the in-process-build / shell-out-transfer seam.
- `lambdas/validate_evidence/cairn_lambda_validate_evidence/ecr_client.py` — the server-side boto3 ECR access path and its "Why not ORAS-CLI?" rationale.
- ADR-0002 — defers ECR managed signing and retains the Notation layer. It is the source of the client-side `notation` binary dependency that defeats Option B's main benefit.
- `/opsx:explore` session, 2026-06-02 (this evaluation).

# ADR-0002: Defer ECR managed signing; retain Notation + AWS Signer sign-on-validate pipeline

## Status

Accepted (2026-05-30)

## Context

Amazon ECR managed container image signing went GA in November 2025, after the system's original design work. Therefore no one evaluated it at design time.

**Mechanism.** ECR auto-signs images *at push time* via registry-level signing rules (up to 10) that reference AWS Signer signing profiles plus repository filters.

**Trust-root alignment.** Managed signing shares Cairn's exact trust root. It uses the same AWS Signer HSM-backed keys and the same Notation OCI-referrer signature format (`artifactType` `application/vnd.cncf.notary.signature`, `subject` = the manifest digest). It therefore satisfies platform-constraints [PLATFORM-004] (the AWS Signer mandate) and signing [SIGN-002] (OCI referrer storage) **unchanged**.

Given that alignment, the question is whether managed signing can replace Cairn's current `sign-evidence` Lambda. The deferral reasons below explain why it cannot, for the MVP.

**1. Gating inversion (the dealbreaker).** Managed signing fires at *push*. In Cairn's pipeline the producer pushes the bundle to ECR *first*, then validate runs, then sign (validate → sign → archive). Managed signing would therefore sign **unvalidated** evidence. The pipeline signature is meant to attest "validated & accepted by the pipeline," not merely "received." Recovering the correct semantics requires a staging → promote two-repo redesign in which validate gates the promotion push and managed signing fires on that push.

**2. Loses [SIGN-009] timestamp flexibility.** Managed signing exposes a signature validity period but no `--timestamp-url`. So the `system-clock` → `rfc3161` SSM switch (the Long-Term-Archival path) has no equivalent.

**3. Failure-semantics mismatch.** Managed signing surfaces failure via push-time `describe-image-signing-status`, not Cairn's EventBridge/DLQ + `signingStatus: unsigned` model ([SIGN-008]).

**4. Does not retire the Notation layer.** Auditor sign-off ([SIGN-003], a separate human signature added later) and manual verification both still require the Notation CLI. Managed signing could only ever retire the *pipeline* signature, not the layer.

**ECR enforcement boundary (verified against AWS docs).** ECR exposes **no** push-time content-validation/admission hook.

1. Its full registry-feature set is: registry permissions, pull-through cache, replication, repository creation templates, scanning (basic/enhanced), pull-time update exclusion, blob mounting, and managed signing.
2. Its repo-level settings are: encryption, tag immutability, lifecycle rules, and scan-on-push.
3. `PutImage` validates only that the manifest is well-formed and that its layers are uploaded.
4. Vulnerability scanning is CVE-only (OS/language packages) and is meaningless for evidence bundles.

Therefore all Cairn envelope + OSCAL checks are irreducibly custom compute. These checks are: required annotations, layer titles, config-blob schema, bundle-type vocabulary, `catalogOrProfileUuid` format, `accountId`/ bundle-type cross-checks, the temporal check, and OSCAL parse/extract. The `validate-evidence` Lambda has no native substitute. The consequence is that managed signing can only change *when* the signature lands, never *whether* validation runs.

## Decision

Defer ECR managed signing for the MVP. Keep the current Notation + AWS Signer sign-on-validate pipeline (the `sign-evidence` Lambda).

Managed signing remains a strong candidate for Service_Hosted: cross-account signing profiles and central registry-level governance are exactly what it offers. If and when the project pursues a staging → promote redesign, that redesign becomes its own opsx change with a propose-phase threat-model checkpoint. This ADR records the evaluation and the deferral, not the eventual implementation.

## Consequences

- The MVP retains the validate → sign → archive Lambda pipeline unchanged. The pipeline signature continues to attest "validated & accepted," not merely "received."
- The `rfc3161` SSM timestamp switch ([SIGN-009]) and the EventBridge/DLQ failure model ([SIGN-008]) are preserved.
- The Notation layer is retained regardless — auditor sign-off ([SIGN-003]) and manual verification depend on it.
- A future move to managed signing is gated on a staging → promote two-repo re-architecture and a fresh threat-model checkpoint. It is not a drop-in swap.
- No spec deltas: managed signing satisfies Reqs 19.3 and 4.2 unchanged, so this deferral does not affect the trust root or the signature format.

## Alternatives Considered

- **Adopt managed signing now in the single-repo MVP layout.** Rejected: the gating inversion would sign unvalidated evidence.
- **Adopt via a staging → promote two-repo redesign now.** Rejected for MVP scope: it is a real re-architecture, it still loses the `rfc3161` knob, and it is better deferred to Service_Hosted.
- **Status quo — keep Notation + AWS Signer sign-on-validate.** Accepted.

## References

- `openspec/specs/signing/spec.md` — Reqs 4.1, 4.2, 4.3, 4.9, 4.10.
- `openspec/specs/platform-constraints/spec.md` — [PLATFORM-004].
- AWS documentation: ECR managed signing, ECR registry settings, ECR `PutImage` API (ECR facts confirmed against live AWS docs via the AWS read-documentation tool).
- `/opsx:explore` session, 2026-05-30 (this evaluation).

"""Multi-signature status aggregation (signing [SIGN-003], [SIGN-005]).

Pure, dependency-light status arithmetic over the set of signature
referrers attached to an Artifact_Bundle. Given a list of per-signature
verification results (one per OCI Notation signature referrer — typically
the ``artifact-pipeline-signing`` pipeline signature plus zero or more
``artifact-reviewer-signoff`` reviewer sign-offs), this module computes the
overall ``signingStatus`` and the per-signature ``signatureDetails`` list
exactly as the canonical retrieval-response contract specifies.

This module performs **no** DynamoDB / Metadata_Index access and has no AWS
SDK dependency: it operates purely on inputs derived from the canonical ECR
artifact (``notation verify`` outcomes + the OCI Referrers API), so the
verdict is index-independent (artifact-verifiability *Index-Independent
Re-Verification*). The CLI ``cairn verify`` wave consumes ``aggregate_status``
to render the verdict; the sign-bundle / retrieval path consumes the same
function so both surfaces agree byte-for-byte.

Status semantics (canonical, signing [SIGN-005] *Signature Status Reporting on
Retrieval*), over the set of *present* signature referrers:

- ``signed``    — at least one present, every present signature valid.
- ``unsigned``  — no signatures present.
- ``partial``   — at least one valid AND at least one revoked.
- ``revoked``   — at least one present, every present signature revoked.

Per-signature status is tracked independently: revoking one profile's
signature never changes another profile's recorded status ([SIGN-005.05]).

Two-signature trust (Decision D2 / threats
T-3/T-4): a fully-trusted bundle requires TWO independent pipeline
signatures — one over the **content** manifest (subject = content digest) and
one over the **Attestation** referrer (subject = attestation digest). Either
one alone is NOT full trust. :func:`classify_bundle_trust` projects the per-
signature results onto those two *roles* and yields a coarse bundle verdict
(``fully-trusted`` / ``partial`` / ``unsigned``) that the retrieval/verify
surfaces (``cairn verify``, ``cairn query``, the TUI) render so a one-signature
artifact is NEVER reported as fully trusted. This sits ON TOP of the
valid/revoked :func:`aggregate_status` arithmetic — they answer different
questions (role coverage vs. revocation), so both compose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from cairn_signing.profile import PIPELINE_PROFILE_NAME, REVIEWER_PROFILE_NAME


class SignatureStatus(StrEnum):
    """Per-signature verification outcome (signing [SIGN-005])."""

    VALID = "valid"
    REVOKED = "revoked"


class SignatureRole(StrEnum):
    """Which subject a signature covers (Decision D2).

    A pipeline submission produces two independent signatures whose SUBJECTS
    differ: one over the content manifest, one over the Attestation referrer.
    The role is what distinguishes them for the two-signature trust verdict.
    ``UNKNOWN`` is the default for a signature whose subject role could not be
    determined (e.g. a reviewer sign-off, or a CLI re-derivation that has not
    resolved the subject) — an unknown-role signature never satisfies either
    required role on its own.
    """

    CONTENT = "content"
    ATTESTATION = "attestation"
    UNKNOWN = "unknown"


class BundleTrust(StrEnum):
    """Coarse two-signature bundle trust verdict (threats T-3/T-4).

    Independent of (and composed with) the valid/revoked :class:`SigningStatus`
    arithmetic: this answers "are BOTH required signatures present and valid?"

    - ``fully-trusted`` — content AND attestation signatures both valid.
    - ``partial``       — exactly one of the two roles is satisfied by a valid
                          signature (the other missing or only revoked); MUST
                          never be reported as fully trusted/verified.
    - ``unsigned``      — neither required role is satisfied.
    """

    FULLY_TRUSTED = "fully-trusted"
    PARTIAL = "partial"
    UNSIGNED = "unsigned"


class SigningStatus(StrEnum):
    """Overall bundle signing status (signing [SIGN-005])."""

    SIGNED = "signed"
    UNSIGNED = "unsigned"
    PARTIAL = "partial"
    REVOKED = "revoked"


# The Signing_Profile names admitted by the Notation trust policy. Reviewer
# sign-off (artifact-verifiability) makes artifact-reviewer-signoff a
# first-class peer of the pipeline profile — both are recognized here so
# aggregation treats a reviewer referrer identically to a pipeline one.
KNOWN_SIGNING_PROFILE_NAMES: frozenset[str] = frozenset(
    {PIPELINE_PROFILE_NAME, REVIEWER_PROFILE_NAME}
)


@dataclass(frozen=True)
class SignatureResult:
    """One signature referrer's verification result.

    Models what ``notation verify`` + the OCI Referrers API yield for a
    single signature attached to the bundle manifest. All fields are derived
    from the canonical ECR artifact — never from the Metadata_Index.

    ``timestamp_provider`` records the TimestampProvider strategy used at
    signing time (``system-clock`` is built; ``rfc3161`` is recognized but
    unsupported) and is
    carried through into the ``signatureDetails`` entry per [SIGN-009].
    ``tsa_endpoint`` is the RFC 3161 TSA endpoint used, or ``None`` under the
    ``system-clock`` strategy where no external TSA is contacted.

    ``profile_arn`` is the Signing_Profile ARN ([SIGN-005]) when recoverable, or
    ``None`` when the source cannot bind the signature to a profile ARN in a
    version-stable way (e.g. a CLI re-derivation off the OCI Referrers API,
    which exposes no signer ARN). ``signature_digest`` carries the signature
    referrer's own OCI image digest — its stable per-signature identity —
    which is a distinct concept from the profile ARN and never a substitute.
    """

    profile_arn: str | None
    profile_name: str
    status: SignatureStatus
    signing_timestamp: str
    tsa_endpoint: str | None = None
    timestamp_provider: str | None = None
    signature_digest: str | None = None
    role: SignatureRole = SignatureRole.UNKNOWN


@dataclass(frozen=True)
class SignatureDetail:
    """A single ``signatureDetails`` entry in the retrieval response ([SIGN-005]).

    ``profile_arn`` is the Signing_Profile ARN when known, else ``None`` (see
    ``SignatureResult``); ``signature_digest`` is the referrer's own digest.
    ``role`` records which subject the signature covers (content vs attestation)
    so a consumer can see which of the two required signatures is present.
    """

    profile_arn: str | None
    profile_name: str
    status: SignatureStatus
    signing_timestamp: str
    tsa_endpoint: str | None
    timestamp_provider: str | None
    signature_digest: str | None = None
    role: SignatureRole = SignatureRole.UNKNOWN


@dataclass(frozen=True)
class AggregatedSignatureStatus:
    """The aggregated verdict for one bundle: overall status + per-signature detail.

    ``signing_status`` and ``signature_details`` map directly onto the
    retrieval response's ``signingStatus`` field and ``signatureDetails``
    list ([SIGN-005]). ``signature_details`` preserves input order so the caller
    can render a stable list.
    """

    signing_status: SigningStatus
    signature_details: tuple[SignatureDetail, ...]


@dataclass(frozen=True)
class BundleTrustVerdict:
    """The two-signature trust verdict for one bundle (threats T-3/T-4).

    ``verdict`` is the coarse :class:`BundleTrust` (fully-trusted / partial /
    unsigned). ``content_signed`` / ``attestation_signed`` report whether each
    required role is satisfied by a VALID signature. ``missing_roles`` lists the
    required roles NOT satisfied (empty iff fully trusted) so a surface can name
    exactly which signature is absent ("missing attestation signature").
    """

    verdict: BundleTrust
    content_signed: bool
    attestation_signed: bool
    missing_roles: tuple[SignatureRole, ...] = field(default_factory=tuple)


def _overall_status(results: list[SignatureResult]) -> SigningStatus:
    """Reduce per-signature statuses to the overall ``signingStatus``.

    Order-independent and computed solely from the multiset of present
    per-signature statuses, so re-deriving from the canonical artifact always
    yields the same verdict ([SIGN-005.03] / index-independence).
    """
    if not results:
        return SigningStatus.UNSIGNED
    has_valid = any(r.status is SignatureStatus.VALID for r in results)
    has_revoked = any(r.status is SignatureStatus.REVOKED for r in results)
    if has_valid and has_revoked:
        return SigningStatus.PARTIAL
    if has_revoked:  # all present are revoked
        return SigningStatus.REVOKED
    return SigningStatus.SIGNED  # all present are valid


def aggregate_status(results: list[SignatureResult]) -> AggregatedSignatureStatus:
    """Aggregate per-signature verification results into a bundle verdict ([SIGN-005]).

    ``results`` is the list of per-signature verification outcomes for one
    bundle — one entry per OCI signature referrer, in any order. Each entry's
    status is determined upstream (``notation verify`` per signature + AWS
    Signer revocation enforcement); this function only composes them.

    Returns the overall ``signingStatus`` plus a ``signatureDetails`` list
    that echoes every input signature's profile ARN, profile name, individual
    status, signing timestamp, TSA endpoint, and timestamp provider — with
    each profile's recorded status preserved verbatim (revoking one profile's
    signature does not mutate another's; [SIGN-005.05]).

    Pure: no I/O, no DynamoDB / Metadata_Index access, no AWS SDK calls.
    """
    details = tuple(
        SignatureDetail(
            profile_arn=r.profile_arn,
            profile_name=r.profile_name,
            status=r.status,
            signing_timestamp=r.signing_timestamp,
            tsa_endpoint=r.tsa_endpoint,
            timestamp_provider=r.timestamp_provider,
            signature_digest=r.signature_digest,
            role=r.role,
        )
        for r in results
    )
    return AggregatedSignatureStatus(
        signing_status=_overall_status(results),
        signature_details=details,
    )


def classify_bundle_trust(results: list[SignatureResult]) -> BundleTrustVerdict:
    """Project per-signature results onto the two-signature trust verdict.

    Full trust (``BundleTrust.FULLY_TRUSTED``) requires BOTH a valid
    content-role signature AND a valid attestation-role signature
    (Decision D2). A role is satisfied only by a present
    signature whose ``status`` is ``VALID`` — a revoked or absent signature
    does NOT satisfy its role, so a content-signed-but-attestation-unsigned (or
    vice-versa) bundle is ``PARTIAL`` and is never reported as fully trusted
    (threats T-3/T-4). With neither role satisfied the verdict is ``UNSIGNED``.

    Pure: no I/O, order-independent, computed solely from the input results.
    """
    content_signed = any(
        r.role is SignatureRole.CONTENT and r.status is SignatureStatus.VALID for r in results
    )
    attestation_signed = any(
        r.role is SignatureRole.ATTESTATION and r.status is SignatureStatus.VALID for r in results
    )
    missing: list[SignatureRole] = []
    if not content_signed:
        missing.append(SignatureRole.CONTENT)
    if not attestation_signed:
        missing.append(SignatureRole.ATTESTATION)

    if content_signed and attestation_signed:
        verdict = BundleTrust.FULLY_TRUSTED
    elif content_signed or attestation_signed:
        verdict = BundleTrust.PARTIAL
    else:
        verdict = BundleTrust.UNSIGNED

    return BundleTrustVerdict(
        verdict=verdict,
        content_signed=content_signed,
        attestation_signed=attestation_signed,
        missing_roles=tuple(missing),
    )


# The projected ``signingStatus`` value (Metadata_Index) the writer sets to
# ``"signed"`` only AFTER the two-signature promotion gate (both content and
# attestation signatures present and valid) passes. Any other value means the
# bundle has NOT reached that fully-signed state.
_FULLY_SIGNED_PROJECTION = "signed"


def bundle_trust_from_projection(signing_status: str | None) -> BundleTrust:
    """Map a PROJECTED ``signingStatus`` string to the two-signature verdict.

    The index-backed read surfaces (``cairn query``, the TUI) read the coarse
    ``signingStatus`` the update-metadata projection writer stamped — they do
    NOT re-derive per-signature roles from ECR. The writer sets ``signed`` only
    after the both-signatures promotion gate, so a projected ``signed`` maps to
    ``FULLY_TRUSTED``; an explicit ``unsigned`` (or absent) maps to
    ``UNSIGNED``; any in-between transient state (e.g. ``pending``) maps to
    ``PARTIAL`` so a not-yet-fully-signed bundle is never shown as fully trusted
    (threats T-3/T-4).

    This is a PROJECTION read, NOT a cryptographic verification: the
    authoritative two-signature verdict comes from :func:`classify_bundle_trust`
    over the live ECR referrers (``cairn verify``). The index is rebuildable and
    non-authoritative, so a consumer needing certainty must re-verify.
    """
    if signing_status == _FULLY_SIGNED_PROJECTION:
        return BundleTrust.FULLY_TRUSTED
    if signing_status in (None, "", "unsigned"):
        return BundleTrust.UNSIGNED
    return BundleTrust.PARTIAL

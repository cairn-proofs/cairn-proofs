"""Producer_Attestation_Referrer model and media-type constant (artifact-ingestion
"Producer Claim Carried as Signed Attestation Referrer").

The producer's compliance claim is NOT part of the content Manifest_Digest. It is
carried in a separate, independently-signed OCI referrer whose ``subject`` is the
content Manifest_Digest. This module is the single source of truth for that
referrer's ``artifactType`` and its claim payload shape; the validate, sign,
archive, and update-metadata stages consume it rather than re-deriving the claim
from the content manifest or its digest.

The claim payload carries
``{orgId, tenantId, projectId, artifactKind, controls, catalogOrProfileUuid,
assessmentPeriod, assessor, assessmentId, supersedes}``. The claim's **identity
tuple** is the opaque ``orgId`` + ``tenantId`` + ``projectId`` triple — each a
stable, opaque logical id (never an AWS account number and never a human display
name). Each of the three coordinates is cross-checked, exact-match, against the
corresponding segment of the IAM-enforced ECR repository path
(``{org-id}``/``{tenant-id}``/``{project-id}``) by the processing pipeline (see
processing-pipeline [PIPELINE-013]); the cross-check is not conditioned on any
deployment mode. Display names for an org/tenant/project live only in the
control-plane identity registry, keyed by the opaque id, and never ride the
signed claim.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from cairn_oci.digest import DIGEST_ALGO, parse_digest

#: The Producer_Attestation_Referrer ``artifactType`` — the single positive validation
#: trigger. An OCI ``PutImage`` for a
#: manifest carrying this ``artifactType`` over a content-manifest ``subject`` is
#: the submission-complete signal.
PRODUCER_ATTESTATION_ARTIFACT_TYPE: Final[str] = (
    "application/vnd.cairnproofs.producer-attestation.v1+json"
)

#: The in-toto Statement ``_type`` (in-toto Statement spec v1).
_INTOTO_STATEMENT_TYPE: Final[str] = "https://in-toto.io/Statement/v1"

#: The referrer claim layer's ``mediaType`` once the payload is an in-toto
#: Statement. The OCI envelope ``artifactType`` stays the Cairn role label
#: (:data:`PRODUCER_ATTESTATION_ARTIFACT_TYPE`); this layer ``mediaType``
#: announces the *format* to external in-toto tooling.
INTOTO_STATEMENT_MEDIA_TYPE: Final[str] = "application/vnd.in-toto+json"

#: The Cairn producer-attestation ``predicateType``. The ``/ns``-rooted predicate
#: URI naming convention is recorded in ADR-0015 (not re-derived here); this is
#: the ``attestation/producer/v1`` leaf. The validate stage asserts the parsed
#: Statement carries exactly this ``predicateType`` before trusting ``predicate``.
PRODUCER_PREDICATE_TYPE: Final[str] = "https://cairnproofs.dev/ns/attestation/producer/v1"


class InvalidStatementError(ValueError):
    """A purported producer in-toto Statement is malformed or wrong-payload.

    Raised on a ``subject`` array that is not exactly one element, or a
    ``predicateType`` that is not :data:`PRODUCER_PREDICATE_TYPE`. The validate
    stage maps this to a permanent validation failure (DLQ) — the artifact
    routed here by its OCI ``artifactType`` but carries a payload that cannot be
    trusted as a Cairn producer claim.
    """


class ProducerAttestationReferrer(BaseModel):
    """The producer's compliance claim, carried as a signed OCI referrer.

    The claim ``subject`` is the content Manifest_Digest (the referrer envelope's
    subject descriptor, not a field of this payload — see
    :data:`PRODUCER_ATTESTATION_ARTIFACT_TYPE` and the "one content, many attestations"
    property). This model carries only the claim payload.

    The opaque identity triple ``orgId`` + ``tenantId`` + ``projectId`` plus
    ``artifactKind`` are the irreducible claim identity and are REQUIRED; the
    rest are optional. ``controls`` defaults to ``[]``. Follows the same
    conventions as :class:`cairn_oci.config_blob.ConfigBlob` — snake_case
    attributes with camelCase aliases, ``extra="allow"`` so a producer's vendor
    fields round-trip, and ``populate_by_name`` so either spelling constructs it.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # Irreducible claim identity (REQUIRED) — the opaque identity tuple
    # orgId + tenantId + projectId. Each is a stable, opaque logical id (never an
    # AWS account number, never a human display name); display names live only in
    # the control-plane identity registry, keyed by the opaque id, and never ride
    # the signed claim. Each coordinate is cross-checked exact-match against the
    # corresponding segment of the IAM-enforced ECR repository path (see
    # processing-pipeline [PIPELINE-013]); the cross-check is not conditioned on
    # any deployment mode.
    org_id: str = Field(alias="orgId")
    tenant_id: str = Field(alias="tenantId")
    project_id: str = Field(alias="projectId")
    artifact_kind: str = Field(alias="artifactKind")

    # Control linkage (OPTIONAL). `controls` defaults to []; the cited OSCAL
    # Catalog OR Profile UUID is the producer's authoritative reference for them.
    controls: list[str] = Field(default_factory=list)
    catalog_or_profile_uuid: str | None = Field(default=None, alias="catalogOrProfileUuid")

    # Assessment claim (OPTIONAL on the model; REQUIRED for evidence-form
    # artifacts and absent-and-never-fabricated for reference-form — that
    # form-dependent requirement is enforced by the validate stage, Decision D1).
    assessment_period: str | None = Field(default=None, alias="assessmentPeriod")
    assessor: str | None = None
    assessment_id: str | None = Field(default=None, alias="assessmentId")

    # Comma-separated list of ECR tags within the same repository this bundle
    # supersedes (OPTIONAL; processed by update-metadata, [INGEST-013.04]).
    supersedes: str | None = None


def identity_triple(
    detail: dict[str, Any], *, include_top_level: bool = False
) -> tuple[str | None, str | None, str | None]:
    """Extract the opaque identity triple ``(orgId, tenantId, projectId)`` from a
    pipeline event ``detail`` ([INGEST-005]).

    The producer's compliance claim — projected onto every pipeline event under
    ``detail["attestation"]`` — carries the bundle's identity tuple as three
    stable, opaque logical ids (``orgId``, ``tenantId``, ``projectId``): never an
    AWS account number, never a human display name. ``tenantId`` is the
    Metadata_Index partition-key isolation boundary and ``projectId`` is the
    sort-key sub-hierarchy ([META-001]); ``orgId`` rides as a plain record
    attribute. This is the single extractor every pipeline stage that threads the
    claim (sign / archive / update-metadata / mint-tag) shares.

    With ``include_top_level=True`` (the archive stage), an explicit top-level
    ``detail`` coordinate takes precedence over the claim projection when present:
    those top-level fields are the event-contract carrier archive threads forward
    on ``artifact.archived`` for the downstream stages, so an idempotent re-emit
    composes without re-reading the claim. By default only the ``attestation``
    projection is read.

    Returns ``(org_id, tenant_id, project_id)`` with ``None`` for any absent
    coordinate; callers apply their own presence/fail-closed checks.
    """
    attestation = detail.get("attestation") or {}
    if include_top_level:
        return (
            detail.get("orgId") or attestation.get("orgId"),
            detail.get("tenantId") or attestation.get("tenantId"),
            detail.get("projectId") or attestation.get("projectId"),
        )
    return (
        attestation.get("orgId"),
        attestation.get("tenantId"),
        attestation.get("projectId"),
    )


def build_producer_statement(
    subject_digest: str, claim: ProducerAttestationReferrer
) -> dict[str, Any]:
    """Wrap a producer claim as a valid in-toto Statement.

    ``subject_digest`` is the OCI prefixed content Manifest_Digest
    (``sha256:<hex>``); it becomes the Statement's single-element ``subject`` as
    a bare-hex :term:`DigestSet` keyed by algorithm. ``claim`` is carried
    verbatim as the ``predicate`` in its existing camelCase wire shape
    (``model_dump(mode="json", by_alias=True, exclude_none=True)``), so the
    read-boundary unwrap (:func:`read_claim_from_statement`) reconstructs the
    flat :class:`ProducerAttestationReferrer` that downstream consumers expect.

    The OCI envelope ``subject`` remains the Notation-signed anti-hijack
    authority; this Statement ``subject`` is an internal-consistency anchor the
    validate stage re-checks against it.
    """
    bare_hex = parse_digest(subject_digest)
    return {
        "_type": _INTOTO_STATEMENT_TYPE,
        "subject": [{"digest": {DIGEST_ALGO: bare_hex}}],
        "predicateType": PRODUCER_PREDICATE_TYPE,
        "predicate": claim.model_dump(mode="json", by_alias=True, exclude_none=True),
    }


def read_claim_from_statement(statement: dict[str, Any]) -> ProducerAttestationReferrer:
    """Unwrap a producer in-toto Statement back to the flat claim.

    Asserts the ``subject`` array has EXACTLY one element that is itself an
    object (so a downstream ``subject[0].get("digest")`` cannot raise on a
    producer-supplied non-object element), and that
    ``predicateType`` is :data:`PRODUCER_PREDICATE_TYPE` (the defensive
    right-envelope/wrong-payload guard), then reconstructs
    :class:`ProducerAttestationReferrer` from ``predicate``. Raises
    :class:`InvalidStatementError` on any structural failure so the validate
    stage can map it to a permanent failure.
    """
    subject = statement.get("subject")
    if not isinstance(subject, list) or len(subject) != 1 or not isinstance(subject[0], dict):
        raise InvalidStatementError(
            f"in-toto Statement subject must be a single-element array of objects, got {subject!r}"
        )
    predicate_type = statement.get("predicateType")
    if predicate_type != PRODUCER_PREDICATE_TYPE:
        raise InvalidStatementError(
            f"unexpected predicateType: expected {PRODUCER_PREDICATE_TYPE!r}, "
            f"got {predicate_type!r}"
        )
    predicate = statement.get("predicate")
    if not isinstance(predicate, dict):
        raise InvalidStatementError(
            f"in-toto Statement predicate must be an object, got {predicate!r}"
        )
    return ProducerAttestationReferrer(**predicate)


def subject_digest_matches(subject_digest: str, digest_set: dict[str, str]) -> bool:
    """Compare an OCI prefixed digest against an in-toto DigestSet by algorithm.

    ``subject_digest`` is the OCI envelope subject (``sha256:<hex>``);
    ``digest_set`` is an in-toto Statement subject's ``digest`` map (algorithm →
    bare hex). The shared algorithm (the one named in the OCI prefix) must agree;
    a DigestSet carrying ADDITIONAL algorithms is tolerated — this is NOT map
    equality. Returns ``False`` (never raises) when the shared
    algorithm is absent from the DigestSet or the bare hex disagrees.

    The OCI prefix is split via :func:`cairn_oci.digest.parse_digest`, whose
    regex anchors with ``$`` — which matches before a trailing ``\\n`` (the
    ``_ID_RE`` trailing-newline gotcha). We reject any ``subject_digest`` that is
    not an exact, newline-free ``sha256:<hex>`` so a contaminated digest cannot
    sneak a false match.
    """
    if not isinstance(subject_digest, str) or subject_digest.strip() != subject_digest:
        return False
    try:
        bare_hex = parse_digest(subject_digest)
    except ValueError:
        return False
    if not isinstance(digest_set, dict):
        return False
    return digest_set.get(DIGEST_ALGO) == bare_hex

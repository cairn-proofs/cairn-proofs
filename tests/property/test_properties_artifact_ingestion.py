"""Property-based tests for the artifact-ingestion capability.

Covers: [INGEST-003.04] (bundle constraints), [INGEST-006.02] (digest
determinism), [INGEST-007.03] (digest verification correctness),
[INGEST-017.07] (bundle validation completeness, criteria a-e),
[INGEST-018.02] (repository path construction), [OSCAL-001]
(OSCAL schema version content-derived).

[INGEST-021.09] (tag round-trip) coverage moved to
``tests/unit/cairn_oci/test_locator.py`` when the tag format and
construction relocated to ``cairn_oci.ArtifactLocator``.
"""

from __future__ import annotations

import hashlib
import io
import json
from typing import Any

import pytest
from cairn_oci.annotations import (
    LAYER_REQUIRED_TITLE,
    MANIFEST_REQUIRED,
    CairnAnnotations,
)
from cairn_oci.categories import (
    ARTIFACT_KINDS,
    ArtifactLocator,
    InvalidArtifactKindError,
    InvalidIdError,
    normalize_artifact_kind,
)
from cairn_oci.config_blob import CONFIG_BLOB_MEDIA_TYPE
from cairn_oci.digest import (
    IntegrityError,
    compute_sha256_digest,
    verify_digest,
)
from cairn_oci.manifest import Descriptor, OciManifest
from cairn_oci.producer_attestation import ProducerAttestationReferrer
from cairn_validation.bundle import validate_bundle, validate_claim
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


def _does_not_resolve(token: str) -> bool:
    """True only when ``token`` is neither a canonical kind nor a known alias.

    Used to harden "bad category" generators so a generated string that happens
    to be a valid alias (e.g. ``scan-reports``, ``policies``) is never treated as
    invalid — the negative-space tests must operate on genuinely-unresolvable
    tokens once aliases normalize on input.
    """
    try:
        normalize_artifact_kind(token)
    except InvalidArtifactKindError:
        return True
    return False


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Lowercase alphanumeric + hyphens, 1-64 chars, no leading/trailing hyphen.
_id_chars = st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-")
_id_inner = st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-")
_id_edge = st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789")


@st.composite
def valid_lowercase_id(draw: st.DrawFn) -> str:
    """Produce a lowercase alphanumeric+hyphen ID matching the _ID_RE pattern."""
    length = draw(st.integers(min_value=1, max_value=64))
    if length == 1:
        return draw(_id_edge)
    inner = draw(st.text(alphabet=_id_inner, min_size=length - 2, max_size=length - 2))
    return draw(_id_edge) + inner + draw(_id_edge)


@st.composite
def invalid_id_string(draw: st.DrawFn) -> str:
    """Produce strings that violate the lowercase-alphanumeric-and-hyphen rule.

    Covers: empty, length > 64, leading hyphen, trailing hyphen, uppercase,
    illegal characters (underscores, dots, slashes, and a trailing newline).

    The trailing-newline slice (`0\n`) is included: `cairn_oci.categories._ID_RE`
    is `\\A...\\Z`-anchored (NOT `^...$`), so `\\Z` matches end-of-string only and
    a value like `0\n` fails closed rather than slipping past a `$` that would
    also match just before a trailing newline.
    """
    kind = draw(
        st.sampled_from(
            [
                "empty",
                "too_long",
                "leading_hyphen",
                "trailing_hyphen",
                "uppercase",
                "illegal_char",
                "trailing_newline",
            ]
        )
    )
    if kind == "empty":
        return ""
    if kind == "too_long":
        return draw(st.text(alphabet=_id_edge, min_size=65, max_size=100))
    if kind == "leading_hyphen":
        body = draw(st.text(alphabet=_id_inner, min_size=1, max_size=63))
        return "-" + body
    if kind == "trailing_hyphen":
        body = draw(st.text(alphabet=_id_inner, min_size=1, max_size=63))
        return body + "-"
    if kind == "uppercase":
        body = draw(st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=1, max_size=10))
        return body
    if kind == "trailing_newline":
        body = draw(st.text(alphabet=_id_edge, min_size=1, max_size=10))
        return body + "\n"
    # illegal_char
    body = draw(st.text(alphabet=_id_edge, min_size=1, max_size=10))
    bad = draw(st.sampled_from("_./ \t!@#$%^&*()"))
    return body + bad


@st.composite
def valid_qualifier(draw: st.DrawFn) -> str:
    """{regionCode}-{stage} where each segment is lowercase alphanumeric."""
    region = draw(st.text(alphabet=_id_edge, min_size=1, max_size=6))
    stage = draw(st.text(alphabet=_id_edge, min_size=1, max_size=6))
    return f"{region}-{stage}"


def _layer(
    title: str = "evidence-a.json",
    media_type: str = "application/json",
    digest: str = "sha256:" + "0" * 64,
) -> Descriptor:
    return Descriptor(
        mediaType=media_type,
        digest=digest,
        size=10,
        annotations={LAYER_REQUIRED_TITLE: title},
    )


def _valid_manifest_dict() -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": CONFIG_BLOB_MEDIA_TYPE,
            "digest": "sha256:" + "1" * 64,
            "size": 100,
        },
        "layers": [
            {
                "mediaType": "application/json",
                "digest": "sha256:" + "0" * 64,
                "size": 10,
                "annotations": {LAYER_REQUIRED_TITLE: "ev.json"},
            }
        ],
        "annotations": {
            CairnAnnotations.PROJECT_ID: "alpha-bank",
        },
    }


_VALID_CONFIG_BODY = json.dumps(
    {
        "projectId": "alpha-bank",
        "catalogOrProfileUuid": "22222222-2222-4222-8222-222222222222",
        "assessmentPeriod": "2026-q1",
    }
).encode("utf-8")


# ---------------------------------------------------------------------------
# [INGEST-003.04] — Bundle constraints: layer count 1..100 AND per-artifact size <=10 GB
# ---------------------------------------------------------------------------
# Per-artifact byte size cannot be exercised directly via Hypothesis without
# allocating up to 15 GB; we test the policy via the layer-count axis (the
# accepted-rejected boundary the validator can be invoked with cheaply) and the
# size axis is exercised through a separate explicit boundary test rather than
# `@given`-style enumeration.


_MAX_LAYERS = 100
_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024 * 1024


@pytest.mark.skip(
    reason=(
        "[INGEST-003.04] layer-count axis — NOT IMPLEMENTED. "
        "Spec calls for the validator to reject bundles with 0 layers or >100 "
        "layers, but cairn_validation.bundle.validate_bundle has no _MAX_LAYERS "
        "check today. Surfacing this as a backlog item: the admission-control "
        "surface where layer-count is enforced does not exist yet. Un-skip when "
        "that surface lands."
    )
)
def test_property_1_layer_count_boundary() -> None:
    """**WHEN** an Evidence_Bundle submission is evaluated for any combination of
    individual artifact size (0 GB to 15 GB) and content layer count (0 to 150)
    **THEN** the Evidence_Store accepts the bundle if and only if every individual
    artifact is at most 10 GB and the total number of content layers is between 1
    and 100 inclusive, and rejects any violating bundle with an error indicating
    which constraint was violated.

    Source: artifact-ingestion/spec.md:40-43 ([INGEST-003.04], layer-count axis).
    """
    raise AssertionError("skipped — see decorator")


@pytest.mark.skip(
    reason=(
        "[INGEST-003.04] size axis — COVERED ELSEWHERE at the Lambda fetch boundary. "
        "The 10 GB per-blob cap is enforced in lambdas/validate_evidence/.../"
        "ecr_client.py:_MAX_BLOB_BYTES and lambdas/archive_to_s3/.../"
        "ecr_client.py:_MAX_BLOB_BYTES; both have dedicated unit tests "
        "(tests/unit/cairn_lambda_validate_bundle/test_ecr_client.py::"
        "test_fetch_blob_enforces_size_cap and the archive-to-s3 equivalent). "
        "A property-test wrapper here would either replicate that coverage or "
        "encode a policy invariant nothing in the codebase actually checks "
        "against. Skip until/unless the cap moves to libs/validation."
    )
)
def test_property_1_artifact_size_acceptance_biconditional_on_10gb_limit() -> None:
    """**WHEN** an Evidence_Bundle submission is evaluated for any combination of
    individual artifact size (0 GB to 15 GB) and content layer count (0 to 150)
    **THEN** the Evidence_Store accepts the bundle if and only if every individual
    artifact is at most 10 GB ...

    Source: artifact-ingestion/spec.md:40-43 ([INGEST-003.04], size axis).
    """
    raise AssertionError("skipped — see decorator")


# ---------------------------------------------------------------------------
# [INGEST-006.02] — Digest computation determinism
# ---------------------------------------------------------------------------


@given(payload=st.binary(max_size=4096))
def test_property_2_digest_determinism_repeated_computation(payload: bytes) -> None:
    """**WHEN** the SHA-256 digest of any given byte sequence is computed twice by
    the Evidence_Store **THEN** the Evidence_Store produces the same result both
    times, and the result matches the standard SHA-256 algorithm output for that
    byte sequence.

    Source: artifact-ingestion/spec.md:56-59 ([INGEST-006.02]).
    """
    first = compute_sha256_digest(payload)
    second = compute_sha256_digest(payload)
    assert first == second
    expected = "sha256:" + hashlib.sha256(payload).hexdigest()
    assert first == expected


@given(payload=st.binary(max_size=4096))
def test_property_2_digest_determinism_across_bytes_and_stream_inputs(payload: bytes) -> None:
    """**WHEN** the SHA-256 digest of any given byte sequence is computed twice by
    the Evidence_Store **THEN** the Evidence_Store produces the same result both
    times, and the result matches the standard SHA-256 algorithm output for that
    byte sequence.

    Source: artifact-ingestion/spec.md:56-59 ([INGEST-006.02] — stream and bytes paths
    must agree, otherwise the streamed digest of a 10 GB blob could disagree with
    the in-memory computation in tests).
    """
    via_bytes = compute_sha256_digest(payload)
    via_stream = compute_sha256_digest(io.BytesIO(payload))
    assert via_bytes == via_stream


# ---------------------------------------------------------------------------
# [INGEST-007.03] — Digest verification correctness
# ---------------------------------------------------------------------------


@given(blob=st.binary(max_size=2048), other=st.binary(max_size=2048))
def test_property_3_verify_digest_truth_table(blob: bytes, other: bytes) -> None:
    """**WHEN** the digest verification function is invoked with any blob content
    and any expected digest pair **THEN** the function returns `true` if and only
    if the SHA-256 of the blob content equals the expected digest, and on failure
    returns an error that includes both the computed digest and the expected
    digest.

    Source: artifact-ingestion/spec.md:77-80 ([INGEST-007.03]).
    """
    correct = compute_sha256_digest(blob)
    assert verify_digest(blob, correct) is True

    other_digest = compute_sha256_digest(other)
    if other_digest == correct:
        assert verify_digest(blob, other_digest) is True
        return

    with pytest.raises(IntegrityError) as excinfo:
        verify_digest(blob, other_digest)
    err = excinfo.value
    assert err.computed == correct
    assert err.expected == other_digest
    msg = str(err)
    assert err.computed in msg
    assert err.expected in msg


# ---------------------------------------------------------------------------
# [INGEST-017.07] (a-e) — Bundle validation completeness
# ---------------------------------------------------------------------------


@given(missing=st.sampled_from(sorted(MANIFEST_REQUIRED)))
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=20)
def test_property_4a_any_missing_required_manifest_annotation_rejected(missing: str) -> None:
    """**WHEN** an Evidence_Bundle is validated **THEN** the Evidence_Store accepts
    the bundle if and only if (a) all required manifest-level annotations are
    present (project-id, controls, control-family, catalog-or-profile-uuid in
    OSCAL UUIDDatatype format, assessment-period, bundle-type, creation timestamp
    in RFC 3339), (b) every content layer has the required
    `org.opencontainers.image.title` annotation, (c) the Config_Blob has media
    type `application/vnd.cairnproofs.artifact.config.v1+json` and contains valid JSON
    with required fields including a well-formed `catalogOrProfileUuid`, (d) the
    manifest `bundle-type` annotation value is in the Artifact_Kind controlled
    vocabulary, and (e) for bundles containing any layer with an
    `application/oscal.*` media type, the Config_Blob includes a non-empty
    `oscalSchemaVersion` field; on failure the error identifies all failing
    criteria.

    Source: artifact-ingestion/spec.md:216-219 ([INGEST-017.07], criterion a).
    """
    manifest_dict = _valid_manifest_dict()
    manifest_dict["annotations"].pop(missing)
    manifest = OciManifest.model_validate(manifest_dict)
    result = validate_bundle(manifest, _VALID_CONFIG_BODY)
    assert not result.ok
    assert any(e.criterion == "a" and missing in e.field for e in result.errors), (
        f"removing {missing} must surface criterion (a) error; got {result.errors}"
    )


@given(layer_count=st.integers(min_value=1, max_value=5))
def test_property_4b_layer_without_title_annotation_rejected(layer_count: int) -> None:
    """**WHEN** an Evidence_Bundle is validated **THEN** the Evidence_Store accepts
    the bundle if and only if ... (b) every content layer has the required
    `org.opencontainers.image.title` annotation ...

    Source: artifact-ingestion/spec.md:216-219 ([INGEST-017.07], criterion b).
    """
    manifest_dict = _valid_manifest_dict()
    manifest_dict["layers"] = [
        {
            "mediaType": "application/json",
            "digest": "sha256:" + f"{i:064x}",
            "size": 10,
            "annotations": ({LAYER_REQUIRED_TITLE: f"ok-{i}"} if i != 0 else {}),
        }
        for i in range(layer_count)
    ]
    manifest = OciManifest.model_validate(manifest_dict)
    result = validate_bundle(manifest, _VALID_CONFIG_BODY)
    assert not result.ok
    assert any(e.criterion == "b" for e in result.errors)


def test_property_4c_wrong_config_blob_media_type_rejected() -> None:
    """**WHEN** an Evidence_Bundle is validated **THEN** ... (c) the Config_Blob
    has media type `application/vnd.cairnproofs.artifact.config.v1+json` and contains
    valid JSON with required fields including a well-formed `catalogOrProfileUuid` ...

    Source: artifact-ingestion/spec.md:216-219 ([INGEST-017.07], criterion c).
    """
    manifest_dict = _valid_manifest_dict()
    manifest_dict["config"]["mediaType"] = "application/json"
    manifest = OciManifest.model_validate(manifest_dict)
    result = validate_bundle(manifest, _VALID_CONFIG_BODY)
    assert not result.ok
    assert any(e.criterion == "c1" for e in result.errors)


@given(bad_category=st.text(min_size=1, max_size=30).filter(_does_not_resolve))
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=30)
def test_property_4d_bundle_type_outside_vocabulary_rejected(bad_category: str) -> None:
    """**WHEN** an Evidence_Bundle's claim is validated **THEN** ... (d) the
    Producer_Attestation_Referrer `artifactKind` value resolves to an Artifact_Kind
    controlled-vocabulary value ...

    The artifact-kind acceptance criterion lives on the signed
    Producer_Attestation_Referrer claim, not the content Config_Blob / manifest
    annotation, so criterion (d) fires from `validate_claim`, not
    `validate_bundle`.

    Source: artifact-ingestion/spec.md ([INGEST-017.07], criterion d — claim leg).
    """
    claim = ProducerAttestationReferrer(
        orgId="org-acme",
        tenantId="tnt-alpha-bank",
        projectId="alpha-bank",
        artifactKind=bad_category,
    )
    errors = validate_claim(claim)
    assert any(e.criterion == "d" for e in errors)


@given(category=st.sampled_from(sorted(ARTIFACT_KINDS)))
def test_property_4d_bundle_type_inside_vocabulary_does_not_trigger_d(category: str) -> None:
    """**WHEN** an Evidence_Bundle's claim is validated **THEN** ... (d) the
    Producer_Attestation_Referrer `artifactKind` value resolves to an Artifact_Kind
    controlled-vocabulary value ...

    Source: artifact-ingestion/spec.md ([INGEST-017.07], criterion d — claim leg,
    positive space: an in-vocabulary `artifactKind` must NOT trigger criterion (d)).
    """
    claim = ProducerAttestationReferrer(
        orgId="org-acme", tenantId="tnt-alpha-bank", projectId="alpha-bank", artifactKind=category
    )
    errors = validate_claim(claim)
    assert not any(e.criterion == "d" for e in errors), (
        f"artifactKind {category!r} is in the vocabulary; criterion (d) must not fire: {errors}"
    )


@given(
    has_oscal_layer=st.booleans(),
    declared_version=st.one_of(st.none(), st.just(""), st.just("1.1.2")),
)
def test_property_4e_oscal_schema_version_is_never_a_validation_criterion(
    has_oscal_layer: bool, declared_version: str | None
) -> None:
    """**WHEN** an Evidence_Bundle is validated **THEN** the former [INGEST-017.07]
    criterion (e) never fires: the OSCAL schema version is content-derived during
    extraction, not a declared Config_Blob acceptance criterion, so a missing,
    empty, or present `oscalSchemaVersion` is irrelevant to bundle validation
    (there is no declared-version criterion (e)). Acceptance criteria
    are (a)-(d),(h).
    """
    manifest_dict = _valid_manifest_dict()
    if has_oscal_layer:
        manifest_dict["layers"].append(
            {
                "mediaType": "application/oscal.assessment-results+json",
                "digest": "sha256:" + "f" * 64,
                "size": 100,
                "annotations": {LAYER_REQUIRED_TITLE: "ar.json"},
            }
        )
    config = {
        "projectId": "alpha-bank",
        "catalogOrProfileUuid": "22222222-2222-4222-8222-222222222222",
        "assessmentPeriod": "2026-q1",
    }
    if declared_version is not None:
        config["oscalSchemaVersion"] = declared_version
    manifest = OciManifest.model_validate(manifest_dict)
    result = validate_bundle(manifest, json.dumps(config).encode("utf-8"))
    assert not any(e.criterion == "e" for e in result.errors), (
        f"criterion (e) must never fire (removed); declared_version={declared_version!r}, "
        f"has_oscal_layer={has_oscal_layer}, errors={result.errors}"
    )


# ---------------------------------------------------------------------------
# [INGEST-018.02] — Uniform repository path construction
# ---------------------------------------------------------------------------
# [INGEST-018] Uniform ECR Repository Path Grammar: there is exactly one
# repository path shape, built by ArtifactLocator.repo_name —
# artifact-{qualifier}/{org-id}/{tenant-id}/{project-id} — with NO artifact-kind
# path segment (the kind rides in the image tag) and nothing branching per
# segment.

_LOCATOR = ArtifactLocator()


@given(
    qualifier=valid_qualifier(),
    org_id=valid_lowercase_id(),
    tenant_id=valid_lowercase_id(),
    project_id=valid_lowercase_id(),
)
def test_property_5_uniform_repo_path_constructs_canonical_form(
    qualifier: str, org_id: str, tenant_id: str, project_id: str
) -> None:
    """**WHEN** the `ArtifactLocator` construction function is invoked with any
    valid `{org-id}`, `{tenant-id}`, and `{project-id}` **THEN** it produces the
    single repository path `artifact-{regionCode}-{stage}/{org-id}/{tenant-id}/
    {project-id}` — exactly three logical-hierarchy segments after the qualifier
    prefix, with no artifact-kind / category segment.

    Source: artifact-ingestion/spec.md ([INGEST-018.02] — positive space, the
    single uniform three-segment shape).
    """
    path = _LOCATOR.repo_name(
        deployment_qualifier=qualifier,
        org_id=org_id,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    assert path == f"artifact-{qualifier}/{org_id}/{tenant_id}/{project_id}"
    # Exactly three logical-hierarchy segments after the qualifier prefix: no
    # artifact-kind / category segment ever enters the path.
    assert path.count("/") == 3


@given(
    qualifier=valid_qualifier(),
    good_org=valid_lowercase_id(),
    good_tenant=valid_lowercase_id(),
    good_project=valid_lowercase_id(),
    bad_segment=invalid_id_string(),
    position=st.sampled_from(["org_id", "tenant_id", "project_id"]),
)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_property_5_uniform_repo_path_rejects_invalid_segment(
    qualifier: str,
    good_org: str,
    good_tenant: str,
    good_project: str,
    bad_segment: str,
    position: str,
) -> None:
    """**WHEN** the `ArtifactLocator` construction function is invoked with an
    `{org-id}`, `{tenant-id}`, or `{project-id}` that violates the segment format
    constraints **THEN** it rejects the input with `InvalidIdError`, on ANY of the
    three segments.

    Source: artifact-ingestion/spec.md ([INGEST-018.02] — negative space: an id
    violating the lowercase-alnum-hyphen 1..64 rule on any of the three segments
    is rejected).
    """
    kwargs: dict[str, str] = {
        "deployment_qualifier": qualifier,
        "org_id": good_org,
        "tenant_id": good_tenant,
        "project_id": good_project,
    }
    kwargs[position] = bad_segment
    with pytest.raises(InvalidIdError):
        _LOCATOR.repo_name(**kwargs)


# ---------------------------------------------------------------------------
# OSCAL schema version is content-derived ([OSCAL-001])
# ---------------------------------------------------------------------------
# The OSCAL schema version is derived from the document's own
# metadata.oscal-version; there is no producer-declared `oscalSchemaVersion` and
# no declared<->embedded cross-check.


@given(has_oscal_layer=st.booleans())
def test_oscal_schema_version_is_not_a_declared_validation_criterion(
    has_oscal_layer: bool,
) -> None:
    """**WHEN** an Evidence_Bundle with or without an `application/oscal.*` layer
    is validated and its Config_Blob omits `oscalSchemaVersion` **THEN** the
    Evidence_Store does NOT raise the former criterion (e): the OSCAL schema
    version is content-derived during extraction, never a declared Config_Blob
    acceptance criterion ([OSCAL-001] — single source of truth is the
    document's metadata.oscal-version).
    """
    manifest_dict = _valid_manifest_dict()
    if has_oscal_layer:
        manifest_dict["layers"].append(
            {
                "mediaType": "application/oscal.system-security-plan+json",
                "digest": "sha256:" + "a" * 64,
                "size": 100,
                "annotations": {LAYER_REQUIRED_TITLE: "ssp.json"},
            }
        )
    config_body_no_version = json.dumps(
        {
            "projectId": "alpha-bank",
            "catalogOrProfileUuid": "22222222-2222-4222-8222-222222222222",
            "assessmentPeriod": "2026-q1",
        }
    ).encode("utf-8")
    manifest = OciManifest.model_validate(manifest_dict)
    result = validate_bundle(manifest, config_body_no_version)
    assert not any(e.criterion == "e" for e in result.errors)

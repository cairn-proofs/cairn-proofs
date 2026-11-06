"""Unit tests for the validate-bundle Lambda handler.

Covers the INVERTED trigger (the Producer_Attestation_Referrer push is the single positive
trigger), the subject-scope anti-hijack binding, claim-sourced
accountId/projectId/artifact-kind/control cross-checks, per-OSCAL-layer temporal
consistency, OSCAL extraction + evidence reference, and the Lambda-level metric
emission and observability_scope wrap.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest
from cairn_oci import (
    INTOTO_STATEMENT_MEDIA_TYPE,
    PRODUCER_PREDICATE_TYPE,
)

from tests.unit.cairn_lambda_validate_bundle.conftest import (
    ACCOUNT_ID,
    ARCHIVE_BUCKET,
    CATALOG_UUID,
    EVENT_BUS_NAME,
    LAST_MODIFIED,
    ORG_ID,
    OSCAL_VERSION,
    PROJECT_ID,
    PUSHED_AT,
    REPO_NAME,
    TENANT_ID,
    AttestationFixture,
    BundleFixture,
    attestation_claim,
    attestation_push_event,
    build_attestation,
    build_bundle,
    catalog_blob_bytes,
    config_blob_body,
    ecr_push_event,
    seed_metadata_index,
    submit,
)


def _parse_emf_metrics(out: str) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for line in out.splitlines():
        if "_aws" not in line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "_aws" in doc:
            docs.append(doc)
    return docs


def _metric_value(doc: dict[str, Any], name: str) -> float | None:
    value = doc.get(name)
    if isinstance(value, list):
        return value[0] if value else None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _emf_dimension_present(out: str, name: str, value: str) -> bool:
    return any(doc.get(name) == value for doc in _parse_emf_metrics(out))


# ===========================================================================
# Happy path — the Producer_Attestation_Referrer push is the positive trigger.
# ===========================================================================


def test_happy_path_emits_validated_with_full_payload(reload_handler, lambda_context) -> None:
    handler = reload_handler
    bundle = build_bundle()
    attestation, event = submit(handler, bundle=bundle)

    payload = handler.lambda_handler(event, lambda_context)

    recorder = handler._test_recorder
    assert recorder.detail_types() == ["artifact.validated"]
    emitted_entry = recorder.entries[0]
    assert emitted_entry["EventBusName"] == EVENT_BUS_NAME
    assert emitted_entry["Source"] == "dev.cairnproofs.artifact"

    # manifestDigest is the CONTENT digest; attestationDigest is the referrer's.
    assert payload["manifestDigest"] == bundle.manifest_digest
    assert payload["attestationDigest"] == attestation.attestation_digest
    # The projected claim rides on the payload (by_alias, exclude_none).
    assert payload["attestation"]["projectId"] == "alpha-bank"
    assert payload["attestation"]["artifactKind"] == "assessment-results"
    assert payload["attestation"]["assessmentPeriod"] == "2026-q2"
    assert payload["attestation"]["controls"] == ["ac-1", "ac-2"]

    assert payload["repositoryName"] == REPO_NAME
    assert payload["registryId"] == ACCOUNT_ID
    assert payload["ecrPushedAt"] == PUSHED_AT
    assert payload["temporalConsistencyWarning"] is False
    principal = payload["authenticatedPushPrincipal"]
    assert principal["arn"].startswith("arn:aws:sts::")
    assert principal["accountId"] == ACCOUNT_ID
    assert principal["mfaAuthenticated"] is True
    # The content Config_Blob carries only content-descriptive fields
    # (projectId; no bundleType — artifact-kind is not a content-manifest field).
    assert payload["configBlob"]["projectId"] == "alpha-bank"
    assert "bundleType" not in payload["configBlob"]
    assert "controls" not in payload["configBlob"]
    assert "catalogOrProfileUuid" not in payload["configBlob"]
    assert payload["manifestAnnotations"]["dev.cairnproofs.artifact.project-id"] == "alpha-bank"
    assert len(payload["layers"]) == 1
    layer_meta = payload["layers"][0]
    # Universal OCI descriptor fields on every layer.
    assert layer_meta["digest"] == bundle.layer_digest
    assert layer_meta["size"] == len(bundle.layer_bytes)
    assert layer_meta["mediaType"] == "application/oscal+json"
    assert layer_meta["title"] == "catalog.json"
    # OSCAL enrichment carried in-line as a per-entry sub-object, not a separate list.
    oscal = layer_meta["oscal"]
    assert oscal["oscalSchemaVersion"] == OSCAL_VERSION
    assert oscal["metadata"]["doc_type"] == "catalog"
    assert oscal["metadata"]["doc_uuid"] == CATALOG_UUID
    # The per-layer last-modified is carried into the validated payload.
    assert oscal["metadata"]["last_modified"].startswith("2026-05-26")
    ref = payload["artifactResourceReference"]
    rlinks = ref["rlinks"]
    assert len(rlinks) == 2
    assert any(rl["href"].startswith(f"s3://{ARCHIVE_BUCKET}/") for rl in rlinks)
    assert any(rl["href"].startswith(f"{ACCOUNT_ID}.dkr.ecr.") for rl in rlinks)


def test_build_side_referrer_media_type_equals_validate_trigger_constant(
    reload_handler,
) -> None:
    """The producer-EMIT media type and the validate-COMPARE discriminator
    constant must not drift apart.

    Both resolve from the single source in ``libs/oci/cairn_oci`` (the rename
    touches one definition). This round-trip asserts they agree end-to-end: the
    ``artifactType`` stamped onto a built Producer_Attestation_Referrer (the
    build/emit side) is byte-for-byte the constant the validate handler keys its
    single-constant trigger compare on (the check side), and it carries the new
    scoped ``producer-attestation`` spelling.
    """
    handler = reload_handler
    bundle = build_bundle()
    attestation = build_attestation(subject_digest=bundle.manifest_digest)

    build_side_artifact_type = attestation.manifest_dict["artifactType"]

    # Emit side == check side: the built referrer's envelope artifactType is
    # exactly the discriminator constant the handler compares against.
    assert build_side_artifact_type == handler.PRODUCER_ATTESTATION_ARTIFACT_TYPE
    # ...and that single-source value carries the new scoped spelling.
    assert build_side_artifact_type == "application/vnd.cairnproofs.producer-attestation.v1+json"


def test_claim_is_read_from_the_layer_not_the_empty_config(reload_handler, lambda_context) -> None:
    """Regression (live-push wire mismatch): `cairn push` attaches the claim via
    `oras attach`, which carries the claim payload as the referrer's LAYER over an
    EMPTY OCI scratch config. Validate MUST read the claim from that layer — an
    earlier config-read parsed the empty `{}` config and rejected every real push
    as `malformed_attestation_claim` (projectId/artifactKind "Field required").

    The claim layer carries a
    standard in-toto Statement (layer mediaType `application/vnd.in-toto+json`);
    the OCI envelope `artifactType` stays the Cairn role label / trigger
    discriminator (asserted in the trigger tests).
    """
    handler = reload_handler
    bundle = build_bundle()
    attestation, event = submit(handler, bundle=bundle)

    # Pin the real oras-attach wire shape the fixture now models: empty config
    # (NOT the claim), in-toto Statement claim in the layer.
    config = attestation.manifest_dict["config"]
    assert config["mediaType"] == "application/vnd.oci.empty.v1+json"
    assert config["digest"] != attestation.claim_digest
    claim_layer = attestation.manifest_dict["layers"][0]
    assert claim_layer["mediaType"] == INTOTO_STATEMENT_MEDIA_TYPE
    assert claim_layer["digest"] == attestation.claim_digest

    payload = handler.lambda_handler(event, lambda_context)

    # The claim was read from the layer and projected onto the validated payload.
    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    assert payload["attestation"]["projectId"] == "alpha-bank"
    assert payload["attestation"]["artifactKind"] == "assessment-results"


def test_handler_emits_success_metric_on_validated_path(
    reload_handler, lambda_context, capsys
) -> None:
    handler = reload_handler
    _attestation, event = submit(handler, bundle=build_bundle())
    handler.lambda_handler(event, lambda_context)

    docs = _parse_emf_metrics(capsys.readouterr().out)
    assert docs
    assert any(_metric_value(d, "Success") == 1.0 for d in docs)
    assert any(_metric_value(d, "DurationMs") is not None for d in docs)


# ===========================================================================
# Inverted trigger / artifact filter — EVERY non-attestation push is SKIPPED.
# ===========================================================================


def _internal_config_manifest(config_media_type: str) -> tuple[str, dict[str, Any], str]:
    from cairn_oci import compute_sha256_digest

    manifest: dict[str, Any] = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": config_media_type,
            "digest": "sha256:" + "c" * 64,
            "size": 100,
        },
        "layers": [],
    }
    raw = json.dumps(manifest, separators=(",", ":"))
    digest = compute_sha256_digest(raw.encode("utf-8"))
    return digest, manifest, raw


def _event_for_digest(digest: str) -> dict[str, Any]:
    bundle = dataclasses.replace(build_bundle(), manifest_digest=digest)
    return ecr_push_event(bundle=bundle)


def test_content_only_manifest_push_is_skipped(reload_handler, lambda_context, capsys) -> None:
    """The content manifest's OWN push is now a benign skip (incomplete submission)
    — it is validated only transitively when its Producer_Attestation_Referrer arrives."""
    handler = reload_handler
    bundle = build_bundle()
    handler._test_fake_ecr.add_bundle(bundle)
    event = ecr_push_event(bundle=bundle)

    result = handler.lambda_handler(event, lambda_context)

    assert result is handler._SKIPPED
    assert handler._test_recorder.detail_types() == []
    out = capsys.readouterr().out
    assert any(_metric_value(d, "ValidationSkipped") == 1.0 for d in _parse_emf_metrics(out))
    assert _emf_dimension_present(out, "SkipReason", "content_manifest")
    assert not any(_metric_value(d, "Success") == 1.0 for d in _parse_emf_metrics(out))


def test_notary_signature_artifact_is_skipped_not_failed(
    reload_handler, lambda_context, capsys
) -> None:
    from cairn_oci import NOTARY_SIGNATURE_MEDIA_TYPE

    handler = reload_handler
    digest, manifest_dict, raw = _internal_config_manifest(NOTARY_SIGNATURE_MEDIA_TYPE)
    handler._test_fake_ecr.manifests[digest] = (manifest_dict, raw)
    event = _event_for_digest(digest)

    result = handler.lambda_handler(event, lambda_context)

    assert result is handler._SKIPPED
    assert handler._test_recorder.detail_types() == []
    out = capsys.readouterr().out
    assert any(_metric_value(d, "ValidationSkipped") == 1.0 for d in _parse_emf_metrics(out))
    assert _emf_dimension_present(out, "SkipReason", "notary_signature")
    assert not any(_metric_value(d, "Failure") == 1.0 for d in _parse_emf_metrics(out))


def test_oci_empty_config_artifact_is_skipped_not_failed(
    reload_handler, lambda_context, capsys
) -> None:
    from cairn_oci import OCI_EMPTY_MEDIA_TYPE

    handler = reload_handler
    digest, manifest_dict, raw = _internal_config_manifest(OCI_EMPTY_MEDIA_TYPE)
    handler._test_fake_ecr.manifests[digest] = (manifest_dict, raw)
    event = _event_for_digest(digest)

    result = handler.lambda_handler(event, lambda_context)

    assert result is handler._SKIPPED
    assert handler._test_recorder.detail_types() == []
    out = capsys.readouterr().out
    assert _emf_dimension_present(out, "SkipReason", "oci_empty")


def test_image_index_manifest_is_skipped(reload_handler, lambda_context, capsys) -> None:
    from cairn_oci import compute_sha256_digest

    handler = reload_handler
    manifest: dict[str, Any] = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:" + "b" * 64,
                "size": 512,
            }
        ],
    }
    raw = json.dumps(manifest, separators=(",", ":"))
    digest = compute_sha256_digest(raw.encode("utf-8"))
    handler._test_fake_ecr.manifests[digest] = (manifest, raw)
    event = _event_for_digest(digest)

    result = handler.lambda_handler(event, lambda_context)

    assert result is handler._SKIPPED
    assert handler._test_recorder.detail_types() == []
    out = capsys.readouterr().out
    assert _emf_dimension_present(out, "SkipReason", "image_index")


def test_unrecognized_artifact_is_skipped_and_left_inert(
    reload_handler, lambda_context, capsys
) -> None:
    """An unrecognized manifest (neither attestation, content, nor a known
    pipeline-internal shape) is skipped and never promoted."""
    handler = reload_handler
    digest, manifest_dict, raw = _internal_config_manifest("application/some.other.config")
    handler._test_fake_ecr.manifests[digest] = (manifest_dict, raw)
    event = _event_for_digest(digest)

    result = handler.lambda_handler(event, lambda_context)

    assert result is handler._SKIPPED
    assert handler._test_recorder.detail_types() == []
    out = capsys.readouterr().out
    assert _emf_dimension_present(out, "SkipReason", "not_attestation")


def test_attestation_without_subject_is_skipped(reload_handler, lambda_context, capsys) -> None:
    """An attestation manifest with NO subject cannot select content to promote —
    it is a benign non-trigger skip, never a permanent content failure."""
    handler = reload_handler
    bundle = build_bundle()
    handler._test_fake_ecr.add_bundle(bundle)
    attestation = build_attestation(subject_digest=bundle.manifest_digest, include_subject=False)
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)

    result = handler.lambda_handler(event, lambda_context)

    assert result is handler._SKIPPED
    assert handler._test_recorder.detail_types() == []
    out = capsys.readouterr().out
    assert _emf_dimension_present(out, "SkipReason", "attestation_missing_subject")


def test_genuine_attestation_is_never_skipped_and_records_success(
    reload_handler, lambda_context, capsys
) -> None:
    handler = reload_handler
    _attestation, event = submit(handler, bundle=build_bundle())

    result = handler.lambda_handler(event, lambda_context)

    assert result is not handler._SKIPPED
    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    docs = _parse_emf_metrics(capsys.readouterr().out)
    assert any(_metric_value(d, "Success") == 1.0 for d in docs)
    assert not any(_metric_value(d, "ValidationSkipped") is not None for d in docs)


# ===========================================================================
# [T-1/T-5] Subject-scope binding — foreign-scope subject is a permanent DLQ failure.
# ===========================================================================


def test_cross_scope_attestation_hijack_is_permanent_failure(
    reload_handler, lambda_context
) -> None:
    """An attestation in repo A whose subject content lives in a DIFFERENT repo
    path is a cross-scope hijack: permanent DLQ failure, no promotion."""
    handler = reload_handler
    # Content bundle lives in a DIFFERENT repo than the attestation.
    content_repo = "artifact-cmh-alpha/victim-tenant"
    bundle = build_bundle(repo_name=content_repo)
    handler._test_fake_ecr.add_bundle(bundle)  # residency = (victim repo, digest)

    attacker_repo = "artifact-cmh-alpha/attacker-tenant"
    attestation = build_attestation(subject_digest=bundle.manifest_digest, repo_name=attacker_repo)
    # residency = (attacker repo, attestation digest) — NOT the subject's repo.
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)

    # The subject digest does NOT reside in the attacker's repo → permanent failure.
    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    failed = handler._test_recorder.details("artifact.validation-failed")
    assert len(failed) == 1
    assert failed[0]["errorKind"] == "cross_scope_attestation_hijack"
    criteria = failed[0]["failedCriteria"]
    assert any(c["criterion"] == "subject_scope" for c in criteria)
    msg = " ".join(c["message"] for c in criteria)
    assert bundle.manifest_digest in msg
    assert attacker_repo in msg
    # No promotion: no artifact.validated emitted.
    assert handler._test_recorder.details("artifact.validated") == []
    # In-band DLQ rejection recorded.
    assert handler._test_fake_sqs.bodies()


def test_subject_scope_check_runs_before_resolve(reload_handler, lambda_context) -> None:
    """The scope check precedes the content-manifest fetch: a foreign subject is
    rejected even though the content manifest IS fetchable from its own repo."""
    handler = reload_handler
    bundle = build_bundle(repo_name="artifact-cmh-alpha/victim-tenant")
    handler._test_fake_ecr.add_bundle(bundle)
    attestation = build_attestation(
        subject_digest=bundle.manifest_digest, repo_name="artifact-cmh-alpha/attacker-tenant"
    )
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    # The content manifest was NEVER fetched (rejected at the scope gate).
    fetched = [d for (_r, _repo, d) in handler._test_fake_ecr.manifest_fetch_log]
    assert bundle.manifest_digest not in fetched


def test_transient_scope_check_error_retries_not_dlq(reload_handler, lambda_context) -> None:
    """A transient ECR fault on the scope HEAD re-raises (Lambda retries), never a
    cross-scope misclassification."""
    from cairn_lambda_validate_bundle.ecr_client import EcrFetchError

    handler = reload_handler
    bundle = build_bundle()
    handler._test_fake_ecr.add_bundle(bundle)
    attestation = build_attestation(subject_digest=bundle.manifest_digest)
    handler._test_fake_ecr.add_attestation(attestation)
    handler._test_fake_ecr.residency_side_effects[bundle.manifest_digest] = EcrFetchError("outage")
    event = attestation_push_event(attestation=attestation)

    with pytest.raises(EcrFetchError):
        handler.lambda_handler(event, lambda_context)
    assert handler._test_recorder.details("artifact.validation-failed") == []


# ===========================================================================
# Cross-checks sourced from the claim ([PIPELINE-013]).
# ===========================================================================


def test_tenant_id_mismatch_from_claim_routes_to_validation_failed(
    reload_handler, lambda_context
) -> None:
    """A claimed tenantId disagreeing with the ECR path's {tenant-id} segment is a
    permanent failure under the uniform org+tenant+project cross-check (design
    D.1/D.5 — no deployment-mode gate)."""
    handler = reload_handler
    bundle = build_bundle(repo_name=REPO_NAME)
    handler._test_fake_ecr.add_bundle(bundle)
    # The CLAIM asserts a foreign tenantId; org-id and project-id still match the
    # path, so only the tenant coordinate fails the exact-match cross-check.
    attestation = build_attestation(
        subject_digest=bundle.manifest_digest,
        repo_name=REPO_NAME,
        claim_bytes=attestation_claim(tenant_id="tnt-foreign"),
    )
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    criteria = failed[-1]["failedCriteria"]
    tenant_errors = [
        c for c in criteria if c["criterion"] == "f" and c["field"] == "attestation.tenantId"
    ]
    assert tenant_errors
    msg = tenant_errors[0]["message"]
    assert "tnt-foreign" in msg
    assert TENANT_ID in msg


def test_project_id_mismatch_from_claim_routes_to_validation_failed(
    reload_handler, lambda_context
) -> None:
    """A claimed projectId disagreeing with the ECR path's project-id segment is a
    permanent failure naming both values (Customer_Hosted)."""
    handler = reload_handler
    bundle = build_bundle(repo_name=REPO_NAME)
    handler._test_fake_ecr.add_bundle(bundle)
    attestation = build_attestation(
        subject_digest=bundle.manifest_digest,
        repo_name=REPO_NAME,
        claim_bytes=attestation_claim(project_id="other-tenant"),
    )
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    criteria = failed[0]["failedCriteria"]
    project_errors = [
        c for c in criteria if c["criterion"] == "f" and c["field"] == "attestation.projectId"
    ]
    assert project_errors
    msg = project_errors[0]["message"]
    assert "other-tenant" in msg
    assert "alpha-bank" in msg


def test_matching_project_id_from_claim_proceeds(reload_handler, lambda_context) -> None:
    handler = reload_handler
    _attestation, event = submit(
        handler,
        bundle=build_bundle(),
        claim_bytes=attestation_claim(project_id="alpha-bank"),
    )
    payload = handler.lambda_handler(event, lambda_context)
    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    assert payload["attestation"]["projectId"] == "alpha-bank"


# ===========================================================================
# T4 anti-mass-DLQ regression — kind is not a content-manifest field.
# ===========================================================================


def test_legless_bundle_validates_and_does_not_mass_dlq(reload_handler, lambda_context) -> None:
    """Anti-mass-DLQ regression.

    A content bundle that omits BOTH content-manifest kind legs — no
    ``dev.cairnproofs.artifact.artifact-kind`` manifest annotation AND no
    ``Config_Blob.bundleType`` — MUST still VALIDATE. Criterion (a) does not
    require kind, so a producer that omits those fields cannot route every
    bundle to the DLQ.

    Distinct from the happy-path payload test: this pins the *negative* — NO
    ``artifact.validation-failed`` event and NO in-band DLQ rejection — which is
    exactly what a re-introduced kind requirement would break.
    """
    handler = reload_handler
    # The legless shape is the default now, but assert it explicitly so the
    # regression cannot silently degrade if the fixtures change.
    bundle = build_bundle()
    assert "bundleType" not in json.loads(bundle.config_bytes)
    # The kind annotation key is not a `cairn_oci.annotations` constant, so pin
    # the literal key: it must not be present on the content manifest.
    assert "dev.cairnproofs.artifact.artifact-kind" not in bundle.manifest_dict["annotations"]

    _attestation, event = submit(handler, bundle=bundle)

    result = handler.lambda_handler(event, lambda_context)

    # Validated, not rejected.
    assert result is not handler._REJECTED
    assert result is not handler._SKIPPED
    detail_types = handler._test_recorder.detail_types()
    assert "artifact.validated" in detail_types
    # The load-bearing anti-mass-DLQ assertions: no failure event, no DLQ.
    assert "artifact.validation-failed" not in detail_types
    assert handler._test_fake_sqs.messages == []


# ===========================================================================
# Claim-derived [INGEST-017.07] / [INGEST-029.06] — malformed artifact-kind / uuid / control id.
# ===========================================================================


def test_invalid_artifact_kind_in_claim_routes_to_validation_failed(
    reload_handler, lambda_context
) -> None:
    handler = reload_handler
    _attestation, event = submit(
        handler,
        bundle=build_bundle(),
        claim_bytes=attestation_claim(artifact_kind="not-a-real-kind"),
    )
    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    criteria = failed[0]["failedCriteria"]
    assert any(c["criterion"] == "d" and c["field"] == "attestation.artifactKind" for c in criteria)


def test_malformed_catalog_uuid_in_claim_routes_to_validation_failed(
    reload_handler, lambda_context
) -> None:
    handler = reload_handler
    _attestation, event = submit(
        handler,
        bundle=build_bundle(),
        claim_bytes=attestation_claim(catalog_or_profile_uuid="not-a-uuid"),
    )
    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    criteria = failed[0]["failedCriteria"]
    assert any(c["criterion"] == "c5" for c in criteria)


def test_malformed_control_id_in_claim_routes_to_validation_failed(
    reload_handler, lambda_context
) -> None:
    handler = reload_handler
    _attestation, event = submit(
        handler,
        bundle=build_bundle(),
        claim_bytes=attestation_claim(controls=["ac-2", "ac#2"]),
    )
    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    criteria = failed[0]["failedCriteria"]
    h_errors = [c for c in criteria if c["criterion"] == "h"]
    assert h_errors
    assert any("'ac#2'" in c["message"] for c in h_errors)


def test_malformed_attestation_claim_routes_to_validation_failed(
    reload_handler, lambda_context
) -> None:
    """An attestation whose Statement predicate omits required fields (artifactKind)
    is a permanent failure classified malformed_attestation_claim — routed through
    the SAME fail-closed malformed-claim → DLQ branch."""
    handler = reload_handler
    bundle = build_bundle()
    handler._test_fake_ecr.add_bundle(bundle)
    # A structurally-valid in-toto Statement whose predicate is missing the
    # required artifactKind field → ProducerAttestationReferrer schema ValidationError
    # inside read_claim_from_statement.
    bad_statement = json.dumps(
        {
            "_type": "https://in-toto.io/Statement/v1",
            "subject": [{"digest": {"sha256": bundle.manifest_digest.split(":", 1)[-1]}}],
            "predicateType": PRODUCER_PREDICATE_TYPE,
            "predicate": {"projectId": "alpha-bank"},
        }
    ).encode("utf-8")
    attestation = build_attestation(
        subject_digest=bundle.manifest_digest, wrap_statement=False, layer_bytes=bad_statement
    )
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    assert failed[0]["errorKind"] == "malformed_attestation_claim"


# ===========================================================================
# Content-manifest structural failures (resolved via the subject).
# ===========================================================================


def test_missing_required_manifest_annotation_routes_to_validation_failed(
    reload_handler, lambda_context
) -> None:
    handler = reload_handler
    bundle = build_bundle(drop_annotations=("dev.cairnproofs.artifact.project-id",))
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    fields = [c["field"] for c in failed[0]["failedCriteria"]]
    assert any("dev.cairnproofs.artifact.project-id" in f for f in fields)


def test_bad_config_blob_media_type_routes_to_validation_failed(
    reload_handler, lambda_context
) -> None:
    handler = reload_handler
    bundle = build_bundle(config_media_type="application/json")
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    assert any(c["criterion"] == "c1" for c in failed[0]["failedCriteria"])


def test_missing_layer_title_routes_to_validation_failed(reload_handler, lambda_context) -> None:
    handler = reload_handler
    bundle = build_bundle(layer_title=None)
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    fields = [c["field"] for c in failed[0]["failedCriteria"]]
    assert any("org.opencontainers.image.title" in f for f in fields)


# ===========================================================================
# Per-OSCAL-layer temporal consistency ([PIPELINE-009]) — ecrPushedAt from EventBridge.
# ===========================================================================


def test_future_dated_oscal_last_modified_routes_to_validation_failed(
    reload_handler, lambda_context
) -> None:
    """A layer whose metadata.last-modified is strictly after ecrPushedAt is a
    permanent failure (evidence cannot be authored after it was submitted)."""
    handler = reload_handler
    bundle = build_bundle(layer_bytes=catalog_blob_bytes(last_modified="2030-01-01T00:00:00Z"))
    _attestation, event = submit(handler, bundle=bundle, pushed_at="2026-05-26T12:00:00Z")

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    criteria = failed[0]["failedCriteria"]
    temporal = [c for c in criteria if c["criterion"] == "temporal"]
    assert temporal
    assert "metadata.last-modified" in temporal[0]["field"]


def test_stale_oscal_last_modified_within_window_emits_validated_with_warning(
    reload_handler, lambda_context
) -> None:
    handler = reload_handler
    # last-modified is ~5 months before push — beyond the 30-day staleness window.
    bundle = build_bundle(layer_bytes=catalog_blob_bytes(last_modified="2026-01-01T00:00:00Z"))
    _attestation, event = submit(handler, bundle=bundle, pushed_at="2026-05-26T12:00:00Z")

    payload = handler.lambda_handler(event, lambda_context)

    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    assert payload["temporalConsistencyWarning"] is True


def test_bundle_with_no_in_band_timestamp_skips_temporal_check(
    reload_handler, lambda_context
) -> None:
    """A non-OSCAL bundle has no in-band timestamp → neither rejected nor warned
    on temporal grounds (the per-layer check is skipped gracefully)."""
    handler = reload_handler
    bundle = build_bundle(
        layer_media_type="text/plain",
        layer_bytes=b"plain evidence",
        layer_title="note.txt",
    )
    # Push time deliberately far in the past relative to nothing — no timestamp exists.
    _attestation, event = submit(handler, bundle=bundle, pushed_at="2020-01-01T00:00:00Z")

    payload = handler.lambda_handler(event, lambda_context)

    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    assert payload["temporalConsistencyWarning"] is False
    # A non-OSCAL bundle still enumerates its single layer, with NO oscal sub-object.
    assert len(payload["layers"]) == 1
    assert "oscal" not in payload["layers"][0]


def test_temporal_check_layers_unit_in_window_no_warn() -> None:
    from cairn_lambda_validate_bundle import handler as handler_module

    layers = [{"digest": "sha256:a", "oscal": {"metadata": {"last_modified": LAST_MODIFIED}}}]
    failures, warn = handler_module._temporal_check_layers(
        layers=layers, ecr_pushed_at_iso=PUSHED_AT
    )
    assert failures == []
    assert warn is False


def test_temporal_check_layers_unit_future_is_failure() -> None:
    from cairn_lambda_validate_bundle import handler as handler_module

    layers = [
        {"digest": "sha256:a", "oscal": {"metadata": {"last_modified": "2030-01-01T00:00:00Z"}}}
    ]
    failures, warn = handler_module._temporal_check_layers(
        layers=layers, ecr_pushed_at_iso=PUSHED_AT
    )
    assert len(failures) == 1
    assert warn is False


def test_temporal_check_layers_unit_no_timestamp_is_noop() -> None:
    from cairn_lambda_validate_bundle import handler as handler_module

    layers = [{"digest": "sha256:a", "oscal": {"metadata": {}}}]
    failures, warn = handler_module._temporal_check_layers(
        layers=layers, ecr_pushed_at_iso=PUSHED_AT
    )
    assert failures == []
    assert warn is False


def test_temporal_check_layers_unit_non_oscal_layer_is_noop() -> None:
    """A unified-list entry with no `oscal` sub-object contributes no assertion."""
    from cairn_lambda_validate_bundle import handler as handler_module

    layers = [{"digest": "sha256:a", "mediaType": "text/plain", "title": "note.txt"}]
    failures, warn = handler_module._temporal_check_layers(
        layers=layers, ecr_pushed_at_iso=PUSHED_AT
    )
    assert failures == []
    assert warn is False


# ===========================================================================
# OSCAL extraction failures and version derivation.
# ===========================================================================


def test_oscal_parse_failure_routes_to_validation_failed(reload_handler, lambda_context) -> None:
    handler = reload_handler
    bundle = build_bundle(layer_bytes=b"{this-is-not-valid-oscal-json")
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    assert failed[0]["errorKind"] == "parse_failure"
    assert failed[0]["layerDigest"] == bundle.layer_digest


def test_oscal_bundle_version_is_derived_from_content(reload_handler, lambda_context) -> None:
    handler = reload_handler
    bundle = build_bundle()
    _attestation, event = submit(handler, bundle=bundle)

    payload = handler.lambda_handler(event, lambda_context)
    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    assert payload["layers"][0]["oscal"]["oscalSchemaVersion"] == OSCAL_VERSION


def test_unsupported_non_1x_embedded_version_routes_to_validation_failed(
    reload_handler, lambda_context
) -> None:
    handler = reload_handler
    bundle = build_bundle(layer_bytes=catalog_blob_bytes(oscal_version="2.0.0"))
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    assert failed[0]["errorKind"] == "unsupported_version"


# ===========================================================================
# Authenticated principal + malformed event (permanent) and transient errors.
# ===========================================================================


def test_missing_user_identity_emits_validation_failed(reload_handler, lambda_context) -> None:
    handler = reload_handler
    _attestation, event = submit(handler, bundle=build_bundle(), omit_user_identity=True)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    assert failed[0]["errorKind"] == "missing_user_identity"


def test_malformed_event_emits_validation_failed(reload_handler, lambda_context) -> None:
    handler = reload_handler
    event = {"detail-type": "AWS API Call via CloudTrail", "detail": {}}

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    assert failed[0]["errorKind"] == "malformed_event"
    assert failed[0]["manifestDigest"] is None


def test_ecr_fetch_error_is_transient_no_validation_failed_emitted(
    reload_handler, lambda_context
) -> None:
    from cairn_lambda_validate_bundle.ecr_client import EcrFetchError

    handler = reload_handler
    bundle = build_bundle()
    attestation = build_attestation(subject_digest=bundle.manifest_digest)
    handler._test_fake_ecr.add_bundle(bundle)
    handler._test_fake_ecr.add_attestation(attestation)
    handler._test_fake_ecr.fetch_manifest_side_effects[attestation.attestation_digest] = (
        EcrFetchError("transient ECR outage")
    )
    event = attestation_push_event(attestation=attestation)

    with pytest.raises(EcrFetchError):
        handler.lambda_handler(event, lambda_context)
    assert handler._test_recorder.details("artifact.validation-failed") == []
    assert handler._test_recorder.detail_types() == []


def test_handler_emits_failure_metric_on_transient_error(
    reload_handler, lambda_context, capsys
) -> None:
    from cairn_lambda_validate_bundle.ecr_client import EcrFetchError

    handler = reload_handler
    bundle = build_bundle()
    attestation = build_attestation(subject_digest=bundle.manifest_digest)
    handler._test_fake_ecr.add_attestation(attestation)
    handler._test_fake_ecr.fetch_manifest_side_effects[attestation.attestation_digest] = (
        EcrFetchError("transient")
    )
    event = attestation_push_event(attestation=attestation)

    with pytest.raises(EcrFetchError):
        handler.lambda_handler(event, lambda_context)
    docs = _parse_emf_metrics(capsys.readouterr().out)
    assert any(_metric_value(d, "Failure") == 1.0 for d in docs)


def test_handler_emits_failure_metric_on_permanent_validation_failure(
    reload_handler, lambda_context, capsys
) -> None:
    handler = reload_handler
    _attestation, event = submit(handler, bundle=build_bundle(), omit_user_identity=True)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    docs = _parse_emf_metrics(capsys.readouterr().out)
    assert any(_metric_value(d, "Failure") == 1.0 for d in docs)


# ===========================================================================
# Re-tag short-circuit on the CONTENT digest.
# ===========================================================================


def test_re_seen_marker_only_row_is_processed_not_skipped(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A content row carrying ONLY validateStatus=validated (no terminal sign/
    archive status) is NON-terminal — a re-push is PROCESSED, not skipped, so the
    parked row can progress. validateStatus alone does not short-circuit the
    re-seen check."""
    handler = reload_handler
    bundle = build_bundle()
    seed_metadata_index(
        handler,
        monkeypatch,
        manifest_digest=bundle.manifest_digest,
        record={"validateStatus": "validated"},
    )
    _attestation, event = submit(handler, bundle=bundle)

    result = handler.lambda_handler(event, lambda_context)

    assert result is not handler._SKIPPED
    assert handler._test_recorder.detail_types() == ["artifact.validated"]


def test_re_seen_same_attestation_on_terminal_row_short_circuits(
    reload_handler, lambda_context, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-push of the SAME Producer_Attestation_Referrer against a terminal-success
    content row whose validatedAttestationDigests CONTAINS the triggering referrer
    short-circuits to a benign skip before any side effect."""
    handler = reload_handler
    bundle = build_bundle()
    # The submitted attestation's digest must be a member of the recorded set.
    attestation, event = submit(handler, bundle=bundle)
    seed_metadata_index(
        handler,
        monkeypatch,
        manifest_digest=bundle.manifest_digest,
        record={
            "validateStatus": "validated",
            "signingStatus": "signed",
            "archiveStatus": "archived",
            "validatedAttestationDigests": {attestation.attestation_digest},
        },
    )

    result = handler.lambda_handler(event, lambda_context)

    assert result is handler._SKIPPED
    assert handler._test_recorder.detail_types() == []
    assert _emf_dimension_present(capsys.readouterr().out, "SkipReason", "re-seen-digest")


# ===========================================================================
# Observability wrap.
# ===========================================================================


def test_handler_invokes_observability_scope_for_each_call(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    handler = reload_handler
    _attestation, event = submit(handler, bundle=build_bundle())

    calls: list[dict[str, Any]] = []
    original_scope = handler.observability_scope

    @contextmanager
    def recording_scope(evt, **kwargs):
        calls.append(
            {"event_type": kwargs.get("event_type"), "request_id": kwargs.get("request_id")}
        )
        with original_scope(evt, **kwargs) as tc:
            yield tc

    monkeypatch.setattr(handler, "observability_scope", recording_scope)
    handler.lambda_handler(event, lambda_context)

    assert len(calls) == 1
    assert calls[0]["event_type"] == "validate"
    assert calls[0]["request_id"] == lambda_context.aws_request_id


# ===========================================================================
# Universal evidence reference (ARCHIVE_BUCKET set) — built for EVERY bundle.
# ===========================================================================


def test_oscal_evidence_reference_uuid_is_deterministic_and_carries_root_doc_id(
    reload_handler, lambda_context
) -> None:
    import uuid as _uuid

    from cairn_oci import CAIRN_NAMESPACE, resource_uuid_for

    handler = reload_handler
    bundle = build_bundle()
    _attestation, event = submit(handler, bundle=bundle)

    payload = handler.lambda_handler(event, lambda_context)
    ref = payload["artifactResourceReference"]

    ns = _uuid.uuid5(_uuid.NAMESPACE_DNS, "cairnproofs.dev")
    expected_resource_uuid = str(_uuid.uuid5(ns, bundle.manifest_digest))
    assert ref["uuid"] == expected_resource_uuid
    assert ref["uuid"] == str(_uuid.uuid5(CAIRN_NAMESPACE, bundle.manifest_digest))
    assert ref["uuid"] == resource_uuid_for(bundle.manifest_digest)
    doc_ids = ref["document-ids"]
    assert doc_ids[0]["identifier"] == CATALOG_UUID
    assert doc_ids[0]["identifier"] != ref["uuid"]


def test_predicted_s3_uri_is_digest_terminal(reload_handler, lambda_context) -> None:
    handler = reload_handler
    bundle = build_bundle()
    _attestation, event = submit(
        handler, bundle=bundle, claim_bytes=attestation_claim(artifact_kind="scan-reports")
    )

    payload = handler.lambda_handler(event, lambda_context)
    rlinks = payload["artifactResourceReference"]["rlinks"]
    s3_hrefs = [rl["href"] for rl in rlinks if rl["href"].startswith(f"s3://{ARCHIVE_BUCKET}/")]
    assert len(s3_hrefs) == 1
    assert s3_hrefs[0] == (
        f"s3://{ARCHIVE_BUCKET}/{ORG_ID}/{TENANT_ID}/{PROJECT_ID}/"
        f"{bundle.manifest_digest}/manifest.json"
    )
    assert "/2026-q2/" not in s3_hrefs[0]
    assert "/scan-reports/" not in s3_hrefs[0]


def _non_oscal_bundle() -> BundleFixture:
    return build_bundle(
        layer_media_type="text/plain",
        layer_bytes=b"plain-text evidence, not oscal",
        layer_title="evidence-note.txt",
    )


def test_non_oscal_bundle_emits_evidence_reference_with_digest_derived_identity(
    reload_handler, lambda_context
) -> None:
    import uuid as _uuid

    from cairn_oscal.reference import CAIRN_NAMESPACE

    handler = reload_handler
    bundle = _non_oscal_bundle()
    _attestation, event = submit(handler, bundle=bundle)

    payload = handler.lambda_handler(event, lambda_context)
    assert len(payload["layers"]) == 1
    assert "oscal" not in payload["layers"][0]
    ref = payload["artifactResourceReference"]
    bare_hex = bundle.manifest_digest.split(":", 1)[1]
    assert ref["uuid"] == str(_uuid.uuid5(CAIRN_NAMESPACE, bundle.manifest_digest))
    assert ref["document-ids"][0]["identifier"] == str(
        _uuid.uuid5(CAIRN_NAMESPACE, bundle.manifest_digest)
    )
    rlinks = ref["rlinks"]
    assert len(rlinks) == 2
    for rl in rlinks:
        assert any(h["value"] == bare_hex for h in rl["hashes"])


def test_non_oscal_bundle_without_archive_bucket_emits_no_reference(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = reload_handler
    monkeypatch.setattr(handler, "ARCHIVE_BUCKET", "")
    bundle = _non_oscal_bundle()
    _attestation, event = submit(handler, bundle=bundle)

    payload = handler.lambda_handler(event, lambda_context)
    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    assert payload.get("artifactResourceReference") is None


def test_evidence_reference_is_byte_identical_across_two_runs(
    reload_handler, lambda_context
) -> None:
    handler = reload_handler
    bundle = _non_oscal_bundle()

    _a1, event1 = submit(handler, bundle=bundle)
    ref1 = handler.lambda_handler(event1, lambda_context)["artifactResourceReference"]
    # Re-run: same content digest, fresh attestation push (idempotent identity).
    _a2, event2 = submit(handler, bundle=bundle, seed_bundle=False)
    ref2 = handler.lambda_handler(event2, lambda_context)["artifactResourceReference"]

    assert json.dumps(ref1, sort_keys=True) == json.dumps(ref2, sort_keys=True)


# ===========================================================================
# Coordinate recording sources artifactKind from the CLAIM.
# ===========================================================================


def test_record_coordinates_sources_artifact_kind_from_claim(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: dict[str, Any] = {}

    class _Index:
        def get_record(self, **_kw: Any) -> None:
            return None

        def create_or_merge(
            self, *, manifest_digest: str, record: dict[str, Any], **_kw: Any
        ) -> None:
            recorded["manifest_digest"] = manifest_digest
            recorded.update(record)

        def add_validated_attestation(self, *, attestation_digest: str, **_kw: Any) -> None:
            recorded.setdefault("validatedAttestationDigests", set()).add(attestation_digest)

    handler = reload_handler
    monkeypatch.setattr(handler, "_metadata_index", lambda: _Index())
    bundle = build_bundle()
    _attestation, event = submit(
        handler, bundle=bundle, claim_bytes=attestation_claim(artifact_kind="assessment-results")
    )

    handler.lambda_handler(event, lambda_context)

    # The durable row is keyed on the CONTENT digest, and artifactKind comes from
    # the claim (resolved canonical).
    assert recorded["manifest_digest"] == bundle.manifest_digest
    assert recorded["artifactKind"] == "assessment-results"
    assert recorded["validateStatus"] == "validated"
    # The canonical tag carries no name, so the
    # row records NO name — even for an OSCAL bundle.
    assert "name" not in recorded


# ===========================================================================
# ECR event parser + image URI (unchanged helpers).
# ===========================================================================


def test_ecr_event_parser_rejects_missing_event_detail() -> None:
    from cairn_lambda_validate_bundle.ecr_event import (
        MalformedEcrEvent,
        parse_ecr_push_event,
    )

    with pytest.raises(MalformedEcrEvent):
        parse_ecr_push_event({})
    with pytest.raises(MalformedEcrEvent):
        parse_ecr_push_event({"detail": "not-a-dict"})


def test_ecr_event_parser_collects_all_missing_fields() -> None:
    from cairn_lambda_validate_bundle.ecr_event import (
        MalformedEcrEvent,
        parse_ecr_push_event,
    )

    with pytest.raises(MalformedEcrEvent) as exc:
        parse_ecr_push_event({"detail": {"requestParameters": {}}})
    msg = str(exc.value)
    assert "repositoryName" in msg
    assert "imageDigest" in msg


def test_ecr_image_uri_format() -> None:
    from cairn_lambda_validate_bundle.ecr_event import ecr_image_uri

    digest = "sha256:" + "1" * 64
    uri = ecr_image_uri(
        registry_id=ACCOUNT_ID, region="us-east-2", repository_name=REPO_NAME, digest=digest
    )
    assert uri == f"{ACCOUNT_ID}.dkr.ecr.us-east-2.amazonaws.com/{REPO_NAME}@{digest}"


# ===========================================================================
# Universal per-layer projection (detail.layers).
# ===========================================================================


def _multi_layer_bundle(
    *,
    layers: list[dict[str, Any]],
    config_bytes: bytes | None = None,
    repo_name: str = REPO_NAME,
) -> tuple[BundleFixture, list[bytes]]:
    """Build a CONTENT manifest with arbitrary layers and return (fixture, layer_blobs).

    Each ``layers`` entry is ``{"media_type", "blob", "title"}`` (title optional).
    The returned ``BundleFixture`` exposes the FIRST layer via ``layer_digest`` /
    ``layer_bytes`` (for ``add_bundle`` seeding); the helper additionally returns
    every layer blob so the caller can seed them all on the fake ECR.
    """
    from cairn_oci import CONFIG_BLOB_MEDIA_TYPE, compute_sha256_digest
    from cairn_oci.annotations import CairnAnnotations

    if config_bytes is None:
        config_bytes = config_blob_body()
    config_digest = compute_sha256_digest(config_bytes)

    manifest_layers: list[dict[str, Any]] = []
    layer_blobs: list[bytes] = []
    for spec in layers:
        blob = spec["blob"]
        layer_blobs.append(blob)
        descriptor: dict[str, Any] = {
            "mediaType": spec["media_type"],
            "digest": compute_sha256_digest(blob),
            "size": len(blob),
        }
        title = spec.get("title")
        descriptor["annotations"] = (
            {"org.opencontainers.image.title": title} if title is not None else {}
        )
        manifest_layers.append(descriptor)

    manifest: dict[str, Any] = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": CONFIG_BLOB_MEDIA_TYPE,
            "digest": config_digest,
            "size": len(config_bytes),
        },
        "layers": manifest_layers,
        "annotations": {
            CairnAnnotations.PROJECT_ID: "alpha-bank",
        },
    }
    manifest_raw = json.dumps(manifest, separators=(",", ":"))
    manifest_digest = compute_sha256_digest(manifest_raw.encode("utf-8"))
    first = manifest_layers[0]
    fixture = BundleFixture(
        manifest_digest=manifest_digest,
        manifest_dict=manifest,
        manifest_raw=manifest_raw,
        config_digest=config_digest,
        config_bytes=config_bytes,
        layer_digest=first["digest"],
        layer_bytes=layer_blobs[0],
        repo_name=repo_name,
        registry_id=ACCOUNT_ID,
        region="us-east-2",
    )
    return fixture, layer_blobs


def _submit_multi_layer(
    handler: Any, bundle: BundleFixture, layer_blobs: list[bytes]
) -> tuple[AttestationFixture, dict[str, Any]]:
    """Seed the content manifest + config + EVERY layer blob, then build the
    attestation + push event. Returns ``(attestation, event)`` like ``submit``.

    ``submit`` only seeds a single-layer bundle via ``add_bundle``; here we first
    seed every layer blob, then hand off to ``submit(seed_bundle=False)`` for the
    attestation + event so the trigger setup stays in one place.
    """
    from cairn_oci import compute_sha256_digest

    for blob in layer_blobs:
        handler._test_fake_ecr.blobs[compute_sha256_digest(blob)] = blob
    return submit(handler, bundle=bundle, seed_bundle=True)


def test_mixed_bundle_projects_every_layer_descriptor(reload_handler, lambda_context) -> None:
    """A mixed OSCAL + non-OSCAL bundle emits detail.layers with every layer's
    universal descriptor detail (digest/size/mediaType/title)."""
    handler = reload_handler
    oscal_blob = catalog_blob_bytes()
    log_blob = b"2026-05-26 INFO evidence log line"
    png_blob = b"\x89PNG\r\n\x1a\nfake-screenshot-bytes"
    bundle, layer_blobs = _multi_layer_bundle(
        layers=[
            {
                "media_type": "application/oscal+json",
                "blob": oscal_blob,
                "title": "cat.json",
            },
            {"media_type": "text/plain", "blob": log_blob, "title": "run.log"},
            {"media_type": "image/png", "blob": png_blob, "title": "screen.png"},
        ]
    )
    _attestation, event = _submit_multi_layer(handler, bundle, layer_blobs)

    payload = handler.lambda_handler(event, lambda_context)

    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    projected = payload["layers"]
    assert len(projected) == 3
    from cairn_oci import compute_sha256_digest

    by_media = {layer["mediaType"]: layer for layer in projected}
    # Every layer carries the universal OCI descriptor fields.
    for media, blob, title in (
        ("application/oscal+json", oscal_blob, "cat.json"),
        ("text/plain", log_blob, "run.log"),
        ("image/png", png_blob, "screen.png"),
    ):
        entry = by_media[media]
        assert entry["digest"] == compute_sha256_digest(blob)
        assert entry["size"] == len(blob)
        assert entry["title"] == title
    # No layer omitted on account of media type.
    assert set(by_media) == {"application/oscal+json", "text/plain", "image/png"}


def test_mixed_bundle_oscal_layer_carries_inline_enrichment(reload_handler, lambda_context) -> None:
    """Only the OSCAL layer carries an `oscal` sub-object; non-OSCAL layers do not,
    and all sit in the SAME layers list."""
    handler = reload_handler
    oscal_blob = catalog_blob_bytes()
    log_blob = b"a plain log, not oscal"
    bundle, layer_blobs = _multi_layer_bundle(
        layers=[
            {"media_type": "text/plain", "blob": log_blob, "title": "run.log"},
            {"media_type": "application/oscal+json", "blob": oscal_blob, "title": "c.json"},
        ]
    )
    _attestation, event = _submit_multi_layer(handler, bundle, layer_blobs)

    payload = handler.lambda_handler(event, lambda_context)

    projected = payload["layers"]
    assert len(projected) == 2
    by_media = {layer["mediaType"]: layer for layer in projected}
    # Non-OSCAL layer: no enrichment.
    assert "oscal" not in by_media["text/plain"]
    # OSCAL layer: enrichment inlined, in the same list.
    oscal = by_media["application/oscal+json"]["oscal"]
    assert oscal["oscalSchemaVersion"] == OSCAL_VERSION
    assert oscal["metadata"]["doc_type"] == "catalog"
    assert oscal["metadata"]["doc_uuid"] == CATALOG_UUID


def test_non_oscal_only_bundle_enumerates_every_layer_without_enrichment(
    reload_handler, lambda_context
) -> None:
    """A bundle with NO OSCAL layer enumerates every layer; no entry has an `oscal`
    sub-object."""
    handler = reload_handler
    bundle, layer_blobs = _multi_layer_bundle(
        layers=[
            {"media_type": "text/plain", "blob": b"log one", "title": "one.log"},
            {"media_type": "application/pdf", "blob": b"%PDF-1.7 fake", "title": "report.pdf"},
        ]
    )
    _attestation, event = _submit_multi_layer(handler, bundle, layer_blobs)

    payload = handler.lambda_handler(event, lambda_context)

    projected = payload["layers"]
    assert len(projected) == 2
    assert all("oscal" not in entry for entry in projected)
    # No blob fetch was triggered for the non-OSCAL layers (only config was fetched
    # for validate_bundle; no per-layer enrichment fetch). The OSCAL fetch path is
    # the only thing that fetches a layer blob, and there is no OSCAL layer here.
    fetched = [d for (_, _, d) in handler._test_fake_ecr.blob_fetch_log]
    layer_digests = {entry["digest"] for entry in projected}
    assert not (layer_digests & set(fetched))


def test_layers_list_excludes_sibling_referrer_blobs(reload_handler, lambda_context) -> None:
    """The layers list covers the content manifest's layers only — the attestation
    referrer's claim layer is NOT in it."""
    handler = reload_handler
    bundle = build_bundle()
    attestation, event = submit(handler, bundle=bundle)

    payload = handler.lambda_handler(event, lambda_context)

    projected_digests = {entry["digest"] for entry in payload["layers"]}
    # The attestation's claim layer digest must not leak into the content layers list.
    assert attestation.claim_digest not in projected_digests
    assert projected_digests == {bundle.layer_digest}


def test_oversized_title_fails_closed(reload_handler, lambda_context) -> None:
    """A layer title exceeding MAX_LAYER_TITLE_LEN (ASCII, 1 byte/char) is a permanent
    validation failure."""
    handler = reload_handler
    huge_title = "a" * (handler.MAX_LAYER_TITLE_LEN + 1)
    bundle = build_bundle(
        layer_media_type="text/plain",
        layer_bytes=b"plain evidence",
        layer_title=huge_title,
    )
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    assert failed[0]["errorKind"] == "layer_title_too_large"
    assert failed[0]["failedCriteria"][0]["criterion"] == "layer_title"
    # The in-band DLQ rejection was also delivered ([PIPELINE-006]).
    assert handler._test_fake_sqs.bodies()[0]["errorKind"] == "layer_title_too_large"


def test_multibyte_title_over_byte_cap_under_char_cap_fails_closed(
    reload_handler, lambda_context
) -> None:
    """BYTE-true cap (the case the old code-point comparison wrongly accepted): a
    title whose code-point count is WELL UNDER MAX_LAYER_TITLE_LEN but whose UTF-8
    byte length EXCEEDS it must fail closed. '✓' (U+2713) is 3 bytes; a string of
    half-the-cap such characters is ~half the code-points but ~1.5x the byte cap."""
    handler = reload_handler
    cap = handler.MAX_LAYER_TITLE_LEN
    # code-points = cap // 2 (< cap), bytes = 3 * (cap // 2) (> cap).
    char_count = cap // 2
    multibyte_title = "✓" * char_count
    assert len(multibyte_title) <= cap  # within cap measured in code-points
    assert len(multibyte_title.encode("utf-8")) > cap  # over the BYTE cap
    bundle = build_bundle(
        layer_media_type="text/plain",
        layer_bytes=b"plain evidence",
        layer_title=multibyte_title,
    )
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    assert failed[0]["errorKind"] == "layer_title_too_large"
    assert failed[0]["failedCriteria"][0]["criterion"] == "layer_title"


def test_title_at_exact_byte_cap_is_accepted(reload_handler, lambda_context) -> None:
    """Title-cap boundary (the helper compares the UTF-8 BYTE length against
    MAX_LAYER_TITLE_LEN): a title of EXACTLY MAX_LAYER_TITLE_LEN ASCII bytes passes
    and is projected verbatim, while +1 byte fails closed (covered by
    test_oversized_title_fails_closed). Pins the `>` (not `>=`) byte comparison the
    production code implements."""
    handler = reload_handler
    boundary_title = "a" * handler.MAX_LAYER_TITLE_LEN  # ASCII: 1 byte/char == cap bytes
    assert len(boundary_title.encode("utf-8")) == handler.MAX_LAYER_TITLE_LEN
    bundle = build_bundle(
        layer_media_type="text/plain",
        layer_bytes=b"plain evidence",
        layer_title=boundary_title,
    )
    _attestation, event = submit(handler, bundle=bundle)

    payload = handler.lambda_handler(event, lambda_context)

    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    # The exactly-at-cap title rides through verbatim (no truncation, no failure).
    assert payload["layers"][0]["title"] == boundary_title
    assert len(payload["layers"][0]["title"].encode("utf-8")) == handler.MAX_LAYER_TITLE_LEN


def test_layer_without_title_omits_title_key_not_null(
    reload_handler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A content layer with NO org.opencontainers.image.title annotation projects
    with the ``title`` key OMITTED entirely (None-pruning discipline) — never a
    null.

    [INGEST-017.07] criterion (b) (``validate_bundle``) requires EVERY layer to carry a
    title, so the no-title projection branch is unreachable through the full
    handler — every bundle that reaches the projection has all titles. This
    isolates the projection's None-pruning at its genuine production seam
    (``_process_layers_and_build_reference``) so a regression that stored ``title:
    null`` for a title-less layer (e.g. if validation ever relaxed criterion (b))
    is caught. ARCHIVE_BUCKET is cleared so no evidence reference / blob fetch is
    needed, and both layers are non-OSCAL so no per-layer blob fetch occurs."""
    from cairn_oci import OciManifest

    handler = reload_handler
    monkeypatch.setattr(handler, "ARCHIVE_BUCKET", "")

    manifest = OciManifest.model_validate(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.cairnproofs.config.v1+json",
                "digest": "sha256:" + "0" * 64,
                "size": 10,
            },
            "layers": [
                {
                    "mediaType": "text/plain",
                    "digest": "sha256:" + "1" * 64,
                    "size": 11,
                    "annotations": {"org.opencontainers.image.title": "first.txt"},
                },
                # Second layer: NO title annotation at all.
                {
                    "mediaType": "image/png",
                    "digest": "sha256:" + "2" * 64,
                    "size": 12,
                    "annotations": {},
                },
            ],
        }
    )
    push = handler.parse_ecr_push_event(ecr_push_event(bundle=build_bundle(), pushed_at=PUSHED_AT))
    layers, evidence_reference = handler._process_layers_and_build_reference(
        manifest=manifest,
        push=push,
        manifest_digest="sha256:" + "3" * 64,
        org_id=ORG_ID,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
    )

    assert evidence_reference is None  # ARCHIVE_BUCKET cleared
    assert len(layers) == 2
    titled, untitled = layers
    assert titled["title"] == "first.txt"
    # The untitled layer omits the key entirely — never present-with-None.
    assert "title" not in untitled
    assert untitled.get("title", "__absent__") == "__absent__"
    # No per-layer blob fetch happened (both layers are non-OSCAL).
    assert handler._test_fake_ecr.blob_fetch_log == []


def test_layers_preserve_manifest_layer_order(reload_handler, lambda_context) -> None:
    """The projected ``layers`` list preserves manifest layer ORDER (a list, not a
    set/map): consumers rely on layer[0] being the manifest's first layer."""
    handler = reload_handler
    bundle, layer_blobs = _multi_layer_bundle(
        layers=[
            {"media_type": "text/plain", "blob": b"alpha layer", "title": "alpha.txt"},
            {
                "media_type": "application/oscal+json",
                "blob": catalog_blob_bytes(),
                "title": "beta.json",
            },
            {"media_type": "image/png", "blob": b"\x89PNG gamma", "title": "gamma.png"},
        ]
    )
    _attestation, event = _submit_multi_layer(handler, bundle, layer_blobs)

    payload = handler.lambda_handler(event, lambda_context)

    projected = payload["layers"]
    # Order matches the manifest declaration order, not media-type or digest sort.
    assert [entry["title"] for entry in projected] == ["alpha.txt", "beta.json", "gamma.png"]
    assert [entry["mediaType"] for entry in projected] == [
        "text/plain",
        "application/oscal+json",
        "image/png",
    ]


def test_oscal_lookalike_media_type_is_not_enriched_and_not_fetched(
    reload_handler, lambda_context
) -> None:
    """A layer whose mediaType merely LOOKS OSCAL-ish but is outside the OSCAL
    media-type family (note: hyphen after ``oscal``, not a dot or plus) is treated
    as a plain content layer — NO ``oscal`` sub-object and NO blob fetch (the OSCAL
    blob fetch is the only per-layer fetch). Pins the family discrimination so a
    near-miss media type can't trigger an OSCAL parse on arbitrary bytes."""
    handler = reload_handler
    lookalike_blob = b"this is NOT valid oscal json {{{"
    bundle, layer_blobs = _multi_layer_bundle(
        layers=[
            # Looks OSCAL-ish but is "application/oscal-catalog..." (hyphen, no dot).
            {
                "media_type": "application/oscal-catalog+json",
                "blob": lookalike_blob,
                "title": "lookalike.json",
            },
        ]
    )
    _attestation, event = _submit_multi_layer(handler, bundle, layer_blobs)

    payload = handler.lambda_handler(event, lambda_context)

    # Not rejected (no OSCAL parse attempted on the non-conforming bytes).
    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    entry = payload["layers"][0]
    assert entry["mediaType"] == "application/oscal-catalog+json"
    assert "oscal" not in entry
    # The lookalike layer's blob was never fetched (only the config blob was).
    from cairn_oci import compute_sha256_digest

    fetched = [d for (_, _, d) in handler._test_fake_ecr.blob_fetch_log]
    assert compute_sha256_digest(lookalike_blob) not in fetched


def test_second_oscal_layer_future_dated_is_rejected(reload_handler, lambda_context) -> None:
    """Temporal consistency applies to EVERY OSCAL layer, not just the first: a
    SECOND OSCAL layer whose metadata.last-modified is after ecrPushedAt is a
    permanent failure even when the first OSCAL layer is in-window. (Version
    selection picks the first; temporal rejection must scan all.)"""
    handler = reload_handler
    in_window = catalog_blob_bytes(last_modified="2026-05-26T00:00:00Z")
    future = catalog_blob_bytes(
        uuid="11111111-58cc-4372-a567-0e02b2c3d479",
        last_modified="2030-01-01T00:00:00Z",
    )
    bundle, layer_blobs = _multi_layer_bundle(
        layers=[
            {"media_type": "application/oscal+json", "blob": in_window, "title": "a.json"},
            {"media_type": "application/oscal+json", "blob": future, "title": "b.json"},
        ]
    )
    _attestation, event = _submit_multi_layer(handler, bundle, layer_blobs)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    temporal = [c for c in failed[0]["failedCriteria"] if c["criterion"] == "temporal"]
    assert temporal
    # The failure names the SECOND (future-dated) layer, not the first.
    from cairn_oci import compute_sha256_digest

    assert compute_sha256_digest(future) in temporal[0]["field"]


def test_temporal_check_layers_unit_scans_all_oscal_layers() -> None:
    """Unit: _temporal_check_layers flags a future-dated SECOND oscal layer even
    when the first is in-window (it does not stop at the first oscal entry)."""
    from cairn_lambda_validate_bundle import handler as handler_module

    future = "2030-01-01T00:00:00Z"
    layers = [
        {"digest": "sha256:first", "oscal": {"metadata": {"last_modified": LAST_MODIFIED}}},
        {"digest": "sha256:second", "oscal": {"metadata": {"last_modified": future}}},
    ]
    failures, _warn = handler_module._temporal_check_layers(
        layers=layers, ecr_pushed_at_iso=PUSHED_AT
    )
    assert len(failures) == 1
    assert "sha256:second" in failures[0]["field"]


# ===========================================================================
# OSCAL media type ([OSCAL-002]) and per-layer size limit ([OSCAL-001])
# ===========================================================================

_MAPPING_UUID = "3d1f0a5e-6b7c-4d8e-9f01-23456789abcd"


def _mapping_collection_blob() -> bytes:
    """A minimal valid OSCAL 1.x mapping-collection document."""
    return json.dumps(
        {
            "mapping-collection": {
                "uuid": _MAPPING_UUID,
                "metadata": {
                    "title": "Test Mapping",
                    "last-modified": LAST_MODIFIED,
                    "version": "1.0",
                    "oscal-version": OSCAL_VERSION,
                },
                "provenance": {
                    "method": "human",
                    "matching-rationale": "semantic",
                    "status": "complete",
                    "mapping-description": "test mapping",
                },
                "mappings": [
                    {
                        "uuid": "4e2f1b6f-7c8d-4e9f-8a12-3456789abcde",
                        "source-resource": {"type": "catalog", "href": "#src"},
                        "target-resource": {"type": "catalog", "href": "#tgt"},
                        "maps": [
                            {
                                "uuid": "5f3a2c7a-8d9e-4fa0-9b23-456789abcdef",
                                "relationship": "equivalent-to",
                                "sources": [{"type": "control", "id-ref": "ac-1"}],
                                "targets": [{"type": "control", "id-ref": "ac-1"}],
                            }
                        ],
                    }
                ],
            }
        }
    ).encode("utf-8")


def _bundle_with_declared_size(blob: bytes, declared_size: int) -> BundleFixture:
    """A single-layer ``application/oscal+json`` bundle whose descriptor declares
    ``declared_size`` instead of ``len(blob)``. The manifest digest is recomputed."""
    from cairn_oci import compute_sha256_digest

    bundle = build_bundle(layer_bytes=blob)
    manifest = json.loads(json.dumps(bundle.manifest_dict))
    manifest["layers"][0]["size"] = declared_size
    raw = json.dumps(manifest, separators=(",", ":"))
    return dataclasses.replace(
        bundle,
        manifest_dict=manifest,
        manifest_raw=raw,
        manifest_digest=compute_sha256_digest(raw.encode("utf-8")),
    )


def _fetched_digests(handler: Any) -> list[str]:
    return [d for (_r, _repo, d) in handler._test_fake_ecr.blob_fetch_log]


def _single_oscal_failure(handler: Any) -> dict[str, Any]:
    assert handler._test_recorder.detail_types() == ["artifact.validation-failed"]
    failed = handler._test_recorder.details("artifact.validation-failed")
    assert len(failed) == 1
    return failed[0]


def test_oscal_json_mapping_collection_layer_is_parsed(reload_handler, lambda_context) -> None:
    """[OSCAL-001.07], [OSCAL-002.01]: an ``application/oscal+json`` layer goes
    through the SDK, and the model comes from the document root key."""
    handler = reload_handler
    bundle = build_bundle(layer_bytes=_mapping_collection_blob(), layer_title="map.json")
    _attestation, event = submit(handler, bundle=bundle)

    payload = handler.lambda_handler(event, lambda_context)

    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    entry = payload["layers"][0]
    assert entry["mediaType"] == "application/oscal+json"
    assert entry["oscal"]["oscalSchemaVersion"] == OSCAL_VERSION
    assert entry["oscal"]["metadata"]["doc_type"] == "mapping-collection"
    assert handler._test_fake_ecr.blob_fetch_max_bytes[-1] == handler.MAX_OSCAL_LAYER_BYTES


@pytest.mark.parametrize(
    "media_type",
    ["Application/OSCAL+JSON; charset=utf-8", "APPLICATION/OSCAL+JSON"],
)
def test_oscal_json_match_ignores_case_and_parameters(
    reload_handler, lambda_context, media_type: str
) -> None:
    """[OSCAL-002.05]: the layer is OSCAL, and the entry keeps the media type verbatim."""
    handler = reload_handler
    bundle = build_bundle(layer_media_type=media_type)
    _attestation, event = submit(handler, bundle=bundle)

    payload = handler.lambda_handler(event, lambda_context)

    entry = payload["layers"][0]
    assert entry["mediaType"] == media_type
    assert entry["oscal"]["metadata"]["doc_type"] == "catalog"


@pytest.mark.parametrize(
    "media_type",
    [
        "application/oscal.catalog+json",
        "APPLICATION/OSCAL.CATALOG+JSON",
        "application/oscal+xml",
        "application/oscal+yaml",
    ],
)
def test_other_oscal_family_media_type_is_rejected_without_fetch(
    reload_handler, lambda_context, media_type: str
) -> None:
    """[OSCAL-002.03], [OSCAL-002.04]: a permanent failure that names the layer
    and ``application/oscal+json``, with no blob fetch and no validated event."""
    handler = reload_handler
    bundle = build_bundle(layer_media_type=media_type)
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    failed = _single_oscal_failure(handler)
    assert failed["errorKind"] == "unsupported_media_type"
    assert failed["layerDigest"] == bundle.layer_digest
    criterion = failed["failedCriteria"][0]
    assert criterion["criterion"] == "oscal"
    assert criterion["field"] == f"layer[{bundle.layer_digest}].mediaType"
    assert "application/oscal+json" in criterion["message"]
    assert bundle.layer_digest in criterion["message"]
    assert bundle.layer_digest not in _fetched_digests(handler)
    assert handler._test_fake_sqs.bodies()[0]["errorKind"] == "unsupported_media_type"


def test_oscal_parse_failure_field_names_the_layer(reload_handler, lambda_context) -> None:
    """Decision 4 field table: ``parse_failure`` gives ``layer[<digest>]``."""
    handler = reload_handler
    bundle = build_bundle(layer_bytes=b"{this-is-not-valid-oscal-json")
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    failed = _single_oscal_failure(handler)
    assert failed["errorKind"] == "parse_failure"
    assert failed["failedCriteria"][0]["field"] == f"layer[{bundle.layer_digest}]"


def test_unsupported_version_field_names_oscal_version(reload_handler, lambda_context) -> None:
    """Decision 4 field table: version errors keep ``.metadata.oscal-version``."""
    handler = reload_handler
    bundle = build_bundle(layer_bytes=catalog_blob_bytes(oscal_version="2.0.0"))
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    failed = _single_oscal_failure(handler)
    assert failed["errorKind"] == "unsupported_version"
    assert (
        failed["failedCriteria"][0]["field"]
        == f"layer[{bundle.layer_digest}].metadata.oscal-version"
    )


def test_oscal_layer_declared_over_limit_is_rejected_without_fetch(
    reload_handler, lambda_context
) -> None:
    """[OSCAL-001.08]: the descriptor declares one byte over the default limit.
    The layer is never fetched, and the message states the declared size and
    the limit in bytes and in MiB."""
    handler = reload_handler
    limit = handler.MAX_OSCAL_LAYER_BYTES
    assert limit == 16 * 1024 * 1024
    bundle = _bundle_with_declared_size(catalog_blob_bytes(), limit + 1)
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    failed = _single_oscal_failure(handler)
    assert failed["errorKind"] == "oscal_layer_too_large"
    assert failed["layerDigest"] == bundle.layer_digest
    criterion = failed["failedCriteria"][0]
    assert criterion["field"] == f"layer[{bundle.layer_digest}].size"
    message = criterion["message"]
    assert bundle.layer_digest in message
    assert "declares 16,777,217 bytes (16.0 MiB)" in message
    assert "16,777,216 bytes (16.0 MiB)" in message
    assert "per OSCAL layer" in message
    assert bundle.layer_digest not in _fetched_digests(handler)


def test_oscal_layer_bytes_longer_than_limit_is_rejected(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[OSCAL-001.09]: the descriptor understates the size. The bounded fetch
    stops at the limit and the failure states that the bytes are longer."""
    handler = reload_handler
    blob = catalog_blob_bytes()
    limit = len(blob) - 1
    monkeypatch.setattr(handler, "MAX_OSCAL_LAYER_BYTES", limit)
    bundle = _bundle_with_declared_size(blob, limit)
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    failed = _single_oscal_failure(handler)
    assert failed["errorKind"] == "oscal_layer_too_large"
    criterion = failed["failedCriteria"][0]
    assert criterion["field"] == f"layer[{bundle.layer_digest}].size"
    assert "bytes longer than" in criterion["message"]
    assert f"{limit:,} bytes" in criterion["message"]
    assert bundle.layer_digest in _fetched_digests(handler)
    assert handler._test_fake_ecr.blob_fetch_max_bytes[-1] == limit


def test_oscal_layer_at_exact_limit_is_parsed(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[OSCAL-001.10]: a layer whose size equals the limit is parsed."""
    handler = reload_handler
    blob = catalog_blob_bytes()
    monkeypatch.setattr(handler, "MAX_OSCAL_LAYER_BYTES", len(blob))
    bundle = build_bundle(layer_bytes=blob)
    _attestation, event = submit(handler, bundle=bundle)

    payload = handler.lambda_handler(event, lambda_context)

    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    assert payload["layers"][0]["oscal"]["metadata"]["doc_type"] == "catalog"


@pytest.fixture
def small_oscal_limit_env(env: None, monkeypatch: pytest.MonkeyPatch) -> int:
    """Set a non-default ``MAX_OSCAL_LAYER_BYTES`` before ``reload_handler`` imports."""
    limit = len(catalog_blob_bytes()) + 10
    monkeypatch.setenv("MAX_OSCAL_LAYER_BYTES", str(limit))
    return limit


def test_non_default_max_oscal_layer_bytes_env_applies(
    small_oscal_limit_env: int, reload_handler, lambda_context
) -> None:
    """[OSCAL-001.11]: the deployment limit from the environment is the limit
    that applies, and the error states it."""
    handler = reload_handler
    limit = small_oscal_limit_env
    assert handler.MAX_OSCAL_LAYER_BYTES == limit

    within = build_bundle()
    _attestation, event = submit(handler, bundle=within)
    payload = handler.lambda_handler(event, lambda_context)
    assert payload["layers"][0]["oscal"]["metadata"]["doc_type"] == "catalog"

    over = _bundle_with_declared_size(catalog_blob_bytes(), limit + 1)
    _attestation, event = submit(handler, bundle=over)
    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    assert failed[-1]["errorKind"] == "oscal_layer_too_large"
    assert f"limit of {limit:,} bytes" in failed[-1]["failedCriteria"][0]["message"]


def test_raised_max_oscal_layer_bytes_env_accepts_layer_over_default(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[OSCAL-001.11]: a limit above the default accepts a declared size that the
    default would reject."""
    import cairn_oscal

    raised = cairn_oscal.DEFAULT_MAX_OSCAL_LAYER_BYTES * 2
    monkeypatch.setenv("MAX_OSCAL_LAYER_BYTES", str(raised))
    module = _load_fresh_handler()
    assert module.MAX_OSCAL_LAYER_BYTES == raised


def _load_fresh_handler() -> Any:
    """Execute the handler module under a throwaway name (no reload side effects)."""
    import importlib.util

    import cairn_lambda_validate_bundle.handler as handler_module

    spec = importlib.util.spec_from_file_location(
        "_fresh_validate_handler", handler_module.__file__
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("raw", ["0", "-1", "1.5", "abc", "", " ", "16MiB", "1e6", "١٢"])
def test_malformed_max_oscal_layer_bytes_raises_at_import(
    env: None, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """A present but malformed value fails the import, never a silent fallback."""
    monkeypatch.setenv("MAX_OSCAL_LAYER_BYTES", raw)
    with pytest.raises(ValueError, match="MAX_OSCAL_LAYER_BYTES must be a whole number"):
        _load_fresh_handler()


def test_absent_max_oscal_layer_bytes_uses_library_default(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cairn_oscal

    monkeypatch.delenv("MAX_OSCAL_LAYER_BYTES", raising=False)
    module = _load_fresh_handler()
    assert module.MAX_OSCAL_LAYER_BYTES == cairn_oscal.DEFAULT_MAX_OSCAL_LAYER_BYTES


def test_rejected_second_layer_media_type_after_valid_oscal_layer(
    reload_handler, lambda_context
) -> None:
    """A per-model media type on any layer rejects the bundle, even after a
    valid ``application/oscal+json`` layer."""
    handler = reload_handler
    bundle, layer_blobs = _multi_layer_bundle(
        layers=[
            {"media_type": "application/oscal+json", "blob": catalog_blob_bytes(), "title": "a"},
            {
                "media_type": "application/oscal.profile+json",
                "blob": b"{}",
                "title": "b",
            },
        ]
    )
    _attestation, event = _submit_multi_layer(handler, bundle, layer_blobs)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    failed = _single_oscal_failure(handler)
    assert failed["errorKind"] == "unsupported_media_type"
    from cairn_oci import compute_sha256_digest

    assert failed["layerDigest"] == compute_sha256_digest(b"{}")


def test_extract_oscal_layer_returns_only_version_and_metadata(
    reload_handler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-layer helper returns ``(version, metadata)`` and no document or bytes."""
    from cairn_oci import Descriptor
    from cairn_oscal import OscalLayerMetadata

    handler = reload_handler
    bundle = build_bundle()
    handler._test_fake_ecr.add_bundle(bundle)
    descriptor = Descriptor.model_validate(bundle.manifest_dict["layers"][0])
    push = type("Push", (), {"registry_id": ACCOUNT_ID, "repository_name": REPO_NAME})()

    result = handler._extract_oscal_layer(descriptor, push=push)

    assert isinstance(result, tuple) and len(result) == 2
    version, meta = result
    assert version == OSCAL_VERSION
    assert isinstance(meta, OscalLayerMetadata)


# ---------------------------------------------------------------------------
# OSCAL rejection paths: no durable write, DLQ record, duplicate delivery,
# per-layer (not cumulative) limit, failure-field table.
# ---------------------------------------------------------------------------


@pytest.fixture
def oscal_fake_index(reload_handler, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Wire ``_metadata_index()`` to the in-memory facade fake."""
    from tests.unit.cairn_lambda_validate_bundle.test_coordinate_recording import (
        FakeMetadataIndex,
    )

    index = FakeMetadataIndex()
    monkeypatch.setattr(reload_handler, "_metadata_index", lambda: index)
    return index


def _too_large_bundle(handler: Any, monkeypatch: pytest.MonkeyPatch, mode: str) -> BundleFixture:
    blob = catalog_blob_bytes()
    if mode == "declared":
        return _bundle_with_declared_size(blob, handler.MAX_OSCAL_LAYER_BYTES + 1)
    # "bytes_longer": the descriptor understates the size; the bounded fetch trips.
    monkeypatch.setattr(handler, "MAX_OSCAL_LAYER_BYTES", len(blob) - 1)
    return _bundle_with_declared_size(blob, len(blob) - 1)


@pytest.mark.parametrize("mode", ["declared", "bytes_longer"])
def test_oscal_too_large_rejection_writes_nothing_durable_and_dlqs(
    reload_handler, lambda_context, oscal_fake_index, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """A too-large OSCAL layer does not advance the pipeline: no validated event,
    no Metadata_Index write, one in-band DLQ rejection that names the layer."""
    handler = reload_handler
    bundle = _too_large_bundle(handler, monkeypatch, mode)
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    failed = _single_oscal_failure(handler)
    assert failed["errorKind"] == "oscal_layer_too_large"
    assert oscal_fake_index.rows == {}
    assert oscal_fake_index.merge_calls == []
    bodies = handler._test_fake_sqs.bodies()
    assert len(bodies) == 1
    assert bodies[0]["errorKind"] == "oscal_layer_too_large"
    assert bodies[0]["layerDigest"] == bundle.layer_digest
    assert bodies[0]["failedCriteria"][0]["field"] == f"layer[{bundle.layer_digest}].size"


def test_unsupported_media_type_rejection_writes_nothing_durable(
    reload_handler, lambda_context, oscal_fake_index
) -> None:
    handler = reload_handler
    bundle = build_bundle(layer_media_type="application/oscal.catalog+json")
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    assert oscal_fake_index.rows == {}
    assert oscal_fake_index.merge_calls == []
    assert "artifact.validated" not in handler._test_recorder.detail_types()


@pytest.mark.parametrize("mode", ["declared", "bytes_longer"])
def test_duplicate_delivery_of_too_large_bundle_rejects_identically(
    reload_handler, lambda_context, oscal_fake_index, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """A re-delivered event for the same oversized bundle gives the same
    permanent rejection each time, never a validated event or a skip."""
    handler = reload_handler
    bundle = _too_large_bundle(handler, monkeypatch, mode)
    _attestation, event = submit(handler, bundle=bundle)

    first = handler.lambda_handler(event, lambda_context)
    second = handler.lambda_handler(json.loads(json.dumps(event)), lambda_context)

    assert first is handler._REJECTED
    assert second is handler._REJECTED
    assert handler._test_recorder.detail_types() == [
        "artifact.validation-failed",
        "artifact.validation-failed",
    ]
    failed = handler._test_recorder.details("artifact.validation-failed")
    assert failed[0]["errorKind"] == failed[1]["errorKind"] == "oscal_layer_too_large"
    assert failed[0]["failedCriteria"] == failed[1]["failedCriteria"]
    assert [b["errorKind"] for b in handler._test_fake_sqs.bodies()] == [
        "oscal_layer_too_large",
        "oscal_layer_too_large",
    ]
    assert oscal_fake_index.rows == {}
    fetched = _fetched_digests(handler).count(bundle.layer_digest)
    assert fetched == (0 if mode == "declared" else 2)


def test_limit_applies_per_layer_not_cumulatively(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two OSCAL layers each at the limit both parse, though together they
    exceed it ([OSCAL-001]: the limit applies per OSCAL layer)."""
    handler = reload_handler
    blob_a = catalog_blob_bytes()
    blob_b = catalog_blob_bytes(uuid="11111111-2222-4333-8444-555555555555")
    assert len(blob_a) == len(blob_b)
    monkeypatch.setattr(handler, "MAX_OSCAL_LAYER_BYTES", len(blob_a))
    bundle, layer_blobs = _multi_layer_bundle(
        layers=[
            {"media_type": "application/oscal+json", "blob": blob_a, "title": "a.json"},
            {"media_type": "application/oscal+json", "blob": blob_b, "title": "b.json"},
        ]
    )
    _attestation, event = _submit_multi_layer(handler, bundle, layer_blobs)

    payload = handler.lambda_handler(event, lambda_context)

    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    assert [entry["oscal"]["metadata"]["doc_type"] for entry in payload["layers"]] == [
        "catalog",
        "catalog",
    ]
    assert handler._test_fake_ecr.blob_fetch_max_bytes[-2:] == [len(blob_a), len(blob_a)]


def test_second_oscal_layer_over_limit_rejects_bundle(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid first OSCAL layer does not carry a bundle whose second OSCAL
    layer is over the limit; the failure names the second layer."""
    from cairn_oci import compute_sha256_digest

    handler = reload_handler
    small = catalog_blob_bytes()
    big = catalog_blob_bytes(uuid="11111111-2222-4333-8444-555555555555") + b" " * 64
    monkeypatch.setattr(handler, "MAX_OSCAL_LAYER_BYTES", len(small))
    bundle, layer_blobs = _multi_layer_bundle(
        layers=[
            {"media_type": "application/oscal+json", "blob": small, "title": "a.json"},
            {"media_type": "application/oscal+json", "blob": big, "title": "b.json"},
        ]
    )
    _attestation, event = _submit_multi_layer(handler, bundle, layer_blobs)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    failed = _single_oscal_failure(handler)
    big_digest = compute_sha256_digest(big)
    assert failed["errorKind"] == "oscal_layer_too_large"
    assert failed["layerDigest"] == big_digest
    assert f"declares {len(big):,} bytes" in failed["failedCriteria"][0]["message"]
    assert big_digest not in _fetched_digests(handler)


def test_non_oscal_layer_over_oscal_limit_is_not_rejected(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OSCAL limit applies only to OSCAL layers; an opaque layer larger than
    the limit is neither size-checked nor fetched by validate."""
    from cairn_oci import compute_sha256_digest

    handler = reload_handler
    oscal = catalog_blob_bytes()
    opaque = b"%PDF-1.7 " + b"x" * (len(oscal) * 2)
    monkeypatch.setattr(handler, "MAX_OSCAL_LAYER_BYTES", len(oscal))
    bundle, layer_blobs = _multi_layer_bundle(
        layers=[
            {"media_type": "application/oscal+json", "blob": oscal, "title": "a.json"},
            {"media_type": "application/pdf", "blob": opaque, "title": "r.pdf"},
        ]
    )
    _attestation, event = _submit_multi_layer(handler, bundle, layer_blobs)

    handler.lambda_handler(event, lambda_context)

    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    assert compute_sha256_digest(opaque) not in _fetched_digests(handler)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("unsupported_media_type", "layer[sha256:d].mediaType"),
        ("oscal_layer_too_large", "layer[sha256:d].size"),
        ("parse_failure", "layer[sha256:d]"),
        ("unsupported_doc_type", "layer[sha256:d]"),
        ("missing_schema_version", "layer[sha256:d].metadata.oscal-version"),
        ("unsupported_version", "layer[sha256:d].metadata.oscal-version"),
    ],
)
def test_oscal_failure_field_table(reload_handler, kind: str, expected: str) -> None:
    from cairn_oscal import OscalExtractionError

    err = OscalExtractionError("m", error_kind=kind, layer_digest="sha256:d")  # type: ignore[arg-type]
    assert reload_handler._oscal_failure_field(err) == expected


def test_oscal_failure_field_without_digest_or_known_kind(reload_handler) -> None:
    """A missing digest renders as ``unknown``; an unlisted kind falls back to
    the layer itself rather than raising."""
    from cairn_oscal import OscalExtractionError

    no_digest = OscalExtractionError("m", error_kind="oscal_layer_too_large")
    assert reload_handler._oscal_failure_field(no_digest) == "layer[unknown].size"
    odd = OscalExtractionError("m", error_kind="not-a-kind", layer_digest="sha256:d")  # type: ignore[arg-type]
    assert reload_handler._oscal_failure_field(odd) == "layer[sha256:d]"


def test_unsupported_doc_type_failure_names_the_layer(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``extract_oscal_metadata`` receives the layer digest, so an
    ``unsupported_doc_type`` failure event names the layer."""
    handler = reload_handler
    monkeypatch.setattr("cairn_oscal.extraction._BODY_TO_DOC_TYPE", {})
    bundle = build_bundle()
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    failed = _single_oscal_failure(handler)
    assert failed["errorKind"] == "unsupported_doc_type"
    assert failed["layerDigest"] == bundle.layer_digest
    assert failed["failedCriteria"][0]["field"] == f"layer[{bundle.layer_digest}]"


def test_transient_fetch_error_on_oscal_layer_is_not_a_size_rejection(
    reload_handler, lambda_context
) -> None:
    """A transient ``EcrFetchError`` on the bounded OSCAL fetch propagates for
    retry; it is never reported as ``oscal_layer_too_large``."""
    from cairn_lambda_validate_bundle.ecr_client import EcrFetchError

    handler = reload_handler
    bundle = build_bundle()
    _attestation, event = submit(handler, bundle=bundle)
    handler._test_fake_ecr.fetch_blob_side_effects[bundle.layer_digest] = EcrFetchError(
        "blob GET returned HTTP 503"
    )

    with pytest.raises(EcrFetchError):
        handler.lambda_handler(event, lambda_context)

    assert "artifact.validated" not in handler._test_recorder.detail_types()
    assert all(
        d.get("errorKind") != "oscal_layer_too_large"
        for d in handler._test_recorder.details("artifact.validation-failed")
    )


def test_env_limit_with_surrounding_whitespace_is_accepted(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_OSCAL_LAYER_BYTES", " 2048\n")
    assert _load_fresh_handler().MAX_OSCAL_LAYER_BYTES == 2048


def test_malformed_env_limit_message_is_bounded_and_escaped(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long or control-character value does not flood or forge the log line."""
    monkeypatch.setenv("MAX_OSCAL_LAYER_BYTES", "\x1b[31m" + "9x" * 5000)
    with pytest.raises(ValueError) as exc_info:
        _load_fresh_handler()
    msg = str(exc_info.value)
    assert "\x1b" not in msg
    assert msg.isascii()
    assert len(msg) < 200

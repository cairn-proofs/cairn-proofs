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
    assert layer_meta["mediaType"] == "application/oscal.catalog+json"
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
                "media_type": "application/oscal.catalog+json",
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
        ("application/oscal.catalog+json", oscal_blob, "cat.json"),
        ("text/plain", log_blob, "run.log"),
        ("image/png", png_blob, "screen.png"),
    ):
        entry = by_media[media]
        assert entry["digest"] == compute_sha256_digest(blob)
        assert entry["size"] == len(blob)
        assert entry["title"] == title
    # No layer omitted on account of media type.
    assert set(by_media) == {"application/oscal.catalog+json", "text/plain", "image/png"}


def test_mixed_bundle_oscal_layer_carries_inline_enrichment(reload_handler, lambda_context) -> None:
    """Only the OSCAL layer carries an `oscal` sub-object; non-OSCAL layers do not,
    and all sit in the SAME layers list."""
    handler = reload_handler
    oscal_blob = catalog_blob_bytes()
    log_blob = b"a plain log, not oscal"
    bundle, layer_blobs = _multi_layer_bundle(
        layers=[
            {"media_type": "text/plain", "blob": log_blob, "title": "run.log"},
            {"media_type": "application/oscal.catalog+json", "blob": oscal_blob, "title": "c.json"},
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
    oscal = by_media["application/oscal.catalog+json"]["oscal"]
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
                "media_type": "application/oscal.catalog+json",
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
        "application/oscal.catalog+json",
        "image/png",
    ]


def test_oscal_lookalike_media_type_is_not_enriched_and_not_fetched(
    reload_handler, lambda_context
) -> None:
    """A layer whose mediaType merely LOOKS OSCAL-ish but lacks the exact
    ``application/oscal.`` prefix (note: hyphen, not the dot delimiter) is treated
    as a plain content layer — NO ``oscal`` sub-object and NO blob fetch (the OSCAL
    blob fetch is the only per-layer fetch). Pins the prefix discrimination so a
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
            {"media_type": "application/oscal.catalog+json", "blob": in_window, "title": "a.json"},
            {"media_type": "application/oscal.catalog+json", "blob": future, "title": "b.json"},
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

"""Tests for cairn_oci.config_blob (reduced to content-descriptive fields)."""

from __future__ import annotations

import pytest
from cairn_oci.config_blob import (
    CONFIG_BLOB_MEDIA_TYPE,
    NOTARY_SIGNATURE_ARTIFACT_TYPE,
    OCI_EMPTY_MEDIA_TYPE,
    RESOURCE_ARTIFACT_TYPE,
    RESOURCE_MEDIA_TYPE,
    WORM_RECEIPT_ARTIFACT_TYPE,
    ConfigBlob,
    OscalReferences,
    SkipReason,
    classify_pipeline_internal,
)
from pydantic import ValidationError


def test_config_blob_media_type_constant() -> None:
    # Media type UNCHANGED at v1 — reduced in place, not version-bumped.
    assert CONFIG_BLOB_MEDIA_TYPE == "application/vnd.cairnproofs.artifact.config.v1+json"


def test_resource_artifact_type_constant() -> None:
    assert RESOURCE_ARTIFACT_TYPE == "application/vnd.cairnproofs.artifact.resource.v1"


def test_resource_media_type_constant() -> None:
    assert RESOURCE_MEDIA_TYPE == "application/vnd.cairnproofs.artifact.resource.v1+json"


def test_worm_receipt_artifact_type_constant() -> None:
    assert WORM_RECEIPT_ARTIFACT_TYPE == "application/vnd.cairnproofs.artifact.worm-receipt.v1"


def test_signature_artifact_type_pins_to_sign_lambda_mint_site() -> None:
    # Drift guard: cairn_oci is the single source of truth for the
    # pipeline-minted referrer artifactTypes, but the sign Lambda mints its own
    # literal. Tests MAY cross the libs/lambdas boundary to pin them equal; the
    # one-way dependency rule forbids this only in PRODUCTION code.
    from cairn_lambda_sign_bundle.referrers import NOTARY_SIGNATURE_ARTIFACT_TYPE as MINT_SITE

    assert NOTARY_SIGNATURE_ARTIFACT_TYPE == MINT_SITE


def test_worm_receipt_artifact_type_pins_to_archive_lambda_mint_site() -> None:
    # Drift guard: WORM_RECEIPT_ARTIFACT_TYPE must equal the archive
    # Lambda's canonical RECEIPT_ARTIFACT_TYPE literal it mints onto the receipt
    # referrer manifest.
    from cairn_lambda_archive_to_s3.receipt import RECEIPT_ARTIFACT_TYPE

    assert WORM_RECEIPT_ARTIFACT_TYPE == RECEIPT_ARTIFACT_TYPE


def test_resource_referrer_classifies_as_oci_empty() -> None:
    # A resource-as-referrer manifest carries the resource artifactType and an
    # empty config. The EXISTING predicate skips it purely on the empty config
    # mediaType — no resource-specific branch is involved.
    manifest = {
        "artifactType": RESOURCE_ARTIFACT_TYPE,
        "config": {"mediaType": OCI_EMPTY_MEDIA_TYPE},
    }
    assert classify_pipeline_internal(manifest) is SkipReason.OCI_EMPTY


@pytest.mark.parametrize(
    "config_media_type",
    [CONFIG_BLOB_MEDIA_TYPE, "application/vnd.example.unknown.v1+json"],
)
def test_resource_artifact_type_does_not_drive_skip_decision(config_media_type: str) -> None:
    # Finding-1 guard: artifactType MUST NOT widen the skip surface. A manifest
    # bearing RESOURCE_ARTIFACT_TYPE but a non-internal config mediaType is NOT
    # skipped — it proceeds to [INGEST-017.07] (None), proving the skip decision keys
    # off config.mediaType alone, never off artifactType.
    manifest = {
        "artifactType": RESOURCE_ARTIFACT_TYPE,
        "config": {"mediaType": config_media_type},
    }
    assert classify_pipeline_internal(manifest) is None


def test_config_blob_minimal_construction() -> None:
    # Reduced model: only project-id (required) and the tolerated
    # oscalSchemaVersion remain. The claim fields AND bundleType (artifact-kind)
    # are gone — kind left the content manifest.
    cb = ConfigBlob(projectId="alpha-bank")
    assert cb.project_id == "alpha-bank"
    assert cb.oscal_schema_version is None


def test_config_blob_round_trips_by_alias() -> None:
    cb = ConfigBlob.model_validate({"projectId": "p"})
    dumped = cb.model_dump(by_alias=True)
    assert dumped["projectId"] == "p"


def test_config_blob_relocated_claim_fields_are_not_model_fields() -> None:
    # The producer-claim fields moved to the Producer_Attestation_Referrer; none of them
    # is a Config_Blob model field anymore (they ride as extras at most).
    # bundle_type (artifact-kind) is likewise gone — kind left the content manifest.
    for removed in (
        "account_id",
        "controls",
        "catalog_or_profile_uuid",
        "assessment_period",
        "producer",
        "bundle_type",
    ):
        assert removed not in ConfigBlob.model_fields, removed
    # And the content-descriptive fields remain. projectId stays in the digest (Req 318).
    assert "project_id" in ConfigBlob.model_fields
    assert "oscal_references" in ConfigBlob.model_fields
    assert "oscal_schema_version" in ConfigBlob.model_fields


def test_config_blob_allows_extra_fields() -> None:
    # extra="allow": relocated claim keys a stale producer might still send are
    # tolerated as extras rather than rejected (clean-break harmlessness).
    cb = ConfigBlob.model_validate(
        {
            "projectId": "p",
            "bundleType": "other",
            "accountId": "111122223333",
            "controls": ["ac-2"],
            "extraVendorField": "ok",
        }
    )
    assert cb.extraVendorField == "ok"
    # The relocated keys ride as extras, not as load-bearing model attributes.
    assert not hasattr(cb, "controls") or cb.controls == ["ac-2"]


def test_config_blob_control_family_key_is_dropped_not_modeled() -> None:
    # control_family is never a model field; a supplied controlFamily key is
    # tolerated as an extra (extra="allow") but never participates as bundle data.
    cb = ConfigBlob.model_validate({"projectId": "p", "bundleType": "other", "controlFamily": "AC"})
    assert not hasattr(cb, "control_family")
    assert "control_family" not in ConfigBlob.model_fields


def test_config_blob_missing_project_id_raises() -> None:
    # project_id remains required (no default); bundle_type is not a field.
    with pytest.raises(ValidationError):
        ConfigBlob.model_validate({})


def test_config_blob_tolerates_stale_bundle_type_as_extra() -> None:
    # A stale producer that still emits bundleType is harmless: extra="allow"
    # tolerates it as an extra (clean-break harmlessness), and it is NOT a
    # load-bearing model attribute (kind is claim-only now).
    cb = ConfigBlob.model_validate({"projectId": "p", "bundleType": "other"})
    assert "bundle_type" not in ConfigBlob.model_fields
    assert cb.project_id == "p"


def test_oscal_references_defaults() -> None:
    o = OscalReferences()
    assert o.ar_uuid is None
    assert o.ssp_uuid is None
    assert o.findings == []
    assert o.observations == []


def test_oscal_references_alias_round_trip() -> None:
    o = OscalReferences.model_validate({"arUuid": "u1", "sspUuid": "u2"})
    assert o.ar_uuid == "u1"
    assert o.ssp_uuid == "u2"


def test_config_blob_with_oscal_references() -> None:
    cb = ConfigBlob.model_validate(
        {
            "projectId": "p",
            "oscalSchemaVersion": "1.1.2",
            "oscalReferences": {"arUuid": "ar-1"},
        }
    )
    assert cb.oscal_schema_version == "1.1.2"
    assert cb.oscal_references is not None
    assert cb.oscal_references.ar_uuid == "ar-1"


def test_config_blob_tolerates_declared_oscal_schema_version() -> None:
    # Back-compat: a declared oscalSchemaVersion is tolerated-and-ignored — the
    # OSCAL schema version is now content-derived. The model still validates and
    # round-trips when a producer supplies the field.
    cb = ConfigBlob.model_validate(
        {
            "projectId": "p",
            "oscalSchemaVersion": "1.1.2",
        }
    )
    assert cb.oscal_schema_version == "1.1.2"
    # And it is preserved on by-alias re-serialization (tolerated, not dropped).
    assert cb.model_dump(by_alias=True)["oscalSchemaVersion"] == "1.1.2"

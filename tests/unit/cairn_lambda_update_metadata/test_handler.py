"""Unit tests for the update-metadata Lambda handler."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.unit.cairn_lambda_update_metadata.conftest import (
    ACCOUNT_ID,
    CATALOG_UUID,
    ORG_ID,
    TENANT_ID,
    archive_failed_event,
    archived_event,
    control_row_key,
    manifest_digest,
    non_oscal_layer,
    oscal_layer,
    row_key,
    signed_event,
    signing_failed_event,
    validated_event,
    validation_failed_event,
)


def _parse_emf(out: str) -> list[dict[str, Any]]:
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


def test_validated_event_persists_identity_triple_on_record(reload_handler, lambda_context) -> None:
    """The Metadata_Index record carries the opaque identity triple: tenantId and
    projectId are the key components (pk / sk sub-hierarchy) and orgId rides as a
    plain attribute ([META-001] item 5)."""
    handler = reload_handler
    md = manifest_digest("a")
    handler.lambda_handler(validated_event(md=md), lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert item["pk"] == TENANT_ID
    assert item["sk"] == f"alpha-bank#{md}"
    assert item["orgId"] == ORG_ID
    assert item["tenantId"] == TENANT_ID
    assert item["projectId"] == "alpha-bank"
    # No AWS-account coordinate lands on the row.
    assert "accountId" not in item


def test_tenant_id_metric_dimension_emitted(reload_handler, lambda_context, capsys) -> None:
    """The per-bundle TenantId dimension lands on the EMF line under the uniform
    tenancy model (design item 8) — replacing the former Service_Hosted AccountId."""
    handler = reload_handler
    handler.lambda_handler(validated_event(), lambda_context)

    docs = _parse_emf(capsys.readouterr().out)
    assert any(d.get("TenantId") == TENANT_ID for d in docs), (
        f"expected TenantId={TENANT_ID!r} on the EMF line; "
        f"saw {[d.get('TenantId') for d in docs]!r}"
    )


def test_signed_event_routes_to_tenant_pk_row(reload_handler, lambda_context) -> None:
    """A post-validated event addresses the SAME tenant-PK row the validated event
    created — the triple threaded on the claim projection keys both writes."""
    handler = reload_handler
    md = manifest_digest("a")
    handler.lambda_handler(validated_event(md=md), lambda_context)
    handler.lambda_handler(signed_event(md=md), lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert item["signingStatus"] == "signed"
    assert item["tenantId"] == TENANT_ID


def test_validated_event_creates_record_with_principal_and_ecr_pushed_at(
    reload_handler, lambda_context
) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    event = validated_event(md=md)

    result = handler.lambda_handler(event, lambda_context)

    assert result == {"detailType": "artifact.validated", "status": "applied"}
    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert item["signingStatus"] == "pending"
    assert item["archiveStatus"] == "pending"
    assert item["validatedAt"] == "2026-05-26T12:05:00Z"
    assert item["createdAt"] == "2026-05-26T12:05:00Z"
    assert item["ecrPushedAt"] == "2026-05-26T12:05:00Z"
    principal = item["authenticatedPushPrincipal"]
    assert principal["accountId"] == ACCOUNT_ID
    assert principal["mfaAuthenticated"] is True
    assert principal["userName"] == "EvidencePushRole"
    assert principal["principalId"] == "AROAEXAMPLE:s"


def test_validated_event_writes_controls_relationship_rows(reload_handler, lambda_context) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    event = validated_event(md=md, controls=["ac-1", "ac-2"])
    handler.lambda_handler(event, lambda_context)

    controls_table = handler._test_tables["controls"]
    row1 = controls_table.get_item(Key=control_row_key(md, "ac-1"))["Item"]
    row2 = controls_table.get_item(Key=control_row_key(md, "ac-2"))["Item"]
    # The tenant-PK plane threads the identity-triple tenant into the partition
    # key; the sort key bands the (catalog/profile, control) listing by digest.
    assert row1["pk"] == TENANT_ID
    assert row1["sk"] == f"{CATALOG_UUID}#ac-1#{md}"
    assert row1["manifestDigest"] == md
    assert row1["catalogOrProfileUuid"] == CATALOG_UUID
    assert row1["controlId"] == "ac-1"
    assert row1["tenantId"] == TENANT_ID
    assert row1["projectId"] == "alpha-bank"
    assert row2["controlId"] == "ac-2"


def test_controls_relationship_rows_artifact_kind_sourced_from_claim(
    reload_handler, lambda_context
) -> None:
    """The denormalized Controls relationship rows carry an `artifactKind`
    attribute sourced from the Producer Attestation referrer claim's `artifactKind`. A
    distinct claim value proves the source AND that the control-lookup access
    path keeps a non-null kind dimension.
    """
    handler = reload_handler
    md = manifest_digest("a")
    event = validated_event(md=md, controls=["ac-1", "ac-2"])
    # Drive a distinct claim artifactKind to prove the denorm row follows it.
    event["detail"]["attestation"]["artifactKind"] = "tool-output"

    handler.lambda_handler(event, lambda_context)

    controls_table = handler._test_tables["controls"]
    row1 = controls_table.get_item(Key=control_row_key(md, "ac-1"))["Item"]
    row2 = controls_table.get_item(Key=control_row_key(md, "ac-2"))["Item"]
    assert row1["artifactKind"] == "tool-output"
    assert row2["artifactKind"] == "tool-output"


def test_validated_event_with_no_controls_skips_controls_fanout(
    reload_handler, lambda_context
) -> None:
    handler = reload_handler
    md = manifest_digest("b")
    event = validated_event(md=md, controls=[])
    handler.lambda_handler(event, lambda_context)

    response = handler._test_tables["controls"].scan()
    assert response["Items"] == []


def test_validated_event_without_catalog_uuid_skips_controls_fanout(
    reload_handler, lambda_context
) -> None:
    handler = reload_handler
    md = manifest_digest("c")
    event = validated_event(md=md, catalog_or_profile_uuid=None, controls=["ac-1"])
    handler.lambda_handler(event, lambda_context)

    response = handler._test_tables["controls"].scan()
    assert response["Items"] == []


def test_validated_event_idempotent_no_op_on_duplicate(reload_handler, lambda_context) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    event = validated_event(md=md)
    handler.lambda_handler(event, lambda_context)
    handler.lambda_handler(event, lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert item["validatedAt"] == "2026-05-26T12:05:00Z"


def test_validated_event_lands_rich_payload_when_it_creates_the_row(
    reload_handler, lambda_context
) -> None:
    """update-metadata wins the create: the system-bounded payload lands and the
    record carries none of the producer-claim attributes ([META-002.06])."""
    handler = reload_handler
    md = manifest_digest("a")
    handler.lambda_handler(
        validated_event(md=md, controls=["ac-1", "ac-2"], layers=[oscal_layer()]),
        lambda_context,
    )
    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert "controls" not in item
    assert "controlLinkage" not in item
    assert "catalogOrProfileUuid" not in item
    assert item["layers"]
    assert item["validatedAt"] == "2026-05-26T12:05:00Z"


def test_validated_event_persists_artifact_resource_reference_under_renamed_key(
    reload_handler, lambda_context
) -> None:
    """Persist leg of the emit→persist round-trip: the validate stage emits the
    OSCAL-reference projection under the detail key ``artifactResourceReference``,
    and update-metadata persists it verbatim onto the row under that same key.

    The emit half is covered in the validate handler tests. This pins both ends
    to the single ``artifactResourceReference`` spelling so an emitter-only or
    consumer-only divergence surfaces here. The ``oscalEvidenceReference``
    attribute must NOT appear on the persisted row.
    """
    handler = reload_handler
    md = manifest_digest("a")
    ref = {
        "uuid": "8c0c1e9a-1111-4222-8333-444455556666",
        "rlinks": [{"href": f"s3://cairn-archive-cmh-alpha/alpha-bank/{md}/"}],
    }
    event = validated_event(md=md, layers=[oscal_layer()])
    event["detail"]["artifactResourceReference"] = ref

    handler.lambda_handler(event, lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert item["artifactResourceReference"] == ref
    # oscalEvidenceReference never lands on the row.
    assert "oscalEvidenceReference" not in item


def test_validated_event_merges_rich_payload_onto_preexisting_marker_row(
    reload_handler, lambda_context
) -> None:
    """Row-completeness invariant: validate pre-created a marker-only
    row (validateStatus/artifactKind). update-metadata's create collides,
    and its rich payload MERGES onto the row instead of being discarded as a
    no-op — the row converges to the UNION."""
    handler = reload_handler
    md = manifest_digest("a")
    # Simulate validate having won the create with its marker-only field set.
    handler._test_tables["metadata"].put_item(
        Item={
            **row_key(md),
            "validateStatus": "validated",
            "artifactKind": "assessment-results",
        }
    )

    handler.lambda_handler(
        validated_event(md=md, controls=["ac-1", "ac-2"], layers=[oscal_layer()]),
        lambda_context,
    )

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    # validate's marker fields survived...
    assert item["validateStatus"] == "validated"
    assert item["artifactKind"] == "assessment-results"
    # ...AND update-metadata's system-bounded payload merged on, carrying none of
    # the producer-claim attributes ([META-002.06]).
    assert "controls" not in item
    assert "controlLinkage" not in item
    assert item["layers"]
    assert item["signingStatus"] == "pending"
    assert item["archiveStatus"] == "pending"


def test_validated_event_missing_project_id_raises(reload_handler, lambda_context) -> None:
    handler = reload_handler
    event = validated_event()
    event["detail"]["attestation"]["projectId"] = None
    with pytest.raises(ValueError, match="tenantId / projectId"):
        handler.lambda_handler(event, lambda_context)


def test_signed_event_updates_existing_record(reload_handler, lambda_context) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    handler.lambda_handler(validated_event(md=md), lambda_context)
    handler.lambda_handler(signed_event(md=md), lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert item["signingStatus"] == "signed"
    assert item["signatureDigest"] == "sha256:" + "5" * 64
    assert item["signedAt"] == "2026-05-26T12:08:00Z"


def test_signed_event_projects_attestation_signature_digest(reload_handler, lambda_context) -> None:
    """Both signed referrers are projected: content + attestation signature digests."""
    handler = reload_handler
    md = manifest_digest("a")
    att_sig = "sha256:" + "7" * 64
    handler.lambda_handler(validated_event(md=md), lambda_context)
    handler.lambda_handler(
        signed_event(md=md, attestation_signature_digest=att_sig), lambda_context
    )

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert item["signingStatus"] == "signed"
    assert item["signatureDigest"] == "sha256:" + "5" * 64
    assert item["attestationSignatureDigest"] == att_sig


def test_signed_event_upserts_when_record_absent(reload_handler, lambda_context) -> None:
    """Out-of-order: signed lands before validated; upsert creates the row."""
    handler = reload_handler
    md = manifest_digest("a")
    handler.lambda_handler(signed_event(md=md), lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert item["signingStatus"] == "signed"
    assert item["signatureDigest"] == "sha256:" + "5" * 64


def test_signed_event_stale_drop_when_record_newer(reload_handler, lambda_context) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    handler.lambda_handler(
        validated_event(md=md, event_time="2026-05-27T00:00:00Z"), lambda_context
    )
    handler.lambda_handler(signed_event(md=md, event_time="2026-05-26T00:00:00Z"), lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert item["signingStatus"] == "pending"


def test_signing_failed_event_marks_unsigned(reload_handler, lambda_context) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    handler.lambda_handler(validated_event(md=md), lambda_context)
    handler.lambda_handler(signing_failed_event(md=md), lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert item["signingStatus"] == "unsigned"
    assert "notation" in item["signingError"]


def test_archived_event_marks_archived(reload_handler, lambda_context) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    handler.lambda_handler(validated_event(md=md), lambda_context)
    handler.lambda_handler(archived_event(md=md), lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert item["archiveStatus"] == "archived"
    assert item["s3Bucket"] == "cairn-archive-cmh-alpha"
    assert item["wormReceiptDigest"] == "sha256:" + "9" * 64
    assert item["wormEnabled"] is False


def test_archive_failed_event_marks_failed_with_detail(reload_handler, lambda_context) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    handler.lambda_handler(validated_event(md=md), lambda_context)
    handler.lambda_handler(archive_failed_event(md=md), lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert item["archiveStatus"] == "failed"
    assert item["archiveError"] == "config"
    detail = item["archiveFailureDetail"]
    assert detail["computedDigest"] == "sha256:" + "b" * 64
    assert detail["expectedDigest"] == "sha256:" + "a" * 64


def test_validation_failed_event_writes_no_record(reload_handler, lambda_context) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    handler.lambda_handler(validation_failed_event(md=md), lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md)).get("Item")
    assert item is None


def test_unhandled_detail_type_raises(reload_handler, lambda_context) -> None:
    handler = reload_handler
    event = {
        "detail-type": "evidence.unrecognized",
        "time": "2026-05-26T12:00:00Z",
        "detail": {},
    }
    with pytest.raises(ValueError, match="unhandled detail-type"):
        handler.lambda_handler(event, lambda_context)


def test_event_detail_not_a_dict_raises(reload_handler, lambda_context) -> None:
    handler = reload_handler
    event = {"detail-type": "artifact.validated", "detail": "not-a-dict", "time": "2026-05-26"}
    with pytest.raises(ValueError, match=r"event\.detail missing or not a dict"):
        handler.lambda_handler(event, lambda_context)


def test_conditional_update_requires_event_time(reload_handler, lambda_context) -> None:
    handler = reload_handler
    event = signed_event()
    event["time"] = ""
    with pytest.raises(ValueError, match="event timestamp guard"):
        handler.lambda_handler(event, lambda_context)


def test_conditional_update_requires_project_id_and_manifest_digest(
    reload_handler, lambda_context
) -> None:
    handler = reload_handler
    event = signed_event()
    event["detail"]["attestation"]["projectId"] = None
    with pytest.raises(ValueError, match="tenantId / projectId / manifestDigest"):
        handler.lambda_handler(event, lambda_context)


def test_environment_must_provide_metadata_table_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("METADATA_TABLE_NAME", raising=False)
    monkeypatch.delenv("CONTROLS_TABLE_NAME", raising=False)
    monkeypatch.setenv("CAIRN_STAGE", "alpha")
    monkeypatch.setenv("CAIRN_REGION_CODE", "cmh")
    import importlib

    import cairn_lambda_update_metadata.handler as handler_module

    handler_module._INDEX = None
    handler_module._CONTROLS = None
    handler = importlib.reload(handler_module)
    handler._INDEX = None
    handler._CONTROLS = None
    with pytest.raises(RuntimeError, match="METADATA_TABLE_NAME"):
        handler._index()


def test_environment_must_provide_controls_table_name(reload_handler, monkeypatch) -> None:
    handler = reload_handler
    monkeypatch.setattr(handler, "CONTROLS_TABLE_NAME", "")
    handler._CONTROLS = None
    with pytest.raises(RuntimeError, match="CONTROLS_TABLE_NAME"):
        handler._controls()


def test_observability_scope_invoked_per_event(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = reload_handler
    captured: list[str | None] = []
    original = handler.observability_scope

    from contextlib import contextmanager

    @contextmanager
    def recording_scope(event, **kwargs):
        captured.append(kwargs.get("event_type"))
        with original(event, **kwargs) as tc:
            yield tc

    monkeypatch.setattr(handler, "observability_scope", recording_scope)
    handler.lambda_handler(validated_event(), lambda_context)
    assert captured == ["metadata-update"]


def test_success_metric_on_each_event(reload_handler, lambda_context, capsys) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    handler.lambda_handler(validated_event(md=md), lambda_context)
    docs = _parse_emf(capsys.readouterr().out)
    assert any(_metric_value(d, "Success") == 1.0 for d in docs)
    assert any(_metric_value(d, "DurationMs") is not None for d in docs)


def test_failure_metric_on_validated_without_project_id(
    reload_handler, lambda_context, capsys
) -> None:
    handler = reload_handler
    event = validated_event()
    event["detail"]["attestation"]["projectId"] = None
    with pytest.raises(ValueError):
        handler.lambda_handler(event, lambda_context)
    docs = _parse_emf(capsys.readouterr().out)
    assert any(_metric_value(d, "Failure") == 1.0 for d in docs)


def test_supplied_control_family_in_attestation_is_not_persisted(
    reload_handler, lambda_context
) -> None:
    """A controlFamily key present anywhere on the incoming event (even injected
    into the attestation projection) must never be persisted — control-family is
    a derived-on-demand projection of `controls`, never a stored attribute."""
    handler = reload_handler
    md = manifest_digest("a")
    event = validated_event(md=md, controls=["ac-2"])
    # Inject a spurious controlFamily onto the claim projection; the handler
    # never reads or persists it.
    event["detail"]["attestation"]["controlFamily"] = "AC"

    handler.lambda_handler(event, lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert "controlFamily" not in item


def test_no_controls_bundle_omits_control_fields_and_writes_no_rows(
    reload_handler, lambda_context
) -> None:
    """An uncontrolled bundle's record carries no control attributes ([META-002.06])
    and writes zero control-claims rows ([PCLAIM-001.03])."""
    handler = reload_handler
    md = manifest_digest("b")
    event = validated_event(md=md, controls=[])

    handler.lambda_handler(event, lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert "controlLinkage" not in item
    assert "controls" not in item
    assert "catalogOrProfileUuid" not in item
    assert "controlFamily" not in item
    # record_controls returns 0 / writes nothing on empty controls — assert it.
    assert handler._test_tables["controls"].scan()["Items"] == []


def test_multi_control_fanout_writes_one_row_per_control(reload_handler, lambda_context) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    event = validated_event(md=md, controls=["ac-2", "ac-3", "au-4"])

    handler.lambda_handler(event, lambda_context)

    controls_table = handler._test_tables["controls"]
    rows = controls_table.scan()["Items"]
    assert len(rows) == 3
    # Tenant-PK plane: every row shares the identity-triple tenant partition,
    # and the sort key bands each control under the catalog/profile by digest.
    assert {row["pk"] for row in rows} == {TENANT_ID}
    assert {row["sk"] for row in rows} == {
        f"{CATALOG_UUID}#ac-2#{md}",
        f"{CATALOG_UUID}#ac-3#{md}",
        f"{CATALOG_UUID}#au-4#{md}",
    }
    assert all("controlFamily" not in row for row in rows)


def test_control_linkage_none_signal_emitted(reload_handler, lambda_context, capsys) -> None:
    """The auditor-trust observability signal fires for a controlLinkage=none record."""
    handler = reload_handler
    md = manifest_digest("b")
    event = validated_event(md=md, controls=[])

    handler.lambda_handler(event, lambda_context)

    docs = _parse_emf(capsys.readouterr().out)
    assert any(_metric_value(d, "ControlLinkageNone") == 1.0 for d in docs)


def test_control_linkage_none_signal_not_emitted_for_cited_record(
    reload_handler, lambda_context, capsys
) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    event = validated_event(md=md, controls=["ac-2"])

    handler.lambda_handler(event, lambda_context)

    docs = _parse_emf(capsys.readouterr().out)
    assert all(_metric_value(d, "ControlLinkageNone") is None for d in docs)


def test_record_carries_no_record_level_oscal_schema_version(
    reload_handler, lambda_context
) -> None:
    """The record-level OSCAL scalar is dropped ([META-008]): even for an OSCAL
    bundle the Metadata_Index record carries no top-level `oscalSchemaVersion`.
    Per-layer OSCAL enrichment stays inside the `layers` entries."""
    handler = reload_handler
    md = manifest_digest("a")
    event = validated_event(md=md, layers=[oscal_layer(schema_version="1.1.2")])

    handler.lambda_handler(event, lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert "oscalSchemaVersion" not in item
    # Per-layer enrichment is untouched: the OSCAL layer keeps its `oscal`
    # sub-object carrying the content-extracted schema version.
    assert item["layers"][0]["oscal"]["oscalSchemaVersion"] == "1.1.2"


def test_controls_fanout_failure_reraises_after_metadata_commit(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = reload_handler

    class _BoomControls:
        def record_controls(self, **kwargs):
            raise RuntimeError("simulated controls fanout failure")

    handler._CONTROLS = _BoomControls()
    md = manifest_digest("a")
    with pytest.raises(RuntimeError, match="simulated"):
        handler.lambda_handler(validated_event(md=md), lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert item["signingStatus"] == "pending"


def test_record_carries_no_claim_attributes(reload_handler, lambda_context) -> None:
    """The Metadata_Index record holds only system-bounded facts: it carries none
    of the producer-claim attributes — `controls`, `catalogOrProfileUuid`,
    `assessmentPeriod`, `assessor`, `assessmentId`, `supersedes`, the derived
    `controlFamily` — and no record-level `oscalSchemaVersion` ([META-002.06],
    [META-008]). The claim's system of record is the signed Producer Attestation
    referrer; control linkage is projected into the producer-claims control-claims
    projection ([PCLAIM-001])."""
    handler = reload_handler
    md = manifest_digest("a")
    event = validated_event(md=md, controls=["ac-2", "au-6"], layers=[oscal_layer()])
    att = event["detail"]["attestation"]
    att["assessor"] = "acme-auditors"
    att["assessmentId"] = "assessment-2026-q2-001"
    att["supersedes"] = "sha256:" + "c" * 64

    handler.lambda_handler(event, lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    for claim_key in (
        "controls",
        "catalogOrProfileUuid",
        "assessmentPeriod",
        "assessor",
        "assessmentId",
        "supersedes",
        "controlLinkage",
        "controlFamily",
        "oscalSchemaVersion",
    ):
        assert claim_key not in item
    # artifactKind is single-owned on the row by the validate stage's marker write
    # (consumed by mint); update-metadata does NOT contribute it, so the isolated
    # update-metadata row omits it (row-ownership disjointness).
    assert "artifactKind" not in item


def test_absent_optional_claim_fields_are_omitted_not_nulled(
    reload_handler, lambda_context
) -> None:
    """When the referrer supplies no assessor/assessmentId/supersedes, those
    attributes are omitted from the row rather than stored as nulls."""
    handler = reload_handler
    md = manifest_digest("a")
    event = validated_event(md=md, controls=["ac-2"])
    # The default attestation projection carries no assessor/assessmentId/supersedes.

    handler.lambda_handler(event, lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert "assessor" not in item
    assert "assessmentId" not in item
    assert "supersedes" not in item
    # artifactKind is single-owned by the validate stage's marker write (consumed
    # by mint); update-metadata does NOT contribute it, so the isolated
    # update-metadata row omits it (row-ownership disjointness).
    assert "artifactKind" not in item


def test_controls_fanout_keyed_from_attestation_catalog_uuid(
    reload_handler, lambda_context
) -> None:
    """The {catalogOrProfileUuid}#{controlId} fan-out is keyed from the referrer
    projection's catalogOrProfileUuid + controls, not from configBlob."""
    handler = reload_handler
    md = manifest_digest("a")
    other_uuid = "00000000-0000-4000-8000-000000000000"
    event = validated_event(md=md, controls=["ac-2"], catalog_or_profile_uuid=other_uuid)

    handler.lambda_handler(event, lambda_context)

    rows = handler._test_tables["controls"].scan()["Items"]
    assert len(rows) == 1
    # The producer-declared catalog/profile uuid leads the SK band; the tenant
    # partition key still comes from the identity triple, not from content.
    assert rows[0]["pk"] == TENANT_ID
    assert rows[0]["sk"] == f"{other_uuid}#ac-2#{md}"
    assert rows[0]["catalogOrProfileUuid"] == other_uuid


def test_attestation_digest_is_indexed_for_rebuild_provenance(
    reload_handler, lambda_context
) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    handler.lambda_handler(validated_event(md=md), lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert item["attestationDigest"] == "sha256:" + "d" * 64


def test_created_at_is_first_seen_from_ecr_pushed_at(reload_handler, lambda_context) -> None:
    """createdAt is the bundle's first-seen time, derived from ecrPushedAt (the
    AWS-set EventBridge eventTime), NEVER a producer/content timestamp — and is
    distinct from the envelope `time`."""
    handler = reload_handler
    md = manifest_digest("a")
    event = validated_event(md=md, event_time="2026-05-26T12:05:00Z")
    # Make ecrPushedAt distinct from the envelope time to prove the source.
    event["detail"]["ecrPushedAt"] = "2026-05-26T11:59:00Z"

    handler.lambda_handler(event, lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert item["createdAt"] == "2026-05-26T11:59:00Z"
    # validatedAt / updatedAt still track the envelope time.
    assert item["validatedAt"] == "2026-05-26T12:05:00Z"
    assert item["updatedAt"] == "2026-05-26T12:05:00Z"


def test_created_at_stable_under_event_redelivery(reload_handler, lambda_context) -> None:
    """Re-delivery of the same triggering event leaves createdAt unchanged
    (write-once via create_or_merge's per-attribute if_not_exists)."""
    handler = reload_handler
    md = manifest_digest("a")
    event = validated_event(md=md)
    event["detail"]["ecrPushedAt"] = "2026-05-26T11:59:00Z"

    handler.lambda_handler(event, lambda_context)
    # Re-deliver with a DIFFERENT ecrPushedAt; createdAt must not move.
    event2 = validated_event(md=md)
    event2["detail"]["ecrPushedAt"] = "2026-05-26T23:00:00Z"
    handler.lambda_handler(event2, lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert item["createdAt"] == "2026-05-26T11:59:00Z"


# --------------------------------------------------------------------------
# Control-claims projection row attributes ([PCLAIM-001], [PCLAIM-001.04]).
#
# A fanned-out row carries only attributes with a defined source. The
# ProducerAttestationReferrer model carries no `controlScope` field and the
# speculative `assessmentPeriod` denormalization is dropped (carried by
# reference), so neither appears on a row.
# --------------------------------------------------------------------------


def test_controls_row_omits_control_scope(reload_handler, lambda_context) -> None:
    """A fanned-out control-claims row carries no `controlScope` attribute
    ([PCLAIM-001.04]): no Producer_Attestation_Referrer claim field sources it."""
    handler = reload_handler
    md = manifest_digest("a")
    event = validated_event(md=md, controls=["ac-1", "ac-2"])

    handler.lambda_handler(event, lambda_context)

    rows = handler._test_tables["controls"].scan()["Items"]
    assert len(rows) == 2
    assert all("controlScope" not in row for row in rows)


def test_controls_row_omits_assessment_period_and_has_defined_sources(
    reload_handler, lambda_context
) -> None:
    """A fanned-out control-claims row carries NO `assessmentPeriod` (dropped as a
    speculative denormalization, carried by reference) and every written attribute
    traces to a defined source ([PCLAIM-001.04]): `projectId`/`artifactKind` from
    the attestation projection, and the three read-state fields
    `signingStatus`/`archiveStatus`/`wormStatus` from pipeline status."""
    handler = reload_handler
    md = manifest_digest("a")
    event = validated_event(md=md, controls=["ac-1", "ac-2"])

    handler.lambda_handler(event, lambda_context)

    rows = handler._test_tables["controls"].scan()["Items"]
    assert len(rows) == 2
    # The speculative assessmentPeriod denormalization is gone.
    assert all("assessmentPeriod" not in row for row in rows)
    # The three read-state fields [PCLAIM-001] names are present, from pipeline status.
    for row in rows:
        assert row["signingStatus"] == "pending"
        assert row["archiveStatus"] == "pending"
        assert row["wormStatus"] == "pending"
        assert row["projectId"] == "alpha-bank"
        assert row["tenantId"] == TENANT_ID
        assert row["orgId"] == ORG_ID
        assert row["artifactKind"] == "assessment-results"
    # Every attribute on the row has a defined source: the key/identity columns
    # the projection sets, plus the attestation-projection and pipeline-status
    # denormalizations. No attribute lacks a source.
    defined_sources = {
        "pk",
        "sk",
        "catalogOrProfileUuid",
        "controlId",
        "manifestDigest",
        "orgId",
        "tenantId",
        "projectId",
        "artifactKind",
        "ecrRepoPath",
        "signingStatus",
        "archiveStatus",
        "wormStatus",
        "createdAt",
    }
    for row in rows:
        assert set(row).issubset(defined_sources), (
            f"row carries undefined-source attribute(s): {set(row) - defined_sources}"
        )


# ===========================================================================
# Row stores the unified `layers` projection.
# ===========================================================================


def test_row_stores_unified_layers_for_mixed_bundle(reload_handler, lambda_context) -> None:
    """A mixed OSCAL + non-OSCAL bundle stores every layer under `layers`, with the
    OSCAL enrichment inlined and no `oscalLayers` attribute on the row."""
    handler = reload_handler
    md = manifest_digest("a")
    event = validated_event(
        md=md,
        layers=[
            oscal_layer(schema_version="1.1.2", layer_digest="sha256:" + "1" * 64),
            non_oscal_layer(layer_digest="sha256:" + "2" * 64, title="run.log"),
        ],
    )

    handler.lambda_handler(event, lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert len(item["layers"]) == 2
    # The legacy OSCAL-only attribute is never written.
    assert "oscalLayers" not in item
    by_media = {layer["mediaType"]: layer for layer in item["layers"]}
    assert "oscal" in by_media["application/oscal+json"]
    assert "oscal" not in by_media["text/plain"]
    # The record carries no record-level oscalSchemaVersion ([META-008]); the
    # schema version stays in the OSCAL layer's enrichment sub-object.
    assert "oscalSchemaVersion" not in item
    assert by_media["application/oscal+json"]["oscal"]["oscalSchemaVersion"] == "1.1.2"


def test_non_oscal_only_bundle_row_stores_layers_and_no_oscal_version(
    reload_handler, lambda_context
) -> None:
    """A non-OSCAL-only bundle stores its layers and carries no record-level
    oscalSchemaVersion ([META-008])."""
    handler = reload_handler
    md = manifest_digest("b")
    event = validated_event(
        md=md,
        layers=[non_oscal_layer(title="screenshot.png", media_type="image/png")],
    )

    handler.lambda_handler(event, lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert len(item["layers"]) == 1
    assert "oscalLayers" not in item
    assert "oscalSchemaVersion" not in item


def test_layers_stored_verbatim_preserving_order_and_title_omission(
    reload_handler, lambda_context
) -> None:
    """The unified `layers` list is stored on the row VERBATIM: order is preserved
    and a layer entry that omits ``title`` (validate's None-pruning for a title-less
    layer) is stored WITHOUT a synthesized null title. Pins that update-metadata is
    a faithful projection of detail.layers, not a re-derivation."""
    handler = reload_handler
    md = manifest_digest("a")
    first = {"digest": "sha256:" + "1" * 64, "size": 5, "mediaType": "text/plain", "title": "a.txt"}
    # Second entry has NO title key at all (validate omits it for a title-less layer).
    second = {"digest": "sha256:" + "2" * 64, "size": 6, "mediaType": "image/png"}
    event = validated_event(md=md, layers=[first, second])

    handler.lambda_handler(event, lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    stored = item["layers"]
    assert len(stored) == 2
    # Order preserved (list semantics, not a set/map).
    assert [layer["mediaType"] for layer in stored] == ["text/plain", "image/png"]
    assert stored[0]["title"] == "a.txt"
    # The title-less layer round-trips WITHOUT a title key — never a null.
    assert "title" not in stored[1]

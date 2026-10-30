"""Cross-stage seam regression: validate + update_metadata Metadata_Index row completeness.

Two pipeline stages write the SAME ``(pk, sk)`` Metadata_Index row off the SAME
``artifact.validated`` event with disjoint, write-once field sets:

- ``validate-bundle`` writes a marker (``validateStatus`` / ``artifactKind`` /
  ``name``) inline, before emitting ``artifact.validated``;
- ``update-metadata`` consumes ``artifact.validated`` and writes the rich OSCAL /
  control / annotation payload.

This module drives BOTH real handlers against ONE shared moto-backed
DynamoDB table and asserts the four row-completeness properties:

(a) the row converges to the UNION of marker + rich payload in BOTH create
    orderings (validate-first AND update-metadata-first);
(b) a re-applied merge is a true no-op (no field changes, no error);
(c) a strictly-older MUTABLE transition is STILL rejected after the merge change
    (the mutable path keeps its event-timestamp guard);
(d) DISJOINTNESS: the marker field-set written by validate and the
    validated-payload field-set written by update-metadata share NO attribute
    name; an intersection would make the if_not_exists merge silently
    order-dependent.

Both handlers are set up inline inside ONE ``mock_aws()`` context so they bind to
the same boto3 DynamoDB table; every other edge (ECR, EventBridge) is stubbed,
so the test is parallel-safe and makes no real AWS call.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from tests.unit.cairn_lambda_update_metadata.conftest import (
    CONTROLS_TABLE_NAME,
    METADATA_TABLE_NAME,
)
from tests.unit.cairn_lambda_validate_bundle.conftest import (
    ARCHIVE_BUCKET,
    EVENT_BUS_NAME,
    ORG_ID,
    PROJECT_ID,
    REGION_CODE,
    STAGE,
    TENANT_ID,
    EventRecorder,
    FakeEcr,
    attestation_push_event,
    build_attestation,
    build_bundle,
)

VALIDATED_AT = "2026-05-26T12:05:00Z"


def _attestation_claim() -> dict[str, str]:
    """The producer-claim projection carrying the opaque identity triple.

    Threaded onto every post-validated event so update-metadata can resolve the
    tenant-PK row coordinates (``tenantId`` partition key, ``projectId``
    sort-key sub-hierarchy) from the claim — never from configBlob (design
    D.5 / [META-001])."""
    return {
        "orgId": ORG_ID,
        "tenantId": TENANT_ID,
        "projectId": PROJECT_ID,
        "artifactKind": "assessment-results",
    }


class _LambdaContext:
    aws_request_id = "seam-req-1"
    function_name = "seam"
    memory_limit_in_mb = 256
    invoked_function_arn = "arn:aws:lambda:us-east-2:123456789012:function:seam"


@pytest.fixture
def shared_pipeline(monkeypatch: pytest.MonkeyPatch, aws_region: str) -> Iterator[dict[str, Any]]:
    """Both real handlers bound to ONE shared moto-backed Metadata_Index table.

    Mirrors the per-package ``env`` + ``tables`` + ``reload_handler`` fixtures,
    but sets ``METADATA_TABLE_NAME`` for BOTH stages so validate's real
    ``MetadataIndex`` and update-metadata's real ``MetadataIndex`` resolve to the
    same boto3 Table under the single ``mock_aws()`` context.
    """
    # Env common to both stages.
    monkeypatch.setenv("STAGE", STAGE)
    monkeypatch.setenv("REGION_CODE", REGION_CODE)
    monkeypatch.setenv("CAIRN_STAGE", STAGE)
    monkeypatch.setenv("CAIRN_REGION_CODE", REGION_CODE)
    monkeypatch.setenv("DEPLOYMENT_MODE", "Customer_Hosted")
    monkeypatch.setenv("METADATA_TABLE_NAME", METADATA_TABLE_NAME)
    monkeypatch.setenv("CONTROLS_TABLE_NAME", CONTROLS_TABLE_NAME)
    # Validate-only env.
    monkeypatch.setenv("ARCHIVE_BUCKET", ARCHIVE_BUCKET)
    monkeypatch.setenv("EVENT_BUS_NAME", EVENT_BUS_NAME)
    monkeypatch.setenv("STALENESS_WINDOW_DAYS", "30")

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=aws_region)
        for name in (METADATA_TABLE_NAME, CONTROLS_TABLE_NAME):
            ddb.create_table(
                TableName=name,
                KeySchema=[
                    {"AttributeName": "pk", "KeyType": "HASH"},
                    {"AttributeName": "sk", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "pk", "AttributeType": "S"},
                    {"AttributeName": "sk", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )

        import cairn_lambda_update_metadata.handler as um_module
        import cairn_lambda_validate_bundle.ecr_client as ecr_module
        import cairn_lambda_validate_bundle.events as events_module
        import cairn_lambda_validate_bundle.handler as ve_module

        ecr_module._ECR_CLIENT = None
        ecr_module._HTTP = None
        events_module._EVENTS_CLIENT = None
        um_module._INDEX = None
        um_module._CONTROLS = None

        validate = importlib.reload(ve_module)
        update_metadata = importlib.reload(um_module)
        update_metadata._INDEX = None
        update_metadata._CONTROLS = None

        fake_ecr = FakeEcr()
        recorder = EventRecorder()
        monkeypatch.setattr(validate, "fetch_manifest", fake_ecr.fetch_manifest)
        monkeypatch.setattr(validate, "fetch_blob", fake_ecr.fetch_blob)
        monkeypatch.setattr(validate, "image_resides_in_repo", fake_ecr.image_resides_in_repo)
        monkeypatch.setattr(events_module, "_events", lambda: recorder)

        table = ddb.Table(METADATA_TABLE_NAME)
        yield {
            "validate": validate,
            "update_metadata": update_metadata,
            "fake_ecr": fake_ecr,
            "recorder": recorder,
            "table": table,
        }

        ecr_module._ECR_CLIENT = None
        ecr_module._HTTP = None
        events_module._EVENTS_CLIENT = None
        um_module._INDEX = None
        um_module._CONTROLS = None


def _run_validate(pipe: dict[str, Any], bundle: Any) -> dict[str, Any]:
    """Drive the real validate handler via the Producer_Attestation_Referrer trigger;
    return its emitted artifact.validated detail."""
    pipe["fake_ecr"].add_bundle(bundle)
    attestation = build_attestation(subject_digest=bundle.manifest_digest)
    pipe["fake_ecr"].add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)
    pipe["validate"].lambda_handler(event, _LambdaContext())
    emitted = pipe["recorder"].details("artifact.validated")
    assert emitted, "validate did not emit artifact.validated"
    return emitted[-1]


def _run_update_metadata(pipe: dict[str, Any], detail: dict[str, Any]) -> None:
    """Drive the real update-metadata handler against the shared table."""
    event = {
        "version": "0",
        "id": "seam-evt-1",
        "detail-type": "artifact.validated",
        "source": "dev.cairnproofs.artifact",
        "account": "123456789012",
        "time": VALIDATED_AT,
        "region": "us-east-2",
        "detail": detail,
    }
    pipe["update_metadata"].lambda_handler(event, _LambdaContext())


def _row(pipe: dict[str, Any], digest: str) -> dict[str, Any]:
    item = pipe["table"].get_item(Key={"pk": TENANT_ID, "sk": f"{PROJECT_ID}#{digest}"})["Item"]
    return item


# Attribute names each stage owns (used by the disjointness assertion). These are
# the WRITE-ONCE contributions; the shared key attributes pk/sk are excluded.
_VALIDATE_MARKER_KEYS = {"validateStatus", "artifactKind"}


def _assert_union_complete(row: dict[str, Any]) -> None:
    """The converged row holds BOTH stages' contributions."""
    # validate's marker fields.
    assert row["validateStatus"] == "validated"
    # The row's single canonical kind attribute is `artifactKind`; the legacy
    # `bundleType` name is never written to the row by either stage ([PCLAIM-001.04]).
    assert row["artifactKind"] == "assessment-results"
    assert "bundleType" not in row
    # The canonical-tag name segment was removed (drop-canonical-tag-name-segment):
    # validate records no `name` marker, so the converged row carries none.
    assert "name" not in row
    # update-metadata's system-bounded payload. The record carries none of the
    # producer-claim attributes ([META-002.06]) — control linkage lives in the
    # producer-claims control-claims projection ([PCLAIM-001]).
    assert "controls" not in row
    assert "controlLinkage" not in row
    assert row["validatedAt"] == VALIDATED_AT
    assert row["signingStatus"] == "pending"
    assert row["archiveStatus"] == "pending"
    # The widened per-layer projection rides through the merge intact: the unified
    # `layers` list lands (with OSCAL enrichment inlined) and no legacy oscalLayers.
    assert len(row["layers"]) == 1
    assert "oscal" in row["layers"][0]
    assert "oscalLayers" not in row


def test_union_completeness_validate_first(shared_pipeline: dict[str, Any]) -> None:
    """(a) validate-first ordering: the row converges to the union."""
    pipe = shared_pipeline
    bundle = build_bundle()

    detail = _run_validate(pipe, bundle)  # validate wins the create (marker only)
    _run_update_metadata(pipe, detail)  # rich payload merges on

    _assert_union_complete(_row(pipe, bundle.manifest_digest))


def test_union_completeness_update_metadata_first(shared_pipeline: dict[str, Any]) -> None:
    """(a) update-metadata-first ordering: the row still converges to the union.

    We run validate first only to capture its real emitted detail, then reset the
    table and apply update-metadata BEFORE re-applying validate's marker write —
    so update-metadata wins the create and validate's marker merges on.
    """
    pipe = shared_pipeline
    bundle = build_bundle()
    detail = _run_validate(pipe, bundle)  # capture the real emitted detail

    # Reset the row so update-metadata wins the create this time.
    pipe["table"].delete_item(Key={"pk": TENANT_ID, "sk": f"{PROJECT_ID}#{bundle.manifest_digest}"})

    _run_update_metadata(pipe, detail)  # update-metadata wins the create (rich payload)
    # Re-run validate; its create_or_merge now collides and merges the marker on.
    _run_validate(pipe, bundle)

    _assert_union_complete(_row(pipe, bundle.manifest_digest))


def test_reapplied_merge_is_a_true_noop(shared_pipeline: dict[str, Any]) -> None:
    """(b) re-applying both stages changes no field and raises no error."""
    pipe = shared_pipeline
    bundle = build_bundle()

    detail = _run_validate(pipe, bundle)
    _run_update_metadata(pipe, detail)
    before = _row(pipe, bundle.manifest_digest)

    # Re-apply both stages (validate short-circuits on its own marker; update
    # metadata's create_or_merge is a pure if_not_exists no-op). Neither raises.
    _run_validate(pipe, bundle)
    _run_update_metadata(pipe, detail)
    after = _row(pipe, bundle.manifest_digest)

    assert after == before


def test_strictly_older_mutable_transition_still_rejected(shared_pipeline: dict[str, Any]) -> None:
    """(c) T3 regression guard: the mutable path keeps its event-timestamp guard.

    After the row is complete (validatedAt / updatedAt = 2026-05-26T12:05:00Z), a
    signed event carrying a STRICTLY-OLDER event timestamp must NOT advance the
    mutable signingStatus — proving the merge change did not route mutable
    transitions through the (guardless) if_not_exists path.
    """
    pipe = shared_pipeline
    bundle = build_bundle()
    detail = _run_validate(pipe, bundle)
    _run_update_metadata(pipe, detail)

    # A stale signed event (event time strictly older than the row's updatedAt).
    stale_signed = {
        "version": "0",
        "id": "seam-evt-signed-stale",
        "detail-type": "artifact.signed",
        "source": "dev.cairnproofs.artifact",
        "account": "123456789012",
        "time": "2026-05-26T00:00:00Z",
        "region": "us-east-2",
        "detail": {
            "manifestDigest": bundle.manifest_digest,
            "signatureDigest": "sha256:" + "5" * 64,
            "signedAt": "2026-05-26T00:00:00Z",
            "signingProfileArn": "arn:aws:signer:us-east-2:123456789012:/signing-profiles/p",
            "timestampProvider": "system-clock",
            "attestation": _attestation_claim(),
        },
    }
    pipe["update_metadata"].lambda_handler(stale_signed, _LambdaContext())

    row = _row(pipe, bundle.manifest_digest)
    # The stale transition was rejected; signingStatus did NOT advance off pending.
    assert row["signingStatus"] == "pending"


def test_stale_signed_into_marker_only_window_is_not_regressed_by_later_merge(
    shared_pipeline: dict[str, Any],
) -> None:
    """Ordering edge: validate-first → marker-only row (no createdAt/updatedAt).

    A validate-first create leaves a marker-only row with NO ``updatedAt`` guard
    attribute (validate single-owns no timestamp; createdAt/updatedAt are owned by
    update-metadata). A ``signed`` event arriving in THAT window is admitted by
    the upsert guard's ``attribute_not_exists(#ts)`` branch — it lands
    ``signingStatus=signed`` and seeds ``updatedAt`` from the (older) signed event
    time. When update-metadata's ``artifact.validated`` merge finally arrives, its
    write-once ``signingStatus="pending"`` / ``updatedAt`` contributions hit
    already-present attributes, so ``if_not_exists`` leaves them untouched: the
    signed transition is NOT regressed back to pending.

    This pins the interaction between the (guardless-on-absent-#ts) upsert path
    and the create-or-merge path on the same row — the merge change must not let
    a later validated merge clobber a transition that the upsert window admitted.
    """
    pipe = shared_pipeline
    bundle = build_bundle()

    # 1. validate-first: marker-only row, no timestamp guard attribute yet.
    detail = _run_validate(pipe, bundle)
    marker_row = _row(pipe, bundle.manifest_digest)
    assert marker_row["validateStatus"] == "validated"
    assert "updatedAt" not in marker_row  # the window the upsert admits into
    assert "signingStatus" not in marker_row

    # 2. A signed event lands in the marker-only window. Its event time is OLDER
    #    than update-metadata's validated envelope time (VALIDATED_AT), but with
    #    no #ts on the row the upsert's attribute_not_exists(#ts) branch admits it.
    signed = {
        "version": "0",
        "id": "seam-evt-signed-window",
        "detail-type": "artifact.signed",
        "source": "dev.cairnproofs.artifact",
        "account": "123456789012",
        "time": "2026-05-26T00:00:00Z",  # older than VALIDATED_AT
        "region": "us-east-2",
        "detail": {
            "manifestDigest": bundle.manifest_digest,
            "signatureDigest": "sha256:" + "5" * 64,
            "signedAt": "2026-05-26T00:00:00Z",
            "signingProfileArn": "arn:aws:signer:us-east-2:123456789012:/signing-profiles/p",
            "timestampProvider": "system-clock",
            "attestation": _attestation_claim(),
        },
    }
    pipe["update_metadata"].lambda_handler(signed, _LambdaContext())
    after_signed = _row(pipe, bundle.manifest_digest)
    assert after_signed["signingStatus"] == "signed"  # admitted into the window
    assert after_signed["updatedAt"] == "2026-05-26T00:00:00Z"  # seeded by upsert

    # 3. update-metadata's validated merge finally arrives. Its write-once
    #    signingStatus="pending" / updatedAt contributions are if_not_exists, so
    #    they must NOT regress the already-present signed transition.
    _run_update_metadata(pipe, detail)

    final = _row(pipe, bundle.manifest_digest)
    assert final["signingStatus"] == "signed"  # NOT regressed to pending
    assert final["updatedAt"] == "2026-05-26T00:00:00Z"  # NOT overwritten by merge
    # The system-bounded payload still merged its complementary (absent) fields on.
    assert len(final["layers"]) == 1
    assert final["artifactKind"] == "assessment-results"
    assert final["validateStatus"] == "validated"  # marker preserved
    # createdAt: validate did not own it; update-metadata's merge fills it now.
    assert final["createdAt"] == VALIDATED_AT


def _capture_contributed_keys(monkeypatch: pytest.MonkeyPatch, run: Any) -> set[str]:
    """Run a single stage and return the key-set it passes to create_or_merge.

    Spies on ``MetadataIndex.create_or_merge`` to record each call's ``record``
    keys — i.e. the attribute names the stage ACTUALLY contributes to the shared
    write-once row, BEFORE the facade adds pk/sk. This is the stage's *declared*
    contribution, captured at the seam rather than reverse-engineered from a
    post-merge row partition (which can only ever be disjoint by construction).
    """
    from cairn_metadata import MetadataIndex

    captured: set[str] = set()
    # Bind the GENUINE method (not whatever a prior capture's spy left installed)
    # so a second invocation does not chain through an earlier spy and pollute its
    # captured set.
    real = MetadataIndex.__dict__["create_or_merge"]

    def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs["record"].keys())
        return real(self, *args, **kwargs)

    monkeypatch.setattr(MetadataIndex, "create_or_merge", spy)
    run()
    monkeypatch.setattr(MetadataIndex, "create_or_merge", real)
    return captured


def test_marker_and_payload_field_sets_are_disjoint(
    shared_pipeline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """(d) T1 disjointness: the two stages share NO write-once attribute name.

    We capture each stage's DECLARED contributed key-set directly from the
    ``record`` dict it passes to ``MetadataIndex.create_or_merge`` (not from a
    post-merge row partition, which is disjoint by construction and so cannot
    detect a genuinely shared attribute), then assert their intersection is empty
    except for the shared key attributes pk/sk. A shared write-once attribute
    would make the if_not_exists merge silently order-dependent.

    This assertion FAILS against the pre-H1-fix code, where validate's
    ``_record_coordinates`` ALSO contributed ``createdAt`` (which update-metadata
    contributes too) — the intersection would be ``{"createdAt"}`` and the assert
    would trip. After H1 (createdAt single-owned by update-metadata), validate
    contributes only its marker and the sets are disjoint.
    """
    pipe = shared_pipeline
    bundle = build_bundle()
    detail = _run_validate(pipe, bundle)  # capture the real emitted detail first

    # Reset the row so each stage is captured running against a clean create.
    pipe["table"].delete_item(Key={"pk": TENANT_ID, "sk": f"{PROJECT_ID}#{bundle.manifest_digest}"})

    validate_keys = _capture_contributed_keys(monkeypatch, lambda: _run_validate(pipe, bundle))
    pipe["table"].delete_item(Key={"pk": TENANT_ID, "sk": f"{PROJECT_ID}#{bundle.manifest_digest}"})
    update_metadata_keys = _capture_contributed_keys(
        monkeypatch, lambda: _run_update_metadata(pipe, detail)
    )

    # pk/sk are the legitimately-shared row coordinates; every OTHER attribute
    # must be single-owned across the two write-once contributors.
    overlap = (validate_keys & update_metadata_keys) - {"pk", "sk"}
    assert not overlap, (
        "validate marker fields and update-metadata payload fields MUST be "
        f"disjoint — shared write-once attribute(s): {sorted(overlap)}. A "
        "shared write-once attribute makes the if_not_exists merge "
        "order-dependent (the create-race winner's value persists)."
    )
    # Sanity: both stages actually contributed (pin to what the handlers really
    # passed, not a hardcoded fixture-coupled set — validate does not contribute
    # a `name` marker, so assert the markers it does contribute are present).
    assert "validateStatus" in validate_keys
    assert validate_keys <= _VALIDATE_MARKER_KEYS
    assert {"layers", "validatedAt", "createdAt"} <= update_metadata_keys
    # update-metadata contributes none of the producer-claim attributes to the
    # record ([META-002.06]) and no kind attribute: the legacy `bundleType` write
    # was dropped and `artifactKind` is single-owned by validate's marker, so
    # update-metadata's contribution carries neither ([PCLAIM-001.04], row-ownership
    # disjointness T1).
    assert "controls" not in update_metadata_keys
    assert "controlLinkage" not in update_metadata_keys
    assert "bundleType" not in update_metadata_keys
    assert "artifactKind" not in update_metadata_keys

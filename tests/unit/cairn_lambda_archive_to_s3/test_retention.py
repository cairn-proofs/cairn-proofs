"""Runtime retention period and Retain_Until in the archive stage.

Covers retention-lifecycle [RETAIN-003.01], [RETAIN-003.02], [RETAIN-003.03],
[RETAIN-007.03] and worm-archive [WORM-005.01], [WORM-005.02].
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest

from tests.unit.cairn_lambda_archive_to_s3.conftest import (
    ARCHIVE_BUCKET,
    RETENTION_DAYS,
    RETENTION_PARAM,
    build_bundle,
    signed_event,
)

RETAIN_UNTIL_KEY = "dev.cairnproofs.artifact.retain-until"


def _seed(handler: Any) -> Any:
    bundle = build_bundle()
    handler._test_fake_ecr.add_bundle(bundle)
    return bundle


def _freeze(handler: Any, monkeypatch: pytest.MonkeyPatch, when: datetime) -> list[int]:
    """Pin the handler's clock; return a counter of how often it was read."""
    calls: list[int] = []

    def _now() -> datetime:
        calls.append(1)
        return when

    monkeypatch.setattr(handler, "_now", _now)
    return calls


def _capture_puts(handler: Any, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    fake_s3 = MagicMock()
    fake_s3.put_object.side_effect = lambda **kw: captured.append(kw) or {}
    fake_s3.head_object.side_effect = handler._test_s3.head_object
    monkeypatch.setattr(handler, "_s3", lambda: fake_s3)
    return captured


def _set_param(value: str) -> None:
    boto3.client("ssm", region_name="us-east-2").put_parameter(
        Name=RETENTION_PARAM, Value=value, Type="String", Overwrite=True
    )


def _s3_receipt(handler: Any, bundle: Any) -> dict[str, Any]:
    prefix = f"{bundle.org_id}/{bundle.tenant_id}/{bundle.project_id}/{bundle.manifest_digest}/"
    body = handler._test_s3.get_object(Bucket=ARCHIVE_BUCKET, Key=f"{prefix}worm-receipt.json")
    return json.loads(body["Body"].read())


# ── [RETAIN-003] single retention clock ──────────────────────────────────────


@pytest.mark.parametrize("worm", [False, True])
def test_retain_until_is_archive_time_plus_period_in_both_modes(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch, worm: bool
) -> None:
    """[RETAIN-003.01] Retain_Until = T + D days, whatever WORM_Enabled is."""
    handler = reload_handler
    bundle = _seed(handler)
    monkeypatch.setattr(handler, "WORM_ENABLED", worm)
    t = datetime(2026, 5, 26, 12, 30, 15, 987654, tzinfo=UTC)
    clock = _freeze(handler, monkeypatch, t)
    puts = _capture_puts(handler, monkeypatch) if worm else None

    payload = handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    expected = (t.replace(microsecond=0) + timedelta(days=RETENTION_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert payload["retainUntil"] == expected
    assert payload["archivedAt"] == t.replace(microsecond=0).isoformat()
    # One clock reading for the whole bundle.
    assert len(clock) == 1
    receipt_manifest = handler._test_fake_ecr.referrer_artifacts[0]
    assert receipt_manifest["annotations"][RETAIN_UNTIL_KEY] == expected
    if worm:
        assert puts
        receipt_put = next(p for p in puts if p["Key"].endswith("worm-receipt.json"))
        assert json.loads(receipt_put["Body"])["retainUntil"] == expected
    else:
        assert _s3_receipt(handler, bundle)["retainUntil"] == expected


def test_object_lock_date_equals_receipt_retain_until_on_every_object(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[RETAIN-003.02] Under WORM every object version carries Retain_Until."""
    handler = reload_handler
    bundle = _seed(handler)
    monkeypatch.setattr(handler, "WORM_ENABLED", True)
    _freeze(handler, monkeypatch, datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    puts = _capture_puts(handler, monkeypatch)

    handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    receipt = json.loads(next(p for p in puts if p["Key"].endswith("worm-receipt.json"))["Body"])
    retain = datetime.strptime(receipt["retainUntil"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert len(puts) >= 5  # manifest, config, layer, attestation (+blobs), receipt
    for call in puts:
        assert call["ObjectLockMode"] == "COMPLIANCE"
        assert call["ObjectLockRetainUntilDate"] == retain


def test_no_object_lock_headers_when_worm_disabled(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = reload_handler
    bundle = _seed(handler)
    puts = _capture_puts(handler, monkeypatch)
    handler.lambda_handler(signed_event(bundle=bundle), lambda_context)
    assert puts
    for call in puts:
        assert "ObjectLockMode" not in call
        assert "ObjectLockRetainUntilDate" not in call


def test_archive_on_29_february_with_worm_succeeds(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[RETAIN-003.03] Leap-day archive: day arithmetic, no ValueError."""
    handler = reload_handler
    bundle = _seed(handler)
    monkeypatch.setattr(handler, "WORM_ENABLED", True)
    t = datetime(2028, 2, 29, 23, 59, 59, tzinfo=UTC)
    _freeze(handler, monkeypatch, t)
    puts = _capture_puts(handler, monkeypatch)

    payload = handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    assert payload["retainUntil"] == (t + timedelta(days=RETENTION_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert {c["ObjectLockRetainUntilDate"] for c in puts} == {t + timedelta(days=RETENTION_DAYS)}


# ── [WORM-005] receipt carries Retain_Until ──────────────────────────────────


def test_receipt_body_and_annotation_agree_in_ecr_and_s3(reload_handler, lambda_context) -> None:
    """[WORM-005.01] / [WORM-005.02] retainUntil in the body (ECR + S3 copy) and annotation."""
    handler = reload_handler
    bundle = _seed(handler)

    handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    receipt_manifest = handler._test_fake_ecr.referrer_artifacts[0]
    annotation = receipt_manifest["annotations"][RETAIN_UNTIL_KEY]
    # The ECR referrer's layer is the same body as the S3 copy.
    ecr_body = json.loads(handler._test_fake_ecr.put_blobs[0])
    s3_body = _s3_receipt(handler, bundle)
    assert ecr_body == s3_body
    assert s3_body["retainUntil"] == annotation
    assert {"s3Bucket", "archivedAt", "blobCount", "integrityVerified"} <= set(s3_body)
    # RFC 3339 UTC.
    datetime.strptime(annotation, "%Y-%m-%dT%H:%M:%SZ")


# ── [RETAIN-007.03] invalid runtime period fails closed ──────────────────────


def _assert_failed_closed(handler: Any, bundle: Any, lambda_context: Any, reason: str) -> None:
    with pytest.raises(handler.RetentionSettingError, match=reason):
        handler.lambda_handler(signed_event(bundle=bundle), lambda_context)
    failed = handler._test_recorder.details("artifact.archive-failed")
    assert len(failed) == 1
    assert failed[0]["artifact"] == "retention-period-setting"
    assert failed[0]["invalidSetting"] == RETENTION_PARAM
    assert reason in failed[0]["reason"]
    assert handler._test_recorder.details("artifact.archived") == []
    listing = handler._test_s3.list_objects_v2(Bucket=ARCHIVE_BUCKET)
    assert listing.get("KeyCount", 0) == 0
    assert handler._test_fake_ecr.referrer_artifacts == []


def test_missing_retention_parameter_fails_archive(reload_handler, lambda_context) -> None:
    handler = reload_handler
    bundle = _seed(handler)
    boto3.client("ssm", region_name="us-east-2").delete_parameter(Name=RETENTION_PARAM)
    _assert_failed_closed(handler, bundle, lambda_context, "is missing")


@pytest.mark.parametrize("value", ["seven", "2555.5", "-1", " ", "1e3"])
def test_non_integer_retention_parameter_fails_archive(
    reload_handler, lambda_context, value: str
) -> None:
    handler = reload_handler
    bundle = _seed(handler)
    _set_param(value)
    _assert_failed_closed(handler, bundle, lambda_context, "not a whole number")


@pytest.mark.parametrize("value", ["0", "36136"])
def test_out_of_bounds_retention_parameter_fails_archive(
    reload_handler, lambda_context, value: str
) -> None:
    handler = reload_handler
    bundle = _seed(handler)
    _set_param(value)
    _assert_failed_closed(handler, bundle, lambda_context, "outside 1 to 36135")


@pytest.mark.parametrize("value", ["1", "36135", " 10 "])
def test_boundary_retention_values_are_accepted(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    handler = reload_handler
    bundle = _seed(handler)
    _set_param(value)
    t = datetime(2026, 5, 26, tzinfo=UTC)
    _freeze(handler, monkeypatch, t)
    payload = handler.lambda_handler(signed_event(bundle=bundle), lambda_context)
    assert payload["retainUntil"] == (t + timedelta(days=int(value))).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_unconfigured_parameter_name_fails_archive(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = reload_handler
    bundle = _seed(handler)
    monkeypatch.setattr(handler, "RETENTION_PERIOD_PARAM", "")
    with pytest.raises(handler.RetentionSettingError, match="not set"):
        handler.lambda_handler(signed_event(bundle=bundle), lambda_context)
    assert handler._test_recorder.details("artifact.archived") == []
    assert handler._test_s3.list_objects_v2(Bucket=ARCHIVE_BUCKET).get("KeyCount", 0) == 0


def test_transient_ssm_error_propagates_without_writing(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Throttling is retried (raised as-is), never defaulted to a period."""
    from botocore.exceptions import ClientError

    handler = reload_handler
    bundle = _seed(handler)
    fake = MagicMock()
    fake.get_parameter.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "GetParameter"
    )
    monkeypatch.setattr(handler, "_ssm", lambda: fake)
    with pytest.raises(ClientError):
        handler.lambda_handler(signed_event(bundle=bundle), lambda_context)
    fake.get_parameter.assert_called_once_with(Name=RETENTION_PARAM)
    assert handler._test_s3.list_objects_v2(Bucket=ARCHIVE_BUCKET).get("KeyCount", 0) == 0


def test_parameter_is_read_by_name_with_no_selector(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M31: no ``:label`` / ``:version`` suffix on the parameter name."""
    handler = reload_handler
    bundle = _seed(handler)
    real = handler._ssm()
    spy = MagicMock(wraps=real)
    monkeypatch.setattr(handler, "_ssm", lambda: spy)
    handler.lambda_handler(signed_event(bundle=bundle), lambda_context)
    spy.get_parameter.assert_called_once_with(Name=RETENTION_PARAM)
    assert ":" not in RETENTION_PARAM


# ── Contract with the `cairn prune` classifier ([RETAIN-012], [RETAIN-009]) ──
# The prune classifier re-reads what this stage writes. These tests feed the
# REAL archive outputs through the classifier's own strict parser and key
# derivation, so a format drift on either side fails here, not in production
# (where it would make every bundle permanently ineligible, or worse).


@pytest.mark.parametrize("worm", [False, True])
def test_archive_outputs_satisfy_the_prune_classifier_contract(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch, worm: bool
) -> None:
    from cairn_cli._prune_eligibility import Deployment, parse_rfc3339_utc

    handler = reload_handler
    bundle = _seed(handler)
    monkeypatch.setattr(handler, "WORM_ENABLED", worm)
    t = datetime(2026, 5, 26, 12, 30, 15, 987654, tzinfo=UTC)
    _freeze(handler, monkeypatch, t)
    puts = _capture_puts(handler, monkeypatch) if worm else None

    payload = handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    if worm:
        assert puts is not None
        receipt_put = next(p for p in puts if p["Key"].endswith("worm-receipt.json"))
        receipt = json.loads(receipt_put["Body"])
    else:
        receipt = _s3_receipt(handler, bundle)
    annotation = handler._test_fake_ecr.referrer_artifacts[0]["annotations"][RETAIN_UNTIL_KEY]
    expected = t.replace(microsecond=0) + timedelta(days=RETENTION_DAYS)

    # The classifier's strict RFC 3339 UTC parser accepts every timestamp the
    # stage writes, and the receipt body and ECR annotation agree exactly.
    assert parse_rfc3339_utc(receipt["retainUntil"]) == expected
    assert parse_rfc3339_utc(annotation) == expected
    assert parse_rfc3339_utc(receipt["archivedAt"]) == t.replace(microsecond=0)
    # update-metadata persists payload["archivedAt"]; the index-remnant path
    # parses it.
    assert parse_rfc3339_utc(payload["archivedAt"]) == t.replace(microsecond=0)

    # The receipt's self-identification matches the classifier's derived keys.
    dep = Deployment(
        org_id=bundle.org_id,
        tenant_id=bundle.tenant_id,
        project_id=bundle.project_id,
        repository_name=bundle.repo_name,
        bucket=ARCHIVE_BUCKET,
        metadata_table="m",
        controls_table="c",
        worm_enabled=worm,
        retention_period_days=RETENTION_DAYS,
        window_days=30,
    )
    assert receipt["manifestDigest"] == bundle.manifest_digest
    assert receipt["s3KeyPrefix"] == dep.prefix(bundle.manifest_digest)
    assert payload["s3KeyPrefix"] == dep.prefix(bundle.manifest_digest)
    assert receipt["s3Bucket"] == ARCHIVE_BUCKET
    if worm:
        assert puts is not None
        assert {p["ObjectLockRetainUntilDate"] for p in puts} == {expected}


def test_idempotent_retry_archived_at_still_parses_for_the_index_remnant_path(
    reload_handler, lambda_context
) -> None:
    """The short-circuit re-emit carries an archivedAt the classifier can parse.

    update-metadata writes it to the Metadata_Index record, which is the only
    Retain_Until evidence left once a prune deleted the ECR and S3 parts.
    """
    from cairn_cli._prune_eligibility import parse_rfc3339_utc

    handler = reload_handler
    bundle = _seed(handler)
    first = handler.lambda_handler(signed_event(bundle=bundle), lambda_context)
    handler._test_existing_receipt_state["value"] = "sha256:" + "9" * 64

    second = handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    assert second["idempotentShortCircuit"] is True
    first_at = parse_rfc3339_utc(first["archivedAt"])
    second_at = parse_rfc3339_utc(second["archivedAt"])
    assert first_at is not None and second_at is not None
    # A retry can only push the remnant's derived Retain_Until later, never
    # earlier, than the original archive's.
    assert second_at >= first_at
    # Exactly one receipt referrer and one set of objects: the retry wrote nothing.
    assert len(handler._test_fake_ecr.referrer_artifacts) == 1


def test_idempotent_retry_does_not_require_the_retention_setting(
    reload_handler, lambda_context
) -> None:
    """A completed archive re-delivered after the parameter vanished still re-emits.

    The short-circuit writes nothing, so [RETAIN-007.03] does not apply to it.
    """
    handler = reload_handler
    bundle = _seed(handler)
    handler.lambda_handler(signed_event(bundle=bundle), lambda_context)
    handler._test_existing_receipt_state["value"] = "sha256:" + "9" * 64
    boto3.client("ssm", region_name="us-east-2").delete_parameter(Name=RETENTION_PARAM)

    payload = handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    assert payload["idempotentShortCircuit"] is True
    assert handler._test_recorder.details("artifact.archive-failed") == []

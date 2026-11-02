"""Fixtures for worm-archive property tests.

The archive-to-s3 object-set property ([WORM-002.05]) needs the same moto-backed
S3 + faked-ECR handler harness the unit tests use. Rather than duplicate it, we
reuse the builders and fakes from the unit conftest and expose them under
property-test-friendly fixture names.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from tests.unit.cairn_lambda_archive_to_s3.conftest import (
    ARCHIVE_BUCKET,
    EVENT_BUS_NAME,
    REGION_CODE,
    RETENTION_DAYS,
    RETENTION_PARAM,
    STAGE,
    EventRecorder,
    FakeEcr,
    LambdaContext,
)
from tests.unit.cairn_lambda_archive_to_s3.conftest import build_bundle as _build_bundle
from tests.unit.cairn_lambda_archive_to_s3.conftest import signed_event as _signed_event


@pytest.fixture
def build_bundle() -> Callable[..., Any]:
    return _build_bundle


@pytest.fixture
def signed_event() -> Callable[..., Any]:
    return _signed_event


@pytest.fixture
def lambda_context() -> LambdaContext:
    return LambdaContext()


@pytest.fixture
def reload_archive_handler(monkeypatch: pytest.MonkeyPatch, aws_region: str) -> Iterator[Any]:
    """Moto-backed S3 + faked-ECR archive-to-s3 handler (mirrors the unit harness)."""
    monkeypatch.setenv("STAGE", STAGE)
    monkeypatch.setenv("REGION_CODE", REGION_CODE)
    monkeypatch.setenv("CAIRN_STAGE", STAGE)
    monkeypatch.setenv("CAIRN_REGION_CODE", REGION_CODE)
    monkeypatch.setenv("ARCHIVE_BUCKET", ARCHIVE_BUCKET)
    monkeypatch.setenv("EVENT_BUS_NAME", EVENT_BUS_NAME)
    monkeypatch.setenv("WORM_ENABLED", "false")
    monkeypatch.setenv("RETENTION_PERIOD_PARAM", RETENTION_PARAM)

    with mock_aws():
        s3 = boto3.client("s3", region_name=aws_region)
        s3.create_bucket(
            Bucket=ARCHIVE_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": aws_region},
        )
        boto3.client("ssm", region_name=aws_region).put_parameter(
            Name=RETENTION_PARAM, Value=str(RETENTION_DAYS), Type="String"
        )

        import cairn_lambda_archive_to_s3.ecr_client as ecr_module
        import cairn_lambda_archive_to_s3.events as events_module
        import cairn_lambda_archive_to_s3.handler as handler_module
        import cairn_lambda_archive_to_s3.referrers as referrers_module

        ecr_module._ECR_CLIENT = None
        ecr_module._HTTP = None
        events_module._EVENTS_CLIENT = None
        referrers_module._ECR_CLIENT = None
        handler_module._S3 = None
        handler_module._SSM = None
        handler = importlib.reload(handler_module)
        handler._S3 = None
        handler._SSM = None

        fake_ecr = FakeEcr()
        recorder = EventRecorder()
        existing_receipt_state: dict[str, str | None] = {"value": None}

        monkeypatch.setattr(handler, "fetch_manifest", fake_ecr.fetch_manifest)
        monkeypatch.setattr(handler, "fetch_blob", fake_ecr.fetch_blob)
        monkeypatch.setattr(handler, "put_blob", fake_ecr.put_blob)
        monkeypatch.setattr(handler, "put_referrer_artifact", fake_ecr.put_referrer_artifact)
        monkeypatch.setattr(
            handler,
            "existing_receipt_referrer",
            lambda **kw: existing_receipt_state["value"],
        )
        monkeypatch.setattr(events_module, "_events", lambda: recorder)

        handler._test_fake_ecr = fake_ecr
        handler._test_recorder = recorder
        handler._test_existing_receipt_state = existing_receipt_state
        handler._test_s3 = s3

        yield handler

        ecr_module._ECR_CLIENT = None
        ecr_module._HTTP = None
        events_module._EVENTS_CLIENT = None
        referrers_module._ECR_CLIENT = None
        handler_module._S3 = None

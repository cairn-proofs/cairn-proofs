"""Test configuration shared across all unit tests.

Resets module-level boto3 client caches between tests so moto mocks bind
to a fresh client per test (avoids stale, real-AWS-region clients leaking
between mocked tests).
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force well-defined credentials/region so boto3 never accidentally
    picks up the developer's real profile while a test is running."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    # Drop any region/stage env that could leak from the host shell.
    for var in ("CAIRN_REGION_CODE", "CAIRN_STAGE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _reset_module_clients() -> None:
    """Reset cached boto3 clients in the libs that hold module-level singletons."""
    # observability.metrics
    import cairn_observability.metrics as _metrics

    _metrics._CLIENT = None
    # signing.timestamp_provider
    import cairn_signing.timestamp_provider as _ts

    _ts._SSM_CLIENT = None
    yield
    _metrics._CLIENT = None
    _ts._SSM_CLIENT = None


@pytest.fixture
def aws_region() -> str:
    return os.environ.get("AWS_DEFAULT_REGION", "us-east-2")

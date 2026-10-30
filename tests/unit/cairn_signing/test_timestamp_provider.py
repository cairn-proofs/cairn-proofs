"""Tests for cairn_signing.timestamp_provider (SSM resolution + rfc3161 gate)."""

from __future__ import annotations

import boto3
import pytest
from cairn_signing.timestamp_provider import (
    TimestampProvider,
    UnsupportedTimestampProvider,
    read_timestamp_provider_from_ssm,
)
from moto import mock_aws


def test_timestamp_provider_enum_values() -> None:
    assert TimestampProvider.SYSTEM_CLOCK.value == "system-clock"
    assert TimestampProvider.RFC3161.value == "rfc3161"


APP = "cairnproofs4af"


@pytest.fixture(autouse=True)
def _app_name(monkeypatch: pytest.MonkeyPatch):
    # The parameter name follows the uniform /{appName}/{stage}/... scheme; the
    # appName is supplied by env (the Lambda gets the full name via
    # TIMESTAMP_PROVIDER_PARAM, tests use the CAIRN_APP_NAME fallback).
    monkeypatch.setenv("CAIRN_APP_NAME", APP)


@pytest.fixture
def ssm(aws_region: str):
    with mock_aws():
        client = boto3.client("ssm", region_name=aws_region)
        yield client


def _put(ssm_client, *, stage: str, value: str, app: str = APP) -> None:
    ssm_client.put_parameter(
        Name=f"/{app}/{stage}/signing/timestamp-provider",
        Value=value,
        Type="String",
        Overwrite=True,
    )


def test_read_returns_system_clock_when_ssm_says_system_clock(
    ssm, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_STAGE", "alpha")
    _put(ssm, stage="alpha", value="system-clock")
    assert read_timestamp_provider_from_ssm() is TimestampProvider.SYSTEM_CLOCK


def test_read_explicit_stage_arg_overrides_env(ssm, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRN_STAGE", "wrong")
    _put(ssm, stage="alpha", value="system-clock")
    assert read_timestamp_provider_from_ssm(stage="alpha") is TimestampProvider.SYSTEM_CLOCK


def test_read_strips_whitespace_from_ssm_value(ssm, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRN_STAGE", "alpha")
    _put(ssm, stage="alpha", value="  system-clock\n")
    assert read_timestamp_provider_from_ssm() is TimestampProvider.SYSTEM_CLOCK


def test_read_rejects_rfc3161_as_unsupported(ssm, monkeypatch: pytest.MonkeyPatch) -> None:
    """rfc3161 is a recognized value but must raise UnsupportedTimestampProvider —
    only system-clock is built."""
    monkeypatch.setenv("CAIRN_STAGE", "alpha")
    _put(ssm, stage="alpha", value="rfc3161")
    with pytest.raises(UnsupportedTimestampProvider) as excinfo:
        read_timestamp_provider_from_ssm()
    message = str(excinfo.value)
    # Message names the unsupported value and conveys that it is not supported.
    assert "rfc3161" in message
    assert "not supported" in message


def test_read_rejects_unknown_provider_string(ssm, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRN_STAGE", "alpha")
    _put(ssm, stage="alpha", value="not-a-strategy")
    with pytest.raises(UnsupportedTimestampProvider):
        read_timestamp_provider_from_ssm()


def test_read_raises_when_stage_env_missing(ssm) -> None:
    # conftest already deleted CAIRN_STAGE; no explicit arg either.
    with pytest.raises(RuntimeError, match="CAIRN_STAGE"):
        read_timestamp_provider_from_ssm()


def test_read_raises_when_ssm_parameter_missing(ssm, monkeypatch: pytest.MonkeyPatch) -> None:
    from botocore.exceptions import ClientError

    monkeypatch.setenv("CAIRN_STAGE", "alpha")
    # Parameter not put — boto/moto raises ParameterNotFound as a ClientError.
    with pytest.raises(ClientError):
        read_timestamp_provider_from_ssm()


def test_read_parameter_name_path_is_per_stage(ssm, monkeypatch: pytest.MonkeyPatch) -> None:
    """beta + alpha parameters are isolated — switching stage reads a
    different parameter, not the same one."""
    monkeypatch.setenv("CAIRN_STAGE", "alpha")
    _put(ssm, stage="alpha", value="system-clock")
    _put(ssm, stage="beta", value="rfc3161")
    assert read_timestamp_provider_from_ssm(stage="alpha") is TimestampProvider.SYSTEM_CLOCK
    with pytest.raises(UnsupportedTimestampProvider):
        read_timestamp_provider_from_ssm(stage="beta")


def test_read_uses_explicit_param_name_env_over_constructed_path(
    ssm, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TIMESTAMP_PROVIDER_PARAM (the name the CDK passes) wins over any
    appName/stage-constructed path — the regression fix for the appName rename,
    where the deployed name is /{appName}/{stage}/... and the IAM grant scopes
    to it exactly."""
    explicit = "/cairnproofs4af/alpha/signing/timestamp-provider"
    monkeypatch.setenv("TIMESTAMP_PROVIDER_PARAM", explicit)
    # Deliberately leave CAIRN_STAGE unset and write ONLY at the explicit name:
    # if the code reconstructed a path it would miss this and raise.
    ssm.put_parameter(Name=explicit, Value="system-clock", Type="String", Overwrite=True)
    assert read_timestamp_provider_from_ssm() is TimestampProvider.SYSTEM_CLOCK

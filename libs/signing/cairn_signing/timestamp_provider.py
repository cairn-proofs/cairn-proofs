"""TimestampProvider strategy resolution from SSM Parameter Store ([SIGN-009]).

Only `system-clock` is built; `rfc3161` is a recognized value that is rejected
(raises UnsupportedTimestampProvider) until its TSA-backed enablement lands.
Switching strategies is a single SSM parameter update at
`/{appName}/{stage}/signing/timestamp-provider`; no code change or Lambda
redeployment is required.

The parameter NAME follows the uniform `/{appName}/{stage}/...` scheme, so the
CDK (which owns that scheme) passes the fully-resolved name to the Lambda via
the ``TIMESTAMP_PROVIDER_PARAM`` env var — exactly as it passes
``SIGNER_PROFILE_ARN`` — and this module reads it rather than reconstructing the
path. That keeps the code in agreement with the deployed name
and its IAM grant. A direct caller / test that doesn't set the env can instead
provide ``CAIRN_APP_NAME`` + the stage.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Any

import boto3


class TimestampProvider(StrEnum):
    SYSTEM_CLOCK = "system-clock"
    RFC3161 = "rfc3161"


class UnsupportedTimestampProvider(ValueError):
    """The configured TimestampProvider isn't supported in this build.

    `rfc3161` is a recognized value but is intentionally rejected; only
    `system-clock` is built.
    """


_SUPPORTED_PROVIDERS: frozenset[TimestampProvider] = frozenset({TimestampProvider.SYSTEM_CLOCK})

_SSM_CLIENT: Any = None


def _ssm() -> Any:
    global _SSM_CLIENT
    if _SSM_CLIENT is None:
        _SSM_CLIENT = boto3.client("ssm")
    return _SSM_CLIENT


TIMESTAMP_PROVIDER_PARAM_ENV = "TIMESTAMP_PROVIDER_PARAM"


def _parameter_name(stage: str | None) -> str:
    # Preferred: the CDK passes the fully-resolved parameter name (it owns the
    # {appName}-aware /{appName}/{stage}/... scheme and the matching IAM grant),
    # so the Lambda never reconstructs the path and cannot drift from it.
    explicit = os.environ.get(TIMESTAMP_PROVIDER_PARAM_ENV)
    if explicit:
        return explicit
    # Fallback (direct callers / tests): compose /{appName}/{stage}/...
    st = stage or os.environ.get("CAIRN_STAGE")
    app = os.environ.get("CAIRN_APP_NAME") or os.environ.get("APP_NAME")
    if not st or not app:
        raise RuntimeError(
            f"set {TIMESTAMP_PROVIDER_PARAM_ENV} (preferred), or both CAIRN_APP_NAME "
            "and CAIRN_STAGE, to derive the SSM timestamp-provider parameter name"
        )
    return f"/{app}/{st}/signing/timestamp-provider"


def read_timestamp_provider_from_ssm(stage: str | None = None) -> TimestampProvider:
    """Resolve the active TimestampProvider strategy for this deployment ([SIGN-009]).

    Fetches `/{appName}/{stage}/signing/timestamp-provider` from SSM (the name is
    supplied by the ``TIMESTAMP_PROVIDER_PARAM`` env var, or composed from
    ``CAIRN_APP_NAME`` + stage) and returns the matching enum value. Raises
    UnsupportedTimestampProvider if the configured value is outside the
    supported set.
    """
    name = _parameter_name(stage)
    response = _ssm().get_parameter(Name=name)
    raw = response["Parameter"]["Value"].strip()
    try:
        provider = TimestampProvider(raw)
    except ValueError as e:
        raise UnsupportedTimestampProvider(
            f"SSM parameter {name} = {raw!r} is not a known TimestampProvider value"
        ) from e
    if provider not in _SUPPORTED_PROVIDERS:
        raise UnsupportedTimestampProvider(
            f"TimestampProvider {provider.value!r} is recognized but not supported "
            "in this build; only 'system-clock' is built"
        )
    return provider

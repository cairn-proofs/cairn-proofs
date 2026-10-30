"""Index-independence regression for `cairn verify` (artifact-verifiability;
Threat #11 / *Index-Independent Re-Verification*).

The verification trust path MUST be re-derivable from the ECR artifact +
referrers + trust policy alone — it must NEVER read the Metadata_Index. This
test asserts the verify code path constructs NO DynamoDB client by recording
every boto3 service name requested across a full verify run, and asserts
"dynamodb" is never among them. It also guards that this STILL holds after the
task-3.2 multi-signature aggregation change (which enumerates signature
referrers via the ECR Referrers API, never DynamoDB).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import boto3
import pytest
from cairn_cli import verify as verify_module


def _make_args(reference: str) -> argparse.Namespace:
    return argparse.Namespace(reference=reference, stage="alpha", region_code="cmh", app_name=None)


class _FakeOras:
    class OrasCommandError(RuntimeError):
        def __init__(self, *, returncode: int, stderr: str, command: list[str]) -> None:
            super().__init__(stderr)
            self.returncode = returncode
            self.stderr = stderr
            self.command = command

    def login(self, *, registry: str, username: str, password: str) -> None:
        return None


def _verify_ok(_reference: str) -> None:
    return None


def test_verify_constructs_no_dynamodb_client(
    aws_region: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from moto import mock_aws

    requested_services: list[str] = []
    real_client = boto3.client

    def recording_client(service_name, *args, **kwargs):
        requested_services.append(service_name)
        return real_client(service_name, *args, **kwargs)

    with mock_aws():
        # Seed only the SSM parameter verify needs; deliberately create NO
        # DynamoDB tables so any stray index read would also blow up.
        _ssm = boto3.client("ssm", region_name=aws_region)
        _ssm.put_parameter(
            Name="/cairnproofs/cmh-alpha/app-name",
            Value="cairnproofs4af",
            Type="String",
            Overwrite=True,
        )
        _ssm.put_parameter(
            Name="/cairnproofs4af/alpha/registry/ecr-host",
            Value="123456789012.dkr.ecr.us-east-2.amazonaws.com",
            Type="String",
            Overwrite=True,
        )
        requested_services.clear()  # ignore the seeding client above

        monkeypatch.setattr(boto3, "client", recording_client)

        policy = tmp_path / "trustpolicy.json"
        policy.write_text("{}")
        args = _make_args("alpha-bank/assessment-results@sha256:" + "a" * 64)

        # Let _run build its OWN ssm + ecr clients (recorded) — only inject the
        # notation verify + signature classifier so no real CLI shells out.
        rc = verify_module._run(
            args,
            ssm_client=None,
            ecr_client=None,
            oras_module=_FakeOras(),
            verify_fn=_verify_ok,
            trust_policy_path=policy,
            classify_signatures_fn=lambda _ecr, **_kw: [],
        )

    assert rc == 0
    # The full verify run requested clients — and none of them was DynamoDB.
    assert requested_services, "verify should have constructed at least one boto3 client"
    assert "dynamodb" not in requested_services
    # Concretely: only ssm + ecr are on the verification trust path.
    assert set(requested_services) <= {"ssm", "ecr"}


def test_verify_aggregation_path_still_index_free(
    aws_region: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 3.2 aggregation (real classifier over list_image_referrers) stays index-free."""
    from moto import mock_aws

    requested_services: list[str] = []
    real_client = boto3.client

    def recording_client(service_name, *args, **kwargs):
        requested_services.append(service_name)
        return real_client(service_name, *args, **kwargs)

    import base64

    class _StubEcr:
        def get_authorization_token(self):
            token = base64.b64encode(b"AWS:pw").decode()
            return {"authorizationData": [{"authorizationToken": token}]}

        def list_image_referrers(self, **kwargs):
            return {
                "referrers": [
                    {
                        "artifactType": verify_module.NOTARY_SIGNATURE_ARTIFACT_TYPE,
                        "imageDigest": "sha256:" + "c" * 64,
                    }
                ]
            }

    with mock_aws():
        _ssm = boto3.client("ssm", region_name=aws_region)
        _ssm.put_parameter(
            Name="/cairnproofs/cmh-alpha/app-name",
            Value="cairnproofs4af",
            Type="String",
            Overwrite=True,
        )
        _ssm.put_parameter(
            Name="/cairnproofs4af/alpha/registry/ecr-host",
            Value="123456789012.dkr.ecr.us-east-2.amazonaws.com",
            Type="String",
            Overwrite=True,
        )
        requested_services.clear()
        monkeypatch.setattr(boto3, "client", recording_client)

        policy = tmp_path / "trustpolicy.json"
        policy.write_text("{}")
        args = _make_args("alpha-bank/assessment-results@sha256:" + "a" * 64)

        # Inject only the ECR stub (so referrer enumeration runs the REAL
        # _classify_signatures) and let _run build its ssm client itself.
        rc = verify_module._run(
            args,
            ssm_client=None,
            ecr_client=_StubEcr(),
            oras_module=_FakeOras(),
            verify_fn=_verify_ok,
            trust_policy_path=policy,
            # No classifier injection → real _classify_signatures over the stub.
        )

    assert rc == 0
    assert "dynamodb" not in requested_services


def test_recording_monitor_would_catch_a_dynamodb_client(
    aws_region: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard the guard: prove the index-independence monitor is NOT vacuous.

    The two tests above assert ``"dynamodb" not in requested_services``. That
    assertion is only meaningful if the recording wrapper would actually
    register a DynamoDB client were one constructed. Here we deliberately
    construct one through the SAME recording wrapper and confirm the monitor
    flags it — so a future regression that re-couples verify to the index
    cannot slip past a recorder that silently records nothing.
    """
    from moto import mock_aws

    requested_services: list[str] = []
    real_client = boto3.client

    def recording_client(service_name, *args, **kwargs):
        requested_services.append(service_name)
        return real_client(service_name, *args, **kwargs)

    with mock_aws():
        monkeypatch.setattr(boto3, "client", recording_client)
        # Sanity: a clean recorder has nothing in it...
        assert "dynamodb" not in requested_services
        # ...and the moment a DynamoDB client is built, the SAME predicate the
        # index-independence tests rely on flips to True.
        boto3.client("dynamodb", region_name=aws_region)

    assert "dynamodb" in requested_services

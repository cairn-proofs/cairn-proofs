"""Notation CLI subprocess wrapper ([SIGN-001], [SIGN-002], [SIGN-009]).

The Notation binary and AWS Signer plugin are provided by the Lambda layer
(lambda-layers/notation). This module exists to keep the subprocess invocation,
argument construction, error classification, and result parsing out of the
sign-bundle Lambda handler.

The current implementation supports the `system-clock` TimestampProvider
strategy only ([SIGN-009]). `rfc3161` is a recognized but unsupported value
and raises UnsupportedTimestampProvider when selected (see
timestamp_provider.py).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from cairn_signing.profile import REVIEWER_PROFILE_NAME, SigningProfile
from cairn_signing.timestamp_provider import TimestampProvider

# Notation binary path; resolvable from PATH inside the Lambda layer.
NOTATION_BIN: Final[str] = "notation"


class NotationCommandError(RuntimeError):
    """Raised when a Notation CLI invocation exits non-zero.

    Carries both stderr and the return code so callers can classify
    transient vs permanent failures ([SIGN-008] — retries vs DLQ routing).
    """

    def __init__(self, *, returncode: int, stderr: str, command: list[str]) -> None:
        super().__init__(f"notation command exited {returncode}: {stderr.strip()}")
        self.returncode = returncode
        self.stderr = stderr
        self.command = command


@dataclass(frozen=True)
class SignArtifactResult:
    """Result of a successful Notation signing call.

    ``signature_digest`` is intentionally not resolved here — caller
    queries the ECR Referrers API (``list_image_referrers``) after this
    function returns to obtain the freshly-attached signature digest.
    Notation's own ``list`` subcommand has flag-incompatibility issues
    across versions that this layer should not own.
    """

    artifact_reference: str  # e.g., registry.example.com/repo@sha256:...
    timestamp_provider: TimestampProvider


def _ensure_notation_installed() -> None:
    if shutil.which(NOTATION_BIN) is None:
        raise RuntimeError(
            f"{NOTATION_BIN!r} not found on PATH; the Notation Lambda layer "
            "(notation + aws-signer-plugin) must be attached to this function"
        )


def configure_ecr_docker_auth(*, registry_host: str, ecr_client: Any) -> None:
    """Write ``$HOME/.docker/config.json`` so Notation can talk to ECR.

    Notation's OCI client reads registry credentials from the Docker config
    file. In a Lambda without Docker installed and without a prior
    ``oras login``, that file doesn't exist and Notation errors with
    ``basic credential not found``. We pre-populate it with the
    short-lived ECR token from the Lambda's IAM credentials so subsequent
    ``notation sign`` calls authenticate cleanly.

    Caller is responsible for setting ``HOME`` (Lambda default is unset).
    """
    token_resp = ecr_client.get_authorization_token()
    data = token_resp["authorizationData"][0]
    encoded = data["authorizationToken"]
    config_dir = Path(os.environ.get("HOME", "/tmp")) / ".docker"  # noqa: S108 - /tmp is the only writable path in the Lambda runtime; used only as a HOME fallback
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"
    existing: dict[str, Any] = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text())
        except json.JSONDecodeError:
            existing = {}
    auths = existing.setdefault("auths", {})
    auths[registry_host] = {"auth": encoded}
    config_path.write_text(json.dumps(existing))


def _run(command: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and raise NotationCommandError on non-zero exit."""
    proc = subprocess.run(  # noqa: S603 - argv list built internally from the fixed NOTATION_BIN, never a shell string or untrusted input
        command,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if proc.returncode != 0:
        raise NotationCommandError(
            returncode=proc.returncode,
            stderr=proc.stderr,
            command=command,
        )
    return proc


def sign_artifact(
    artifact_reference: str,
    profile: SigningProfile,
    timestamp_provider: TimestampProvider,
    *,
    timeout_seconds: float = 30.0,
) -> SignArtifactResult:
    """Sign an OCI artifact via Notation + AWS Signer plugin ([SIGN-001], [SIGN-002]).

    `artifact_reference` must be a Notation-acceptable reference
    (registry/repo[:tag|@digest]); typically the bundle's
    `{ecr-uri}@{manifest-digest}` form so signing pins to a specific digest.

    Raises:
      - NotationCommandError if the CLI invocation fails (caller decides
        retry vs DLQ per [SIGN-008])
      - UnsupportedTimestampProvider for `rfc3161` (recognized but unsupported)
    """
    _ensure_notation_installed()
    if timestamp_provider != TimestampProvider.SYSTEM_CLOCK:
        from cairn_signing.timestamp_provider import UnsupportedTimestampProvider

        raise UnsupportedTimestampProvider(
            f"TimestampProvider {timestamp_provider.value!r} is not supported "
            "by the signing wrapper; only system-clock is built"
        )

    # No --plugin-config: the AWS Signer Notation plugin reads region and
    # credentials from the standard AWS SDK default chain, which the Lambda
    # execution role and AWS_REGION env var populate correctly. Passing
    # empty aws-profile= / aws-region= here would either be misparsed or
    # silently mask the SDK defaults.
    command = [
        NOTATION_BIN,
        "sign",
        "--plugin",
        "com.amazonaws.signer.notation.plugin",
        "--id",
        profile.arn,
        artifact_reference,
    ]
    _run(command, timeout_seconds=timeout_seconds)
    return SignArtifactResult(
        artifact_reference=artifact_reference,
        timestamp_provider=timestamp_provider,
    )


def sign_off(
    artifact_reference: str,
    reviewer_profile: SigningProfile,
    *,
    timeout_seconds: float = 30.0,
) -> SignArtifactResult:
    """Create a manual reviewer sign-off on an OCI artifact (signing [SIGN-003]).

    A sign-off is "just another OCI signature referrer" — a stock
    ``notation sign`` against the ``artifact-reviewer-signoff`` profile, so a
    reviewer could create it without Cairn's CLI at all (design.md
    *Multi-signature build*). This thin wrapper exists to make the reviewer
    profile a first-class, type-checked entrypoint and to reject a
    mis-targeted profile early.

    The sign-off always uses the ``system-clock`` TimestampProvider (the only
    supported strategy in this build); ``rfc3161`` is a recognized but
    unsupported value rejected by ``sign_artifact``.

    Raises:
      - ValueError if ``reviewer_profile`` is not the reviewer Signing_Profile.
      - NotationCommandError if the CLI invocation fails.
    """
    if reviewer_profile.name != REVIEWER_PROFILE_NAME:
        raise ValueError(
            f"sign_off requires the {REVIEWER_PROFILE_NAME!r} profile; "
            f"got {reviewer_profile.name!r}"
        )
    return sign_artifact(
        artifact_reference,
        reviewer_profile,
        TimestampProvider.SYSTEM_CLOCK,
        timeout_seconds=timeout_seconds,
    )


def verify_artifact(
    artifact_reference: str,
    *,
    timeout_seconds: float = 30.0,
) -> None:
    """Verify all signatures attached to an OCI artifact against the local trust policy.

    Raises NotationCommandError if any signature fails strict verification
    ([SIGN-004]). Caller maps that to a verification-failure outcome in the
    retrieval response (`signingStatus`).
    """
    _ensure_notation_installed()
    command = [NOTATION_BIN, "verify", artifact_reference]
    _run(command, timeout_seconds=timeout_seconds)

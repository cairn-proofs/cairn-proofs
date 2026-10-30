"""`cairn sign` — manual Attesting Reviewer sign-off over stock Notation tooling.

A reviewer sign-off is "just another OCI signature referrer": a stock
``notation sign`` against the artifact-reviewer-signoff Signing_Profile
(signing [SIGN-003]; artifact-verifiability *Index-Independent Re-Verification*).
This command is a thin convenience wrapper — a reviewer could create the same
referrer with bare ``notation sign`` and no Cairn CLI at all.

It resolves the bundle reference exactly as ``verify`` / ``pull`` do (the
shared ``_refs``/``_aws``/``_oras`` helpers), authenticates to ECR the same
way, reads the reviewer Signing_Profile version ARN from SSM, and delegates to
``cairn_signing.notation.sign_off`` — which rejects any non-reviewer profile.
The result is a NEW Notation signature referrer attached to the bundle's
manifest digest. No Metadata_Index write is required for the sign-off to be
valid: a verifier re-derives it from the ECR artifact + referrers alone.
"""

from __future__ import annotations

import argparse

import boto3
from cairn_signing import NotationCommandError, SigningProfile
from cairn_signing.notation import sign_off

from cairn_cli import _oras
from cairn_cli._aws import (
    AppNameUnresolvedError,
    DeploymentTarget,
    EcrAuthError,
    ecr_login,
    get_ecr_registry_host,
    resolve_app_name,
)
from cairn_cli._errors import fail
from cairn_cli._help import CairnHelpFormatter, build_epilog
from cairn_cli._refs import InvalidReferenceError, resolve_reference


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "sign",
        help="create a reviewer sign-off on an Artifact_Bundle (artifact-reviewer-signoff)",
        description=(
            "Attach a manual Attesting Reviewer sign-off to an Artifact_Bundle as a "
            "new Notation signature referrer, using the artifact-reviewer-signoff "
            "Signing_Profile. The sign-off is stock Notation tooling — it "
            "requires no Metadata_Index write and is independently verifiable "
            "from the ECR artifact alone. The reference may be a long-form ref "
            "or the short <project-id>/<artifact-kind>(:tag|@digest) form."
        ),
        formatter_class=CairnHelpFormatter,
        epilog=build_epilog(["  cairn sign alpha-bank/assessment-results:2026-q1"]),
    )
    p.add_argument(
        "reference",
        help="long-form ref OR <project-id>/<artifact-kind>(:tag|@digest)",
    )
    p.add_argument("--stage", default="alpha")
    p.add_argument("--region-code", default="cmh")
    p.add_argument(
        "--app-name",
        default=None,
        help="deployment appName override (else CAIRN_APP_NAME or SSM discovery)",
    )
    p.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    return _run(args, ssm_client=None, ecr_client=None, sign_off_fn=sign_off, oras_module=_oras)


def _resolve_reviewer_profile_arn(ssm_client, target: DeploymentTarget) -> str:
    """Read the reviewer Signing_Profile version ARN from SSM (written by CDK)."""
    response = ssm_client.get_parameter(Name=target.ssm_reviewer_profile_arn_param)
    return response["Parameter"]["Value"].strip()


def _run(
    args: argparse.Namespace,
    *,
    ssm_client,
    ecr_client,
    sign_off_fn,
    oras_module=_oras,
) -> int:
    """Inner entry point: clients + sign callback injected so tests pass mocks.

    Mirrors ``verify._run`` / ``push._run`` — production goes through ``run()``
    which builds real boto3 clients lazily; tests call ``_run`` with stubs.
    """
    target = DeploymentTarget(stage=args.stage, region_code=args.region_code)

    if ssm_client is None:
        ssm_client = boto3.client("ssm")
    if ecr_client is None:
        ecr_client = boto3.client("ecr")

    try:
        target = resolve_app_name(ssm_client, target, override=args.app_name)
    except AppNameUnresolvedError as e:
        return fail(str(e), exit_code=4)

    try:
        registry_host = get_ecr_registry_host(ssm_client, target)
    except Exception as e:
        return fail(f"failed to resolve ECR registry host: {e}", exit_code=4)

    try:
        ref = resolve_reference(args.reference, registry_host=registry_host, target=target)
    except InvalidReferenceError as e:
        return fail(str(e), exit_code=2)

    try:
        reviewer_arn = _resolve_reviewer_profile_arn(ssm_client, target)
    except Exception as e:
        return fail(f"failed to resolve reviewer Signing_Profile ARN: {e}", exit_code=4)

    # Authenticate the shared docker/notation credential store to ECR before
    # signing — `sign` obtains credentials exactly as `push`/`pull`/`verify` do,
    # so the operator needs no ambient `docker login`.
    try:
        ecr_login(ecr_client, oras_module, registry_host=registry_host)
    except (EcrAuthError, oras_module.OrasCommandError) as e:
        return fail(f"ECR login failed: {e}", exit_code=4)

    reviewer_profile = SigningProfile.reviewer(reviewer_arn)
    try:
        sign_off_fn(ref.full, reviewer_profile)
    except ValueError as e:
        # sign_off rejects a non-reviewer profile early.
        return fail(str(e), exit_code=2)
    except NotationCommandError as e:
        return fail(e.stderr.strip(), exit_code=4)
    except RuntimeError as e:
        return fail(f"notation not available: {e}", exit_code=4)

    print(f"signed-off: {ref.full}")
    return 0

"""`cairn verify` — standalone Notation signature check + multi-signature status.

Does not pull layers. Pre-flight checks that the operator has a Notation
trust policy installed; if not, prints a one-line hint pointing at the
SSM parameter that ships the rendered policy.

Beyond the pass/fail verdict, `verify` enumerates the bundle's Notation
signature referrers, maps each to a per-signature result, and renders the
AGGREGATED multi-signature status (signing *Signature Status Reporting on
Retrieval*; [SIGN-003], [SIGN-005]) — overall ``signingStatus`` plus per-signature
``signatureDetails``. The whole path stays index-independent: it builds only
``ssm`` + ``ecr`` clients and NEVER a DynamoDB client (artifact-verifiability
*Index-Independent Re-Verification*; Threat #11). The aggregated status is a
re-derivation of the cryptographic verdict, never a read of the Metadata_Index
projection.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import boto3
from cairn_oci import PRODUCER_ATTESTATION_ARTIFACT_TYPE, compute_sha256_digest
from cairn_signing import (
    PIPELINE_PROFILE_NAME,
    REVIEWER_PROFILE_NAME,
    NotationCommandError,
    SignatureResult,
    SignatureRole,
    SignatureStatus,
    aggregate_status,
    classify_bundle_trust,
    verify_artifact,
)

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
from cairn_cli._pending import (
    TAG_PENDING_EXIT_CODE,
    is_by_tag,
    is_tag_miss,
    surface_tag_pending,
)
from cairn_cli._refs import InvalidReferenceError, ResolvedReference, resolve_reference
from cairn_cli.pull import alias_tag_of, mutable_alias_label

# Re-exported from cairn_cli._pending (shared with `cairn pull`) so scripts
# importing it off this module keep working.
__all__ = ["TAG_PENDING_EXIT_CODE"]

# OCI artifactType of a Notation signature referrer; the enumeration filter for
# the multi-signature status roll-up (mirrors the sign-bundle Lambda's
# referrer discovery — both read the same ECR Referrers API, neither the index).
NOTARY_SIGNATURE_ARTIFACT_TYPE = "application/vnd.cncf.notary.signature"


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "verify",
        help="verify the Notation signature on an Artifact_Bundle (no layer pull)",
        description=(
            "Run a standalone Notation signature check on an Artifact_Bundle "
            "without pulling its layers. The reference may be a long-form ref "
            "or the short <project-id>/<artifact-kind>:tag form."
        ),
        formatter_class=CairnHelpFormatter,
        epilog=build_epilog(["  cairn verify alpha-bank/assessment-results:2026-q1"]),
    )
    p.add_argument("reference", help="long-form ref OR <project-id>/<artifact-kind>:tag")
    p.add_argument("--stage", default="alpha")
    p.add_argument("--region-code", default="cmh")
    p.add_argument(
        "--app-name",
        default=None,
        help="deployment appName override (else CAIRN_APP_NAME or SSM discovery)",
    )
    p.set_defaults(handler=run)


def _default_trust_policy_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "notation" / "trustpolicy.json"


def run(args: argparse.Namespace) -> int:
    # classify_signatures_fn is left at its default — _run falls back to the
    # real _classify_signatures; tests inject a stub in its place.
    return _run(args, ssm_client=None, verify_fn=verify_artifact, oras_module=_oras)


def _run(
    args: argparse.Namespace,
    *,
    ssm_client,
    verify_fn,
    oras_module=_oras,
    ecr_client=None,
    trust_policy_path: Path | None = None,
    classify_signatures_fn=None,
) -> int:
    target = DeploymentTarget(stage=args.stage, region_code=args.region_code)

    if ssm_client is None:
        ssm_client = boto3.client("ssm")
    if ecr_client is None:
        ecr_client = boto3.client("ecr")

    # Resolve appName first — the trust-policy install hint and every SSM read
    # path are keyed on it.
    try:
        target = resolve_app_name(ssm_client, target, override=args.app_name)
    except AppNameUnresolvedError as e:
        return fail(str(e), exit_code=4)

    policy_path = trust_policy_path or _default_trust_policy_path()
    if not policy_path.exists():
        return fail(
            f"Notation trust policy not found at {policy_path}; install via:\n"
            f"  aws ssm get-parameter --name {target.ssm_trust_policy_param} "
            f"--query 'Parameter.Value' --output text > {policy_path}",
            exit_code=4,
        )

    try:
        registry_host = get_ecr_registry_host(ssm_client, target)
    except Exception as e:
        return fail(f"failed to resolve ECR registry host: {e}", exit_code=4)

    try:
        ref = resolve_reference(args.reference, registry_host=registry_host, target=target)
    except InvalidReferenceError as e:
        return fail(str(e), exit_code=2)

    # Authenticate the local docker/notation credential store to ECR before the
    # signature check — `verify` obtains credentials exactly as `push`/`pull` do,
    # so the operator needs no ambient `docker login`.
    try:
        ecr_login(ecr_client, oras_module, registry_host=registry_host)
    except (EcrAuthError, oras_module.OrasCommandError) as e:
        return fail(f"ECR login failed: {e}", exit_code=4)

    try:
        verify_fn(ref.full)
    except NotationCommandError as e:
        # By-tag miss during the pre-mint window is "tag pending", not a
        # verification failure; the digest stays resolvable.
        if is_by_tag(ref) and is_tag_miss(e.stderr):
            return surface_tag_pending(ref)
        return fail(e.stderr.strip(), exit_code=4)
    except RuntimeError as e:
        return fail(f"notation not available: {e}", exit_code=4)

    # The verdict is anchored on the digest + signature + WORM_Receipt (Notation
    # verified those above). When the reference was a mutable alias, present that
    # alias only as a LABEL pointing at the resolved immutable digest and key the
    # rendered verdict on the digest — never on the alias string.
    alias_tag = alias_tag_of(ref)
    subject_digest = _resolve_subject_digest(ref, alias_tag, oras_module)
    if alias_tag is not None and subject_digest is not None:
        print(mutable_alias_label(alias_tag, subject_digest))
        print(f"verified: {subject_digest}")
    else:
        print(f"verified: {ref.full}")

    # Multi-signature roll-up: enumerate the bundle's Notation signature
    # referrers, map each to a per-signature result, and render the aggregated
    # signingStatus + per-signature signatureDetails. This is a re-derivation of
    # the cryptographic verdict from the ECR artifact + referrers — it issues NO
    # DynamoDB / Metadata_Index call (artifact-verifiability *Index-Independent
    # Re-Verification*). A degraded enumeration (no resolvable digest, no ECR
    # client, or a registry hiccup) never turns a SUCCESSFUL verify into a
    # failure — the pass/fail verdict above stands on its own.
    classify = classify_signatures_fn or _classify_signatures
    if subject_digest is not None and ecr_client is not None:
        try:
            results = classify(
                ecr_client,
                registry_host=registry_host,
                repository=ref.repository,
                subject_digest=subject_digest,
                verify_fn=verify_fn,
            )
        except Exception:
            results = None
        if results is not None:
            _render_signature_status(results)
    return 0


def _resolve_subject_digest(
    ref: ResolvedReference, alias_tag: str | None, oras_module
) -> str | None:
    """Return the bundle's Manifest_Digest (``sha256:...``) for referrer lookup.

    A by-digest reference already carries the digest in its selector — no
    round-trip. An ALIAS by-tag reference is resolved via the EXISTING tag→digest
    machinery (the same ``oras manifest fetch`` primitive ``cairn pull`` uses) so
    the alias can be labeled with its underlying digest; that digest doubles as
    the referrer-lookup subject. A *canonical* (non-alias) by-tag reference is
    NOT resolved here — the non-alias path makes no extra ECR round-trip just to
    label (preserved invariant), so it returns ``None`` and the signature roll-up
    is skipped for that shape. A fetch failure degrades to ``None`` so a
    transient registry hiccup never turns a SUCCESSFUL verify into a failure.
    """
    if ref.selector.startswith("@"):
        return ref.selector[1:]
    if alias_tag is None:
        return None
    try:
        return _resolve_alias_digest(ref, oras_module)
    except Exception:
        return None


def _render_signature_status(results: list[SignatureResult]) -> None:
    """Render the aggregated signingStatus + the two-signature trust verdict.

    Two layers, deliberately distinct:

    - ``signingStatus`` is the valid/revoked roll-up over ALL present
      signatures (``aggregate_status``).
    - ``trust`` is the two-signature coverage verdict (``classify_bundle_trust``):
      a bundle is fully trusted ONLY when BOTH the content signature and the
      attestation signature are present and valid. A one-signature artifact is
      ``partial`` and the missing role is named, so it is never read as fully
      trusted/verified.
    """
    aggregated = aggregate_status(results)
    print(f"signingStatus: {aggregated.signing_status.value}")

    trust = classify_bundle_trust(results)
    line = f"trust: {trust.verdict.value}"
    if trust.missing_roles:
        missing = ", ".join(f"{role.value} signature" for role in trust.missing_roles)
        line += f" (missing {missing})"
    print(line)

    for detail in aggregated.signature_details:
        line = (
            f"  signatureDetails: {detail.profile_name} [{detail.role.value}] {detail.status.value}"
        )
        if detail.signing_timestamp:
            line += f" @ {detail.signing_timestamp}"
        print(line)


def _profile_name_for_referrer(referrer: dict[str, Any]) -> str:
    """Best-effort Signing_Profile name for a Notation signature referrer.

    Notation does not stamp the signer profile ARN onto the referrer manifest
    in a machine-readable place stable across versions, so we read a Cairn
    annotation when present and otherwise fall back to the pipeline profile
    (the always-present automated signature). The aggregation treats a reviewer
    referrer identically to a pipeline one; only the rendered label differs.
    """
    annotations = referrer.get("annotations") or {}
    for value in annotations.values():
        if REVIEWER_PROFILE_NAME in str(value):
            return REVIEWER_PROFILE_NAME
        if PIPELINE_PROFILE_NAME in str(value):
            return PIPELINE_PROFILE_NAME
    return PIPELINE_PROFILE_NAME


def _list_referrers(
    ecr_client: Any, *, repository_name: str, subject_digest: str
) -> list[dict[str, Any]]:
    """Walk the paginated OCI Referrers API for one subject digest.

    boto3 registers no paginator for ``list_image_referrers``, so we walk
    ``nextToken`` manually and accumulate every referrer entry. The SAME
    index-free primitive the sign-bundle Lambda uses — never the
    Metadata_Index.
    """
    referrers: list[dict[str, Any]] = []
    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "repositoryName": repository_name,
            "subjectId": {"imageDigest": subject_digest},
        }
        if next_token:
            kwargs["nextToken"] = next_token
        resp = ecr_client.list_image_referrers(**kwargs)
        referrers.extend(resp.get("referrers", []))
        next_token = resp.get("nextToken")
        if not next_token:
            break
    return referrers


def _referrer_digest(referrer: dict[str, Any]) -> str:
    """The referrer entry's own image digest (``imageDigest`` or ``digest``), or ``""``.

    ECR has used both key spellings across the API surface, so read either.
    """
    return str(referrer.get("imageDigest") or referrer.get("digest") or "")


def _signature_result(
    referrer: dict[str, Any],
    role: SignatureRole,
    status: SignatureStatus = SignatureStatus.VALID,
) -> SignatureResult:
    """Map one Notation signature referrer entry to a SignatureResult.

    Each signature subject is verified INDEPENDENTLY before its result is built,
    so ``status`` reflects that specific subject's own cryptographic verdict —
    it is NOT a blanket "the top-level verify passed" assumption.
    ``notation verify <content-ref>`` only verifies signatures whose subject is
    the content manifest, so it covers the content-role signatures but says
    NOTHING about the Attestation referrer's own signatures. The default
    ``VALID`` is therefore correct only for the content role (whose subject the
    top-level ``verify_fn(ref.full)`` already verified); :func:`_classify_signatures`
    re-runs ``verify_fn`` against each Attestation referrer's by-digest reference
    and passes the per-attestation verdict here — ``REVOKED`` when that verify
    raised — so a present-but-unverified attestation signature is never reported
    ``VALID`` and never yields a ``fully-trusted`` bundle.

    The signature referrer's own image digest is its stable per-signature
    identity (Notation does not expose the signer profile ARN on the referrer in
    a version-stable place). It belongs in ``signature_digest``, NOT
    ``profile_arn``: ``profile_arn`` stays ``None`` precisely because this
    index-free CLI re-derivation cannot bind the signature to a profile ARN, and
    stuffing a digest into an ARN-named field would mislead any signatureDetails
    consumer.
    """
    referrer_digest = _referrer_digest(referrer)
    return SignatureResult(
        profile_arn=None,
        profile_name=_profile_name_for_referrer(referrer),
        status=status,
        signing_timestamp="",
        timestamp_provider=None,
        signature_digest=referrer_digest or None,
        role=role,
    )


def _classify_signatures(
    ecr_client: Any,
    *,
    registry_host: str,
    repository: str,
    subject_digest: str,
    verify_fn,
) -> list[SignatureResult]:
    """Enumerate the bundle's signatures and assign each a content/attestation role.

    A submission carries TWO independent signature subjects:
    the content manifest and the Attestation
    referrer. This walks the OCI Referrers API in two hops, all index-free:

    1. List referrers on the CONTENT manifest digest. Notation signatures here
       sign the content → ``role=CONTENT``; they are recorded ``VALID`` because
       the caller's top-level ``verify_fn(ref.full)`` already verified the
       content manifest's signatures. Attestation referrers (artifactType
       ``application/vnd.cairnproofs.producer-attestation.v1+json``) are collected so
       their own signatures can be independently verified.
    2. For each Attestation referrer, INDEPENDENTLY verify its signature by
       running ``verify_fn`` against the referrer's own by-digest reference
       (``{registry_host}/{repository}@{attestation_digest}``) — the top-level
       ``notation verify <content-ref>`` only covers signatures whose subject is
       the content manifest, so the attestation signature is unverified until
       this second pass. List ITS referrers; the Notation signatures there sign
       the attestation → ``role=ATTESTATION``, recorded ``VALID`` only if that
       per-attestation verify succeeded and ``REVOKED`` (the only available
       non-VALID status) if it raised ``NotationCommandError``.

    A present-but-cryptographically-invalid attestation signature therefore maps
    to ``REVOKED``, so it does NOT satisfy the attestation role and
    ``classify_bundle_trust`` yields ``partial`` — a second signature that does
    not verify is never reported as fully trusted. A clean
    ``NotationCommandError`` on the attestation is the REVOKED signal (not
    swallowed); a degraded/transient verify (``RuntimeError`` etc.) propagates to
    the caller's resilient ``except`` so the authoritative content pass/fail
    verdict still stands on its own.
    """
    repository_name = repository.split("/", 1)[1] if "/" in repository else repository
    results: list[SignatureResult] = []

    content_referrers = _list_referrers(
        ecr_client, repository_name=repository_name, subject_digest=subject_digest
    )
    attestation_digests: list[str] = []
    for referrer in content_referrers:
        artifact_type = referrer.get("artifactType")
        if artifact_type == NOTARY_SIGNATURE_ARTIFACT_TYPE:
            results.append(_signature_result(referrer, SignatureRole.CONTENT))
        elif artifact_type == PRODUCER_ATTESTATION_ARTIFACT_TYPE:
            digest = _referrer_digest(referrer)
            if digest:
                attestation_digests.append(digest)

    for attestation_digest in attestation_digests:
        # Independently verify THIS attestation referrer's own signature; the
        # top-level content verify never covered it. A clean NotationCommandError
        # is the REVOKED verdict (so the attestation role is not satisfied);
        # success means its signatures are VALID.
        att_ref = f"{registry_host}/{repository}@{attestation_digest}"
        try:
            verify_fn(att_ref)
            attestation_status = SignatureStatus.VALID
        except NotationCommandError:
            attestation_status = SignatureStatus.REVOKED
        for referrer in _list_referrers(
            ecr_client, repository_name=repository_name, subject_digest=attestation_digest
        ):
            if referrer.get("artifactType") == NOTARY_SIGNATURE_ARTIFACT_TYPE:
                results.append(
                    _signature_result(referrer, SignatureRole.ATTESTATION, attestation_status)
                )

    return results


def _resolve_alias_digest(ref, oras_module) -> str | None:
    """Resolve an alias reference to its underlying Manifest_Digest for labeling.

    Reuses the EXISTING tag→digest machinery (`oras manifest fetch`, the same
    primitive `cairn pull` uses) — not a new ECR client path: the fetched
    manifest's own sha256 IS the resolved digest the registry pointed the alias
    at. Verification has already succeeded against that digest via Notation; this
    fetch only recovers the digest string to display. A fetch failure degrades to
    ``None`` (the caller falls back to the resolved reference) so a transient
    registry hiccup never turns a SUCCESSFUL verify into a failure.
    """
    try:
        manifest_bytes = oras_module.fetch_manifest(reference=ref.full)
    except oras_module.OrasCommandError:
        return None
    return compute_sha256_digest(manifest_bytes)

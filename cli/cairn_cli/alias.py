"""`cairn alias set` / `cairn alias move` — operator alias-tag management.

A thin wrapper over the ECR tag primitive that enforces exactly the
three invariants the raw ``oras tag`` / ``put-image`` primitive does NOT:

1. **Grammar** — the alias string is a well-formed ``_als``/``_chn`` tag, built
   through the grammar authority (:class:`ArtifactLocator`), fail-closed
   on a malformed slug.
2. **Filed-digest** — the target digest is *already filed*: present
   in the repository with a canonical ``{artifact-kind}--_ref--{digest}``
   tag AND a Metadata_Index row. An alias is a *view* over a validated artifact,
   never an independent address. The error distinguishes "pushed but not yet
   filed" (digest present, no ``_ref`` tag / no index row) from "never pushed"
   (digest absent).
3. **Legible authz surfacing** — the load-bearing alias-movement
   control is the dedicated ``{appName}-alias-mover-{qualifier}`` IAM role (ECR
   ``PutImage`` on the alias namespace), NOT this client. The wrapper does not
   re-implement authz; it surfaces an ECR ``AccessDenied`` legibly, pointing the
   operator at that role.

``set`` is create-or-fail (refuses if the alias tag already exists → directs the
operator to ``move``); ``move`` is the explicit atomic rebind of an existing
alias (a single ``put_image`` call). On ``move`` a structured pre/post-binding
audit event (alias, prior digest → new digest, caller identity) is emitted to
strengthen the repudiation posture beyond CloudTrail's API record.

Aliases never carry the verification guarantee — that invariant
lives in the resolution paths (``pull``/``verify``/``query``/TUI), not here.
"""

from __future__ import annotations

import argparse
import logging

import boto3
from botocore.exceptions import ClientError
from cairn_metadata import MetadataIndex
from cairn_oci import ArtifactLocator, InvalidIdError, InvalidTagError, is_alias_tag

from cairn_cli import _aws
from cairn_cli._aws import (
    AppNameUnresolvedError,
    DeploymentTarget,
    resolve_app_name,
    resolve_table_names,
)
from cairn_cli._errors import fail
from cairn_cli._help import CairnHelpFormatter, build_epilog
from cairn_cli.config import TargetResolutionError, resolve_target

#: ECR / STS error codes that mean the caller lacks the alias-movement grant.
_ACCESS_DENIED_CODES = frozenset({"AccessDeniedException", "AccessDenied"})

#: One structured logger for the move audit event. Read-only module
#: state (a logger handle) — never per-invocation mutable state.
_AUDIT_LOG = logging.getLogger("cairn.alias.audit")

_LOCATOR = ArtifactLocator()


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "alias",
        help="set or move a mutable alias tag over an already-filed artifact digest",
        description=(
            "Manage mutable alias tags (named aliases and channel pointers) over "
            "an already-filed artifact digest. An alias is a view over a validated "
            "artifact, never an independent address; the target digest must already "
            "be filed (canonical _ref tag + Metadata_Index row)."
        ),
        formatter_class=CairnHelpFormatter,
        epilog=build_epilog(
            [
                "cairn alias set my-project catalog sha256:<64-hex> --name nist-800-53-latest",
                "cairn alias move my-project catalog sha256:<64-hex> --channel latest",
            ],
            notes=(
                "Use 'cairn alias set' to create a new alias (fails if it exists) and "
                "'cairn alias move' to atomically repoint an existing one. Moving an "
                "alias requires the dedicated '{appName}-alias-mover-<qualifier>' IAM grant."
            ),
        ),
    )
    alias_sub = p.add_subparsers(dest="alias_command")
    for verb, blurb, example in (
        (
            "set",
            "create an alias tag pointing at a filed digest (fails if it exists)",
            "cairn alias set my-project catalog sha256:<64-hex> --channel latest",
        ),
        (
            "move",
            "atomically repoint an existing alias tag to another filed digest",
            "cairn alias move my-project catalog sha256:<64-hex> --channel latest",
        ),
    ):
        sp = alias_sub.add_parser(
            verb,
            help=blurb,
            description=blurb,
            formatter_class=CairnHelpFormatter,
            epilog=build_epilog([example]),
        )
        sp.add_argument("project_id")
        sp.add_argument(
            "artifact_kind",
            help="leading tag segment / artifact-kind of the target (e.g. 'catalog')",
        )
        sp.add_argument("digest", help="target Manifest_Digest (sha256:<64-hex>)")
        # The alias target's repository is the uniform three-segment path
        # {org-id}/{tenant-id}/{project-id}; {project-id} is the
        # positional above, while {org-id}/{tenant-id} resolve from these
        # switches, the CAIRN_ORG_ID/CAIRN_TENANT_ID env vars, or a persisted
        # config — the same [INGEST-032] precedence chain `cairn push` uses.
        sp.add_argument("--org-id", help="opaque org-id identity coordinate (else env/config)")
        sp.add_argument(
            "--tenant-id", help="opaque tenant-id identity coordinate (else env/config)"
        )
        selector = sp.add_mutually_exclusive_group(required=True)
        selector.add_argument(
            "--name",
            help="free-form named alias slug → {artifact-kind}--_als--{name}",
        )
        selector.add_argument(
            "--channel",
            help="channel-pointer slug (latest, stable, …) → {artifact-kind}--_chn--{channel}",
        )
        sp.add_argument("--stage", default="alpha")
        sp.add_argument("--region-code", default="cmh")
        sp.add_argument(
            "--app-name",
            default=None,
            help="deployment appName override (else CAIRN_APP_NAME or SSM discovery)",
        )
        sp.set_defaults(handler=run, alias_command=verb)


def run(args: argparse.Namespace) -> int:
    return _run(args, ecr_client=None, dynamodb_resource=None, sts_client=None, ssm_client=None)


def _build_alias_tag(args: argparse.Namespace) -> str:
    """Build the alias tag string through the grammar authority (fail-closed)."""
    if args.name is not None:
        return _LOCATOR.build_named_alias(artifact_kind=args.artifact_kind, name=args.name)
    return _LOCATOR.build_channel_pointer(artifact_kind=args.artifact_kind, channel=args.channel)


def _caller_identity(sts_client) -> str:
    """Best-effort caller ARN for the move audit event (never blocks the move)."""
    try:
        return str(sts_client.get_caller_identity().get("Arn") or "unknown")
    except Exception:
        return "unknown"


def _run(
    args: argparse.Namespace,
    *,
    ecr_client,
    dynamodb_resource,
    sts_client,
    ssm_client=None,
) -> int:
    """Inner entry point: clients are injected so tests can pass mocks/stubs."""
    if getattr(args, "alias_command", None) not in ("set", "move"):
        return fail("specify a subcommand: 'cairn alias set' or 'cairn alias move'", exit_code=2)

    target = DeploymentTarget(stage=args.stage, region_code=args.region_code)
    if ssm_client is None:
        ssm_client = boto3.client("ssm")
    try:
        target = resolve_app_name(ssm_client, target, override=args.app_name)
    except AppNameUnresolvedError as e:
        return fail(str(e), exit_code=4)
    target = resolve_table_names(ssm_client, target)

    digest = args.digest
    _hex = digest[len("sha256:") :]
    if (
        not digest.startswith("sha256:")
        or len(_hex) != 64
        or not all(c in "0123456789abcdef" for c in _hex)
    ):
        return fail(
            f"target digest {digest!r} is not a sha256:<64-lowercase-hex> Manifest_Digest",
            exit_code=2,
        )

    # Invariant 1 — grammar. Build the alias tag through the registry authority;
    # a malformed slug raises InvalidTagError, surfaced legibly (R11: the message
    # names the offending form class, never echoes raw bytes).
    try:
        alias_tag = _build_alias_tag(args)
    except InvalidTagError as e:
        return fail(f"malformed alias: {e}", exit_code=2)

    if ecr_client is None:
        ecr_client = boto3.client("ecr")
    if sts_client is None:
        sts_client = boto3.client("sts")

    # Resolve the {org-id}/{tenant-id}/{project-id} identity triple:
    # {project-id} is the positional, {org-id}/{tenant-id} come from --org-id/
    # --tenant-id, the CAIRN_* env vars, or a persisted config, via the same
    # [INGEST-032] precedence chain `cairn push` uses. An unresolved coordinate
    # fails loud naming what is missing.
    try:
        coordinates = resolve_target(
            org_id=args.org_id,
            tenant_id=args.tenant_id,
            project_id=args.project_id,
        )
    except TargetResolutionError as e:
        return fail(str(e), exit_code=2)
    org_id = coordinates["org-id"].value
    tenant_id = coordinates["tenant-id"].value
    project_id = coordinates["project-id"].value

    # The boto3 ECR client operates on the caller's own registry, so no SSM
    # registry-host lookup is needed (every call here is digest/tag scoped
    # within one repository, addressed by name).
    #
    # Uniform three-segment layout: one repo per
    # {org-id}/{tenant-id}/{project-id}; the artifact-kind lives in the tag, not
    # the repo path. The locator emits the bare ``artifact-{qualifier}/...``
    # prefix; ``repo_path`` namespaces it with the resolved appName so the target
    # matches the deployed ECR repo. A malformed coordinate fails loud here.
    try:
        repo_name = target.repo_path(
            _LOCATOR.repo_name(
                deployment_qualifier=target.qualifier,
                org_id=org_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )
        )
    except InvalidIdError as e:
        return fail(str(e), exit_code=2)

    # Invariant 2 — filed-digest precondition.
    filed_error = _check_filed(
        ecr_client=ecr_client,
        dynamodb_resource=dynamodb_resource,
        target=target,
        repo_name=repo_name,
        tenant_id=tenant_id,
        project_id=project_id,
        artifact_kind=args.artifact_kind,
        digest=digest,
    )
    if filed_error is not None:
        return filed_error

    # set = create-or-fail; move = rebind-existing. Both resolve the alias tag's
    # current binding first.
    try:
        prior_digest = _aws.digest_for_tag(
            ecr_client, repository_name=repo_name, image_tag=alias_tag
        )
    except ClientError as e:
        return _maybe_access_denied(e, target) or fail(
            f"failed to inspect alias tag {alias_tag!r}: {e}", exit_code=4
        )

    if args.alias_command == "set" and prior_digest is not None:
        if prior_digest == digest:
            print(f"{alias_tag} already points at {digest} (no-op)")
            return 0
        return fail(
            f"alias {alias_tag!r} already exists (points at {prior_digest}); "
            "use 'cairn alias move' to repoint it",
            exit_code=2,
        )
    if args.alias_command == "move" and prior_digest is None:
        return fail(
            f"alias {alias_tag!r} does not exist; use 'cairn alias set' to create it",
            exit_code=2,
        )

    # Invariant 3 — write through the registry primitive; surface AccessDenied
    # legibly (IAM is the enforcement, not this client).
    try:
        _aws.put_alias_tag(
            ecr_client,
            repository_name=repo_name,
            image_digest=digest,
            alias_tag=alias_tag,
        )
    except ClientError as e:
        return _maybe_access_denied(e, target) or fail(
            f"failed to write alias tag {alias_tag!r}: {e}", exit_code=5
        )

    if args.alias_command == "move":
        _emit_move_audit(
            sts_client=sts_client,
            alias_tag=alias_tag,
            prior_digest=prior_digest,
            new_digest=digest,
            repo_name=repo_name,
        )
        print(f"moved {alias_tag}: {prior_digest} -> {digest}")
    else:
        print(f"set {alias_tag} -> {digest}")
    return 0


def _check_filed(
    *,
    ecr_client,
    dynamodb_resource,
    target: DeploymentTarget,
    repo_name: str,
    tenant_id: str,
    project_id: str,
    artifact_kind: str,
    digest: str,
) -> int | None:
    """Return None when the digest is filed, else a populated ``fail`` exit code.

    Filed = the digest is present in the repository, carries a
    canonical ``{artifact-kind}--_ref--{digest}`` tag, AND has a
    Metadata_Index row. The error distinguishes the two not-filed branches.
    """
    try:
        tags = _aws.tags_for_digest(ecr_client, repository_name=repo_name, image_digest=digest)
        exists = bool(tags) or _aws.image_exists(
            ecr_client, repository_name=repo_name, image_digest=digest
        )
    except ClientError as e:
        denied = _maybe_access_denied(e, target)
        if denied is not None:
            return denied
        return fail(f"failed to inspect target digest {digest!r}: {e}", exit_code=4)

    if not exists:
        # Never pushed — the digest is absent from the repository entirely.
        return fail(
            f"target digest {digest} is not present in repository {repo_name!r} "
            "(never pushed); push the bundle and let the pipeline file it first",
            exit_code=2,
        )

    has_canonical_tag = _has_canonical_ref_tag(tags, artifact_kind=artifact_kind)

    if dynamodb_resource is None:
        dynamodb_resource = boto3.resource("dynamodb")
    index = MetadataIndex(target.metadata_table, dynamodb_resource=dynamodb_resource)
    record = index.get_record(manifest_digest=digest, tenant_id=tenant_id, project_id=project_id)

    if not has_canonical_tag or record is None:
        # Pushed but not yet filed — present, but missing the canonical tag
        # and/or the index row (the eventual-consistency window).
        missing = []
        if not has_canonical_tag:
            missing.append("no canonical _ref tag")
        if record is None:
            missing.append("no Metadata_Index row")
        return fail(
            f"target digest {digest} is present but not yet filed "
            f"({'; '.join(missing)}); wait for the pipeline to validate, tag, and "
            "index it, then retry",
            exit_code=2,
        )
    return None


def _has_canonical_ref_tag(tags: list[str], *, artifact_kind: str) -> bool:
    """True iff the digest carries a canonical ``_ref`` tag for ``artifact_kind``.

    Parses each tag through the grammar authority (never a hand-match) and
    accepts only a ``_ref`` form whose leading artifact-kind matches the alias
    target's — so an alias cannot point at a digest filed under a different kind.
    """
    for tag in tags:
        try:
            parsed = _LOCATOR.parse_tag(tag)
        except InvalidTagError:
            continue
        if not is_alias_tag(parsed) and parsed.artifact_kind == artifact_kind:
            return True
    return False


def _maybe_access_denied(error: ClientError, target: DeploymentTarget) -> int | None:
    """Map an ECR/STS AccessDenied to a legible alias-movement-role hint, else None."""
    code = error.response.get("Error", {}).get("Code")
    if code not in _ACCESS_DENIED_CODES:
        return None
    return fail(
        "access denied writing the alias tag. Moving an alias requires the "
        f"dedicated alias-movement grant (managed policy '{target.name_for('alias-mover')}'); "
        "the artifact push/sign roles do NOT grant it. "
        "Assume the alias-mover role and retry.",
        exit_code=4,
    )


def _emit_move_audit(
    *,
    sts_client,
    alias_tag: str,
    prior_digest: str | None,
    new_digest: str,
    repo_name: str,
) -> None:
    """Emit the structured pre/post-binding move audit event.

    Strengthens the repudiation posture beyond CloudTrail's API record: the
    alias string, the prior → new digest rebinding, and the caller identity are
    recorded together at the moment of the move.
    """
    _AUDIT_LOG.info(
        "alias move",
        extra={
            "event": "alias.move",
            "alias": alias_tag,
            "repository": repo_name,
            "priorDigest": prior_digest,
            "newDigest": new_digest,
            "principal": _caller_identity(sts_client),
        },
    )

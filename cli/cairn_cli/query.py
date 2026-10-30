"""`cairn query` — read the Metadata_Index via its supported access paths.

Supported access paths (per metadata-index + producer-claims specs):
  --tenant-id <tenant-id> --project <project-id>  → MetadataIndex tenant-PK
                                                    (tenant exact-match PK +
                                                    begins_with project SK)
  --tenant-id <tenant-id> --control <controlId> --catalog-or-profile-uuid <uuid>
                                                  → ControlClaims tenant-PK
                                                    (tenant exact-match PK +
                                                    begins_with
                                                    {uuid}#{controlId} SK)

Both the Metadata_Index and the control-claims projection are tenant-PK planes
([META-001] / [PCLAIM-001]). The Metadata_Index partition key is
the opaque ``{tenant-id}`` and its sort key is ``{project-id}#{manifest-digest}``
— so a project listing is scoped to one tenant (exact-match) plus a project
``begins_with`` prefix. The Control Claims table partition key is likewise the
opaque ``{tenant-id}`` and its sort key is
``{catalogOrProfileUuid}#{controlId}#{manifest-digest}``. Both access paths
therefore require ``--tenant-id``: the project path needs ``--project`` and the
control path needs ``--control`` + ``--catalog-or-profile-uuid``.

Unsupported access paths (--assessment-id, --date-range/--date-from/--date-to,
--text, --digest) are rejected with a pointer to the metadata-index spec. The
producer-claim dimensions are not core query dimensions of the Metadata_Index
record; control-scoped discovery is the control-claims projection (the
``ControlClaims`` table, [PCLAIM-001]) reached via --control.

Output is JSONL on stdout (one record per line). The records carry the opaque
identity coordinates (``orgId``/``tenantId``/``projectId``); display-name
resolution is the human-facing TUI's job (a read-time Identity_Registry join),
NOT this machine-readable stream. The pagination token, when one is present, is
emitted as `__next_token__: <token>` on STDERR so stdout remains a clean JSONL
stream that downstream `jq` pipelines can consume without a trailing-line hack.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

import boto3
from cairn_metadata import ControlClaims, MetadataIndex
from cairn_metadata.pagination import InvalidPaginationToken
from cairn_oci import alias_tag_of
from cairn_signing import bundle_trust_from_projection

from cairn_cli._aws import (
    APP_NAME_ENV_VAR,
    AppNameUnresolvedError,
    DeploymentTarget,
    resolve_app_name,
    resolve_table_names,
)
from cairn_cli._errors import fail
from cairn_cli._help import CairnHelpFormatter, build_epilog
from cairn_cli._pending import TAG_PENDING_LABEL, is_tag_pending

_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[45][0-9A-Fa-f]{3}-[89ABab][0-9A-Fa-f]{3}-[0-9A-Fa-f]{12}$"
)

# Access paths the query surface does not support. The producer-claim
# dimensions (--assessment-id, --date-range/--date-from/--date-to, --text)
# are not core query dimensions of the Metadata_Index record; --digest is a
# core dimension the CLI does not expose. Listed by argparse name so the
# rejection message is symmetric with the spec wording.
_UNSUPPORTED_SELECTORS: dict[str, str] = {
    "date_range": "--date-range",
    "date_from": "--date-from",
    "date_to": "--date-to",
    "assessment_id": "--assessment-id",
    "text": "--text",
    "digest": "--digest",
}


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "query",
        help="query the Metadata_Index by project or control (supported access paths only)",
        description=(
            "Read the Metadata_Index via the supported access paths and emit "
            "matching records as JSONL on stdout. Specify exactly one of "
            "--project or --control; both require --tenant-id, the partition-key "
            "isolation boundary, and --control also requires "
            "--catalog-or-profile-uuid."
        ),
        formatter_class=CairnHelpFormatter,
        epilog=build_epilog(
            [
                "  cairn query --tenant-id acme --project alpha-bank",
                "  cairn query --tenant-id acme --control ac-2 --catalog-or-profile-uuid <uuid>",
            ]
        ),
    )
    p.add_argument(
        "--tenant-id",
        help=(
            "opaque tenant-id (the tenant-PK partition key); required with "
            "--project and with --control"
        ),
    )
    p.add_argument("--project")
    p.add_argument("--control")
    p.add_argument(
        "--catalog-or-profile-uuid",
        help="OSCAL Catalog or Profile UUID; required with --control",
    )
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--page-token", default=None)
    p.add_argument("--stage", default="alpha")
    p.add_argument("--region-code", default="cmh")
    p.add_argument(
        "--app-name",
        default=None,
        help="deployment appName override (else CAIRN_APP_NAME or SSM discovery)",
    )
    # Unsupported selectors — declared so we can emit a clear error rather
    # than argparse's generic "unrecognized arguments". `nargs="?"` lets
    # us detect presence without requiring a value.
    p.add_argument("--date-range", nargs="?", const=True, default=None)
    p.add_argument("--date-from", nargs="?", const=True, default=None)
    p.add_argument("--date-to", nargs="?", const=True, default=None)
    p.add_argument("--assessment-id", nargs="?", const=True, default=None)
    p.add_argument("--text", nargs="?", const=True, default=None)
    p.add_argument("--digest", nargs="?", const=True, default=None)
    p.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    return _run(args, dynamodb_resource=None, ssm_client=None)


def _run(args: argparse.Namespace, *, dynamodb_resource, ssm_client=None) -> int:
    # Reject unsupported query shapes early — before any DynamoDB call.
    for attr, flag in _UNSUPPORTED_SELECTORS.items():
        if getattr(args, attr, None) is not None:
            return fail(
                f"{flag} is not a supported query shape; only --project and "
                "--control (+ --catalog-or-profile-uuid) are supported access "
                "paths. See openspec/specs/metadata-index/spec.md for the "
                "access-path scoping.",
                exit_code=2,
            )

    # Mutual exclusion (exactly one of --project / --control).
    if bool(args.project) == bool(args.control):
        return fail("specify exactly one of --project or --control", exit_code=2)

    # The project access path is tenant-scoped: the Metadata_Index partition key
    # is the opaque {tenant-id} ([META-001]), so a project listing
    # cannot be issued without its tenant. Fail before any DynamoDB call.
    if args.project and not args.tenant_id:
        return fail(
            "--project requires --tenant-id; the Metadata_Index is partitioned "
            "on {tenant-id} (exact-match) with a {project-id} sort-key prefix, so "
            "a project query must name its tenant. See "
            "openspec/specs/metadata-index/spec.md.",
            exit_code=2,
        )

    if args.limit < 1 or args.limit > 100:
        return fail(f"--limit must be between 1 and 100 (got {args.limit})", exit_code=2)

    if args.control:
        if not args.tenant_id:
            return fail(
                "--control requires --tenant-id; the Control Claims table is "
                "partitioned on {tenant-id} (exact-match) with a "
                "{catalogOrProfileUuid}#{controlId} sort-key prefix, so a control "
                "query must name its tenant. See "
                "openspec/specs/producer-claims/spec.md.",
                exit_code=2,
            )
        if not args.catalog_or_profile_uuid:
            return fail(
                "--control requires --catalog-or-profile-uuid; the Control Claims "
                "table sort key leads with {catalogOrProfileUuid}#{controlId} so we "
                "cannot scope the query without the UUID.",
                exit_code=2,
            )
        if not _UUID_RE.match(args.catalog_or_profile_uuid):
            return fail(
                f"--catalog-or-profile-uuid {args.catalog_or_profile_uuid!r} is "
                "not a valid OSCAL UUID (type-4 or type-5 per OSCAL 1.x UUIDDatatype)",
                exit_code=2,
            )

    target = DeploymentTarget(stage=args.stage, region_code=args.region_code)
    if dynamodb_resource is None:
        dynamodb_resource = boto3.resource("dynamodb")

    # Resolve appName before composing the DynamoDB table names (they are
    # appName-prefixed under the uniform naming scheme). The SSM client is built
    # lazily ONLY when discovery is actually needed (no explicit --app-name /
    # CAIRN_APP_NAME), so an override path issues zero AWS calls.
    if ssm_client is None and not (args.app_name or os.environ.get(APP_NAME_ENV_VAR)):
        ssm_client = boto3.client("ssm")
    try:
        target = resolve_app_name(ssm_client, target, override=args.app_name)
    except AppNameUnresolvedError as e:
        return fail(str(e), exit_code=4)
    # Resolve the auto-named DynamoDB table physical names from SSM (best-effort;
    # falls back to the legacy name_for reconstruction if a param is absent).
    target = resolve_table_names(ssm_client, target)

    try:
        if args.project:
            page = _query_by_project(args, target, dynamodb_resource)
        else:
            page = _query_by_control(args, target, dynamodb_resource)
    except InvalidPaginationToken as e:
        return fail(f"invalid --page-token: {e}", exit_code=2)

    for item in page.items:
        json.dump(
            _annotate_trust(_annotate_alias(_annotate_pending(item))),
            sys.stdout,
            default=str,
            sort_keys=True,
        )
        sys.stdout.write("\n")
    if page.next_token:
        print(f"__next_token__: {page.next_token}", file=sys.stderr)
    return 0


def _annotate_pending(item: dict[str, Any]) -> dict[str, Any]:
    """Surface the pushed-but-not-yet-tagged window in the emitted record.

    A record that resolves by digest but has no canonical tag yet is in the
    eventual-consistency window. Rather than letting a downstream
    consumer misread the absent tag as a fault, the record is resolved by its
    digest anchor and tagged with ``tagPending: true`` + a human ``tagState``
    label. The original item is never mutated.
    """
    if not is_tag_pending(item):
        return item
    annotated = dict(item)
    annotated["tagPending"] = True
    annotated["tagState"] = TAG_PENDING_LABEL
    return annotated


def _annotate_alias(item: dict[str, Any]) -> dict[str, Any]:
    """Label a record whose ``imageTag`` is a mutable alias, keyed on the digest.

    A queried record reached via an alias tag (`_als`/`_chn`) is a *view* over an
    already-filed artifact; the verification guarantee lives on the
    ``manifestDigest``, never on the mutable alias string. When
    the record's tag parses to an alias form, surface ``reachedViaAlias: true`` +
    the ``aliasTag`` so a downstream consumer reads the handle as mutable and
    anchors on the digest. A canonical `_ref` tag, an absent/pending tag, or any
    tag the parser rejects is passed through untouched. The original item is
    never mutated.
    """
    alias_tag = alias_tag_of(item.get("imageTag"))
    if alias_tag is None:
        return item
    annotated = dict(item)
    annotated["reachedViaAlias"] = True
    annotated["aliasTag"] = alias_tag
    return annotated


def _annotate_trust(item: dict[str, Any]) -> dict[str, Any]:
    """Project the coarse ``signingStatus`` onto the two-signature trust verdict.

    A bundle is fully trusted only when BOTH the content signature and the
    attestation signature are present and valid. The Metadata_Index carries only
    the coarse projected ``signingStatus``
    (the writer sets it ``signed`` after the both-signatures promotion gate), so
    this maps that projection onto a ``trust`` label — ``fully-trusted`` /
    ``partial`` / ``unsigned`` — and tags ``trustSource: projection`` to mark it
    a non-authoritative index read. The authoritative two-signature verdict is a
    live re-verification via ``cairn verify``. The original
    item is never mutated. A record carrying no ``signingStatus`` at all (e.g. a
    synthetic unvalidated entry) is passed through untouched.
    """
    if "signingStatus" not in item:
        return item
    annotated = dict(item)
    annotated["trust"] = bundle_trust_from_projection(item.get("signingStatus")).value
    annotated["trustSource"] = "projection"
    return annotated


def _query_by_project(args: argparse.Namespace, target: DeploymentTarget, resource: Any):
    index = MetadataIndex(target.metadata_table, dynamodb_resource=resource)
    return index.query_by_project(
        tenant_id=args.tenant_id,
        project_id=args.project,
        page_token=args.page_token,
        limit=args.limit,
    )


def _query_by_control(args: argparse.Namespace, target: DeploymentTarget, resource: Any):
    index = ControlClaims(target.control_claims_table, dynamodb_resource=resource)
    return index.query_by_control(
        args.tenant_id,
        catalog_or_profile_uuid=args.catalog_or_profile_uuid,
        control_id=args.control,
        page_token=args.page_token,
        limit=args.limit,
    )

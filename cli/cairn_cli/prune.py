"""`cairn prune` — report Prune_Candidates and delete only the ones named.

The only sanctioned deletion path for artifacts (retention-lifecycle
[RETAIN-005]). Behavior:

- Without ``--digest`` it REPORTS every Prune_Candidate in the project (digest,
  class, reason) and every failed check, and deletes nothing. Any identity that
  can read the deployment can run it.
- With ``--digest`` (repeatable; explicit ``sha256:<64-hex>`` only — no
  wildcard or class-wide selector) it deletes those candidates, and only when
  the STS caller is a session of the project's pruner role ([ACCESS-007]).
  When the operator's credentials are an IAM user's, this command makes the
  ``sts:AssumeRole`` call itself: it prompts for the MFA code and sends
  ``SerialNumber``, ``TokenCode``, and ``SourceIdentity`` equal to the IAM user
  name, which the pruner trust policy requires. botocore's profile-based
  assume-role provider cannot send a source identity, so a profile
  ``role_arn`` cannot produce a pruner session. Credentials that already are
  a pruner session are used as they are.

For each named digest, immediately before its first delete: check that the
pruner session has enough lifetime left (M35), then classify again (M16). An
item that is no longer a candidate is refused and reported; the other named
items continue. A bundle is deleted in a fixed order ([RETAIN-010]): ECR
referrers, ECR manifest, every S3 object version and delete marker (the receipt
copy last, so an interrupted run can resume from it), ControlClaims rows, and
the Metadata_Index record. A failed step skips the item's later steps.

Every named item is audited ([RETAIN-011]) to the stack-owned prune audit log
group: a record before each delete step and after each outcome, naming the STS
caller identity. If an audit write fails, no further delete runs.

Exit codes: 0 all named items deleted (or a clean report); 1 an item was
refused or failed, a check failed, or an audit write failed; 2 usage error;
3 the caller is not a pruner session; 4 deployment discovery failed.
"""

from __future__ import annotations

import argparse
import functools
import getpass
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from cairn_cli._aws import (
    AppNameUnresolvedError,
    DeploymentTarget,
    resolve_app_name,
    resolve_table_names,
)
from cairn_cli._errors import fail
from cairn_cli._help import CairnHelpFormatter, build_epilog
from cairn_cli._prune_eligibility import (
    AWS_ERRORS,
    DIGEST_RE,
    RECEIPT_FILE,
    CheckError,
    Classification,
    Clients,
    Deployment,
    ItemClass,
    RunCache,
    S3Version,
    classify,
    list_versions,
    project_digests,
    prove_coordinates,
)
from cairn_cli.config import TargetResolutionError, resolve_target

#: Minimum pruner-session lifetime left before an item's first delete (M35).
MIN_SESSION_REMAINING = timedelta(minutes=5)
#: Pruner sessions last at most one hour ([ACCESS-007]).
PRUNER_SESSION_SECONDS = 3600

#: An IAM user ARN; group 3 is the user name (the value of ``aws:username``).
_IAM_USER_ARN_RE = re.compile(
    r"^arn:(aws(?:-[a-z]+)*):iam::(\d{12}):user/(?:[^/]+/)*([\w+=,.@-]+)$"
)
_MFA_CODE_RE = re.compile(r"^\d{6}$")
_WHOLE_NUMBER_RE = re.compile(r"[0-9]+")

#: [RETAIN-007] bounds, in days: the retention period and the
#: unarchived-manifest window.
RETENTION_PERIOD_BOUNDS = (1, 36_135)
WINDOW_BOUNDS = (1, 365)

EXIT_OK = 0
EXIT_ITEM_FAILED = 1
EXIT_USAGE = 2
EXIT_NOT_PRUNER = 3
EXIT_DISCOVERY = 4


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "prune",
        help="report Prune_Candidates; delete only explicitly named ones (pruner role)",
        description=(
            "Report every Prune_Candidate in a project (expired bundles and unarchived "
            "manifests) with its digest, class, and reason. With --digest, delete exactly "
            "the named candidates after re-checking eligibility; requires a pruner-role "
            "session. Every deletion is audited."
        ),
        formatter_class=CairnHelpFormatter,
        epilog=build_epilog(
            [
                "cairn prune --target acme-org/acme/payments",
                "cairn prune --target acme-org/acme/payments --digest sha256:<64-hex>",
            ],
            notes=(
                "Report mode deletes nothing. Delete mode accepts only explicit "
                "sha256:<64-hex> digests — no wildcard or class-wide selector. With IAM "
                "user credentials, delete mode assumes the pruner role itself and "
                "prompts for the MFA code."
            ),
        ),
    )
    p.add_argument("--target", help="org-id/tenant-id/project-id (else env/config)")
    p.add_argument("--org-id")
    p.add_argument("--tenant-id")
    p.add_argument("--project-id")
    p.add_argument(
        "--digest",
        action="append",
        default=[],
        help="a Prune_Candidate to delete (sha256:<64-hex>); repeat for several",
    )
    p.add_argument(
        "--mfa-serial",
        default=None,
        help="MFA device ARN for the pruner assume-role call "
        "(default arn:<partition>:iam::<account>:mfa/<iam-user>)",
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
    return _run(args, session=None, now=None, prompt=None)


# ── output ───────────────────────────────────────────────────────────────────


def _safe(text: object) -> str:
    """Escape control characters so producer-written data cannot drive the terminal."""
    return "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in str(text))


def _out(line: str) -> None:
    print(_safe(line))


# ── audit ────────────────────────────────────────────────────────────────────


class AuditWriteError(RuntimeError):
    """An audit record could not be written; no further delete may run."""


@dataclass
class AuditLog:
    """Append-only writer for the stack-owned prune audit log group ([RETAIN-011])."""

    logs: Any
    group: str
    caller: dict[str, str]
    clock: Callable[[], datetime]
    stream: str = ""

    def open(self) -> None:
        self.stream = f"{self.clock().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex}"
        try:
            self.logs.create_log_stream(logGroupName=self.group, logStreamName=self.stream)
        except AWS_ERRORS as e:
            raise AuditWriteError(f"cannot create audit stream in {self.group}: {e}") from e

    def write(
        self,
        *,
        digest: str,
        item_class: ItemClass | None,
        reason: str,
        step: str,
        outcome: str,
        detail: str | None = None,
        deleted: list[str] | None = None,
    ) -> None:
        """Write one record. ``deleted`` names what the step removed: ECR image
        digests, S3 ``{key}?versionId={id}`` pairs, or DynamoDB ``{pk}/{sk}`` keys.
        """
        now = self.clock()
        record = {
            "timestamp": now.isoformat(),
            "callerArn": self.caller.get("Arn"),
            "callerUserId": self.caller.get("UserId"),
            "callerAccount": self.caller.get("Account"),
            "digest": digest,
            "class": item_class.value if item_class else None,
            "reason": reason,
            "step": step,
            "outcome": outcome,
        }
        if detail:
            record["detail"] = detail
        if deleted is not None:
            record["deleted"] = deleted
        try:
            resp = self.logs.put_log_events(
                logGroupName=self.group,
                logStreamName=self.stream,
                logEvents=[
                    {"timestamp": int(now.timestamp() * 1000), "message": json.dumps(record)}
                ],
            )
        except AWS_ERRORS as e:
            raise AuditWriteError(f"audit write failed ({step}/{outcome}): {e}") from e
        if resp.get("rejectedLogEventsInfo"):
            raise AuditWriteError(
                f"audit write rejected ({step}/{outcome}): {resp['rejectedLogEventsInfo']}"
            )


# ── session ──────────────────────────────────────────────────────────────────


def _is_pruner_session(caller_arn: str, pruner_role_arn: str) -> bool:
    """True iff ``caller_arn`` is an STS session of exactly ``pruner_role_arn``."""
    try:
        prefix, role_path = pruner_role_arn.split(":role/", 1)
        _arn, partition, _iam, _region, account = prefix.split(":")
    except ValueError:
        return False
    role_name = role_path.rsplit("/", 1)[-1]
    expected = f"arn:{partition}:sts::{account}:assumed-role/{role_name}/"
    return caller_arn.startswith(expected) and len(caller_arn) > len(expected)


class PrunerSessionError(RuntimeError):
    """A pruner session could not be obtained."""


def _session_from_credentials(base: Any, creds: dict[str, Any]) -> Any:
    """A boto3 session over temporary credentials, in the base session's region."""
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=getattr(base, "region_name", None),
    )


def _assume_pruner_role(
    base: Any,
    pruner_role_arn: str,
    caller_arn: str,
    *,
    mfa_serial: str | None,
    prompt: Callable[[str], str],
) -> tuple[Any, datetime]:
    """Assume the pruner role as the calling IAM user ([ACCESS-007]).

    The trust policy requires MFA and ``sts:SourceIdentity`` equal to
    ``${aws:username}``, so the source identity and session name are the IAM
    user name taken from the caller's own ARN. Returns the pruner session and
    its expiry.
    """
    match = _IAM_USER_ARN_RE.fullmatch(caller_arn)
    if not match:
        raise PrunerSessionError(
            f"caller {_safe(caller_arn)} is not a session of the pruner role "
            f"{pruner_role_arn} and is not an IAM user, so it cannot assume that role "
            "([ACCESS-007])"
        )
    partition, account, user = match.groups()
    serial = mfa_serial or f"arn:{partition}:iam::{account}:mfa/{user}"
    code = prompt(f"MFA code for {serial}: ").strip()
    if not _MFA_CODE_RE.fullmatch(code):
        raise PrunerSessionError("the MFA code must be six digits")
    try:
        resp = base.client("sts").assume_role(
            RoleArn=pruner_role_arn,
            RoleSessionName=user,
            SourceIdentity=user,
            SerialNumber=serial,
            TokenCode=code,
            DurationSeconds=PRUNER_SESSION_SECONDS,
        )
    except AWS_ERRORS as e:
        raise PrunerSessionError(f"cannot assume the pruner role {pruner_role_arn}: {e}") from e
    creds = resp["Credentials"]
    expiry = creds["Expiration"]
    if not isinstance(expiry, datetime):
        raise PrunerSessionError("STS AssumeRole returned no credential expiry")
    expiry = expiry if expiry.tzinfo else expiry.replace(tzinfo=UTC)
    return _session_from_credentials(base, creds), expiry


def _session_expiry(session: Any) -> datetime | None:
    """Expiry of the session's (refreshable) credentials, if boto3 knows it."""
    creds = session.get_credentials() if session is not None else None
    expiry = getattr(creds, "_expiry_time", None)
    if isinstance(expiry, datetime):
        return expiry if expiry.tzinfo else expiry.replace(tzinfo=UTC)
    return None


# ── deletion ─────────────────────────────────────────────────────────────────


class StepFailed(RuntimeError):
    pass


def _delete_images(ecr: Any, repository: str, digests: list[str], deleted: list[str]) -> None:
    """Delete images one at a time in the given order; absent images are fine.

    Appends each digest ECR reports as deleted to ``deleted`` ([RETAIN-011]).
    """
    for digest in digests:
        try:
            resp = ecr.batch_delete_image(
                repositoryName=repository, imageIds=[{"imageDigest": digest}]
            )
        except AWS_ERRORS as e:
            raise StepFailed(f"BatchDeleteImage {digest}: {e}") from e
        for failure in resp.get("failures") or []:
            if failure.get("failureCode") != "ImageNotFound":
                raise StepFailed(
                    f"BatchDeleteImage {digest}: {failure.get('failureCode')} "
                    f"{failure.get('failureReason', '')}".strip()
                )
        deleted.extend(
            str(i["imageDigest"]) for i in resp.get("imageIds") or [] if i.get("imageDigest")
        )


def _delete_versions(clients: Clients, dep: Deployment, digest: str, deleted: list[str]) -> None:
    """Delete every object version and delete marker; the receipt copy last.

    Appends ``{key}?versionId={id}`` for each deleted version to ``deleted``
    ([RETAIN-011]).
    """
    prefix = dep.prefix(digest)
    try:
        versions = list_versions(clients, dep, prefix)
    except CheckError as e:
        raise StepFailed(str(e)) from e
    receipt_key = f"{prefix}{RECEIPT_FILE}"
    ordered: list[S3Version] = [v for v in versions if v.key != receipt_key]
    ordered += [v for v in versions if v.key == receipt_key]
    for v in ordered:
        try:
            clients.s3.delete_object(Bucket=dep.bucket, Key=v.key, VersionId=v.version_id)
        except AWS_ERRORS as e:
            raise StepFailed(f"DeleteObject {v.key} version {v.version_id}: {e}") from e
        deleted.append(f"{v.key}?versionId={v.version_id}")
    # Keep the index record until S3 reports zero remaining versions.
    try:
        remaining = list_versions(clients, dep, prefix)
    except CheckError as e:
        raise StepFailed(str(e)) from e
    if remaining:
        raise StepFailed(f"{len(remaining)} object version(s) remain under {prefix}")


def _control_claim_keys(clients: Clients, dep: Deployment, digest: str) -> list[dict[str, Any]]:
    keys: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "TableName": dep.controls_table,
        "KeyConditionExpression": "pk = :pk",
        "FilterExpression": "manifestDigest = :d",
        "ExpressionAttributeValues": {":pk": {"S": dep.tenant_id}, ":d": {"S": digest}},
        "ProjectionExpression": "pk, sk",
        "ConsistentRead": True,
    }
    while True:
        try:
            resp = clients.ddb.query(**kwargs)
        except AWS_ERRORS as e:
            raise StepFailed(f"ControlClaims Query: {e}") from e
        keys.extend({"pk": item["pk"], "sk": item["sk"]} for item in resp.get("Items") or [])
        if not resp.get("LastEvaluatedKey"):
            return keys
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _delete_items(
    ddb: Any, table: str, keys: list[dict[str, Any]], label: str, deleted: list[str]
) -> None:
    for key in keys:
        try:
            ddb.delete_item(TableName=table, Key=key)
        except AWS_ERRORS as e:
            raise StepFailed(f"{label} DeleteItem {key['sk']['S']}: {e}") from e
        deleted.append(f"{key['pk']['S']}/{key['sk']['S']}")


#: One delete step: it appends what it deleted to the list it is given.
_Step = tuple[str, Callable[[list[str]], object]]


def _steps(clients: Clients, dep: Deployment, c: Classification) -> list[_Step]:
    """The ordered delete steps for one classified item ([RETAIN-010])."""
    digest = c.digest
    repo = dep.repository_name
    if c.item_class is ItemClass.UNARCHIVED_MANIFEST:
        return [("ecr-manifest", lambda out: _delete_images(clients.ecr, repo, [digest], out))]
    steps: list[_Step] = []
    if c.referrer_digests:
        steps.append(
            (
                "ecr-referrers",
                lambda out: _delete_images(clients.ecr, repo, list(c.referrer_digests), out),
            )
        )
    if c.ecr_present:
        steps.append(("ecr-manifest", lambda out: _delete_images(clients.ecr, repo, [digest], out)))
    steps.append(("s3-versions", lambda out: _delete_versions(clients, dep, digest, out)))
    steps.append(
        (
            "control-claims",
            lambda out: _delete_items(
                clients.ddb,
                dep.controls_table,
                _control_claim_keys(clients, dep, digest),
                "ControlClaims",
                out,
            ),
        )
    )
    steps.append(
        (
            "index-record",
            lambda out: _delete_items(
                clients.ddb, dep.metadata_table, [dep.index_key(digest)], "Metadata_Index", out
            ),
        )
    )
    return steps


@dataclass
class _PruneContext:
    clients: Clients
    dep: Deployment
    audit: AuditLog
    clock: Callable[[], datetime]
    session_expiry: Callable[[], datetime | None]


def _prune_one(ctx: _PruneContext, digest: str) -> str:
    """Prune one named digest. Returns ``deleted`` / ``refused`` / ``failed``.

    Raises :class:`AuditWriteError` when an audit record cannot be written.
    """
    expiry = ctx.session_expiry()
    now = ctx.clock()
    if expiry is None or expiry - now < MIN_SESSION_REMAINING:
        reason = (
            "cannot determine the pruner session's remaining lifetime"
            if expiry is None
            else f"pruner session expires at {expiry.isoformat()}; re-run with a fresh session"
        )
        ctx.audit.write(
            digest=digest, item_class=None, reason=reason, step="preflight", outcome="refused"
        )
        _out(f"refused  {digest}  {reason}")
        return "refused"

    # Each item reads fresh: an earlier item's deletes change the index and S3.
    unproven = prove_coordinates(ctx.clients, ctx.dep)
    c = classify(ctx.clients, ctx.dep, digest, now=now, coordinates_unproven=unproven)
    if not c.eligible:
        ctx.audit.write(
            digest=digest,
            item_class=c.item_class,
            reason=c.reason,
            step="classify",
            outcome="refused",
        )
        _out(f"refused  {digest}  {c.reason}")
        return "refused"

    audit = functools.partial(
        ctx.audit.write, digest=digest, item_class=c.item_class, reason=c.reason
    )
    for step, action in _steps(ctx.clients, ctx.dep, c):
        audit(step=step, outcome="starting")
        deleted: list[str] = []
        try:
            action(deleted)
        except (StepFailed, *AWS_ERRORS) as e:
            # A boto3 error the step did not wrap is still a failed step: it is
            # audited and reported, never an unaudited traceback.
            audit(step=step, outcome="failed", detail=str(e), deleted=deleted)
            _out(f"failed   {digest}  step {step}: {e}")
            return "failed"
        audit(step=step, outcome="done", deleted=deleted)
    audit(step="item", outcome="deleted")
    _out(f"deleted  {digest}  {c.item_class.value if c.item_class else ''}  {c.reason}")
    return "deleted"


# ── discovery ────────────────────────────────────────────────────────────────


class DiscoveryError(RuntimeError):
    pass


def _param(ssm: Any, name: str) -> str:
    try:
        value = ssm.get_parameter(Name=name)["Parameter"]["Value"].strip()
    except (ClientError, BotoCoreError, KeyError) as e:
        raise DiscoveryError(f"cannot read SSM parameter {name}: {e}") from e
    if not value:
        raise DiscoveryError(f"SSM parameter {name} is empty")
    return value


def _int_param(ssm: Any, name: str, *, low: int, high: int) -> int:
    """A whole-number setting in ``[low, high]`` ([RETAIN-007]).

    ASCII digits only: ``str.isdigit`` also accepts characters such as ``²``
    that ``int()`` rejects.
    """
    value = _param(ssm, name)
    if not _WHOLE_NUMBER_RE.fullmatch(value):
        raise DiscoveryError(f"SSM parameter {name} is not a whole number: {value!r}")
    number = int(value)
    if not low <= number <= high:
        raise DiscoveryError(
            f"SSM parameter {name} is {number}, outside the bounds {low} to {high} ([RETAIN-007])"
        )
    return number


def _worm_enabled(s3: Any, bucket: str) -> bool:
    """WORM_Enabled from the bucket itself: a positive Object Lock answer."""
    try:
        resp = s3.get_object_lock_configuration(Bucket=bucket)
    except AWS_ERRORS as e:
        if (
            isinstance(e, ClientError)
            and e.response.get("Error", {}).get("Code") == "ObjectLockConfigurationNotFoundError"
        ):
            return False
        raise DiscoveryError(f"cannot read Object Lock configuration of {bucket}: {e}") from e
    return (resp.get("ObjectLockConfiguration") or {}).get("ObjectLockEnabled") == "Enabled"


def _ssm_path(target: DeploymentTarget, suffix: str) -> str:
    return f"/{target.app_name}/{target.stage}/{suffix}"


# ── entry point ──────────────────────────────────────────────────────────────


def _validate_digests(raw: list[str]) -> list[str] | str:
    """Explicit digests only ([RETAIN-005] item 2). Returns the list or an error."""
    for d in raw:
        if not DIGEST_RE.fullmatch(d):
            return (
                f"--digest {d!r} is not an explicit sha256:<64-lowercase-hex> digest; "
                "wildcards and class-wide selectors are not accepted"
            )
    return list(dict.fromkeys(raw))


def _run(
    args: argparse.Namespace,
    *,
    session: Any,
    now: Callable[[], datetime] | None,
    prompt: Callable[[str], str] | None = None,
) -> int:
    clock = now or (lambda: datetime.now(UTC))
    digests = _validate_digests(args.digest)
    if isinstance(digests, str):
        return fail(digests, exit_code=EXIT_USAGE)

    try:
        coords = resolve_target(
            org_id=args.org_id,
            tenant_id=args.tenant_id,
            project_id=args.project_id,
            target=args.target,
        )
    except TargetResolutionError as e:
        return fail(str(e), exit_code=EXIT_USAGE)
    org_id = coords["org-id"].value
    tenant_id = coords["tenant-id"].value
    project_id = coords["project-id"].value

    session = session or boto3.Session()
    ssm = session.client("ssm")
    target = DeploymentTarget(stage=args.stage, region_code=args.region_code)
    try:
        target = resolve_app_name(ssm, target, override=args.app_name)
    except AppNameUnresolvedError as e:
        return fail(str(e), exit_code=EXIT_DISCOVERY)
    target = resolve_table_names(ssm, target)
    clients = Clients(
        ecr=session.client("ecr"), s3=session.client("s3"), ddb=session.client("dynamodb")
    )
    bucket = target.name_for("artifact-archive")
    try:
        dep = Deployment(
            org_id=org_id,
            tenant_id=tenant_id,
            project_id=project_id,
            repository_name=f"{target.artifact_namespace}/{org_id}/{tenant_id}/{project_id}",
            bucket=bucket,
            metadata_table=target.metadata_table,
            controls_table=target.control_claims_table,
            worm_enabled=_worm_enabled(clients.s3, bucket),
            retention_period_days=_int_param(
                ssm,
                _ssm_path(target, "retention/period-days"),
                low=RETENTION_PERIOD_BOUNDS[0],
                high=RETENTION_PERIOD_BOUNDS[1],
            ),
            window_days=_int_param(
                ssm,
                _ssm_path(target, "retention/unarchived-manifest-window-days"),
                low=WINDOW_BOUNDS[0],
                high=WINDOW_BOUNDS[1],
            ),
        )
    except DiscoveryError as e:
        return fail(str(e), exit_code=EXIT_DISCOVERY)

    if not digests:
        return _report(clients, dep, clock())
    return _delete(
        session,
        ssm,
        target,
        dep,
        digests,
        clock,
        mfa_serial=args.mfa_serial,
        prompt=prompt or getpass.getpass,
    )


def _report(clients: Clients, dep: Deployment, now: datetime) -> int:
    """[RETAIN-005] item 1: list every Prune_Candidate; delete nothing."""
    try:
        digests = project_digests(clients, dep)
    except CheckError as e:
        return fail(f"cannot enumerate the project: {e}", exit_code=EXIT_ITEM_FAILED)
    unproven = prove_coordinates(clients, dep)
    # Report mode deletes nothing, so one index read serves every item.
    cache = RunCache()
    candidates: list[Classification] = []
    failures: list[Classification] = []
    for d in digests:
        c = classify(clients, dep, d, now=now, coordinates_unproven=unproven, cache=cache)
        if c.eligible:
            candidates.append(c)
        elif c.check_failed:
            failures.append(c)
    _out(f"Prune_Candidates in {dep.project_prefix.rstrip('/')}: {len(candidates)}")
    for c in candidates:
        _out(f"{c.digest}  {c.item_class.value if c.item_class else ''}  {c.reason}")
    if failures:
        _out(f"Checks that failed (items not classified as candidates): {len(failures)}")
        for c in failures:
            _out(f"{c.digest}  {c.reason}")
    if candidates:
        _out(
            "Delete a candidate with: cairn prune --target "
            f"{dep.project_prefix.rstrip('/')} --digest <digest>  (pruner-role session)"
        )
    return EXIT_ITEM_FAILED if failures else EXIT_OK


_NOT_PRUNER_HINT = (
    "Run delete mode with IAM user credentials named in the pruner trust policy "
    "(cairn prune prompts for the MFA code), or with an existing pruner-role session "
    "(see runbooks/deploy-alpha.md); report mode needs no pruner session."
)


def _caller(session: Any) -> dict[str, Any]:
    try:
        return session.client("sts").get_caller_identity()
    except AWS_ERRORS as e:
        raise PrunerSessionError(f"cannot read the STS caller identity: {e}") from e


def _pruner_session(
    base: Any,
    pruner_role_arn: str,
    *,
    mfa_serial: str | None,
    prompt: Callable[[str], str],
) -> tuple[Any, dict[str, Any], Callable[[], datetime | None]]:
    """The pruner session, its STS caller identity, and its expiry reader."""
    caller = _caller(base)
    caller_arn = str(caller.get("Arn") or "")
    if _is_pruner_session(caller_arn, pruner_role_arn):
        return base, caller, lambda: _session_expiry(base)
    session, expiry = _assume_pruner_role(
        base, pruner_role_arn, caller_arn, mfa_serial=mfa_serial, prompt=prompt
    )
    caller = _caller(session)
    assumed_arn = str(caller.get("Arn") or "")
    if not _is_pruner_session(assumed_arn, pruner_role_arn):
        raise PrunerSessionError(
            f"assumed session {_safe(assumed_arn)} is not a session of the pruner role "
            f"{pruner_role_arn}"
        )
    return session, caller, lambda: expiry


def _delete(
    base: Any,
    ssm: Any,
    target: DeploymentTarget,
    dep: Deployment,
    digests: list[str],
    clock: Callable[[], datetime],
    *,
    mfa_serial: str | None,
    prompt: Callable[[str], str],
) -> int:
    try:
        pruner_role_arn = _param(ssm, _ssm_path(target, "prune/pruner-role-arn"))
        audit_group = _param(ssm, _ssm_path(target, "prune/audit-log-group"))
    except DiscoveryError as e:
        return fail(str(e), exit_code=EXIT_DISCOVERY)
    try:
        session, caller, expiry = _pruner_session(
            base, pruner_role_arn, mfa_serial=mfa_serial, prompt=prompt
        )
    except PrunerSessionError as e:
        return fail(f"refusing to delete: {e}", exit_code=EXIT_NOT_PRUNER, hint=_NOT_PRUNER_HINT)
    # Every classification read and delete runs as the pruner session.
    clients = Clients(
        ecr=session.client("ecr"), s3=session.client("s3"), ddb=session.client("dynamodb")
    )
    audit = AuditLog(logs=session.client("logs"), group=audit_group, caller=caller, clock=clock)
    ctx = _PruneContext(
        clients=clients,
        dep=dep,
        audit=audit,
        clock=clock,
        session_expiry=expiry,
    )
    try:
        audit.open()
        outcomes = [_prune_one(ctx, d) for d in digests]
    except AuditWriteError as e:
        return fail(
            f"{e}; no further delete was performed ([RETAIN-011])", exit_code=EXIT_ITEM_FAILED
        )
    return EXIT_OK if all(o == "deleted" for o in outcomes) else EXIT_ITEM_FAILED

"""Integration-test scaffolding for the cmh-alpha Cairn Proofs deployment.

Tests under this tree exercise a *real* AWS deployment end-to-end and are
skipped by default. Enable by exporting ``CAIRN_INTEGRATION_ENV=cmh-alpha``
(or another deployment qualifier) before invoking pytest.

Fixtures discovered here:

- ``integration_env``         deployment qualifier read from
                              ``CAIRN_INTEGRATION_ENV`` (session-scoped)
- ``stack_outputs``           dict of resource names / ARNs resolved from
                              the CDK stack outputs (session-scoped)
- ``aws_session``             ``boto3.Session`` pinned to ``us-east-2``
                              (session-scoped)
- ``cairn_cli``               sub-process helper for invoking ``cairn``
                              from the workspace (session-scoped)
- ``target_org_id`` /
  ``target_tenant_id`` /
  ``target_project_id``       the three opaque identity coordinates the suite
                              pushes under, read from
                              ``CAIRN_INTEGRATION_ORG_ID`` /
                              ``CAIRN_INTEGRATION_TENANT_ID`` /
                              ``CAIRN_INTEGRATION_PROJECT_ID`` (defaults
                              ``mvp-org`` / ``mvp-tenant`` / ``mvp-project``,
                              the stack's configured org/tenant/project)
                              (session-scoped)
- ``shared_repo_name``        the pre-provisioned per-project ECR repo the
                              whole suite reuses; describe-not-create — skips
                              the suite if it is absent (session-scoped)
- ``bundle_cleanup``          tracker that deletes ECR manifests +
                              Metadata_Index rows + S3 keys created during a
                              test, even on failure; never deletes a
                              repository. ECR deletes run as the
                              non-production integration-test cleanup role,
                              the only principal besides the pruner that the
                              stack-owned repository policy admits for
                              ``ecr:BatchDeleteImage`` ([ACCESS-010])
                              (function-scoped)
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

_INTEGRATION_ENV_VAR = "CAIRN_INTEGRATION_ENV"
_ORG_ID_VAR = "CAIRN_INTEGRATION_ORG_ID"
_TENANT_ID_VAR = "CAIRN_INTEGRATION_TENANT_ID"
_PROJECT_ID_VAR = "CAIRN_INTEGRATION_PROJECT_ID"
_DEFAULT_ORG_ID = "mvp-org"
_DEFAULT_TENANT_ID = "mvp-tenant"
_DEFAULT_PROJECT_ID = "mvp-project"
_STACK_NAME_TEMPLATE = "cairnproofs4af-{qualifier}"
_REGION = "us-east-2"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every integration test unless ``CAIRN_INTEGRATION_ENV`` is set.

    The parent ``tests/conftest.py`` injects placeholder AWS credentials via an
    autouse fixture; without this gate every integration test would attempt
    to call real AWS APIs with those placeholders and fail noisily.
    """
    if os.environ.get(_INTEGRATION_ENV_VAR):
        return
    reason = f"{_INTEGRATION_ENV_VAR} not set; integration tests require a real deployment"
    skip_marker = pytest.mark.skip(reason=reason)
    integration_root = Path(__file__).resolve().parent
    for item in items:
        try:
            item_path = Path(str(item.fspath)).resolve()
        except (OSError, ValueError):
            continue
        if integration_root in item_path.parents or item_path == integration_root:
            item.add_marker(skip_marker)


@pytest.fixture(autouse=True)
def _restore_real_aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the placeholder credentials set by ``tests/conftest.py`` so that
    integration tests resolve real credentials from the caller's environment
    or shared config files.

    Runs for every integration test regardless of whether the skip gate
    above fired — pytest still evaluates fixtures for skipped items in some
    configurations, and a no-op when skipped costs nothing.
    """
    for var in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)


@pytest.fixture(scope="session")
def integration_env() -> str:
    value = os.environ.get(_INTEGRATION_ENV_VAR)
    if not value:
        pytest.skip(f"{_INTEGRATION_ENV_VAR} not set")
    return value


@pytest.fixture(scope="session")
def aws_session(integration_env: str) -> Any:
    import boto3

    return boto3.Session(region_name=_REGION)


@dataclass(frozen=True)
class StackOutputs:
    """Resource handles required by the integration suite.

    Resolved once per session from CloudFormation stack outputs +
    deterministic naming conventions (see ``cdk/lib/constructs/``).
    """

    qualifier: str
    stack_name: str
    account_id: str
    region: str
    ecr_registry_host: str
    archive_bucket: str
    metadata_table: str
    control_claims_table: str
    event_bus_name: str
    validate_bundle_dlq_url: str
    sign_bundle_dlq_url: str
    archive_to_s3_dlq_url: str
    update_metadata_dlq_url: str
    integrity_mismatch_alarm_name: str
    signing_failure_alarm_name: str
    archive_failure_alarm_name: str
    # ARN of the non-production integration-test cleanup role ([ACCESS-010]),
    # from SSM. ``None`` when the deployment publishes no such role.
    cleanup_role_arn: str | None = None


@pytest.fixture(scope="session")
def stack_outputs(integration_env: str, aws_session: Any) -> StackOutputs:
    qualifier = integration_env
    stack_name = _STACK_NAME_TEMPLATE.format(qualifier=qualifier)

    cfn = aws_session.client("cloudformation")
    sts = aws_session.client("sts")
    sqs = aws_session.client("sqs")
    ssm = aws_session.client("ssm")

    description = cfn.describe_stacks(StackName=stack_name)
    outputs = {
        item["OutputKey"]: item["OutputValue"]
        for item in description["Stacks"][0].get("Outputs", [])
    }
    account_id = sts.get_caller_identity()["Account"]
    ecr_registry_host = outputs.get(
        "EcrRegistryHost", f"{account_id}.dkr.ecr.{_REGION}.amazonaws.com"
    )

    def _queue_url(queue_name: str) -> str:
        return sqs.get_queue_url(QueueName=queue_name)["QueueUrl"]

    # DynamoDB tables are auto-named (no explicit TableName), so their physical
    # names are NOT reconstructable from the naming convention — read them from
    # the deterministic SSM discovery parameters the stack publishes
    # (/{appName}/{stage}/tables/*), exactly as the cairn CLI/TUI now do.
    stage = qualifier.rsplit("-", 1)[-1]

    def _table_name(suffix: str) -> str:
        return ssm.get_parameter(Name=f"/cairnproofs4af/{stage}/tables/{suffix}")["Parameter"][
            "Value"
        ].strip()

    def _cleanup_role_arn() -> str | None:
        try:
            value = ssm.get_parameter(Name=f"/cairnproofs4af/{stage}/integration/cleanup-role-arn")
        except ssm.exceptions.ParameterNotFound:
            return None
        return value["Parameter"]["Value"].strip() or None

    return StackOutputs(
        qualifier=qualifier,
        stack_name=stack_name,
        account_id=account_id,
        region=_REGION,
        ecr_registry_host=ecr_registry_host,
        archive_bucket=f"cairnproofs4af-artifact-archive-{qualifier}",
        metadata_table=_table_name("metadata"),
        control_claims_table=_table_name("control-claims"),
        event_bus_name=f"cairnproofs4af-artifact-{qualifier}",
        validate_bundle_dlq_url=_queue_url(f"cairnproofs4af-validate-bundle-dlq-{qualifier}"),
        sign_bundle_dlq_url=_queue_url(f"cairnproofs4af-sign-bundle-dlq-{qualifier}"),
        archive_to_s3_dlq_url=_queue_url(f"cairnproofs4af-archive-to-s3-dlq-{qualifier}"),
        update_metadata_dlq_url=_queue_url(f"cairnproofs4af-update-metadata-dlq-{qualifier}"),
        integrity_mismatch_alarm_name=f"cairn-IntegrityMismatch-{qualifier}",
        signing_failure_alarm_name=f"cairn-SigningFailure-{qualifier}",
        archive_failure_alarm_name=f"cairn-ArchiveFailure-{qualifier}",
        cleanup_role_arn=_cleanup_role_arn(),
    )


@pytest.fixture(scope="session")
def target_org_id() -> str:
    """Org the whole suite pushes under (the leading identity coordinate).

    Read from ``CAIRN_INTEGRATION_ORG_ID``; defaults to the stack's configured
    org ``mvp-org`` (``cdk/bin/artifact-store.ts``). The deployment provisions
    exactly one ECR repository per ``{org}/{tenant}/{project}`` triple, so
    reusing the configured triple gives every test a pre-provisioned target
    with no per-test repo creation.
    """
    return os.environ.get(_ORG_ID_VAR, _DEFAULT_ORG_ID)


@pytest.fixture(scope="session")
def target_tenant_id() -> str:
    """Tenant the whole suite pushes under (the middle identity coordinate).

    Read from ``CAIRN_INTEGRATION_TENANT_ID``; defaults to the stack's
    configured tenant ``mvp-tenant`` (``cdk/bin/artifact-store.ts``).
    """
    return os.environ.get(_TENANT_ID_VAR, _DEFAULT_TENANT_ID)


@pytest.fixture(scope="session")
def target_project_id() -> str:
    """Project the whole suite pushes under (the trailing identity coordinate).

    Read from ``CAIRN_INTEGRATION_PROJECT_ID``; defaults to the stack's
    configured project ``mvp-project`` (``cdk/bin/artifact-store.ts``).
    The deployment provisions exactly one ECR repository per
    ``{org}/{tenant}/{project}`` triple, so reusing the configured project
    gives every test a pre-provisioned target with no per-test repo creation.
    """
    return os.environ.get(_PROJECT_ID_VAR, _DEFAULT_PROJECT_ID)


@pytest.fixture(scope="session")
def shared_repo_name(
    stack_outputs: StackOutputs,
    target_org_id: str,
    target_tenant_id: str,
    target_project_id: str,
    aws_session: Any,
) -> str:
    """Resolve and verify the pre-provisioned per-project ECR repository.

    ``ArtifactLocator.repo_name`` yields the uniform three-segment logical
    hierarchy ``artifact-{qualifier}/{org-id}/{tenant-id}/{project-id}`` — the
    artifact-kind collapses into the image tag rather
    than the repo path, so one repository per ``{org}/{tenant}/{project}``
    triple serves all artifact kinds. This guard **describes, never creates**:
    it asserts the repo exists once per session and ``pytest.skip``s the suite
    with a clear message if it is absent (no fallback to
    creating/guessing a repo).
    """
    from cairn_cli._aws import DeploymentTarget, resolve_app_name
    from cairn_oci import ArtifactLocator

    bare = ArtifactLocator().repo_name(
        deployment_qualifier=stack_outputs.qualifier,
        org_id=target_org_id,
        tenant_id=target_tenant_id,
        project_id=target_project_id,
    )
    # Compose the {appName}- namespace the way the production CLI does, threading
    # through the single composition point (DeploymentTarget.repo_path) so the
    # harness and the push path cannot drift on naming. appName resolves
    # from the same SSM param the deployment writes (/cairnproofs/{qualifier}/
    # app-name); resolve_app_name honors CAIRN_APP_NAME first (the moto seam).
    # Split only to satisfy DeploymentTarget(stage, region_code); repo_path below
    # consumes only the recombined .qualifier, so this round-trips losslessly for
    # any hyphenation. Don't read target.stage / target.region_code off this — a
    # multi-hyphen qualifier ("us-east-2") would misattribute them.
    region_code, stage = stack_outputs.qualifier.split("-", 1)  # "cmh-alpha" -> ("cmh", "alpha")
    target = resolve_app_name(
        aws_session.client("ssm"), DeploymentTarget(stage=stage, region_code=region_code)
    )
    # -> cairnproofs4af-artifact-cmh-alpha/mvp-org/mvp-tenant/mvp-project
    repo_name = target.repo_path(bare)
    ecr = aws_session.client("ecr")
    try:
        ecr.describe_repositories(repositoryNames=[repo_name])
    except ecr.exceptions.RepositoryNotFoundException:
        pytest.skip(
            f"pre-provisioned ECR repository {repo_name!r} is absent in "
            f"deployment {stack_outputs.qualifier!r}; the integration suite "
            f"reuses the stack's per-project repo and never creates one "
            f"(set {_ORG_ID_VAR}/{_TENANT_ID_VAR}/{_PROJECT_ID_VAR} to a "
            f"provisioned triple, default "
            f"{_DEFAULT_ORG_ID!r}/{_DEFAULT_TENANT_ID!r}/{_DEFAULT_PROJECT_ID!r})"
        )
    return repo_name


@dataclass(frozen=True)
class CliResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass
class CairnCli:
    """Sub-process wrapper around the workspace ``cairn`` CLI.

    Tests invoke ``cairn(...)`` to run the CLI under ``uv run`` so the
    sub-process inherits the same workspace virtual environment without a
    second install pass.
    """

    workspace_root: Path

    def __call__(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        check: bool = False,
    ) -> CliResult:
        cmd = ["uv", "run", "cairn", *args]
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)
        completed = subprocess.run(
            cmd,
            cwd=self.workspace_root,
            env=proc_env,
            check=False,
            capture_output=True,
            text=True,
        )
        result = CliResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"cairn {' '.join(args)} exited {result.returncode}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result


@pytest.fixture(scope="session")
def cairn_cli(integration_env: str) -> CairnCli:
    workspace_root = Path(__file__).resolve().parents[2]
    return CairnCli(workspace_root=workspace_root)


@dataclass
class BundleCleanup:
    """Tracks ECR manifests, Metadata_Index rows, and S3 keys created by
    per-test setup, so each test leaves zero residue.

    Cleanup is strictly tracker-scoped targeted-delete: only the exact
    ``(repo, reference)`` / ``(pk, sk)`` / S3 key a test recorded as created
    is deleted. It never enumerates-and-deletes, and it never
    deletes the shared, pre-provisioned repository itself — the suite
    describes, never creates or destroys, a repo."""

    aws_session: Any
    stack: StackOutputs
    # Builds a boto3 session from assumed-role credentials (seam for unit tests).
    session_factory: Callable[..., Any] | None = None
    ecr_refs: list[tuple[str, str]] = field(default_factory=list)
    metadata_keys: list[tuple[str, str]] = field(default_factory=list)
    s3_keys: list[str] = field(default_factory=list)

    def track_ecr_manifest(self, repo_path: str, reference: str) -> None:
        self.ecr_refs.append((repo_path, reference))

    def track_metadata_row(self, tenant_id: str, project_id: str, manifest_digest: str) -> None:
        """Record a Metadata_Index row for targeted teardown.

        Stores the row's tenant-PK key (pk = ``{tenant-id}``,
        sk = ``{project-id}#{manifest-digest}``) built via
        ``cairn_metadata.keys`` so cleanup deletes the exact row the pipeline
        wrote ([META-001]).
        """
        from cairn_metadata import keys as _mkeys

        self.metadata_keys.append(
            (_mkeys.partition_key(tenant_id), _mkeys.sort_key(project_id, manifest_digest))
        )

    def track_s3_key(self, key: str) -> None:
        self.s3_keys.append(key)

    def track_archived_objects(
        self, org_id: str, tenant_id: str, project_id: str, manifest_digest: str
    ) -> list[str]:
        """List + track every archived object under a bundle's digest-terminal
        prefix (``{org-id}/{tenant-id}/{project-id}/{manifest-digest}/`` per
        [WORM-002]) and return the keys.

        Scoping the listing to the content digest keeps both the assertion and
        the targeted cleanup confined to what one test created, never the whole
        shared-project prefix."""
        s3 = self.aws_session.client("s3")
        prefix = f"{org_id}/{tenant_id}/{project_id}/{manifest_digest}/"
        listing = s3.list_objects_v2(Bucket=self.stack.archive_bucket, Prefix=prefix)
        keys = [obj["Key"] for obj in listing.get("Contents", [])]
        self.s3_keys.extend(keys)
        return keys

    def _list_referrer_digests(self, ecr: Any, repo_path: str, subject_digest: str) -> list[str]:
        """Referrer image digests for one subject, via the OCI Referrers API.

        Mirrors ``cli/cairn_cli/verify._list_referrers``: boto3 has no paginator
        for ``list_image_referrers``, so walk ``nextToken`` manually. ECR has
        used both ``imageDigest`` and ``digest`` for the referrer's own digest,
        so read either. Best-effort — a missing/already-deleted subject yields
        an empty list rather than raising."""
        digests: list[str] = []
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "repositoryName": repo_path,
                "subjectId": {"imageDigest": subject_digest},
            }
            if next_token:
                kwargs["nextToken"] = next_token
            try:
                resp = ecr.list_image_referrers(**kwargs)
            except Exception:
                # Best-effort teardown: a missing/already-deleted subject, a
                # throttle, or an ECR mock without the Referrers API (moto, in
                # the harness-seam unit tests) must not break cleanup. Skip the
                # referrer purge; the tracked manifest/row/key deletes still run.
                break
            for ref in resp.get("referrers", []):
                d = ref.get("imageDigest") or ref.get("digest")
                if d:
                    digests.append(d)
            next_token = resp.get("nextToken")
            if not next_token:
                break
        return digests

    def _purge_referrers(
        self, ecr: Any, repo_path: str, subject_digest: str, seen: set[str]
    ) -> None:
        """Recursively delete every referrer of ``subject_digest`` (and their own
        referrers — e.g. the Producer_Attestation_Referrer's independent signature).

        A test creates its content manifest fresh, so every referrer hanging off
        it (the attestation claim, the Notation signatures, the WORM receipt) is
        this test's residue — deleting them is tracker-scoped to the recorded
        content digest, not a repo-wide enumerate-and-delete.
        Load-bearing because the producer claim referrer is now DETERMINISTIC
        (its ``created`` is pinned): if it survived a run, the next identical push
        would be an ECR no-op (no PutImage event) and validate would never
        re-trigger. The ``seen`` set bounds the recursion."""
        for digest in self._list_referrer_digests(ecr, repo_path, subject_digest):
            if digest in seen:
                continue
            seen.add(digest)
            self._purge_referrers(ecr, repo_path, digest, seen)
            try:
                ecr.batch_delete_image(repositoryName=repo_path, imageIds=[{"imageDigest": digest}])
            except ecr.exceptions.ClientError:
                pass

    def _ecr_client(self) -> Any:
        """ECR client for the deletes: a session of the cleanup role when published.

        The [ACCESS-010] repository policy denies ``ecr:BatchDeleteImage`` to every
        principal except the pruner and this role, so the contributor's own
        credentials can no longer delete test images. A configured role that
        cannot be assumed RAISES — teardown must not silently skip deletes.
        A deployment that publishes no cleanup role falls back to the
        caller's session.
        """
        role_arn = self.stack.cleanup_role_arn
        if not role_arn:
            return self.aws_session.client("ecr")
        creds = self.aws_session.client("sts").assume_role(
            RoleArn=role_arn, RoleSessionName="cairn-integ-cleanup", DurationSeconds=900
        )["Credentials"]
        factory = self.session_factory
        if factory is None:
            import boto3

            factory = boto3.Session
        return factory(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=self.stack.region,
        ).client("ecr")

    def run(self) -> None:
        ecr = self._ecr_client()
        ddb = self.aws_session.client("dynamodb")
        s3 = self.aws_session.client("s3")

        # Purge each tracked content manifest's referrer tree BEFORE deleting the
        # manifests, so no deterministic Producer_Attestation_Referrer survives to suppress
        # the next run's validate trigger.
        seen_referrers: set[str] = set()
        for repo_path, reference in self.ecr_refs:
            if reference.startswith("sha256:"):
                self._purge_referrers(ecr, repo_path, reference, seen_referrers)

        for repo_path, reference in self.ecr_refs:
            try:
                ecr.batch_delete_image(
                    repositoryName=repo_path,
                    imageIds=[
                        (
                            {"imageDigest": reference}
                            if reference.startswith("sha256:")
                            else {"imageTag": reference}
                        )
                    ],
                )
            except ecr.exceptions.ClientError:
                pass

        for pk, sk in self.metadata_keys:
            try:
                ddb.delete_item(
                    TableName=self.stack.metadata_table,
                    Key={"pk": {"S": pk}, "sk": {"S": sk}},
                )
            except ddb.exceptions.ClientError:
                pass

        for key in self.s3_keys:
            try:
                s3.delete_object(Bucket=self.stack.archive_bucket, Key=key)
            except s3.exceptions.ClientError:
                pass


@pytest.fixture
def bundle_cleanup(aws_session: Any, stack_outputs: StackOutputs) -> Iterator[BundleCleanup]:
    tracker = BundleCleanup(aws_session=aws_session, stack=stack_outputs)
    try:
        yield tracker
    finally:
        tracker.run()

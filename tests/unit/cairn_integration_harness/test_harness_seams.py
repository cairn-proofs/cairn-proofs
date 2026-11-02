"""Unit coverage for the integration-harness seams in
``tests/integration/conftest.py`` and ``tests/integration/test_dedup_and_drift.py``.

The integration suite itself is gated by ``CAIRN_INTEGRATION_ENV`` and only
runs against a live deployment (the separate ``mise run integration`` gate), so
that harness logic would otherwise have NO coverage
in ``mise run check``. These tests exercise the cheap, deterministic seams of
that logic WITHOUT a live deployment — moto for the AWS-touching helpers, a
``tmp_path`` file for the pure pin-parser — pinning the three load-bearing
guarantees the harness calls out:

- ``shared_repo_name`` **describes, never creates** and skips when the
  pre-provisioned repo is absent.
- ``target_project_id`` defaults to the stack's configured project
  (``mvp-tenant``) and is overridable by env.
- ``track_archived_objects`` scopes its listing/tracking to the bundle's
  digest-terminal prefix — never the whole shared-project prefix.
- ``BundleCleanup.run`` discriminates a sha256 reference from a tag and never
  touches the repository itself.
- ``_read_pinned_digest`` returns ``None`` for the ``SENTINEL`` placeholder so
  the drift pin can never falsely pass against the sentinel.

These mirror the source layout convention: harness seams live in
``tests/integration/`` so the unit mirror is ``tests/unit/cairn_integration_harness/``.
This module carries NO ``pytest.mark.integration`` marker, so it runs in the
normal (parallel ``-n auto``) suite and in ``mise run check``.
"""

from __future__ import annotations

import re

import boto3
import pytest
from cairn_cli._aws import DeploymentTarget
from cairn_oci import ArtifactLocator
from hypothesis import given
from hypothesis import strategies as st
from moto import mock_aws

from tests.integration import conftest
from tests.integration.conftest import BundleCleanup, StackOutputs
from tests.integration.test_dedup_and_drift import _read_pinned_digest

_APP_NAME = "cairnproofs4af"

_REGION = "us-east-2"

# The raw (un-fixturized) callables behind the session-scoped fixtures.
_target_org_id = conftest.target_org_id.__wrapped__
_target_tenant_id = conftest.target_tenant_id.__wrapped__
_target_project_id = conftest.target_project_id.__wrapped__
_shared_repo_name = conftest.shared_repo_name.__wrapped__


def _stack_outputs(qualifier: str = "cmh-alpha") -> StackOutputs:
    """A minimal ``StackOutputs`` for seams that only read ``qualifier`` /
    ``archive_bucket`` / ``metadata_table``."""
    return StackOutputs(
        qualifier=qualifier,
        stack_name=f"cairnproofs4af-{qualifier}",
        account_id="000000000000",
        region=_REGION,
        ecr_registry_host=f"000000000000.dkr.ecr.{_REGION}.amazonaws.com",
        archive_bucket=f"cairnproofs4af-artifact-archive-{qualifier}",
        metadata_table=f"cairnproofs4af-ArtifactMetadata-{qualifier}",
        control_claims_table=f"cairnproofs4af-ControlClaims-{qualifier}",
        event_bus_name=f"cairnproofs4af-artifact-{qualifier}",
        validate_bundle_dlq_url="https://sqs/validate",
        sign_bundle_dlq_url="https://sqs/sign",
        archive_to_s3_dlq_url="https://sqs/archive",
        update_metadata_dlq_url="https://sqs/metadata",
        integrity_mismatch_alarm_name="cairn-IntegrityMismatch",
        signing_failure_alarm_name="cairn-SigningFailure",
        archive_failure_alarm_name="cairn-ArchiveFailure",
    )


def _resolve_or_fail(
    stack: StackOutputs,
    org_id: str,
    tenant_id: str,
    project_id: str,
    session: boto3.Session,
    *,
    expected: str,
) -> str:
    """Run ``shared_repo_name`` but turn its ``pytest.skip`` into a ``pytest.fail``.

    These tests pre-provision the repo under ``expected`` and require
    the fixture to RESOLVE it; a skip there means the fixture queried the wrong
    (bare) name, so it must surface as a FAILURE,
    not a (passing) SKIP. ``expected`` is the name we provisioned, named only in
    the failure message.
    """
    try:
        return _shared_repo_name(stack, org_id, tenant_id, project_id, session)
    except pytest.skip.Exception as exc:
        pytest.fail(f"shared_repo_name skipped instead of resolving {expected!r}: {exc}")


# ---------------------------------------------------------------------------
# target_{org,tenant,project}_id — default resolution
# ---------------------------------------------------------------------------


def test_target_org_id_defaults_to_stack_configured_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no env var set, the suite targets the stack's configured org.

    ``mvp-org`` is the stack default (``cdk/bin/artifact-store.ts``); if this
    default drifts the whole suite would silently target a non-provisioned
    ``{org}/{tenant}/{project}`` triple and skip, so the default is
    load-bearing.
    """
    monkeypatch.delenv("CAIRN_INTEGRATION_ORG_ID", raising=False)
    assert _target_org_id() == "mvp-org"


def test_target_org_id_honors_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-empty env var overrides the default org."""
    monkeypatch.setenv("CAIRN_INTEGRATION_ORG_ID", "other-org")
    assert _target_org_id() == "other-org"


def test_target_tenant_id_defaults_to_stack_configured_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no env var set, the suite targets the stack's configured tenant.

    ``mvp-tenant`` is the stack default (``cdk/bin/artifact-store.ts``).
    """
    monkeypatch.delenv("CAIRN_INTEGRATION_TENANT_ID", raising=False)
    assert _target_tenant_id() == "mvp-tenant"


def test_target_tenant_id_honors_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-empty env var overrides the default tenant."""
    monkeypatch.setenv("CAIRN_INTEGRATION_TENANT_ID", "other-tenant")
    assert _target_tenant_id() == "other-tenant"


def test_target_project_id_defaults_to_stack_configured_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no env var set, the suite targets the stack's configured project.

    ``mvp-project`` is the stack default (``cdk/bin/artifact-store.ts``);
    if this default drifts the whole suite would silently target a
    non-provisioned project and skip, so the default is load-bearing.
    """
    monkeypatch.delenv("CAIRN_INTEGRATION_PROJECT_ID", raising=False)
    assert _target_project_id() == "mvp-project"


def test_target_project_id_honors_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-empty env var overrides the default project."""
    monkeypatch.setenv("CAIRN_INTEGRATION_PROJECT_ID", "other-project")
    assert _target_project_id() == "other-project"


# ---------------------------------------------------------------------------
# shared_repo_name — describe-not-create guard
# ---------------------------------------------------------------------------


@mock_aws
def test_shared_repo_name_resolves_existing_provisioned_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixture resolves the ``{appName}-``prefixed name the stack provisions.

    A real deployment provisions the per-project repo as the uniform
    three-segment hierarchy
    ``{appName}-artifact-{qualifier}/{orgId}/{tenantId}/{projectId}`` (=
    ``cairnproofs4af-artifact-cmh-alpha/mvp-org/mvp-tenant/mvp-project``), so
    the fixture must resolve THAT prefixed name for ``describe_repositories``
    to succeed. We pre-provision the repo under the prefixed name ONLY
    (mirroring the stack) and assert the fixture returns it, calling only
    describe (never create).

    If the fixture queried the bare
    ``artifact-cmh-alpha/...`` (omitting the ``{appName}-`` prefix),
    ``describe_repositories`` would raise ``RepositoryNotFoundException`` and the
    fixture would ``pytest.skip``. The ``skip.Exception``→``fail`` wrapper below
    converts such a skip into a FAILURE rather than a SKIP.
    """
    # ``resolve_app_name`` honors CAIRN_APP_NAME before any SSM read, so the
    # fixture's appName resolution needs no network/SSM in moto.
    monkeypatch.setenv("CAIRN_APP_NAME", _APP_NAME)

    session = boto3.Session(region_name=_REGION)
    stack = _stack_outputs(qualifier="cmh-alpha")
    expected = f"{_APP_NAME}-artifact-cmh-alpha/mvp-org/mvp-tenant/mvp-project"

    # Pre-provision the repo under the PREFIXED name the way the stack does.
    session.client("ecr").create_repository(repositoryName=expected)

    resolved = _resolve_or_fail(
        stack, "mvp-org", "mvp-tenant", "mvp-project", session, expected=expected
    )
    assert resolved == expected


@mock_aws
def test_shared_repo_name_matches_production_push_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixture's resolved name is byte-identical to the CLI push target.

    The integration harness MUST query the exact repository the production push
    path writes to; otherwise it tests a different name than the one under test.
    This pins the fixture's output against the single composition the CLI uses —
    ``DeploymentTarget.repo_path(ArtifactLocator().repo_name(...))`` — for the
    same inputs.
    """
    monkeypatch.setenv("CAIRN_APP_NAME", _APP_NAME)

    session = boto3.Session(region_name=_REGION)
    stack = _stack_outputs(qualifier="cmh-alpha")

    production_target = DeploymentTarget(
        stage="alpha", region_code="cmh", app_name=_APP_NAME
    ).repo_path(
        ArtifactLocator().repo_name(
            deployment_qualifier="cmh-alpha",
            org_id="mvp-org",
            tenant_id="mvp-tenant",
            project_id="mvp-project",
        )
    )

    # Pre-provision under the production target name so describe succeeds.
    session.client("ecr").create_repository(repositoryName=production_target)

    resolved = _resolve_or_fail(
        stack, "mvp-org", "mvp-tenant", "mvp-project", session, expected=production_target
    )
    assert resolved == production_target


@mock_aws
def test_shared_repo_name_skips_when_repo_absent_and_never_creates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the per-project repo is absent the fixture SKIPS (does not create).

    This is the mistyped-project guard: a wrong/mistyped project must skip loudly, not
    fall back to creating or guessing a repo. We both assert the skip and prove
    no repository was created as a side effect.

    Seeds CAIRN_APP_NAME so appName resolution succeeds under moto
    (the fixture composes the prefixed target before describing); the target is
    still genuinely absent, so the describe-not-create skip path holds.
    """
    monkeypatch.setenv("CAIRN_APP_NAME", _APP_NAME)

    session = boto3.Session(region_name=_REGION)
    stack = _stack_outputs(qualifier="cmh-alpha")

    with pytest.raises(pytest.skip.Exception):
        _shared_repo_name(stack, "mvp-org", "mvp-tenant", "missing-project", session)

    # Describe-not-create: the registry is still empty after the skip.
    repos = session.client("ecr").describe_repositories().get("repositories", [])
    assert repos == [], "shared_repo_name created a repository on the skip path"


# ---------------------------------------------------------------------------
# StackOutputs non-repo names — appName prefix preserved (Unchanged Behavior #3)
# ---------------------------------------------------------------------------


def test_stack_outputs_non_repo_names_keep_appname_prefix() -> None:
    """Non-repository deployment resources keep their existing prefixed values.

    Every non-repository resource
    name (archive bucket, the two tables, the four DLQ URLs, the three alarm
    names) the conftest resolves must equal the ``_stack_outputs`` construction,
    so a repo-name change cannot collaterally drift a sibling literal.
    """
    stack = _stack_outputs(qualifier="cmh-alpha")

    assert stack.archive_bucket == "cairnproofs4af-artifact-archive-cmh-alpha"
    assert stack.metadata_table == "cairnproofs4af-ArtifactMetadata-cmh-alpha"
    assert stack.control_claims_table == "cairnproofs4af-ControlClaims-cmh-alpha"

    assert stack.validate_bundle_dlq_url == "https://sqs/validate"
    assert stack.sign_bundle_dlq_url == "https://sqs/sign"
    assert stack.archive_to_s3_dlq_url == "https://sqs/archive"
    assert stack.update_metadata_dlq_url == "https://sqs/metadata"

    assert stack.integrity_mismatch_alarm_name == "cairn-IntegrityMismatch"
    assert stack.signing_failure_alarm_name == "cairn-SigningFailure"
    assert stack.archive_failure_alarm_name == "cairn-ArchiveFailure"


# ---------------------------------------------------------------------------
# repo_path composition — always prefixes, never bare (Unchanged Behavior #5)
# ---------------------------------------------------------------------------

# Valid slug strategies matching the cairn_oci grammars the locator enforces
# (``categories.py``): project-id / app-name follow ``_ID_RE`` (lowercase
# alphanumeric, single internal hyphens, no leading/trailing hyphen, 1-64
# chars); region-code / stage are the ``[a-z0-9]+`` halves of ``_QUALIFIER_RE``
# so ``{region}-{stage}`` is a valid Deployment_Qualifier.
_slug = st.from_regex(r"\A[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?\Z", fullmatch=True)
_segment = st.from_regex(r"\A[a-z0-9]{1,16}\Z", fullmatch=True)
# An app_name that begins ``artifact-`` (or is exactly ``artifact``) would make
# the prefixed result itself start with ``artifact-`` — exclude those so the
# "never bare" leg of the invariant is well-posed (a real appName like
# ``cairnproofs4af`` never collides with the reserved ``artifact-`` prefix).
_app_name = _slug.filter(lambda s: s != "artifact" and not s.startswith("artifact-"))


@given(
    app_name=_app_name,
    region_code=_segment,
    stage=_segment,
    org_id=_slug,
    tenant_id=_slug,
    project_id=_slug,
)
def test_repo_path_composition_always_prefixes(
    app_name: str, region_code: str, stage: str, org_id: str, tenant_id: str, project_id: str
) -> None:
    """``repo_path(locator.repo_name(...))`` always yields the prefixed namespace.

    The naming invariant: for any valid ``app_name`` /
    ``region_code`` / ``stage`` and any ``{org_id}/{tenant_id}/{project_id}``
    triple, composing the bare locator output through
    ``DeploymentTarget.repo_path`` produces
    ``{app_name}-artifact-{region}-{stage}/{org}/{tenant}/{project}`` — it
    ALWAYS carries the ``{app_name}-`` prefix, embeds the full identity triple,
    and NEVER resolves to the bare ``artifact-`` form. Guards
    ``cli/cairn_cli/_aws.py`` ``repo_path`` and documents why the fixture cannot
    drift onto the bare name.
    """
    qualifier = f"{region_code}-{stage}"
    bare = ArtifactLocator().repo_name(
        deployment_qualifier=qualifier,
        org_id=org_id,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    resolved = DeploymentTarget(stage=stage, region_code=region_code, app_name=app_name).repo_path(
        bare
    )

    assert resolved.startswith(f"{app_name}-artifact-{qualifier}/")
    assert resolved.endswith(f"/{org_id}/{tenant_id}/{project_id}")
    assert not resolved.startswith("artifact-")


# ---------------------------------------------------------------------------
# track_archived_objects — digest-prefix scoping
# ---------------------------------------------------------------------------


@mock_aws
def test_track_archived_objects_scopes_to_digest_prefix() -> None:
    """Listing/tracking is confined to ``{project}/{digest}/`` and never the
    whole shared-project prefix.

    Seeds the archive bucket with one in-scope object plus two decoys: a
    different digest under the SAME project, and the same digest under a
    DIFFERENT project. Only the in-scope key is returned and tracked.
    """
    session = boto3.Session(region_name=_REGION)
    stack = _stack_outputs()
    s3 = session.client("s3")
    s3.create_bucket(
        Bucket=stack.archive_bucket,
        CreateBucketConfiguration={"LocationConstraint": _REGION},
    )

    org = "mvp-org"
    tenant = "mvp-tenant"
    project = "mvp-project"
    digest = "sha256:" + "a" * 64
    other_digest = "sha256:" + "b" * 64
    in_scope = f"{org}/{tenant}/{project}/{digest}/bundle.json"
    decoy_same_project = f"{org}/{tenant}/{project}/{other_digest}/bundle.json"
    decoy_other_tenant = f"{org}/other-tenant/{project}/{digest}/bundle.json"
    for key in (in_scope, decoy_same_project, decoy_other_tenant):
        s3.put_object(Bucket=stack.archive_bucket, Key=key, Body=b"x")

    cleanup = BundleCleanup(aws_session=session, stack=stack)
    keys = cleanup.track_archived_objects(org, tenant, project, digest)

    assert keys == [in_scope]
    # And the tracker recorded exactly that one key (so cleanup stays targeted).
    assert cleanup.s3_keys == [in_scope]


@mock_aws
def test_track_archived_objects_returns_empty_when_nothing_archived() -> None:
    """No objects under the digest prefix yields an empty list (the drift test
    asserts on this to detect a pipeline that archived nothing)."""
    session = boto3.Session(region_name=_REGION)
    stack = _stack_outputs()
    s3 = session.client("s3")
    s3.create_bucket(
        Bucket=stack.archive_bucket,
        CreateBucketConfiguration={"LocationConstraint": _REGION},
    )

    cleanup = BundleCleanup(aws_session=session, stack=stack)
    assert (
        cleanup.track_archived_objects("mvp-org", "mvp-tenant", "mvp-project", "sha256:" + "c" * 64)
        == []
    )


# ---------------------------------------------------------------------------
# BundleCleanup.run — targeted, repo-preserving teardown
# ---------------------------------------------------------------------------


@mock_aws
def test_cleanup_deletes_tracked_manifest_row_and_key_but_not_repo() -> None:
    """``run`` deletes exactly the tracked manifest / row / S3 key and leaves
    the shared repository (and untracked co-resident data) intact."""
    session = boto3.Session(region_name=_REGION)
    stack = _stack_outputs()
    ecr = session.client("ecr")
    ddb = session.client("dynamodb")
    s3 = session.client("s3")

    repo = "artifact-cmh-alpha/mvp-tenant"
    ecr.create_repository(repositoryName=repo)
    # Push a real image so we have a digest to delete.
    put = ecr.put_image(
        repositoryName=repo,
        imageManifest='{"schemaVersion":2,"mediaType":'
        '"application/vnd.oci.image.manifest.v1+json","config":'
        '{"mediaType":"application/vnd.oci.image.config.v1+json","size":0,'
        '"digest":"sha256:' + "0" * 64 + '"},"layers":[]}',
        imageTag="t1",
    )
    image_digest = put["image"]["imageId"]["imageDigest"]

    ddb.create_table(
        TableName=stack.metadata_table,
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.put_item(
        TableName=stack.metadata_table,
        Item={"pk": {"S": "mvp-tenant"}, "sk": {"S": f"mvp-project#{image_digest}"}},
    )
    # A co-resident row that is NOT tracked must survive cleanup.
    ddb.put_item(
        TableName=stack.metadata_table,
        Item={"pk": {"S": "mvp-tenant"}, "sk": {"S": "sha256:" + "9" * 64}},
    )

    s3.create_bucket(
        Bucket=stack.archive_bucket,
        CreateBucketConfiguration={"LocationConstraint": _REGION},
    )
    tracked_key = f"mvp-org/mvp-tenant/mvp-project/{image_digest}/bundle.json"
    s3.put_object(Bucket=stack.archive_bucket, Key=tracked_key, Body=b"x")

    cleanup = BundleCleanup(aws_session=session, stack=stack)
    cleanup.track_ecr_manifest(repo, image_digest)
    cleanup.track_metadata_row("mvp-tenant", "mvp-project", image_digest)
    cleanup.track_s3_key(tracked_key)
    cleanup.run()

    # Manifest deleted; repository itself still present (never DeleteRepository).
    assert ecr.describe_repositories(repositoryNames=[repo])["repositories"]
    remaining = ecr.describe_images(repositoryName=repo).get("imageDetails", [])
    assert all(image_digest != d.get("imageDigest") for d in remaining)

    # Tracked row gone, untracked row survives.
    assert "Item" not in ddb.get_item(
        TableName=stack.metadata_table,
        Key={"pk": {"S": "mvp-tenant"}, "sk": {"S": f"mvp-project#{image_digest}"}},
    )
    assert "Item" in ddb.get_item(
        TableName=stack.metadata_table,
        Key={"pk": {"S": "mvp-tenant"}, "sk": {"S": "sha256:" + "9" * 64}},
    )

    # Tracked S3 key gone.
    assert s3.list_objects_v2(Bucket=stack.archive_bucket).get("KeyCount", 0) == 0


@mock_aws
def test_cleanup_swallows_already_absent_resources() -> None:
    """``run`` is best-effort: deleting a manifest/row/key that no longer
    exists does not raise (re-run / partial-failure safety)."""
    session = boto3.Session(region_name=_REGION)
    stack = _stack_outputs()
    session.client("ecr").create_repository(repositoryName="artifact-cmh-alpha/mvp-tenant")
    session.client("dynamodb").create_table(
        TableName=stack.metadata_table,
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    session.client("s3").create_bucket(
        Bucket=stack.archive_bucket,
        CreateBucketConfiguration={"LocationConstraint": _REGION},
    )

    cleanup = BundleCleanup(aws_session=session, stack=stack)
    cleanup.track_ecr_manifest("artifact-cmh-alpha/mvp-tenant", "sha256:" + "f" * 64)
    cleanup.track_metadata_row("mvp-tenant", "mvp-project", "sha256:" + "f" * 64)
    cleanup.track_s3_key("mvp-tenant/never-created/bundle.json")
    cleanup.run()  # must not raise


class _FakeEcr:
    """Minimal ECR stand-in exposing the Referrers API moto lacks.

    ``referrers_by_subject`` maps a subject digest to its referrer digests;
    ``list_image_referrers`` answers from it and ``batch_delete_image`` records
    every deleted digest."""

    class exceptions:
        class ClientError(Exception): ...

    def __init__(self, referrers_by_subject: dict[str, list[str]]) -> None:
        self._referrers = referrers_by_subject
        self.deleted: list[str] = []

    def list_image_referrers(self, *, repositoryName: str, subjectId: dict, nextToken=None) -> dict:
        subject = subjectId["imageDigest"]
        return {"referrers": [{"imageDigest": d} for d in self._referrers.get(subject, [])]}

    def batch_delete_image(self, *, repositoryName: str, imageIds: list[dict]) -> dict:
        self.deleted.extend(i["imageDigest"] for i in imageIds if "imageDigest" in i)
        return {"imageIds": imageIds, "failures": []}


class _NoopClient:
    class exceptions:
        class ClientError(Exception): ...

    def __getattr__(self, _name: str):
        return lambda *a, **k: {}


class _FakeSession:
    def __init__(self, ecr: _FakeEcr) -> None:
        self._ecr = ecr

    def client(self, name: str):
        return self._ecr if name == "ecr" else _NoopClient()


def test_cleanup_purges_producer_attestation_referrer_tree() -> None:
    """``run`` recursively deletes a tracked content manifest's referrers — the
    (now deterministic) Producer_Attestation_Referrer AND its own independent signature —
    so no referrer survives to suppress the next run's validate trigger (an ECR
    PutImage of an already-present deterministic referrer emits no event). moto
    has no Referrers API, so this drives a fake ECR client directly."""
    content = "sha256:" + "a" * 64
    attestation = "sha256:" + "b" * 64
    signature = "sha256:" + "c" * 64
    # content -> attestation referrer -> its own signature referrer
    ecr = _FakeEcr({content: [attestation], attestation: [signature]})
    cleanup = BundleCleanup(aws_session=_FakeSession(ecr), stack=_stack_outputs())
    cleanup.track_ecr_manifest("artifact-cmh-alpha/mvp-tenant", content)
    cleanup.run()

    assert attestation in ecr.deleted  # the trigger-blocking deterministic referrer
    assert signature in ecr.deleted  # recursion reached the attestation's signature
    assert content in ecr.deleted  # the tracked manifest itself still deleted


def test_cleanup_referrer_purge_is_best_effort_when_api_unavailable() -> None:
    """If the Referrers API is unavailable (raises), the purge is skipped and the
    tracked manifest is still deleted — teardown never raises (the moto path)."""

    class _RaisingEcr(_FakeEcr):
        def list_image_referrers(self, **_kwargs):
            raise NotImplementedError("no Referrers API here")

    content = "sha256:" + "a" * 64
    ecr = _RaisingEcr({})
    cleanup = BundleCleanup(aws_session=_FakeSession(ecr), stack=_stack_outputs())
    cleanup.track_ecr_manifest("artifact-cmh-alpha/mvp-tenant", content)
    cleanup.run()  # must not raise

    assert content in ecr.deleted  # core deletion still happens despite purge skip


# ---------------------------------------------------------------------------
# BundleCleanup — ECR deletes run as the integration-test cleanup role
# ([ACCESS-010]: the repository policy admits only the pruner and that role)
# ---------------------------------------------------------------------------

_CLEANUP_ROLE = "arn:aws:iam::000000000000:role/cairnproofs4af-integ-cleanup-cmh-alpha"


class _FakeSts:
    def __init__(self, fail: bool = False) -> None:
        self.assumed: list[dict] = []
        self.fail = fail

    def assume_role(self, **kwargs) -> dict:
        if self.fail:
            raise RuntimeError("AccessDenied: not authorized to perform sts:AssumeRole")
        self.assumed.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "AKIA",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
            }
        }


class _CallerSession:
    """The contributor's session: its ECR must never be used for deletes."""

    def __init__(self, sts: _FakeSts, caller_ecr: _FakeEcr) -> None:
        self._sts = sts
        self._ecr = caller_ecr

    def client(self, name: str):
        if name == "sts":
            return self._sts
        if name == "ecr":
            return self._ecr
        return _NoopClient()


def _stack_with_cleanup_role(role: str | None) -> StackOutputs:
    from dataclasses import replace

    return replace(_stack_outputs(), cleanup_role_arn=role)


def test_cleanup_deletes_images_as_the_cleanup_role() -> None:
    content = "sha256:" + "a" * 64
    referrer = "sha256:" + "b" * 64
    role_ecr = _FakeEcr({content: [referrer]})
    caller_ecr = _FakeEcr({})
    sts = _FakeSts()
    made: list[dict] = []

    def factory(**kwargs):
        made.append(kwargs)
        return _FakeSession(role_ecr)

    cleanup = BundleCleanup(
        aws_session=_CallerSession(sts, caller_ecr),
        stack=_stack_with_cleanup_role(_CLEANUP_ROLE),
        session_factory=factory,
    )
    cleanup.track_ecr_manifest("artifact-cmh-alpha/mvp-tenant", content)
    cleanup.run()

    assert sts.assumed and sts.assumed[0]["RoleArn"] == _CLEANUP_ROLE
    assert made[0]["aws_session_token"] == "token"
    assert made[0]["region_name"] == _REGION
    assert role_ecr.deleted == [referrer, content]
    assert caller_ecr.deleted == []


def test_cleanup_fails_loudly_when_the_cleanup_role_cannot_be_assumed() -> None:
    caller_ecr = _FakeEcr({})
    cleanup = BundleCleanup(
        aws_session=_CallerSession(_FakeSts(fail=True), caller_ecr),
        stack=_stack_with_cleanup_role(_CLEANUP_ROLE),
    )
    cleanup.track_ecr_manifest("artifact-cmh-alpha/mvp-tenant", "sha256:" + "a" * 64)
    with pytest.raises(RuntimeError, match="AssumeRole"):
        cleanup.run()
    assert caller_ecr.deleted == []


def test_cleanup_uses_the_caller_session_when_no_cleanup_role_is_published() -> None:
    caller_ecr = _FakeEcr({})
    sts = _FakeSts()
    cleanup = BundleCleanup(
        aws_session=_CallerSession(sts, caller_ecr), stack=_stack_with_cleanup_role(None)
    )
    cleanup.track_ecr_manifest("artifact-cmh-alpha/mvp-tenant", "sha256:" + "a" * 64)
    cleanup.run()
    assert sts.assumed == []
    assert caller_ecr.deleted == ["sha256:" + "a" * 64]


# ---------------------------------------------------------------------------
# _read_pinned_digest — SENTINEL guard
# ---------------------------------------------------------------------------


def test_read_pinned_digest_committed_pin_is_wellformed() -> None:
    """The committed pin file is always well-formed regardless of whether the pin
    is currently armed: ``_read_pinned_digest`` returns either ``None`` (the
    SENTINEL placeholder — pin not yet captured, drift test SKIPS) or a
    syntactically valid ``sha256:`` digest (pin armed). This guards the live
    fixture against a malformed commit in either state. (The SENTINEL→None and
    real-value→verbatim parsing seams are covered exhaustively by the
    monkeypatched tmp-file tests below.)"""
    pinned = _read_pinned_digest()
    assert pinned is None or re.fullmatch(r"sha256:[0-9a-f]{64}", pinned)


def test_read_pinned_digest_sentinel_token_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """An explicit SENTINEL line yields ``None`` even with surrounding comments
    — the exact-digest assertion can never run against the sentinel."""
    import tests.integration.test_dedup_and_drift as dd

    pin = tmp_path / "pin.txt"
    pin.write_text("# capture instructions\n#\nSENTINEL\n")
    monkeypatch.setattr(dd, "_EXPECTED_DIGEST_PATH", pin)
    assert dd._read_pinned_digest() is None


def test_read_pinned_digest_missing_file_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A missing pin file is 'not captured', not an error."""
    import tests.integration.test_dedup_and_drift as dd

    monkeypatch.setattr(dd, "_EXPECTED_DIGEST_PATH", tmp_path / "absent.txt")
    assert dd._read_pinned_digest() is None


def test_read_pinned_digest_returns_first_real_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A real ``sha256:`` value (first non-comment, non-blank line) is returned
    verbatim so the exact-digest drift assertion can compare against it."""
    import tests.integration.test_dedup_and_drift as dd

    real = "sha256:" + "a" * 64
    pin = tmp_path / "pin.txt"
    pin.write_text(f"# header comment\n\n{real}\n# trailing\n")
    monkeypatch.setattr(dd, "_EXPECTED_DIGEST_PATH", pin)
    assert dd._read_pinned_digest() == real

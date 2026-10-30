"""End-to-end test for the ``cairn tui`` CLI wiring.

This closes the loop on the whole TUI: it drives ``cairn tui`` from the CLI
entry point through a REAL :class:`~cairn_cli.tui._sources.ArtifactSources` and
:class:`~cairn_cli.tui.app.ArtifactStoreApp`, over a *recording* read-only
backend that mirrors :class:`~cairn_cli.tui._readonly_client.ReadOnlyAwsClient`'s
public surface and RAISES on any non-allowlisted operation.

Three properties are proven here that the per-component tests cannot, because
this is the only test that exercises the full launch path end-to-end:

1. **End-to-end render + clean exit.** ``build_app(args, sources=<stub>)`` builds
   the real app; the Textual pilot mounts it, lets the list + DLQ workers fan
   out, selects a bundle to render detail, then exits cleanly.
2. **Allowlisted-only reads across the WHOLE run.** The recording
   backend's op log is asserted ⊆ the allowlist, with explicit negatives for the
   dangerous ops (``scan``, ``receive_message``, ``get_queue_url``, mutators).
3. **TTY guard before any AWS client.** ``run`` with a non-TTY stdout
   returns non-zero, prints a clear message, and builds NO AWS client.

Plus a parser smoke test that the subcommand is registered.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from cairn_cli._aws import DeploymentTarget
from cairn_cli.tui import command as tui_command
from cairn_cli.tui._sources import ArtifactSources
from cairn_cli.tui.command import build_app, run

from tests.unit.cairn_cli.conftest import subparser_help


def build_parser() -> argparse.ArgumentParser:
    """A local, tui-only argument parser (decoupled from ``cairn_cli.main``).

    ``cairn_cli.main`` currently cannot be imported: the CLI READ paths
    (``query.py`` / ``alias.py``) still consume the pre-tenancy ``cairn_metadata``
    API (``ControlsIndex`` / ``MetadataIndex(deployment_mode=...)``) and have no
    assigned migration task — an unassigned cross-wave seam flagged for the
    aggregate gate. To keep this TUI suite's registration + help assertions
    meaningful without importing the broken module, build a parser that wires
    ONLY the ``tui`` subcommand exactly as ``main`` does (via
    ``tui_command.add_subparser``).
    """
    parser = argparse.ArgumentParser(prog="cairn")
    subparsers = parser.add_subparsers(dest="command")
    tui_command.add_subparser(subparsers)
    return parser


TARGET = DeploymentTarget(stage="alpha", region_code="cmh", app_name="cairnproofs4af")
QUALIFIER = "cmh-alpha"
REGION = "us-east-2"
ACCOUNT_ID = "123456789012"
CATALOG_UUID = "11111111-1111-1111-1111-111111111111"

# The complete read allowlist the TUI may issue, across all four services.
# Anything outside this set in the recording backend's op log is a T3 violation.
ALLOWLISTED_OPS = {
    "describe_repositories",
    "describe_images",
    "batch_get_image",
    "list_image_referrers",
    "head_object",
    "get_object",
    "get_queue_attributes",
    "query",
    "get_item",
    "batch_get_item",
}

# Dangerous ops that must NEVER appear in the op log under any code path.
FORBIDDEN_OPS = {
    "scan",
    "receive_message",
    "get_queue_url",  # the TUI builds the SQS URL deterministically instead
    "delete_message",
    "send_message",
    "put_item",
    "update_item",
    "delete_item",
    "put_object",
    "delete_object",
    "batch_delete_image",
    "purge_queue",
    "start_message_move_task",
}


def _run_async(coro_fn: Callable[[], Awaitable[None]]) -> None:
    """Drive a Textual pilot coroutine on a fresh event loop.

    ``pytest-asyncio`` is not a project dependency, so (matching ``test_app``)
    the pilot scenario is a plain synchronous test that runs its coroutine via
    ``asyncio.run`` — Textual's ``run_test()`` works inside that fresh loop.
    """
    asyncio.run(coro_fn())


# --------------------------------------------------------------------------
# Recording read-only backend: stub boto3 clients + DynamoDB resource that
# RECORD every op and RAISE on anything not allowlisted (mirrors the structural
# absence of mutators in the real ReadOnlyAwsClient).
# --------------------------------------------------------------------------


class _RecordingTable:
    """DynamoDB Table exposing ONLY query/get_item; records each op.

    A ``scan`` (or any other) attribute is structurally absent — reaching for
    one raises ``AttributeError`` exactly as the real ``_ReadOnlyTable`` does.
    """

    def __init__(self, name: str, calls: list[str], pages_by_pk: dict[str, list[dict]]) -> None:
        self._name = name
        self._calls = calls
        self._pages_by_pk = pages_by_pk
        self._cursor: dict[str, int] = {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self._calls.append("query")
        pk = kwargs["ExpressionAttributeValues"][":pk"]
        pages = self._pages_by_pk.get(pk, [{"Items": []}])
        idx = 0 if "ExclusiveStartKey" not in kwargs else self._cursor.get(pk, 0)
        page = pages[idx] if idx < len(pages) else {"Items": []}
        if "LastEvaluatedKey" in page:
            self._cursor[pk] = idx + 1
        return page

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self._calls.append("get_item")
        return {}


class _RecordingResource:
    def __init__(self, calls: list[str], pages_by_pk: dict[str, list[dict]]) -> None:
        self._calls = calls
        self._pages_by_pk = pages_by_pk

    def Table(self, name: str) -> _RecordingTable:  # boto3 API name
        return _RecordingTable(name, self._calls, self._pages_by_pk)

    def batch_get_item(self, **kwargs: Any) -> dict[str, Any]:
        # Read-only BatchGetItem backing the Identity_Registry display-name join;
        # no items registered, so the list renders opaque ids (fallback).
        self._calls.append("batch_get_item")
        return {"Responses": {}}


class _RecordingReadOnlyBackend:
    """Stub matching the ReadOnlyAwsClient public surface; records every op.

    Only the allowlisted operations are defined. Any other attribute access
    raises ``AttributeError`` (e.g. ``receive_message``, ``scan``, ``put_item``,
    ``get_queue_url``), mirroring the structural absence in the real client, so a
    stray non-allowlisted call fails loudly rather than being silently issued.
    """

    def __init__(
        self,
        *,
        ecr_pages: list[dict],
        pages_by_pk: dict[str, list[dict]] | None = None,
        sqs_attributes: dict[str, Any] | None = None,
        images_by_repo: dict[str, list[dict]] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.queue_urls: list[str] = []
        self._ecr_pages = ecr_pages
        self._resource = _RecordingResource(self.calls, pages_by_pk or {})
        self._sqs_attributes = sqs_attributes if sqs_attributes is not None else {}
        self._images_by_repo = images_by_repo or {}

    @property
    def dynamodb_resource(self) -> _RecordingResource:
        return self._resource

    def describe_repositories(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("describe_repositories")
        idx = int(kwargs["nextToken"]) if "nextToken" in kwargs else 0
        return self._ecr_pages[idx] if idx < len(self._ecr_pages) else {"repositories": []}

    def describe_images(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("describe_images")
        repo = kwargs.get("repositoryName")
        return {"imageDetails": list(self._images_by_repo.get(repo, []))}

    def batch_get_image(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("batch_get_image")
        return {"images": [{"imageId": kwargs["imageIds"][0]}]}

    def list_image_referrers(self, **kwargs: Any) -> dict[str, Any]:
        # The lazy per-bundle referrer read: the TUI issues this on
        # selection, so the end-to-end run exercises it. Counts-only stub — an
        # empty referrer set keeps the allowlist assertion focused on the OP being
        # allowlisted, not on referrer content (covered by test_app.py).
        self.calls.append("list_image_referrers")
        return {"referrers": []}

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("head_object")
        return {"ContentLength": 1, "_kwargs": kwargs}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("get_object")
        return {"Body": b"", "_kwargs": kwargs}

    def get_queue_attributes(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("get_queue_attributes")
        self.queue_urls.append(kwargs["QueueUrl"])
        return {"Attributes": dict(self._sqs_attributes)}


def _repo(name: str) -> dict[str, Any]:
    return {"repositoryName": name, "registryId": ACCOUNT_ID}


def _record(project_id: str, suffix: str, **extra: Any) -> dict[str, Any]:
    """A realistic Metadata_Index record for the corpus."""
    base = {
        "projectId": project_id,
        "tenantId": project_id,
        "orgId": "org-a",
        "artifactKind": "assessment-results",
        "signingStatus": "signed",
        "archiveStatus": "archived",
        "manifestDigest": "sha256:" + suffix * 64,
        "createdAt": "2026-05-28T00:00:00Z",
        "repositoryName": f"cairnproofs4af-artifact-{QUALIFIER}/org-a/{project_id}/{project_id}",
        "registryId": ACCOUNT_ID,
        "validatedAt": "2026-05-28T00:00:01Z",
        "signedAt": "2026-05-28T00:00:02Z",
        "archivedAt": "2026-05-28T00:00:03Z",
        "s3Bucket": "cairn-worm-bucket",
        "s3KeyPrefix": f"{project_id}/sha256/{suffix}/",
        "catalogOrProfileUuid": CATALOG_UUID,
        "controlFamily": "AC",
    }
    base.update(extra)
    return base


def _sample_backend() -> _RecordingReadOnlyBackend:
    """A small realistic corpus: two projects, one bundle each, one cites a control.

    proj-a's bundle carries OSCAL controls; proj-b's is a plain bundle. The ECR
    enumeration names both repos so ``list_all_bundles`` fans out to both.
    """
    return _RecordingReadOnlyBackend(
        ecr_pages=[
            {
                "repositories": [
                    _repo(f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"),
                    _repo(f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-b/proj-b"),
                ]
            }
        ],
        pages_by_pk={
            "proj-a": [{"Items": [_record("proj-a", "a", controls=["ac-2", "ac-3"])]}],
            "proj-b": [{"Items": [_record("proj-b", "b")]}],
            # The control-claims projection is a tenant-PK plane: the control
            # query partitions on the exact {tenant-id} (here tnt-a), with
            # {uuid}#{controlId} as the sort-key prefix (ignored by the pk-keyed
            # stub). Keyed under the tenant, distinct from the project pk above.
            "tnt-a": [{"Items": [_record("proj-a", "a", controls=["ac-2"])]}],
        },
        sqs_attributes={"ApproximateNumberOfMessages": "0"},
    )


def _real_sources_over(backend: _RecordingReadOnlyBackend) -> ArtifactSources:
    # A REAL ArtifactSources over the recording backend — the only stubbed seam
    # is the AWS surface, so the list/detail/DLQ logic under test is genuine.
    return ArtifactSources(target=TARGET, client=backend, region=REGION)  # type: ignore[arg-type]


def _args() -> argparse.Namespace:
    # Parse through the REAL parser so the test exercises the registered
    # subcommand's defaults exactly as a CLI invocation would.
    return build_parser().parse_args(["tui"])


# --------------------------------------------------------------------------
# 1. End-to-end: launch via build_app, render, select detail, exit cleanly,
#    and prove ONLY allowlisted reads were issued across the whole run.
# --------------------------------------------------------------------------


def test_cairn_tui_end_to_end_renders_and_issues_only_allowlisted_reads() -> None:
    backend = _sample_backend()
    sources = _real_sources_over(backend)
    app = build_app(_args(), sources=sources)

    async def scenario() -> None:
        from textual.widgets import DataTable

        async with app.run_test() as pilot:
            await pilot.pause()
            # On-mount workers fan out (list + DLQ); let them complete.
            await app.workers.wait_for_complete()
            await pilot.pause()

            table = app.query_one("#bundle-table", DataTable)
            # Both corpus bundles surfaced in the list.
            assert table.row_count == 2

            # Select the first row -> lazy detail fetch for that bundle only.
            table.move_cursor(row=0)
            row_key = table.coordinate_to_cell_key((0, 0)).row_key
            app.on_data_table_row_selected(
                DataTable.RowSelected(table, cursor_row=0, row_key=row_key)
            )
            await app.workers.wait_for_complete()
            await pilot.pause()
            # The selection registered and the lazy detail worker ran for it.
            assert app._selected is not None
            # The detail producer surface renders the lifecycle for that bundle.
            detail_lines = app.render_detail_lines(app._selected)
            assert any("Lifecycle" in line for line in detail_lines)

        # run_test()'s context-manager exit is the clean shutdown — reaching
        # here without an exception is the "exits cleanly" assertion.

    _run_async(scenario)

    # --- THE T3 assertion: across the ENTIRE run, only allowlisted ops issued.
    assert backend.calls, "expected the launched TUI to issue reads"
    illegal = set(backend.calls) - ALLOWLISTED_OPS
    assert not illegal, f"cairn tui issued non-allowlisted ops: {sorted(illegal)}"

    # Explicit negatives for the dangerous ops the task calls out by name.
    for forbidden in FORBIDDEN_OPS:
        assert forbidden not in backend.calls, f"forbidden op issued: {forbidden}"

    # The list path actually ran (describe + per-project query), detail was lazy,
    # and DLQ health was counts-only against the four deterministic queue URLs.
    assert "describe_repositories" in backend.calls
    assert backend.calls.count("query") >= 2  # one query per project
    assert "batch_get_image" in backend.calls  # detail fetch for the selected bundle
    assert "get_queue_attributes" in backend.calls
    assert len(backend.queue_urls) == 4
    assert all(
        url == f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT_ID}/{name}"
        for url, name in zip(
            backend.queue_urls,
            (
                f"cairnproofs4af-validate-bundle-dlq-{QUALIFIER}",
                f"cairnproofs4af-sign-bundle-dlq-{QUALIFIER}",
                f"cairnproofs4af-archive-to-s3-dlq-{QUALIFIER}",
                f"cairnproofs4af-update-metadata-dlq-{QUALIFIER}",
            ),
            strict=True,
        )
    )


def test_cairn_tui_control_view_query_is_allowlisted() -> None:
    # Drive the control-view fetch (the third worker path) end-to-end and prove
    # it too stays within the allowlist — control lookups must never Scan.
    backend = _sample_backend()
    sources = _real_sources_over(backend)
    app = build_app(_args(), sources=sources)

    async def scenario() -> None:
        from textual.widgets import DataTable, Input

        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.query_one("#ctl-tenant-id", Input).value = "tnt-a"
            app.query_one("#ctl-uuid", Input).value = CATALOG_UUID
            app.query_one("#ctl-id", Input).value = "ac-2"
            app._refresh_control_worker("tnt-a", CATALOG_UUID, "ac-2")
            await app.workers.wait_for_complete()
            await pilot.pause()
            control_table = app.query_one("#control-table", DataTable)
            assert control_table.row_count == 1

    _run_async(scenario)

    illegal = set(backend.calls) - ALLOWLISTED_OPS
    assert not illegal, f"control view issued non-allowlisted ops: {sorted(illegal)}"
    assert "scan" not in backend.calls
    assert "receive_message" not in backend.calls


# --------------------------------------------------------------------------
# 2. Non-TTY failure path — fails fast, no AWS client built.
# --------------------------------------------------------------------------


def test_run_fails_without_tty_and_builds_no_aws_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # stdout is not a terminal (the usual case under pytest / a pipe / CI).
    monkeypatch.setattr(tui_command.sys.stdout, "isatty", lambda: False)

    # If any AWS-client construction were reached, this would explode — proving
    # the TTY guard runs BEFORE step 2/3/4 (no boto3 session, no ReadOnlyAwsClient).
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no AWS client may be built when stdout is not a TTY")

    monkeypatch.setattr(tui_command, "_resolve_region", _boom)

    rc = run(_args())

    assert rc != 0
    err = capsys.readouterr().err
    assert "interactive terminal" in err
    assert "TTY" in err


def test_run_fails_when_no_region_after_tty_ok(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # TTY check passes, but no region is resolvable -> clear failure with a hint,
    # and still no app is launched (build_app must not be reached).
    monkeypatch.setattr(tui_command.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(tui_command, "_resolve_region", lambda: None)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("build_app must not be reached when region is unresolved")

    monkeypatch.setattr(tui_command, "build_app", _boom)

    rc = run(_args())

    assert rc != 0
    err = capsys.readouterr().err
    assert "region" in err.lower()
    assert "AWS_REGION" in err  # the actionable hint


# --------------------------------------------------------------------------
# 3. Parser smoke test — the subcommand is registered with the right shape.
# --------------------------------------------------------------------------


def test_tui_subcommand_is_registered_in_the_parser() -> None:
    ns = build_parser().parse_args(["tui"])
    assert getattr(ns, "handler", None) is run
    assert ns.stage == "alpha"
    assert ns.region_code == "cmh"
    assert ns.watch_interval == 3.0


def test_tui_subcommand_accepts_overrides() -> None:
    ns = build_parser().parse_args(
        ["tui", "--stage", "beta", "--region-code", "iad", "--watch-interval", "10"]
    )
    assert ns.stage == "beta"
    assert ns.region_code == "iad"
    assert ns.watch_interval == 10.0


def test_tui_subcommand_accepts_app_name_override() -> None:
    ns = build_parser().parse_args(["tui", "--app-name", "cairnproofs4af"])
    assert ns.app_name == "cairnproofs4af"
    # Default is None so the env/SSM fallbacks apply.
    assert build_parser().parse_args(["tui"]).app_name is None


# --------------------------------------------------------------------------
# 4. appName resolution — the real-stack build path must resolve appName via
#    SSM BEFORE ArtifactSources composes any table/queue name (regression: a
#    bare DeploymentTarget raised AppNameUnresolvedError at `cairn tui` start).
# --------------------------------------------------------------------------


class _StubDdbResource:
    """Minimal DynamoDB resource — `.Table(name)` returns an inert table.

    Construction of ArtifactSources' MetadataIndex/ControlsIndex facades over
    this never issues a query, so the inert table is sufficient.
    """

    def Table(self, name: str) -> Any:  # boto3 API name
        return object()


class _StubSsm:
    """Records the requested parameter name and returns a canned value.

    ``value`` is the default returned for any name; ``values`` overrides it
    per-parameter-name (used to return distinct table-name discovery values).
    """

    def __init__(
        self,
        *,
        value: str | None = None,
        values: dict[str, str] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._value = value
        self._values = values or {}
        self._raise = raise_exc
        self.requested: list[str] = []

    def get_parameter(self, *, Name: str) -> dict[str, Any]:  # boto3 API shape
        self.requested.append(Name)
        if Name in self._values:
            return {"Parameter": {"Value": self._values[Name]}}
        if self._raise is not None:
            raise self._raise
        return {"Parameter": {"Value": self._value}}


def _ro_client(ssm: _StubSsm) -> Any:
    from cairn_cli.tui._readonly_client import ReadOnlyAwsClient

    return ReadOnlyAwsClient(
        region_name=REGION,
        dynamodb_resource=_StubDdbResource(),
        ssm_client=ssm,
    )


def test_build_app_resolves_app_name_from_ssm_discovery() -> None:
    # No --app-name, no env override -> appName comes from the SSM discovery
    # parameter, then the auto-named DynamoDB table physical names are resolved
    # from their own SSM discovery parameters (resolve_table_names).
    ssm = _StubSsm(
        value="cairnproofs4af",
        values={
            "/cairnproofs/cmh-alpha/app-name": "cairnproofs4af",
            # The tables are auto-named, so SSM carries CFN-generated names.
            "/cairnproofs4af/alpha/tables/metadata": "PassedStack-MetadataTableABC123",
            "/cairnproofs4af/alpha/tables/control-claims": "PassedStack-ControlClaimsDEF456",
            "/cairnproofs4af/alpha/tables/identity-registry": "PassedStack-IdentityRegistryGHI789",
        },
    )
    app = build_app(_args(), region=REGION, client=_ro_client(ssm))

    # appName discovery happens first, then the three table-name lookups.
    assert ssm.requested[0] == "/cairnproofs/cmh-alpha/app-name"
    assert "/cairnproofs4af/alpha/tables/metadata" in ssm.requested
    assert "/cairnproofs4af/alpha/tables/control-claims" in ssm.requested
    assert "/cairnproofs4af/alpha/tables/identity-registry" in ssm.requested
    assert app._deployment_target is not None
    assert app._deployment_target.app_name == "cairnproofs4af"
    # The table name is the SSM-published auto-name, NOT a reconstructed one.
    assert app._deployment_target.metadata_table == "PassedStack-MetadataTableABC123"


def test_build_app_app_name_flag_skips_appname_lookup() -> None:
    # --app-name short-circuits the appName SSM discovery lookup. Table-name
    # discovery still queries SSM (the tables are auto-named, so their physical
    # names live ONLY in SSM); a missing/unreadable table param falls back to
    # the legacy name_for reconstruction, so the build still succeeds.
    ssm = _StubSsm(raise_exc=RuntimeError("ParameterNotFound"))
    args = build_parser().parse_args(["tui", "--app-name", "cairnproofsxyz"])
    app = build_app(args, region=REGION, client=_ro_client(ssm))

    # appName came from the flag, so its discovery param was NOT consulted.
    assert "/cairnproofs/cmh-alpha/app-name" not in ssm.requested
    assert app._deployment_target.app_name == "cairnproofsxyz"
    # Table-name discovery was attempted; the params are unreadable here, so the
    # property falls back to the name_for reconstruction (best-effort discovery).
    assert "/cairnproofsxyz/alpha/tables/metadata" in ssm.requested
    assert app._deployment_target.metadata_table == "cairnproofsxyz-ArtifactMetadata-cmh-alpha"


def test_build_app_raises_app_name_unresolved_when_ssm_absent() -> None:
    from cairn_cli._aws import AppNameUnresolvedError

    ssm = _StubSsm(raise_exc=RuntimeError("ParameterNotFound"))
    with pytest.raises(AppNameUnresolvedError):
        build_app(_args(), region=REGION, client=_ro_client(ssm))


def test_run_surfaces_app_name_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # TTY + region OK, but appName cannot resolve -> a friendly CLI error with a
    # hint and a non-zero exit, NOT the AppNameUnresolvedError traceback the user
    # hit when the resolution step was missing.
    from cairn_cli._aws import AppNameUnresolvedError

    monkeypatch.setattr(tui_command.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(tui_command, "_resolve_region", lambda: REGION)

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise AppNameUnresolvedError("appName for deployment 'cmh-alpha' is empty/unresolved")

    monkeypatch.setattr(tui_command, "build_app", _raise)

    rc = run(_args())

    assert rc != 0
    err = capsys.readouterr().err
    assert "appName" in err
    assert "--app-name" in err  # the actionable hint
    assert "Traceback" not in err


def test_tui_subcommand_help_has_usage_and_examples() -> None:
    help_text = subparser_help(build_parser(), "tui")
    assert "usage" in help_text
    assert "Examples:" in help_text
    assert "cairn tui --stage alpha" in help_text

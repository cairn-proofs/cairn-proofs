"""Tests for By-Project / Control query error resilience.

When the operator-driven query workers (``_refresh_project_worker`` /
``_refresh_control_worker``) hit a botocore ``ClientError`` / ``BotoCoreError``
from the Metadata_Index read, the exception MUST NOT escape the Textual thread
worker or terminate the session — the worker has an error boundary (like the lazy
detail path, which degrades to ``None``).

These tests assert that behavior: the session stays interactive and a
non-fatal, render-safe status indication appears in-view, distinct from an
empty success.

Self-contained by design (own raising stub + pilot harness mirroring
``test_app.py``); safe under ``pytest -n auto`` (no module-level mutable state).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from botocore.exceptions import ClientError, EndpointConnectionError
from cairn_cli._aws import DeploymentTarget
from cairn_cli.tui._render_safety import render_safe
from cairn_cli.tui._sources import BundleDetail, DlqDepth
from cairn_cli.tui.app import ArtifactStoreApp, format_read_error

TARGET = DeploymentTarget(stage="alpha", region_code="cmh", app_name="cairnproofs4af")
QUALIFIER = "cmh-alpha"
ACCOUNT_ID = "123456789012"


def _run_async(coro_fn: Callable[[], Awaitable[None]]) -> None:
    """Drive a Textual pilot coroutine on a fresh event loop (see test_app.py)."""
    asyncio.run(coro_fn())


def _resource_not_found() -> ClientError:
    """A botocore ClientError mirroring a missing/forbidden Metadata_Index table."""
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "Requested resource not found"}},
        "Query",
    )


class _Page:
    def __init__(self, items: list[dict[str, Any]], next_token: str | None = None) -> None:
        self.items = items
        self.next_token = next_token


class _RaisingSources:
    """ArtifactSources stub whose project/control listing can RAISE.

    Mirrors the surface the app touches. ``project_error`` / ``control_error``,
    when set, are raised from the corresponding listing method to reproduce a
    DynamoDB ``ClientError`` surfacing through the thread worker.
    """

    def __init__(
        self,
        *,
        bundles: list[dict[str, Any]] | None = None,
        project_error: BaseException | None = None,
        control_error: BaseException | None = None,
        dlq: list[DlqDepth] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._bundles = bundles or []
        self._project_error = project_error
        self._control_error = control_error
        self._dlq = dlq or [
            DlqDepth(stage=s, queue_name=f"q-{s}", approximate_messages=0)
            for s in ("validate", "sign", "archive", "metadata")
        ]

    def list_all_bundles(self) -> list[dict[str, Any]]:
        self.calls.append("list_all_bundles")
        return list(self._bundles)

    def get_bundle_detail(self, record: dict[str, Any]) -> BundleDetail:
        self.calls.append("get_bundle_detail")
        return BundleDetail(record=record, manifest=None, s3_head=None)

    def list_control_bundles(
        self, *, catalog_or_profile_uuid: str, control_id: str, **_: Any
    ) -> _Page:
        self.calls.append("list_control_bundles")
        if self._control_error is not None:
            raise self._control_error
        return _Page([])

    def list_project_bundles(
        self, *, tenant_id: str, project_id: str, page_token: str | None = None, **_: Any
    ) -> _Page:
        self.calls.append("list_project_bundles")
        if self._project_error is not None:
            raise self._project_error
        return _Page([])

    def dlq_health(self) -> list[DlqDepth]:
        self.calls.append("dlq_health")
        return list(self._dlq)

    def resolve_identity_names(self, opaque_ids: Any) -> dict[str, str]:
        self.calls.append("resolve_identity_names")
        return {}


def _app(stub: _RaisingSources) -> ArtifactStoreApp:
    return ArtifactStoreApp(stub, deployment_target=TARGET)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 1. Project error -> session stays alive.
# --------------------------------------------------------------------------


def test_project_query_error_keeps_session_running() -> None:
    stub = _RaisingSources(project_error=_resource_not_found())
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import Input, TabbedContent

            app.query_one(TabbedContent).active = "tab-project"
            app.query_one("#tenant-id", Input).value = "mvp-tenant"
            inp = app.query_one("#proj-id", Input)
            inp.value = "mvp-tenant"
            app.on_input_submitted(Input.Submitted(inp, "mvp-tenant"))
            await app.workers.wait_for_complete()
            await pilot.pause()

            # The worker hit the error path — and the session did NOT crash.
            assert "list_project_bundles" in stub.calls
            assert app.is_running is True

    _run_async(scenario)


# --------------------------------------------------------------------------
# 2. Project error -> non-empty status, zero rows.
# --------------------------------------------------------------------------


def test_project_query_error_shows_status_and_zero_rows() -> None:
    stub = _RaisingSources(project_error=_resource_not_found())
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable, Input, Label, TabbedContent

            app.query_one(TabbedContent).active = "tab-project"
            app.query_one("#tenant-id", Input).value = "mvp-tenant"
            inp = app.query_one("#proj-id", Input)
            inp.value = "mvp-tenant"
            app.on_input_submitted(Input.Submitted(inp, "mvp-tenant"))
            await app.workers.wait_for_complete()
            await pilot.pause()

            table = app.query_one("#project-table", DataTable)
            assert table.row_count == 0
            status = str(app.query_one("#proj-status", Label).render())
            assert status.strip() != "", "error status must be non-empty (error != empty success)"

    _run_async(scenario)


# --------------------------------------------------------------------------
# 3. Control error -> session stays alive with status (Unchanged clause #3).
# --------------------------------------------------------------------------


def test_control_query_error_keeps_session_running_with_status() -> None:
    stub = _RaisingSources(control_error=_resource_not_found())
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import Input, Label, TabbedContent

            app.query_one(TabbedContent).active = "tab-control"
            app.query_one("#ctl-tenant-id", Input).value = "tnt-a"
            uuid_inp = app.query_one("#ctl-uuid", Input)
            id_inp = app.query_one("#ctl-id", Input)
            uuid_inp.value = "11111111-1111-1111-1111-111111111111"
            id_inp.value = "ac-2"
            app.on_input_submitted(Input.Submitted(id_inp, "ac-2"))
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert "list_control_bundles" in stub.calls
            assert app.is_running is True
            status = str(app.query_one("#ctl-status", Label).render())
            assert status.strip() != "", "control error status must be non-empty"

    _run_async(scenario)


# --------------------------------------------------------------------------
# 4. Recovered control query clears the stale error status (symmetry guard).
# --------------------------------------------------------------------------


def test_control_query_recovery_clears_stale_status() -> None:
    stub = _RaisingSources(control_error=_resource_not_found())
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import Input, Label, TabbedContent

            app.query_one(TabbedContent).active = "tab-control"
            app.query_one("#ctl-tenant-id", Input).value = "tnt-a"
            uuid_inp = app.query_one("#ctl-uuid", Input)
            id_inp = app.query_one("#ctl-id", Input)
            uuid_inp.value = "11111111-1111-1111-1111-111111111111"
            id_inp.value = "ac-2"
            # First submit fails -> status populated.
            app.on_input_submitted(Input.Submitted(id_inp, "ac-2"))
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert str(app.query_one("#ctl-status", Label).render()).strip() != ""

            # The table becomes readable; the next query succeeds and MUST clear
            # the stale error banner so good data is not shadowed by it.
            stub._control_error = None
            app.on_input_submitted(Input.Submitted(id_inp, "ac-2"))
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.is_running is True
            assert str(app.query_one("#ctl-status", Label).render()).strip() == ""

    _run_async(scenario)


# --------------------------------------------------------------------------
# 5. format_read_error is pure + render-safe + never echoes producer data.
# --------------------------------------------------------------------------


def test_format_read_error_uses_code_only_and_is_render_safe() -> None:
    # A crafted error whose MESSAGE carries control bytes + a producer marker:
    # the formatted string must surface the fixed CODE token and NOTHING from
    # the message (no marker, no escape sequence) — and be inert under
    # render_safe (defense in depth at the eventual render boundary).
    hostile = ClientError(
        {
            "Error": {
                "Code": "ResourceNotFoundException",
                "Message": "table \x1b[31mPROducer-MARKER\x07 arn:aws:dynamodb:...",
            }
        },
        "Query",
    )
    msg = format_read_error(hostile)
    assert "ResourceNotFoundException" in msg
    assert "PROducer-MARKER" not in msg
    assert "\x1b" not in msg and "\x07" not in msg
    assert render_safe(msg) == msg  # already inert; no control bytes to strip


def test_format_read_error_falls_back_for_non_clienterror_and_malformed() -> None:
    # A transport-level BotoCoreError carries no error code -> generic fallback.
    transport = format_read_error(EndpointConnectionError(endpoint_url="https://x"))
    assert "read failed: ReadError" in transport

    # A degenerate ClientError with a non-dict response MUST NOT raise inside the
    # handler (that would re-introduce the very crash this boundary prevents).
    malformed = ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "Query")
    malformed.response = None  # type: ignore[assignment]
    assert "read failed: ReadError" in format_read_error(malformed)


def test_format_read_error_is_code_aware_and_names_table_and_region() -> None:
    # Not-found names the table + region and points at the operator's knobs.
    rnf = format_read_error(
        _resource_not_found(),
        resource="cairnproofs4af-ArtifactMetadata-cmh-alpha",
        region="us-east-2",
    )
    assert "ResourceNotFoundException" in rnf
    assert "cairnproofs4af-ArtifactMetadata-cmh-alpha" in rnf
    assert "us-east-2" in rnf
    assert "not found" in rnf
    assert "--stage" in rnf  # actionable pointer

    # Access denial phrases differently — and does NOT claim "not found".
    denied = format_read_error(
        ClientError({"Error": {"Code": "AccessDeniedException"}}, "Query"),
        resource="cairnproofs4af-Controls-cmh-alpha",
        region="us-east-2",
    )
    assert "AccessDeniedException" in denied
    assert "access" in denied.lower()
    assert "not found" not in denied
    assert "cairnproofs4af-Controls-cmh-alpha" in denied

    # Throttling is retryable, not a misconfiguration.
    throttled = format_read_error(
        ClientError({"Error": {"Code": "ThrottlingException"}}, "Query"),
        resource="T",
        region="us-east-2",
    )
    assert "retry" in throttled.lower()

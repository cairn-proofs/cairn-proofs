"""Regression guards for the TUI surface.

These property tests pin the TUI invariants:

- ``list_all_bundles`` sources validated bundles via
  per-project ``query_by_project`` key-condition queries and issues NO
  ``dynamodb:Scan``; the ECR-diff path issues only ``DescribeImages`` /
  image reads.
- Validated bundles are not duplicated as unvalidated entries.
- DLQ health and all refreshes (incl. watch ticks) issue no
  ``sqs:ReceiveMessage`` and tap no EventBridge subscription.
- The watch interval clamp is unaffected by the notifications.
- Every new string (unvalidated marker + notification text)
  passes through the shared render-safety neutralization, so control/escape
  bytes are stripped before reaching the terminal.

The recording client mirrors the ReadOnlyAwsClient surface; ``scan`` /
``receive_message`` are structurally ABSENT so a regression that reaches for
them fails loudly with ``AttributeError`` rather than passing silently.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from cairn_cli._aws import DeploymentTarget
from cairn_cli.tui._render_safety import render_safe
from cairn_cli.tui._sources import ArtifactSources, DlqDepth
from cairn_cli.tui.app import _UNVALIDATED_MARKER, ArtifactStoreApp
from rich.text import Text

REGION = "us-east-2"
ACCOUNT_ID = "123456789012"
TARGET = DeploymentTarget(stage="alpha", region_code="cmh", app_name="cairnproofs4af")
QUALIFIER = "cmh-alpha"


# --------------------------------------------------------------------------
# Recording client: ReadOnlyAwsClient-shaped, records every op it serves.
# scan / receive_message are deliberately absent (structural no-Scan / no-
# ReceiveMessage guard).
# --------------------------------------------------------------------------


class _RecordingTable:
    def __init__(self, name: str, calls: list[str], pages_by_pk: dict[str, list[dict]]) -> None:
        self._name = name
        self._calls = calls
        self._pages_by_pk = pages_by_pk

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self._calls.append("query")
        pk = kwargs["ExpressionAttributeValues"][":pk"]
        pages = self._pages_by_pk.get(pk, [{"Items": []}])
        return pages[0] if pages else {"Items": []}

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
        # Read-only BatchGetItem (Identity_Registry join); no items registered,
        # so it resolves nothing — sufficient for the allowlist/no-Scan guards.
        self._calls.append("batch_get_item")
        return {"Responses": {}}


class _RecordingClient:
    def __init__(
        self,
        *,
        ecr_pages: list[dict],
        pages_by_pk: dict[str, list[dict]] | None = None,
        images_by_repo: dict[str, list[dict]] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._ecr_pages = ecr_pages
        self._resource = _RecordingResource(self.calls, pages_by_pk or {})
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
        # The per-unindexed-image manifest read (ecr:BatchGetImage) the data
        # layer now issues to classify each orphan. These guards assert the
        # allowlist + non-duplication, not the classification value, so an empty
        # image set (which degrades to "unclassified") is a sufficient stub.
        self.calls.append("batch_get_image")
        return {"images": []}

    def get_queue_attributes(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("get_queue_attributes")
        return {"Attributes": {"ApproximateNumberOfMessages": "0"}}


def _repo(name: str) -> dict[str, Any]:
    return {"repositoryName": name, "registryId": ACCOUNT_ID}


def _record(project_id: str, digest: str, **extra: Any) -> dict[str, Any]:
    return {"projectId": project_id, "manifestDigest": digest, **extra}


def _image(digest: str, *, tag: str = "other--_ref--0123456789ab") -> dict[str, Any]:
    return {"imageDigest": digest, "imageTags": [tag]}


def _sources(client: _RecordingClient) -> ArtifactSources:
    return ArtifactSources(target=TARGET, client=client, region=REGION)


def _client_with_orphan() -> _RecordingClient:
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/mvp-tenant/mvp-tenant"
    validated_digest = "sha256:" + "a" * 64
    orphan_digest = "sha256:" + "b" * 64
    return _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        pages_by_pk={"mvp-tenant": [{"Items": [_record("mvp-tenant", validated_digest)]}]},
        images_by_repo={repo: [_image(validated_digest), _image(orphan_digest)]},
    )


# --------------------------------------------------------------------------
# Unchanged #4 / #5 — validated bundles via query, new path only DescribeImages,
# never a Scan / GetQueueUrl / receive_message.
# --------------------------------------------------------------------------


def test_list_all_bundles_uses_query_and_describe_images_never_scan() -> None:
    client = _client_with_orphan()
    _sources(client).list_all_bundles()

    # Validated bundles still sourced via per-project key-condition Query.
    assert "query" in client.calls, "validated bundles must ride query_by_project (Query)"
    # New unvalidated path rides only DescribeImages image reads.
    assert "describe_images" in client.calls
    # No unscoped Scan, ever — and the op is structurally absent on the client.
    assert "scan" not in client.calls
    assert not hasattr(client, "scan")


def test_list_all_bundles_issues_no_message_consuming_op() -> None:
    client = _client_with_orphan()
    _sources(client).list_all_bundles()

    assert "receive_message" not in client.calls
    assert not hasattr(client, "receive_message")
    # The list path consumes no DLQ messages and taps no event bus.
    assert "get_queue_attributes" not in client.calls


# --------------------------------------------------------------------------
# Validated bundles are not duplicated as unvalidated entries.
# --------------------------------------------------------------------------


def test_validated_bundle_not_duplicated_as_unvalidated() -> None:
    client = _client_with_orphan()
    bundles = _sources(client).list_all_bundles()

    validated_digest = "sha256:" + "a" * 64
    matching = [b for b in bundles if b.get("manifestDigest") == validated_digest]
    assert len(matching) == 1, f"validated digest appeared {len(matching)} times: {matching!r}"
    assert matching[0].get("status") != "unvalidated"

    unvalidated = [b for b in bundles if b.get("status") == "unvalidated"]
    assert len(unvalidated) == 1
    assert unvalidated[0]["manifestDigest"] == "sha256:" + "b" * 64


def test_no_repos_yields_no_image_reads() -> None:
    client = _RecordingClient(ecr_pages=[{"repositories": []}])
    assert _sources(client).list_all_bundles() == []
    # No project IDs -> no describe_images and no query fan-out.
    assert "describe_images" not in client.calls
    assert "query" not in client.calls


def test_describe_images_failure_degrades_to_validated_only() -> None:
    """One repo's ecr:DescribeImages failure must not blank the all-bundles view.

    A delete race (RepositoryNotFoundException) or an operator lacking
    ecr:DescribeImages must degrade to 'validated-only for this repo' rather
    than discarding the already-fetched validated bundles or killing the worker.
    Guards the aggregate-review M1 finding.
    """
    from botocore.exceptions import ClientError

    base = _client_with_orphan()

    class _RaisingClient(_RecordingClient):
        def describe_images(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append("describe_images")
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no ecr:DescribeImages"}},
                "DescribeImages",
            )

    client = _RaisingClient(
        ecr_pages=base._ecr_pages,
        pages_by_pk={"mvp-tenant": [{"Items": [_record("mvp-tenant", "sha256:" + "a" * 64)]}]},
        images_by_repo=base._images_by_repo,
    )

    bundles = _sources(client).list_all_bundles()

    # Validated bundle still surfaced; no unvalidated entries (image read failed).
    assert [b["manifestDigest"] for b in bundles] == ["sha256:" + "a" * 64]
    assert all(b.get("status") != "unvalidated" for b in bundles)
    assert "describe_images" in client.calls  # the failing read was attempted


def test_list_path_issues_no_list_image_referrers() -> None:
    # Regression guard: the all-bundles LIST path builds the
    # referrer grouping from the manifests it already reads (ecr:BatchGetImage)
    # and the per-project queries — it must NOT issue ecr:ListImageReferrers
    # (that op is reserved for the detail path). The recording client
    # has no list_image_referrers method, so reaching for it raises AttributeError
    # — this asserts the structural absence holds end-to-end through list_all_bundles.
    client = _client_with_orphan()
    _sources(client).list_all_bundles()

    assert "list_image_referrers" not in client.calls
    assert not hasattr(client, "list_image_referrers")
    # The grouping inputs ARE the already-issued reads.
    assert "describe_images" in client.calls
    assert "batch_get_image" in client.calls  # per-unindexed-image manifest read
    assert "query" in client.calls


def test_focused_referrer_raw_manifest_read_is_batch_get_image() -> None:
    # Regression guard: the focused-referrer raw-manifest
    # drill-down rides ONLY the allowlisted ecr:BatchGetImage (the SAME op the
    # list/detail paths use — no new call type), never ListImageReferrers/Scan.
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/mvp-tenant/mvp-tenant"
    referrer_digest = "sha256:" + "c" * 64
    client = _RecordingClient(ecr_pages=[{"repositories": [_repo(repo)]}])
    manifest = _sources(client).get_referrer_manifest(
        registry_id=ACCOUNT_ID, repository_name=repo, manifest_digest=referrer_digest
    )

    # Exactly the allowlisted BatchGetImage was issued (the stub returns an empty
    # image set, which degrades to None — we assert the CALL TYPE, not the value).
    assert client.calls == ["batch_get_image"]
    assert manifest is None  # empty images -> degrade-don't-crash
    assert "list_image_referrers" not in client.calls
    assert not hasattr(client, "list_image_referrers")
    assert "scan" not in client.calls


def test_dlq_health_is_attributes_only_never_receive() -> None:
    client = _client_with_orphan()
    depths = _sources(client).dlq_health()

    assert [d.stage for d in depths] == ["validate", "sign", "archive", "metadata"]
    assert "get_queue_attributes" in client.calls
    assert "receive_message" not in client.calls
    assert not hasattr(client, "receive_message")


# --------------------------------------------------------------------------
# App-level: refreshes (incl. watch ticks) consume no DLQ messages / no bus;
# the watch interval clamp is unchanged by the notifications.
# --------------------------------------------------------------------------


class _StubSources:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_all_bundles(self) -> list[dict[str, Any]]:
        self.calls.append("list_all_bundles")
        return [_record("proj-a", "sha256:" + "a" * 64, artifactKind="other")]

    def dlq_health(self) -> list[DlqDepth]:
        self.calls.append("dlq_health")
        return [
            DlqDepth(stage=s, queue_name=f"q-{s}", approximate_messages=0)
            for s in ("validate", "sign", "archive", "metadata")
        ]

    def resolve_identity_names(self, opaque_ids: Any) -> dict[str, str]:
        # Read-time Identity_Registry join ([IDREG-001]); a benign read surface.
        self.calls.append("resolve_identity_names")
        return {}


def _run_async(coro_fn: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(coro_fn())


def test_refresh_and_watch_use_only_allowlisted_reads() -> None:
    stub = _StubSources()
    app = ArtifactStoreApp(stub, deployment_target=TARGET, watch_interval=1.0)  # type: ignore[arg-type]

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            app.action_refresh()
            await pilot.pause()
            await app.workers.wait_for_complete()
            app.action_toggle_watch()  # enable a watch tick path
            await pilot.pause()
            await app.workers.wait_for_complete()

    _run_async(scenario)

    # Only the sanctioned read surfaces were invoked — no message-consuming or
    # event-bus method exists on the stub for the app to have called.
    assert set(stub.calls) <= {"list_all_bundles", "dlq_health", "resolve_identity_names"}
    assert not hasattr(stub, "receive_message")
    assert not hasattr(stub, "subscribe")


def test_watch_interval_clamp_unchanged_by_notifications() -> None:
    # Below-minimum interval is clamped up; the notification feedback must not
    # alter the clamp (Unchanged #7).
    app = ArtifactStoreApp(
        _StubSources(),  # type: ignore[arg-type]
        deployment_target=TARGET,
        watch_interval=0.2,
        min_watch_interval=1.0,
    )
    assert app._watch_interval == 1.0  # clamped at construction

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()

            app.action_toggle_watch()  # enable
            await pilot.pause()
            assert app.is_watching is True
            assert app._watch_interval == 1.0  # unchanged by the toggle/notify
            assert app._min_watch_interval == 1.0

            app.action_toggle_watch()  # disable
            await pilot.pause()
            assert app.is_watching is False
            assert app._watch_interval == 1.0

    _run_async(scenario)


# --------------------------------------------------------------------------
# Unchanged #8 — every new string passes through render-safety neutralization.
# --------------------------------------------------------------------------


def test_unvalidated_marker_is_render_safe() -> None:
    # The marker itself is inert; render_safe is idempotent on it.
    assert render_safe(_UNVALIDATED_MARKER) == _UNVALIDATED_MARKER


def test_unvalidated_entry_cells_are_neutralized() -> None:
    """A hostile tag/repo on an unvalidated entry is stripped before render."""
    hostile_tag = "ev\x1b]0;pwn\x07il--\x9b31m"
    hostile_digest = "sha256:\x1b[2J" + "c" * 60
    app = ArtifactStoreApp(_StubSources(), deployment_target=TARGET)  # type: ignore[arg-type]
    entry = {
        "projectId": "proj-a",
        "repositoryName": f"cairnproofs4af-artifact-{QUALIFIER}/proj-a",
        "manifestDigest": hostile_digest,
        "imageTag": hostile_tag,
        "status": "unvalidated",
    }

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            app._populate_bundle_table([entry])
            await pilot.pause()
            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            # Every rendered cell is a literal Text whose plain form carries no
            # control/escape bytes (render-safety neutralization).
            for row_key in table.rows:
                for cell in table.get_row(row_key):
                    assert isinstance(cell, Text)
                    plain = cell.plain
                    assert "\x1b" not in plain
                    assert "\x9b" not in plain
                    assert "\x07" not in plain
                    assert plain == render_safe(plain)
            # This entry carries no classification, so the artifactKind column
            # falls back to the unvalidated marker (signing/archive now read
            # "unknown"); the marker is therefore present somewhere in the row.
            first = next(iter(table.rows))
            assert any(_UNVALIDATED_MARKER in c.plain for c in table.get_row(first))

    _run_async(scenario)


def test_unvalidated_entry_surfaces_its_tag_in_detail_and_classification_in_list() -> None:
    """Spec: a did-not-land entry shows its repository, TAG, and digest.

    The ECR image tag (captured on the synthetic entry) must still reach the
    operator in the detail pane's cross-references and pass render-safety
    (guards the M1 review finding: tag collected but never rendered). In the
    LIST row, the artifactKind column now identifies the entry by its
    CLASSIFICATION (what the artifact is) rather than echoing the raw tag — the
    tag/digest stay visible in their own columns. With no classification on this
    entry the column degrades to the unvalidated marker, and the bare tag must
    NOT appear in the artifactKind cell (spec: never a bare ``sha256-…`` tag).
    """
    tag = "other--_ref--0123456789ab"
    app = ArtifactStoreApp(_StubSources(), deployment_target=TARGET)  # type: ignore[arg-type]
    entry = {
        "projectId": "proj-a",
        "repositoryName": f"cairnproofs4af-artifact-{QUALIFIER}/proj-a",
        "manifestDigest": "sha256:" + "e" * 64,
        "imageTag": tag,
        "status": "unvalidated",
        "classification": "unvalidated bundle",
    }

    # Detail pane surfaces the tag (pure, render-safe composition).
    detail_lines = app.render_detail_lines(entry)
    assert any("ECR tag:" in line and tag in line for line in detail_lines), (
        f"detail pane did not surface the ECR tag; lines were {detail_lines!r}"
    )

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            app._populate_bundle_table([entry])
            await pilot.pause()
            from textual.coordinate import Coordinate
            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            # artifactKind column (index 1) shows the classification, not the tag.
            artifact_kind = table.get_cell_at(Coordinate(0, 1)).plain
            assert artifact_kind == "unvalidated bundle", (
                f"artifactKind column did not surface the classification; was {artifact_kind!r}"
            )
            assert tag not in artifact_kind, (
                f"artifactKind column echoed the raw ECR tag; was {artifact_kind!r}"
            )

    _run_async(scenario)


def test_refresh_on_empty_input_tab_still_notifies_completion() -> None:
    """Spec: an on-demand refresh shows invocation AND completion.

    On the project/control tab with an empty input no view worker is dispatched,
    so the off-thread completion never fires; ``action_refresh`` must emit the
    completion notification synchronously instead. Guards the L1 review finding.
    """
    from textual.widgets import TabbedContent

    app = ArtifactStoreApp(_StubSources(), deployment_target=TARGET)  # type: ignore[arg-type]
    notified: list[str] = []
    real_notify = app.notify

    def _spy(message: str, **kw: Any) -> None:
        notified.append(message)
        real_notify(message, **kw)

    app.notify = _spy  # type: ignore[method-assign]

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            app.query_one(TabbedContent).active = "tab-project"  # empty #proj-id
            await pilot.pause()
            notified.clear()

            app.action_refresh()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()

    _run_async(scenario)

    blob = " ".join(notified).lower()
    assert "refreshing" in blob, f"no invocation notification: {notified!r}"
    assert "complete" in blob, f"no completion notification on empty-input refresh: {notified!r}"


def test_refresh_and_watch_notifications_are_render_safe() -> None:
    app = ArtifactStoreApp(_StubSources(), deployment_target=TARGET, watch_interval=2.0)  # type: ignore[arg-type]
    notified: list[str] = []
    real_notify = app.notify

    def _spy(message: str, **kw: Any) -> None:
        notified.append(message)
        real_notify(message, **kw)

    app.notify = _spy  # type: ignore[method-assign]

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            notified.clear()
            app.action_refresh()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_toggle_watch()
            await pilot.pause()

    _run_async(scenario)

    assert notified, "expected refresh/watch notifications"
    for message in notified:
        # Notification text is render-safe (passed through self._safe).
        assert render_safe(message) == message
        assert "\x1b" not in message and "\x07" not in message

    _run_async(scenario)

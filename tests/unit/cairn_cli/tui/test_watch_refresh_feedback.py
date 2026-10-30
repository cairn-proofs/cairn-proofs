"""Watch-toggle and refresh actions emit operator-visible notifications.

Property:
- invoking ``action_toggle_watch`` produces a user-visible notification
  reflecting the new watch state (enabled / disabled), and
- invoking ``action_refresh`` produces an invocation notification followed by
  a completion notification.

The mechanism: ``App.notify`` feedback in ``action_toggle_watch`` (app.py:790)
and ``action_refresh`` (app.py:787, invocation + completion via
call_from_thread).

Observation: ``App.notify`` posts a ``Notify`` message internally; rather than
reach into Textual's notification deque, we spy on ``app.notify`` itself
(thread-safe by Textual's contract, and the single chokepoint the fix calls),
recording every message string. The refresh completion notification is emitted
off-thread via ``call_from_thread`` once the refresh worker finishes, so the
scenario waits for workers to complete before asserting.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from cairn_cli._aws import DeploymentTarget
from cairn_cli.tui._sources import DlqDepth
from cairn_cli.tui.app import ArtifactStoreApp

TARGET = DeploymentTarget(stage="alpha", region_code="cmh", app_name="cairnproofs4af")
QUALIFIER = "cmh-alpha"
ACCOUNT_ID = "123456789012"


def _run_async(coro_fn: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(coro_fn())


class _Page:
    def __init__(self, items: list[dict[str, Any]], next_token: str | None = None) -> None:
        self.items = items
        self.next_token = next_token


class _StubSources:
    """Minimal ArtifactSources surface the app's refresh/watch path uses."""

    def __init__(self, *, bundles: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[str] = []
        self._bundles = bundles or []

    def list_all_bundles(self) -> list[dict[str, Any]]:
        self.calls.append("list_all_bundles")
        return list(self._bundles)

    def dlq_health(self) -> list[DlqDepth]:
        self.calls.append("dlq_health")
        return [
            DlqDepth(stage=s, queue_name=f"q-{s}", approximate_messages=0)
            for s in ("validate", "sign", "archive", "metadata")
        ]

    def resolve_identity_names(self, opaque_ids: Any) -> dict[str, str]:
        self.calls.append("resolve_identity_names")
        return {}


def _record() -> dict[str, Any]:
    return {
        "projectId": "proj-a",
        "artifactKind": "assessment-results",
        "signingStatus": "signed",
        "archiveStatus": "archived",
        "manifestDigest": "sha256:" + "a" * 64,
        "createdAt": "2026-05-28T00:00:00Z",
        "repositoryName": f"cairnproofs4af-artifact-{QUALIFIER}/proj-a",
        "registryId": ACCOUNT_ID,
    }


def _app_with_notify_spy(stub: _StubSources) -> tuple[ArtifactStoreApp, list[str]]:
    app = ArtifactStoreApp(stub, deployment_target=TARGET)  # type: ignore[arg-type]
    notified: list[str] = []
    real_notify = app.notify

    def _spy(message: str, **kwargs: Any) -> None:
        notified.append(message)
        real_notify(message, **kwargs)

    app.notify = _spy  # type: ignore[method-assign]
    return app, notified


def test_toggle_watch_notifies_resulting_state() -> None:
    app, notified = _app_with_notify_spy(_StubSources(bundles=[_record()]))

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            notified.clear()

            app.action_toggle_watch()  # enable
            await pilot.pause()
            assert notified, (
                "action_toggle_watch (enable) emitted no notification — "
                "operator gets no visible indication watch turned on"
            )
            enable_msgs = " ".join(notified).lower()
            assert "watch" in enable_msgs and ("enabl" in enable_msgs or "on" in enable_msgs), (
                f"enable notification did not reflect watch state: {notified!r}"
            )

            notified.clear()
            app.action_toggle_watch()  # disable
            await pilot.pause()
            assert notified, "action_toggle_watch (disable) emitted no notification"
            disable_msgs = " ".join(notified).lower()
            assert "watch" in disable_msgs and (
                "disabl" in disable_msgs or "off" in disable_msgs
            ), f"disable notification did not reflect watch state: {notified!r}"

    _run_async(scenario)


def test_refresh_notifies_invocation_then_completion() -> None:
    app, notified = _app_with_notify_spy(_StubSources(bundles=[_record()]))

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            notified.clear()

            app.action_refresh()
            # Invocation notification is synchronous; wait for workers so the
            # off-thread completion notification has fired too.
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert len(notified) >= 2, (
                "action_refresh did not emit an invocation notification followed "
                f"by a completion notification; observed notifications: {notified!r}"
            )

    _run_async(scenario)

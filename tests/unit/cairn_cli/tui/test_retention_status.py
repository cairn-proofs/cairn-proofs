"""Retention status in the TUI bundle detail view ([TUI-016], [TUI-001.01]).

The value is read from the WORM_Receipt referrer annotation on the referrer
listing the detail view already fetches; no new AWS operation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cairn_cli.tui._sources import Referrer, ReferrerListing
from cairn_cli.tui.app import (
    RETENTION_EXPIRED,
    RETENTION_IN_FORCE,
    RETENTION_UNKNOWN,
    compose_prune_report_command,
    retention_status,
)
from cairn_oci import (
    NOTARY_SIGNATURE_ARTIFACT_TYPE,
    RETAIN_UNTIL_ANNOTATION,
    WORM_RECEIPT_ARTIFACT_TYPE,
)

from tests.unit.cairn_cli.tui.test_app import (
    QUALIFIER,
    TARGET,
    _record,
    _run_async,
    _select_row,
    _StubSources,
)

NOW = datetime(2034, 1, 1, tzinfo=UTC)
_HOSTILE = "\x1b[2J\x1b]52;c;cHduZWQ=\x07[bold]x[/bold]"


def _receipt(annotation: object = "2033-05-24T12:00:00Z", digest_char: str = "9") -> Referrer:
    annotations = {} if annotation is None else {RETAIN_UNTIL_ANNOTATION: annotation}
    return Referrer(
        artifact_type=WORM_RECEIPT_ARTIFACT_TYPE,
        digest="sha256:" + digest_char * 64,
        tag=None,
        annotations=annotations,  # type: ignore[arg-type]
    )


def _signature() -> Referrer:
    return Referrer(
        artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE,
        digest="sha256:" + "1" * 64,
        tag=None,
        annotations={RETAIN_UNTIL_ANNOTATION: "2000-01-01T00:00:00Z"},
    )


def _listing(*refs: Referrer, available: bool = True) -> ReferrerListing:
    return ReferrerListing(referrers=list(refs), available=available)


def _app(stub: _StubSources):
    from cairn_cli.tui.app import ArtifactStoreApp

    return ArtifactStoreApp(stub, deployment_target=TARGET, clock=lambda: NOW)  # type: ignore[arg-type]


def test_past_annotation_is_expired() -> None:
    """[TUI-016.01]"""
    value, status = retention_status(_listing(_signature(), _receipt()), NOW)
    assert value == datetime(2033, 5, 24, 12, tzinfo=UTC)
    assert status == RETENTION_EXPIRED


def test_future_annotation_is_in_force() -> None:
    _, status = retention_status(_listing(_receipt("2040-01-01T00:00:00Z")), NOW)
    assert status == RETENTION_IN_FORCE


@pytest.mark.parametrize(
    "listing",
    [
        _listing(_receipt(None)),
        _listing(_receipt("2033-05-24")),
        _listing(_receipt("2033-05-24T12:00:00+02:00")),
        _listing(_receipt(f"2033-05-24T12:00:00Z{_HOSTILE}")),
        _listing(_receipt(12345)),
        _listing(_signature()),  # a signature's annotation is never read
        _listing(_receipt(), _receipt("2033-05-24T12:00:00Z", "8")),  # two receipts
        _listing(_receipt(), available=False),
        None,
    ],
)
def test_missing_or_malformed_annotation_is_unknown(listing) -> None:
    """[TUI-016.02]"""
    assert retention_status(listing, NOW) == (None, RETENTION_UNKNOWN)


def test_rendered_lines_show_value_and_status() -> None:
    """[TUI-016.01] render-safe lines, prune REPORT command only."""
    app = _app(_StubSources())
    lines = app.render_retention_lines(_record(), _listing(_receipt()))
    blob = "\n".join(lines)
    assert "Retain_Until: 2033-05-24T12:00:00Z" in blob
    assert f"Retention:    {RETENTION_EXPIRED}" in blob
    assert "cairn prune --target org-a/tnt-a/proj-a --stage alpha --region-code cmh" in blob
    assert "--digest" not in blob


def test_malformed_annotation_never_renders_as_a_date() -> None:
    """[TUI-016.02] the raw value is not echoed."""
    app = _app(_StubSources())
    hostile = f"2033-05-24T12:00:00Z{_HOSTILE}"
    blob = "\n".join(app.render_retention_lines(_record(), _listing(_receipt(hostile))))
    assert "Retain_Until: unknown" in blob
    assert f"Retention:    {RETENTION_UNKNOWN}" in blob
    assert "2033-05-24" not in blob
    assert "\x1b" not in blob and "\x07" not in blob


@pytest.mark.parametrize(
    "repo",
    [
        f"artifact-{QUALIFIER}/org-a/tnt-a/PROJ;rm -rf",
        f"artifact-{QUALIFIER}/org-a/tnt-a",
        None,
    ],
)
def test_prune_command_requires_valid_coordinates(repo) -> None:
    record = _record(repositoryName=repo)
    assert compose_prune_report_command(record, stage="alpha", region_code="cmh") is None
    app = _app(_StubSources())
    blob = "\n".join(app.render_retention_lines(record, _listing(_receipt())))
    assert "cannot construct command" in blob
    assert "rm -rf" not in blob


def test_detail_view_shows_retention_and_offers_no_delete() -> None:
    """[TUI-016.01] / [TUI-016.03] / [TUI-001.01] through the pilot."""
    record = _record()
    stub = _StubSources(bundles=[record], referrers=_listing(_signature(), _receipt()))
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            _select_row(app, "bundle-table")
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import Static

            body = app.query_one("#retention-body", Static).content.plain
            assert "Retain_Until: 2033-05-24T12:00:00Z" in body
            assert RETENTION_EXPIRED in body
            assert "cairn prune --target org-a/tnt-a/proj-a" in body
            # No AWS call beyond the ones the detail view already made.
            assert set(stub.calls) <= {
                "list_all_bundles",
                "resolve_identity_names",
                "dlq_health",
                "get_bundle_detail",
                "get_bundle_referrers",
            }
            assert stub.calls.count("get_bundle_referrers") == 1
            # No prune / delete binding is offered.
            for binding in app.BINDINGS:
                key = binding if isinstance(binding, tuple) else (binding.key, binding.action)
                text = " ".join(str(x) for x in key).lower()
                assert "prune" not in text and "delete" not in text

    _run_async(scenario)


def test_readonly_client_allowlist_has_no_new_or_delete_operation() -> None:
    """[TUI-001.01] the retention view rides list_image_referrers; nothing is added."""
    from cairn_cli.tui._readonly_client import ReadOnlyAwsClient

    public = {n for n in dir(ReadOnlyAwsClient) if not n.startswith("_")}
    assert public == {
        "describe_repositories",
        "describe_images",
        "batch_get_image",
        "list_image_referrers",
        "head_object",
        "get_object",
        "get_queue_attributes",
        "get_parameter",
        "dynamodb_resource",
    }


def test_annotation_equal_to_now_is_expired() -> None:
    """[TUI-016.01] same boundary as the prune classifier: expired at Retain_Until."""
    _, status = retention_status(_listing(_receipt("2034-01-01T00:00:00Z")), NOW)
    assert status == RETENTION_EXPIRED
    _, status = retention_status(_listing(_receipt("2034-01-01T00:00:01Z")), NOW)
    assert status == RETENTION_IN_FORCE

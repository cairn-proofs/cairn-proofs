"""Tests for the Artifact_TUI Textual application (``app.ArtifactStoreApp``).

The app is the first component to render stored, producer-controlled values
into an interactive TTY, so the security-critical properties under test are:

1. **List + detail render** — bundles appear in the list; selecting one renders
   the lifecycle timeline + cross-references + OSCAL from the record (driven via
   Textual's ``App.run_test()`` pilot for a real render smoke).
2. **Hostile bundle inert across ALL views** — a record whose ``projectId``, an
   OSCAL ref/annotation, and a cross-ref (``signingProfileArn``) carry ANSI/OSC/
   DCS escapes + raw C0/C1 bytes renders with NO byte in ``\\x00..\\x1f``,
   ``\\x7f``, ``\\x80..\\x9f`` in the list, detail, OR control views.
3. **Verify string inert + not auto-executed** — the composed verify string is
   render-safe and built only from validated identifiers; the app never shells
   out (``subprocess`` / ``os.system`` are absent from the codepath and patched
   to fail if ever called).
4. **Watch-mode refresh issues only allowlisted reads** — driving a watch
   refresh against a recording read-only client issues only allowlisted ops
   (no ``scan``, no ``receive_message``, no mutators, no ``get_queue_url``).

The fetch/compose methods are plain synchronous functions on the app, so most
assertions drive them directly (no event loop); the pilot is reserved for the
list+detail render smoke.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from cairn_cli._aws import DeploymentTarget
from cairn_cli.tui._render_safety import render_safe
from cairn_cli.tui._sources import (
    NOTARY_SIGNATURE_ARTIFACT_TYPE,
    WORM_RECEIPT_ARTIFACT_TYPE,
    ArtifactSources,
    BundleDetail,
    DlqDepth,
    Referrer,
    ReferrerListing,
)
from cairn_cli.tui.app import (
    _PLACEHOLDER,
    _UNVALIDATED_MARKER,
    ArtifactStoreApp,
    compose_verify_command,
    compute_lifecycle,
    projection_trust_label,
)

TARGET = DeploymentTarget(stage="alpha", region_code="cmh", app_name="cairnproofs4af")
QUALIFIER = "cmh-alpha"
REGION = "us-east-2"
ACCOUNT_ID = "123456789012"

# A control-byte corpus: 7-bit OSC (title/clipboard), CSI (SGR color + cursor),
# DCS, plus raw C0/C1 bytes and DEL.
_HOSTILE = (
    "\x1b]0;PWNED\x07"  # OSC window title (BEL-terminated)
    "\x1b]52;c;BASE64\x07"  # OSC 52 clipboard write
    "\x1b[31mRED\x1b[0m"  # CSI SGR color
    "\x1b[2J\x1b[H"  # CSI clear + cursor home
    "\x1bPq\x1b\\"  # DCS sequence
    "\x9b1m"  # 8-bit C1 CSI
    "\x9d0;x\x9c"  # 8-bit C1 OSC
    "\x00\x07\x1f\x7f\x80\x9f"  # raw C0 / DEL / C1 bytes
)

# Every byte that must NEVER survive to the terminal.
_FORBIDDEN = set(range(0x00, 0x20)) | {0x7F} | set(range(0x80, 0xA0))


def _assert_inert(text: str) -> None:
    """Assert no control byte survived into a string the app renders."""
    offenders = [ch for ch in text if ord(ch) in _FORBIDDEN]
    assert not offenders, f"control bytes leaked: {[hex(ord(c)) for c in offenders]}"


def _run_async(coro_fn: Callable[[], Awaitable[None]]) -> None:
    """Drive a Textual pilot coroutine on a fresh event loop.

    ``pytest-asyncio`` is not a dependency, so the pilot tests are plain
    synchronous tests that run their coroutine via ``asyncio.run`` — Textual's
    ``App.run_test()`` works inside the fresh loop ``asyncio.run`` creates.
    """
    asyncio.run(coro_fn())


# --------------------------------------------------------------------------
# Recording stub ArtifactSources (drives fetch/compose without AWS).
# --------------------------------------------------------------------------


class _Page:
    def __init__(self, items: list[dict[str, Any]], next_token: str | None = None) -> None:
        self.items = items
        self.next_token = next_token


class _StubSources:
    """Records every data-layer call and returns canned data.

    Implements the ArtifactSources surface the app uses. Anything outside that
    surface is structurally absent, so a stray call fails loudly.
    """

    def __init__(
        self,
        *,
        bundles: list[dict[str, Any]] | None = None,
        control_items: list[dict[str, Any]] | None = None,
        project_pages: list[_Page] | None = None,
        dlq: list[DlqDepth] | None = None,
        detail_record: dict[str, Any] | None = None,
        referrers: ReferrerListing | None = None,
        bundle_manifest: dict[str, Any] | None = None,
        referrer_manifests: dict[str | None, dict[str, Any]] | None = None,
        identity_names: dict[str, str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        # Opaque id -> display name for the read-time Identity_Registry join.
        self._identity_names = identity_names or {}
        # The bundle manifest the detail worker returns inside BundleDetail —
        # wrapped in a batch_get_image-shaped response (images[0].imageManifest
        # is a JSON string), matching the real data layer.
        self._bundle_manifest = bundle_manifest
        # Maps a referrer's digest -> its parsed raw manifest for the lazy
        # focused-referrer read; absent digests return None ("unavailable").
        self._referrer_manifests = referrer_manifests or {}
        self.referrer_manifest_args: list[tuple[str | None, str | None, str | None]] = []
        # The lazy referrer listing returned for any selected bundle. Defaults to
        # the empty-but-available case ("no referrers"); tests override it to
        # exercise populated / unavailable listings.
        self._referrers = (
            referrers if referrers is not None else ReferrerListing(referrers=[], available=True)
        )
        self._bundles = bundles or []
        self._control_items = control_items or []
        # query_by_project pages, served in order; default single empty page.
        self._project_pages = project_pages if project_pages is not None else [_Page([])]
        self._project_page_index = 0
        self.project_ids_queried: list[str] = []
        self.tenant_ids_queried: list[str] = []
        self._dlq = dlq or [
            DlqDepth(stage=s, queue_name=f"q-{s}", approximate_messages=0)
            for s in ("validate", "sign", "archive", "metadata")
        ]
        self._detail_record = detail_record

    def list_all_bundles(self) -> list[dict[str, Any]]:
        self.calls.append("list_all_bundles")
        return list(self._bundles)

    def get_bundle_detail(self, record: dict[str, Any]) -> BundleDetail:
        self.calls.append("get_bundle_detail")
        manifest_response: dict[str, Any] | None = None
        if self._bundle_manifest is not None:
            # Mirror the real _fetch_manifest: BundleDetail.manifest is the raw
            # batch_get_image response whose imageManifest is a JSON string.
            import json as _json

            manifest_response = {"images": [{"imageManifest": _json.dumps(self._bundle_manifest)}]}
        return BundleDetail(record=record, manifest=manifest_response, s3_head=None)

    def get_bundle_referrers(self, record: dict[str, Any]) -> ReferrerListing:
        self.calls.append("get_bundle_referrers")
        return self._referrers

    def list_control_bundles(
        self, *, catalog_or_profile_uuid: str, control_id: str, **_: Any
    ) -> _Page:
        self.calls.append("list_control_bundles")
        return _Page(list(self._control_items))

    def list_project_bundles(
        self, *, tenant_id: str, project_id: str, page_token: str | None = None, **_: Any
    ) -> _Page:
        # Serves the configured pages in order; the app's fetch loop walks the
        # token to exhaustion, so each call advances to the next page.
        self.calls.append("list_project_bundles")
        self.project_ids_queried.append(project_id)
        self.tenant_ids_queried.append(tenant_id)
        index = self._project_page_index
        self._project_page_index += 1
        if index < len(self._project_pages):
            return self._project_pages[index]
        return _Page([])

    def resolve_identity_names(self, opaque_ids: Any) -> dict[str, str]:
        # Read-time Identity_Registry join ([IDREG-001]). Records the call and
        # returns the configured opaque-id -> display-name map (default empty, so
        # the app falls back to rendering the opaque id).
        self.calls.append("resolve_identity_names")
        return {i: self._identity_names[i] for i in opaque_ids if i in self._identity_names}

    def dlq_health(self) -> list[DlqDepth]:
        self.calls.append("dlq_health")
        return list(self._dlq)

    def get_referrer_manifest(
        self,
        *,
        registry_id: str | None,
        repository_name: str | None,
        manifest_digest: str | None,
    ) -> dict[str, Any] | None:
        # Records the lazy focused-referrer raw-manifest read. Returns the
        # manifest keyed by the requested digest (so a test can map a referrer's
        # digest to a specific manifest), defaulting to None ("unavailable").
        self.calls.append("get_referrer_manifest")
        self.referrer_manifest_args.append((registry_id, repository_name, manifest_digest))
        return self._referrer_manifests.get(manifest_digest)


def _app(stub: _StubSources, **kwargs: Any) -> ArtifactStoreApp:
    return ArtifactStoreApp(stub, deployment_target=TARGET, **kwargs)  # type: ignore[arg-type]


def _record(**extra: Any) -> dict[str, Any]:
    base = {
        "projectId": "proj-a",
        "tenantId": "tnt-a",
        "orgId": "org-a",
        "artifactKind": "assessment-results",
        "signingStatus": "signed",
        "archiveStatus": "archived",
        "manifestDigest": "sha256:" + "a" * 64,
        "createdAt": "2026-05-28T00:00:00Z",
        "repositoryName": f"artifact-{QUALIFIER}/org-a/tnt-a/proj-a",
        "registryId": ACCOUNT_ID,
        "validatedAt": "2026-05-28T00:00:01Z",
        "signedAt": "2026-05-28T00:00:02Z",
        "archivedAt": "2026-05-28T00:00:03Z",
        "s3Bucket": "cairn-worm-bucket",
        "s3KeyPrefix": "proj-a/sha256/aaa/",
        "signingProfileArn": "arn:aws:signer:us-east-2:1:signing-profiles/evidence",
        "oscalReferences": {"catalog": "nist-800-53"},
        "manifestAnnotations": {"org.cairn.note": "ok"},
    }
    base.update(extra)
    return base


# --------------------------------------------------------------------------
# 1. List + detail render (pilot smoke).
# --------------------------------------------------------------------------


def test_list_and_detail_render_via_pilot() -> None:
    record = _record()
    stub = _StubSources(bundles=[record])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            # Workers fan out on mount; let them complete and post results.
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            assert table.row_count == 1
            assert "list_all_bundles" in stub.calls
            assert "dlq_health" in stub.calls

            # Selecting the row triggers the lazy detail fetch.
            table.move_cursor(row=0)
            row_key = table.coordinate_to_cell_key((0, 0)).row_key
            app.on_data_table_row_selected(
                DataTable.RowSelected(table, cursor_row=0, row_key=row_key)
            )
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "get_bundle_detail" in stub.calls

    _run_async(scenario)


def test_detail_lines_contain_lifecycle_crossrefs_and_oscal() -> None:
    # Drives the compose method directly (no event loop) for content assertions.
    record = _record()
    app = _app(_StubSources())
    lines = app.render_detail_lines(record)
    blob = "\n".join(lines)

    assert "== Lifecycle ==" in blob
    assert "validate" in blob and "sign" in blob and "archive" in blob and "metadata" in blob
    assert "[x] validate" in blob  # validatedAt present -> done
    assert "== Cross-references ==" in blob
    assert "cairn-worm-bucket" in blob
    assert "arn:aws:signer" in blob
    assert "== OSCAL ==" in blob
    assert "OSCAL refs:" in blob
    assert "nist-800-53" in blob


def test_detail_omits_oscal_section_when_no_oscal_data() -> None:
    # A non-OSCAL bundle (or a drilled-into referrer child) carries no
    # oscalReferences on its system-bounded Metadata_Index record. The OSCAL
    # section must be omitted entirely, not rendered as a block of empty "—"
    # placeholders. The producer's control claim (controls, catalog/profile) is
    # never on the record — it lives in the control-claims projection
    # ([PCLAIM-001]) and the signed Producer_Attestation_Referrer.
    record = _record(oscalReferences=None)
    app = _app(_StubSources())
    blob = "\n".join(app.render_detail_lines(record))

    assert "== OSCAL ==" not in blob
    assert "OSCAL refs:" not in blob
    # The rest of the detail still renders.
    assert "== Lifecycle ==" in blob
    assert "== Cross-references ==" in blob


def test_detail_keeps_oscal_section_when_oscal_refs_present() -> None:
    # oscalReferences on the record is the sole gate for the OSCAL section.
    record = _record(oscalReferences={"catalog": "nist-800-53"})
    app = _app(_StubSources())
    blob = "\n".join(app.render_detail_lines(record))
    assert "== OSCAL ==" in blob
    assert "nist-800-53" in blob


def test_detail_lines_never_surface_removed_claim_attributes() -> None:
    """Stray-reader sweep ([META-002.06]/[META-008]): the Metadata_Index record no
    longer carries the producer-claim attributes (controls, controlLinkage,
    catalogOrProfileUuid, controlFamily, assessmentPeriod, assessor) nor a
    record-level oscalSchemaVersion / oscalLayers. The detail pane reads ONLY
    system-bounded facts, so even if such an attribute reappeared on a record
    (a stale write, a future regression), the TUI must not read or render it.

    Inject every removed attribute with a unique sentinel value and assert NONE
    of those sentinels reach the rendered detail — pinning that no stray reader
    crept back into ``render_detail_lines``."""
    sentinels = {
        "controls": ["ac-99-STRAYSENTINEL"],
        "controlLinkage": "linkage-STRAYSENTINEL",
        "catalogOrProfileUuid": "uuid-STRAYSENTINEL",
        "controlFamily": "FAM-STRAYSENTINEL",
        "assessmentPeriod": "period-STRAYSENTINEL",
        "assessor": "assessor-STRAYSENTINEL",
        "oscalSchemaVersion": "9.9.9-STRAYSENTINEL",
        "oscalLayers": ["layers-STRAYSENTINEL"],
    }
    record = _record(**sentinels)
    app = _app(_StubSources())
    blob = "\n".join(app.render_detail_lines(record))

    # The detail still renders the system-bounded sections...
    assert "== Lifecycle ==" in blob
    assert "== Cross-references ==" in blob
    # ...and NONE of the removed-attribute values surface.
    for value in ("STRAYSENTINEL",):
        assert value not in blob, (
            f"detail pane surfaced a removed claim attribute (stray reader): {value}"
        )


def test_detail_renders_from_claim_stripped_record() -> None:
    """Stray-reader sweep (positive): the detail pane renders fully from a record
    carrying ONLY system-bounded facts — no claim attribute present at all — so
    the view has no hidden dependency on a removed attribute being there."""
    record = _record()
    # The default _record() fixture is already claim-free; assert that explicitly
    # so a future fixture change that reintroduces a claim attribute trips here.
    for removed in (
        "controls",
        "controlLinkage",
        "catalogOrProfileUuid",
        "controlFamily",
        "assessmentPeriod",
        "assessor",
        "oscalSchemaVersion",
        "oscalLayers",
    ):
        assert removed not in record
    app = _app(_StubSources())
    blob = "\n".join(app.render_detail_lines(record))

    assert "== Lifecycle ==" in blob
    assert "[x] validate" in blob
    assert "== Cross-references ==" in blob
    assert "cairn-worm-bucket" in blob
    # OSCAL section gates on the system-bounded oscalReferences, not a claim.
    assert "== OSCAL ==" in blob


# Canonical 3-segment ECR tag: {artifact-kind}--_ref--{digest}. The TUI renders a
# tag as an opaque convenience label and never parses its segments.
_CANONICAL_TAG = "assessment-results--_ref--0123456789ab"


def _ecr_tag_line(lines: list[str]) -> str:
    """Return the single 'ECR tag:' detail line."""
    return next(line for line in lines if line.startswith("ECR tag:"))


def test_canonical_tag_renders_verbatim_in_detail() -> None:
    # A record carrying the canonical tag is "tagged" (digest + imageTag present),
    # so the detail view renders the full tag verbatim — every ``--`` separator
    # survives the render boundary unchanged.
    from cairn_cli._pending import TAG_PENDING_LABEL

    app = _app(_StubSources())
    record = _record(imageTag=_CANONICAL_TAG)
    line = _ecr_tag_line(app.render_detail_lines(record))
    assert _CANONICAL_TAG in line
    assert line.count("--") == 2
    assert TAG_PENDING_LABEL not in line


def test_untagged_record_shows_tag_pending_not_placeholder() -> None:
    # A digest-resolvable record with no tag yet (pre-mint window) renders the
    # "tag pending" state — the digest-first fallback is unaffected by the tag
    # form change (tags remain a non-load-bearing convenience).
    from cairn_cli._pending import TAG_PENDING_LABEL

    app = _app(_StubSources())
    record = _record()
    record.pop("imageTag", None)  # digest present, tag absent → pending
    line = _ecr_tag_line(app.render_detail_lines(record))
    assert TAG_PENDING_LABEL in line


def test_missing_fields_render_placeholders_not_crash() -> None:
    app = _app(_StubSources())
    # A nearly-empty record must still render (every field may be missing).
    lines = app.render_detail_lines({})
    blob = "\n".join(lines)
    assert "== Lifecycle ==" in blob
    # metadata stage is always "done" because the row exists.
    assert "metadata" in blob


def test_lifecycle_states_for_pending_and_failed() -> None:
    rows = compute_lifecycle(
        {"signingStatus": "pending", "archiveStatus": "failed", "createdAt": "t"}
    )
    states = {stage: state for stage, state, _ in rows}
    assert states["validate"] == "unknown"  # no validatedAt
    assert states["sign"] == "pending"
    assert states["archive"] == "failed"
    assert states["metadata"] == "done"


# --- Two-signature trust label --


def test_projection_trust_label_signed_is_fully_trusted() -> None:
    assert projection_trust_label({"signingStatus": "signed"}) == "fully-trusted"


def test_projection_trust_label_pending_is_partial() -> None:
    # A not-yet-fully-signed bundle is never labelled fully trusted.
    label = projection_trust_label({"signingStatus": "pending"})
    assert label == "partial"
    assert label != "fully-trusted"


def test_projection_trust_label_unsigned_is_unsigned() -> None:
    assert projection_trust_label({"signingStatus": "unsigned"}) == "unsigned"


def test_projection_trust_label_unvalidated_entry_is_unknown() -> None:
    # An ECR-present-but-unindexed entry has no projection → unknown, never full.
    label = projection_trust_label({"status": _UNVALIDATED_MARKER})
    assert label == "unknown"
    assert label != "fully-trusted"


def test_detail_view_renders_trust_line_as_projection() -> None:
    app = _app(_StubSources())
    blob = "\n".join(app.render_detail_lines(_record(signingStatus="signed")))
    assert "Trust:" in blob
    assert "fully-trusted" in blob
    assert "(projection)" in blob


def test_detail_view_partial_signing_never_reads_fully_trusted() -> None:
    app = _app(_StubSources())
    blob = "\n".join(app.render_detail_lines(_record(signingStatus="pending")))
    # The Trust line shows partial; fully-trusted must not appear anywhere.
    assert "Trust:" in blob
    assert "partial" in blob
    assert "fully-trusted" not in blob


def test_bundle_table_trust_column_reflects_signing_status() -> None:
    signed = _record(manifestDigest="sha256:" + "a" * 64, signingStatus="signed")
    pending = _record(
        projectId="proj-b", manifestDigest="sha256:" + "b" * 64, signingStatus="pending"
    )
    stub = _StubSources(bundles=[signed, pending])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            trust = _trust_cells(app)
            assert "fully-trusted" in trust
            assert "partial" in trust

    _run_async(scenario)


# --------------------------------------------------------------------------
# 2. Hostile bundle inert across ALL views.
# --------------------------------------------------------------------------


def _hostile_record() -> dict[str, Any]:
    return _record(
        projectId=f"proj{_HOSTILE}",
        signingProfileArn=f"arn{_HOSTILE}",
        oscalReferences={f"k{_HOSTILE}": f"v{_HOSTILE}"},
        manifestAnnotations={f"ann{_HOSTILE}": f"val{_HOSTILE}"},
        s3Bucket=f"bucket{_HOSTILE}",
    )


def test_hostile_bundle_inert_in_detail_view() -> None:
    app = _app(_StubSources())
    record = _hostile_record()
    for line in app.render_detail_lines(record):
        _assert_inert(line)
    _assert_inert(app.render_verify_line(record))


def test_hostile_bundle_inert_in_list_view() -> None:
    record = _hostile_record()
    stub = _StubSources(bundles=[record])
    app = _app(stub)
    # The list cells are produced by _safe; assert each rendered cell is inert.
    for value in (
        record.get("projectId"),
        record.get("artifactKind"),
        record.get("signingStatus"),
        record.get("archiveStatus"),
        app._short_digest(record.get("manifestDigest")),
        record.get("createdAt"),
    ):
        _assert_inert(app._safe(value))


def _unindexed(**extra: Any) -> dict[str, Any]:
    """A synthetic ECR-present-but-unindexed entry (``status == "unvalidated"``).

    The data layer attaches a ``classification`` field to every such entry
    (one of ``signature`` / ``worm-receipt`` / ``index`` / ``unvalidated bundle``
    / ``unclassified``) alongside the surviving ``imageTag`` / ``manifestDigest``
    / ``projectId``. Validated bundles do NOT carry ``classification``.
    """
    base = {
        "status": "unvalidated",
        "classification": "unvalidated bundle",
        "projectId": "proj-a",
        "imageTag": "sha256-deadbeef",
        "manifestDigest": "sha256:" + "b" * 64,
        "createdAt": "2026-05-28T00:00:00Z",
        "repositoryName": f"artifact-{QUALIFIER}/proj-a",
    }
    base.update(extra)
    return base


def _artifact_kind_cells(app: ArtifactStoreApp) -> list[str]:
    """The rendered ``artifactKind`` column (index 1) of #bundle-table, as plain text."""
    from textual.coordinate import Coordinate
    from textual.widgets import DataTable

    table = app.query_one("#bundle-table", DataTable)
    return [table.get_cell_at(Coordinate(row, 1)).plain for row in range(table.row_count)]


def _signing_archive_cells(app: ArtifactStoreApp) -> list[tuple[str, str]]:
    """The (signing, archive) columns (indices 2, 4) of #bundle-table, per row.

    A ``trust`` column sits at index 3, so
    ``archive`` is now at index 4.
    """
    from textual.coordinate import Coordinate
    from textual.widgets import DataTable

    table = app.query_one("#bundle-table", DataTable)
    return [
        (
            table.get_cell_at(Coordinate(row, 2)).plain,
            table.get_cell_at(Coordinate(row, 4)).plain,
        )
        for row in range(table.row_count)
    ]


def _trust_cells(app: ArtifactStoreApp) -> list[str]:
    """The ``trust`` column (index 3) of #bundle-table, per row."""
    from textual.coordinate import Coordinate
    from textual.widgets import DataTable

    table = app.query_one("#bundle-table", DataTable)
    return [table.get_cell_at(Coordinate(row, 3)).plain for row in range(table.row_count)]


@pytest.mark.parametrize(
    "classification",
    ["signature", "worm-receipt", "index", "unvalidated bundle", "unclassified"],
)
def test_unindexed_entry_shows_classification_in_artifactkind_column(classification: str) -> None:
    # 2.2(a) — an unindexed entry's artifactKind column shows its classification
    # label (what the artifact IS), NOT the raw imageTag (a bare sha256-… tag).
    record = _unindexed(classification=classification)
    stub = _StubSources(bundles=[record])
    app = _app(stub)
    app._show_indexes = True  # this test asserts rendering; bypass the index-hide filter

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            cells = _artifact_kind_cells(app)
            assert cells == [classification]
            # The raw image tag is NOT what the column surfaces.
            assert record["imageTag"] not in cells

    _run_async(scenario)


def test_unindexed_entry_without_classification_falls_back_to_marker() -> None:
    # 2.2(a) — defense in depth: if the data layer somehow omits classification,
    # the column degrades to the unvalidated marker rather than an empty cell.
    record = _unindexed()
    record.pop("classification", None)
    stub = _StubSources(bundles=[record])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert _artifact_kind_cells(app) == [_UNVALIDATED_MARKER]

    _run_async(scenario)


@pytest.mark.parametrize("classification", ["signature", "worm-receipt", "resource-ref", "index"])
def test_unindexed_pipeline_artifact_signing_archive_not_applicable(classification: str) -> None:
    # A pipeline byproduct is never itself signed/archived as
    # evidence, so both columns read "n/a" (an expected state), not a problem.
    stub = _StubSources(bundles=[_unindexed(classification=classification)])
    app = _app(stub)
    app._show_indexes = True  # this test asserts rendering; bypass the index-hide filter

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert _signing_archive_cells(app) == [("n/a", "n/a")]

    _run_async(scenario)


@pytest.mark.parametrize("classification", ["unvalidated bundle", "unclassified"])
def test_unindexed_did_not_land_signing_archive_unknown(classification: str) -> None:
    # 3.2 — a did-not-land bundle / unclassified image has no Metadata_Index row,
    # so its signing/archive status is genuinely unknown (the "unvalidated
    # bundle" artifactKind is the attention signal).
    stub = _StubSources(bundles=[_unindexed(classification=classification)])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert _signing_archive_cells(app) == [("unknown", "unknown")]

    _run_async(scenario)


def test_unindexed_missing_classification_signing_archive_unknown() -> None:
    # 3.2 — fail-safe: a missing classification is treated as unknown (it is not
    # a recognized pipeline-artifact label), never "n/a".
    record = _unindexed()
    record.pop("classification", None)
    stub = _StubSources(bundles=[record])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert _signing_archive_cells(app) == [("unknown", "unknown")]

    _run_async(scenario)


def test_validated_bundle_keeps_stored_signing_archive() -> None:
    # 3.2 — a validated bundle still shows its stored signing/archive status; the
    # n/a / unknown derivation applies ONLY to unindexed entries.
    stub = _StubSources(bundles=[_record()])  # signingStatus=signed, archiveStatus=archived
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert _signing_archive_cells(app) == [("signed", "archived")]

    _run_async(scenario)


def test_no_unindexed_entry_shows_determinate_signing_status() -> None:
    # 3.2 — the view never invents a determinate signed/unsigned/pending for an
    # unindexed entry (no Metadata_Index row backs it): every classification's
    # signing column is one of the derived {n/a, unknown}.
    records = [
        _unindexed(classification=c, manifestDigest="sha256:" + ch * 64)
        for ch, c in zip(
            "abcdef",
            [
                "signature",
                "worm-receipt",
                "resource-ref",
                "index",
                "unvalidated bundle",
                "unclassified",
            ],
            strict=True,
        )
    ]
    stub = _StubSources(bundles=records)
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            signing = [s for s, _ in _signing_archive_cells(app)]
            assert set(signing) <= {"n/a", "unknown"}
            assert not ({"signed", "unsigned", "pending"} & set(signing))

    _run_async(scenario)


def test_validated_bundle_shows_metadata_artifactkind_not_classification() -> None:
    # 2.2(b) — a validated bundle (Metadata_Index row, no status/classification)
    # still renders its stored artifactKind; it is not re-classified.
    record = _record()  # no "status", no "classification"
    stub = _StubSources(bundles=[record])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert _artifact_kind_cells(app) == [record["artifactKind"]]

    _run_async(scenario)


def test_hostile_unindexed_entry_inert_in_bundle_table() -> None:
    # 2.2(c) — a hostile unindexed entry (escape bytes in imageTag/manifestDigest
    # AND a crafted classification) renders inert in the bundle table. Mirrors the
    # hostile-bundle list test: every cell _populate_bundle_table emits routes
    # through self._safe. The artifactKind column now surfaces classification, so a
    # crafted classification must also be stripped to inert text.
    record = _unindexed(
        classification=f"unvalidated bundle{_HOSTILE}",
        projectId=f"proj{_HOSTILE}",
        imageTag=f"sha256-{_HOSTILE}",
        manifestDigest=f"sha256:{_HOSTILE}" + "b" * 30,
    )
    stub = _StubSources(bundles=[record])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            # The rendered artifactKind cell (classification) is inert end-to-end.
            for cell in _artifact_kind_cells(app):
                _assert_inert(cell)

    _run_async(scenario)

    # Per-cell render boundary (mirrors test_hostile_bundle_inert_in_list_view):
    # the artifactKind column now sources classification for unvalidated entries.
    for value in (
        record.get("projectId"),
        record.get("classification"),
        _UNVALIDATED_MARKER,
        _PLACEHOLDER,
        app._short_digest(record.get("manifestDigest")),
        record.get("createdAt"),
    ):
        _assert_inert(app._safe(value))


def test_hostile_bundle_inert_in_control_view() -> None:
    record = _hostile_record()
    app = _app(_StubSources())
    # Mirror _populate_control_table's per-cell render.
    for value in (
        record.get("projectId"),
        record.get("artifactKind"),
        app._short_digest(record.get("manifestDigest")),
        record.get("createdAt"),
    ):
        _assert_inert(app._safe(value))


def test_hostile_bundle_inert_in_dlq_bar() -> None:
    app = _app(_StubSources())
    depths = [
        DlqDepth(stage=f"validate{_HOSTILE}", queue_name="q", approximate_messages=3),
        DlqDepth(stage="sign", queue_name="q", approximate_messages=None),
    ]
    _assert_inert(app.render_dlq_text(depths))


def test_rich_markup_is_rendered_literally_not_parsed() -> None:
    # render_safe strips control bytes but NOT Rich markup brackets; the app
    # wraps rendered strings in rich.text.Text (_literal) so a producer value
    # like "[red]" / "[/]" is shown verbatim and cannot inject a style/link tag.
    from rich.text import Text

    markup = "proj-[red]a[/red][link=http://evil]x[/link]"
    literal = ArtifactStoreApp._literal(markup)
    assert isinstance(literal, Text)
    # The literal Text carries the markup characters as plain content (no spans
    # parsed from the brackets), so the brackets render as text.
    assert literal.plain == markup
    assert literal.spans == []


def test_hostile_bundle_inert_through_pilot_render() -> None:
    # End-to-end: a hostile record flows through the real widget render path.
    record = _hostile_record()
    stub = _StubSources(bundles=[record], control_items=[record])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            # Inspect the producer strings the app composed for each view.
            for line in app.render_detail_lines(record):
                _assert_inert(line)
            _assert_inert(app.render_verify_line(record))
            _assert_inert(app.render_dlq_text(stub._dlq))

    _run_async(scenario)


# --------------------------------------------------------------------------
# 3. Verify string inert + built from validated identifiers + never executed.
# --------------------------------------------------------------------------


def test_verify_string_inert_and_from_validated_identifiers() -> None:
    app = _app(_StubSources())
    record = _record()
    line = app.render_verify_line(record)
    _assert_inert(line)
    # The composed command is the validated form.
    cmd = compose_verify_command(record, stage="alpha", region_code="cmh")
    assert cmd is not None
    assert cmd.startswith("cairn verify ")
    assert "@sha256:" + "a" * 64 in cmd
    assert "--stage alpha --region-code cmh" in cmd
    assert cmd in render_safe(line)  # validated -> survives the render boundary unchanged


def test_verify_string_refuses_malformed_identifiers() -> None:
    app = _app(_StubSources())
    # Hostile/malformed digest and repo -> no command, safe message instead.
    bad = _record(
        manifestDigest="sha256:NOT-HEX",
        repositoryName=f"evil{_HOSTILE}",
    )
    assert compose_verify_command(bad, stage="alpha", region_code="cmh") is None
    line = app.render_verify_line(bad)
    _assert_inert(line)
    assert "cannot construct verify command" in line


def test_verify_command_rejects_uppercase_and_wrong_length_digest() -> None:
    base = _record()
    # uppercase hex
    assert (
        compose_verify_command(
            {**base, "manifestDigest": "sha256:" + "A" * 64}, stage="alpha", region_code="cmh"
        )
        is None
    )
    # wrong length
    assert (
        compose_verify_command(
            {**base, "manifestDigest": "sha256:" + "a" * 63}, stage="alpha", region_code="cmh"
        )
        is None
    )
    # wrong segment count in repo (collapsed layout is exactly 2 segments; a
    # trailing category segment is now an extra one and must be rejected)
    assert (
        compose_verify_command(
            {**base, "repositoryName": "artifact-cmh-alpha/proj-a/other"},
            stage="alpha",
            region_code="cmh",
        )
        is None
    )


def test_verify_command_reference_parses_via_resolve_reference() -> None:
    # C1 regression: the surfaced `cairn verify` reference MUST be one the real
    # `cairn verify` reference parser accepts. The two halves (this composer and
    # _refs.resolve_reference) are independently plausible but were inconsistent
    # at the seam — the full ECR repo path is not a parseable reference. Build
    # the command, extract the reference argument, and resolve it for real.
    from cairn_cli._aws import DeploymentTarget
    from cairn_cli._refs import resolve_reference

    cmd = compose_verify_command(_record(), stage="alpha", region_code="cmh")
    assert cmd is not None
    # The reference is the token between "cairn verify " and the first " --".
    reference = cmd[len("cairn verify ") :].split(" --", 1)[0]
    resolved = resolve_reference(
        reference,
        registry_host="123456789012.dkr.ecr.us-east-2.amazonaws.com",
        target=DeploymentTarget(stage="alpha", region_code="cmh", app_name="cairnproofs4af"),
    )
    # Short form expands back to a full Customer_Hosted repo path + digest. The
    # artifact-kind in the short ref comes from the record's artifactKind (the
    # collapsed repo name does not carry it).
    assert resolved.repository == "cairnproofs4af-artifact-cmh-alpha/proj-a/assessment-results"
    assert resolved.selector == "@sha256:" + "a" * 64


def test_verify_command_accepts_alias_artifact_kind() -> None:
    # expand-artifact-kind-vocabulary (4.1): the verify composer validates the
    # artifactKind via the single normalize_artifact_kind resolver, so a stored
    # ALIAS (not just a canonical) is accepted; the supplied spelling is preserved
    # verbatim in the reference (consistent with the producer-surface preservation).
    cmd = compose_verify_command(
        _record(artifactKind="attestations"), stage="alpha", region_code="cmh"
    )
    assert cmd is not None
    assert "/attestations@sha256:" in cmd


def test_verify_command_rejects_unknown_artifact_kind() -> None:
    # An unresolvable kind degrades to None (caller renders "cannot construct").
    assert (
        compose_verify_command(_record(artifactKind="screenshot"), stage="alpha", region_code="cmh")
        is None
    )


def test_app_never_shells_out(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch every shell-out primitive to explode; render the verify line and
    # detail; assert nothing was executed.
    import os
    import subprocess

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the TUI must never execute a shell command")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "call", _boom)
    monkeypatch.setattr(subprocess, "check_call", _boom)
    monkeypatch.setattr(subprocess, "check_output", _boom)
    monkeypatch.setattr(os, "system", _boom)

    app = _app(_StubSources())
    record = _record()
    # Exercising the verify + detail render must not invoke any of the above.
    line = app.render_verify_line(record)
    assert "cairn verify" in line
    app.render_detail_lines(record)


def test_app_module_imports_no_shell_primitives() -> None:
    # Structural: the app module must not bind subprocess/os.system/popen.
    from cairn_cli.tui import app as app_module

    assert not hasattr(app_module, "subprocess")
    assert not hasattr(app_module, "Popen")
    # os may be transitively importable, but the module must not reach system().
    import inspect

    src = inspect.getsource(app_module)
    # Only the docstring may mention these; no call expression.
    assert "os.system(" not in src
    assert "subprocess." not in src
    assert ".Popen(" not in src


# --------------------------------------------------------------------------
# 4. Watch-mode refresh issues only allowlisted reads.
# --------------------------------------------------------------------------


class _RecordingTable:
    """DynamoDB table exposing ONLY query/get_item; records each op.

    Walks multi-page result sets the real way: a page that is not the last for
    its partition key emits a ``LastEvaluatedKey``, and a follow-up query
    arrives with the matching ``ExclusiveStartKey`` so the data layer's
    pagination-token loop advances rather than re-reading page 0.
    """

    def __init__(self, calls: list[str], pages_by_pk: dict[str, list[dict]]) -> None:
        self._calls = calls
        self._pages_by_pk = pages_by_pk

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self._calls.append("query")
        pk = kwargs["ExpressionAttributeValues"][":pk"]
        pages = self._pages_by_pk.get(pk, [{"Items": []}])
        start = kwargs.get("ExclusiveStartKey")
        # The page cursor rides the "sk" key (one of the validator's allowed
        # token keys) so the real pagination encode/decode round-trips it.
        index = int(start["sk"]) if start else 0
        page = dict(pages[index]) if index < len(pages) else {"Items": []}
        if index + 1 < len(pages):
            page.setdefault("LastEvaluatedKey", {"sk": str(index + 1)})
        return page

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self._calls.append("get_item")
        return {}


class _RecordingResource:
    def __init__(
        self,
        calls: list[str],
        pages_by_pk: dict[str, list[dict]],
        registry_items: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._calls = calls
        self._pages_by_pk = pages_by_pk
        self._registry_items = registry_items or {}

    def Table(self, name: str) -> _RecordingTable:  # boto3 API name
        return _RecordingTable(self._calls, self._pages_by_pk)

    def batch_get_item(self, **kwargs: Any) -> dict[str, Any]:
        # Read-only BatchGetItem backing the Identity_Registry display-name join.
        self._calls.append("batch_get_item")
        responses: dict[str, list[dict[str, Any]]] = {}
        for table_name, spec in kwargs.get("RequestItems", {}).items():
            responses[table_name] = [
                self._registry_items[k["id"]]
                for k in spec.get("Keys", [])
                if k.get("id") in self._registry_items
            ]
        return {"Responses": responses}


class _RecordingReadOnlyClient:
    """Stub matching the ReadOnlyAwsClient surface; records each op.

    Non-allowlisted operations (``scan``, ``receive_message``, ``put_item``,
    mutators, ``get_queue_url``) are simply not defined — calling one raises
    ``AttributeError``, mirroring the structural absence of the real client.
    """

    def __init__(
        self,
        *,
        ecr_pages: list[dict],
        pages_by_pk: dict[str, list[dict]] | None = None,
        images_by_repo: dict[str, list[dict]] | None = None,
        registry_items: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._ecr_pages = ecr_pages
        self._resource = _RecordingResource(self.calls, pages_by_pk or {}, registry_items)
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

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("head_object")
        return {"ContentLength": 1}

    def get_queue_attributes(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("get_queue_attributes")
        return {"Attributes": {"ApproximateNumberOfMessages": "0"}}


_ALLOWLISTED_OPS = {
    "describe_repositories",
    "describe_images",
    "batch_get_image",
    "head_object",
    "get_queue_attributes",
    "query",
    "get_item",
    "batch_get_item",
}


def _real_sources_over_recording_client() -> tuple[ArtifactSources, _RecordingReadOnlyClient]:
    repo = f"artifact-{QUALIFIER}/org-a/tnt-a/proj-a"
    client = _RecordingReadOnlyClient(
        ecr_pages=[{"repositories": [{"repositoryName": repo, "registryId": ACCOUNT_ID}]}],
        pages_by_pk={"tnt-a": [{"Items": [_record()]}]},
    )
    sources = ArtifactSources(target=TARGET, client=client, region=REGION)  # type: ignore[arg-type]
    return sources, client


def test_watch_refresh_issues_only_allowlisted_reads() -> None:
    # Drive a watch refresh's fetch logic directly against a recording read-only
    # client — the worker methods just wrap these fetch methods.
    sources, client = _real_sources_over_recording_client()
    app = ArtifactStoreApp(sources, deployment_target=TARGET)

    # Simulate one watch-cycle's reads (list view active is the default).
    app._fetch_bundles()
    app._fetch_dlq_health()

    assert client.calls, "expected the watch cycle to issue reads"
    illegal = set(client.calls) - _ALLOWLISTED_OPS
    assert not illegal, f"watch issued non-allowlisted ops: {illegal}"
    # Explicit negatives for the dangerous ops.
    assert "scan" not in client.calls
    assert "receive_message" not in client.calls
    assert "put_item" not in client.calls
    assert "update_item" not in client.calls
    assert "delete_item" not in client.calls
    assert "get_queue_url" not in client.calls


def test_watch_detail_refresh_is_lazy_single_bundle() -> None:
    sources, client = _real_sources_over_recording_client()
    app = ArtifactStoreApp(sources, deployment_target=TARGET)

    # Detail fetch (the watch refresh when the detail view is active) issues
    # only the lazy per-bundle reads — never a scan or receive.
    app._fetch_detail(_record())
    app._fetch_dlq_health()

    illegal = set(client.calls) - _ALLOWLISTED_OPS
    assert not illegal
    assert "scan" not in client.calls
    assert "receive_message" not in client.calls
    # Lazy reads happened for the selected bundle.
    assert "batch_get_image" in client.calls
    assert "head_object" in client.calls


def test_control_fetch_issues_only_allowlisted_reads() -> None:
    uuid = "11111111-1111-1111-1111-111111111111"
    tenant_id = "tnt-a"
    # The control-claims projection is a tenant-PK plane: the partition key is
    # the exact-match {tenant-id}, so the page is keyed under the tenant, with
    # {uuid}#{controlId} serving as the sort-key prefix.
    client = _RecordingReadOnlyClient(
        ecr_pages=[{"repositories": []}],
        pages_by_pk={tenant_id: [{"Items": [_record()]}]},
    )
    sources = ArtifactSources(target=TARGET, client=client, region=REGION)  # type: ignore[arg-type]
    app = ArtifactStoreApp(sources, deployment_target=TARGET)

    items = app._fetch_control_bundles(tenant_id, uuid, "ac-2")

    assert len(items) == 1
    illegal = set(client.calls) - _ALLOWLISTED_OPS
    assert not illegal
    assert "scan" not in client.calls
    assert "receive_message" not in client.calls


def test_control_view_paginates_to_exhaustion() -> None:
    # *Paginated listing without truncation* names BOTH project and control
    # result sets. A control cited by more than one page of bundles must surface
    # ALL pages via the token, not truncate to page 1.
    uuid = "11111111-1111-1111-1111-111111111111"
    tenant_id = "tnt-a"
    page1 = [_record(manifestDigest="sha256:" + "a" * 64)]
    page2 = [_record(manifestDigest="sha256:" + "b" * 64)]
    # Tenant-PK plane: the token-walked pages are keyed under the {tenant-id}
    # partition (see test_control_fetch_issues_only_allowlisted_reads).
    client = _RecordingReadOnlyClient(
        ecr_pages=[{"repositories": []}],
        pages_by_pk={tenant_id: [{"Items": page1}, {"Items": page2}]},
    )
    sources = ArtifactSources(target=TARGET, client=client, region=REGION)  # type: ignore[arg-type]
    app = ArtifactStoreApp(sources, deployment_target=TARGET)

    items = app._fetch_control_bundles(tenant_id, uuid, "ac-2")

    # Both pages surfaced — none truncated.
    assert len(items) == 2
    assert {it["manifestDigest"] for it in items} == {
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    }
    # Two query calls means the token loop fetched the second page.
    assert client.calls.count("query") == 2
    illegal = set(client.calls) - _ALLOWLISTED_OPS
    assert not illegal
    assert "scan" not in client.calls


def test_control_tab_both_inputs_render_on_screen() -> None:
    # Regression: ALL control inputs must be fully visible. They were once laid
    # side-by-side in a Horizontal, which pushed the second (control id) off the
    # right edge at standard terminal widths — so the operator only saw the UUID
    # box and the control view looked broken. The tenant id is the partition key
    # and reads first, so the view now carries three stacked inputs.
    app = _app(_StubSources())

    async def scenario() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()

            from textual.widgets import Input, TabbedContent

            app.query_one(TabbedContent).active = "tab-control"
            await pilot.pause()
            for wid in ("#ctl-tenant-id", "#ctl-uuid", "#ctl-id"):
                region = app.query_one(wid, Input).region
                assert region.x >= 0 and region.right <= 80, (
                    f"{wid} is not fully on-screen at 80 cols: {region}"
                )

    _run_async(scenario)


# --------------------------------------------------------------------------
# 5. Project-ID listing view (direct query_by_project, NOT ECR-gated).
# --------------------------------------------------------------------------


def test_project_row_drilldown_lands_and_stays_on_detail() -> None:
    # Drilling into Detail from the project view must LAND and STAY on the Detail
    # tab; focus is anchored on the Detail pane (deferred via call_after_refresh).
    #
    # Assert only the DETERMINISTIC, user-visible outcome — the active tab — not
    # keyboard-focus identity: focus settling is async + environment-sensitive in
    # headless Textual and is NOT stable under parallel (xdist) execution, which
    # would make a focus-identity assertion a flaky test bug (see toolchain spec
    # "Tests Safe Under Parallel Execution"). The active tab is set synchronously
    # and is the property the operator actually observes.
    record = _record()
    stub = _StubSources(project_pages=[_Page([record])])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable, Input, TabbedContent

            app.query_one(TabbedContent).active = "tab-project"
            app.query_one("#tenant-id", Input).value = "tnt-a"
            app.query_one("#proj-id", Input).value = "proj-a"
            app.on_input_submitted(Input.Submitted(app.query_one("#proj-id", Input), "proj-a"))
            await app.workers.wait_for_complete()
            await pilot.pause()

            app.query_one("#project-table", DataTable).focus()
            await pilot.pause()

            # Drill in the way the row-select handler does (resolve the record,
            # then call _open_detail), then let the activation + deferred focus
            # reconcile.
            app._open_detail(record, origin="project-table")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.pause()

            assert app.query_one(TabbedContent).active == "tab-detail"

    _run_async(scenario)


def test_project_view_lists_bundles_for_entered_id() -> None:
    # Basic render: entering a known project ID lists its bundles in the
    # project table via the direct query_by_project path.
    record = _record()
    stub = _StubSources(project_pages=[_Page([record])])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable, Input

            app.query_one("#tenant-id", Input).value = "tnt-a"
            app.query_one("#proj-id", Input).value = "proj-a"
            app.on_input_submitted(Input.Submitted(app.query_one("#proj-id", Input), "proj-a"))
            await app.workers.wait_for_complete()
            await pilot.pause()

            table = app.query_one("#project-table", DataTable)
            assert table.row_count == 1
            assert "list_project_bundles" in stub.calls
            assert stub.project_ids_queried == ["proj-a"]

    _run_async(scenario)


def test_project_view_hostile_input_and_rows_render_inert() -> None:
    # Threat 2: an escape-laden project-ID input AND escape-laden returned rows
    # render with no surviving control byte. The echoed identifier and every
    # cell route through _safe; mirror those render sites here.
    hostile_id = f"proj{_HOSTILE}"
    record = _hostile_record()

    # The echoed/entered identifier is rendered inert.
    app = _app(_StubSources())
    _assert_inert(app._safe(hostile_id))

    # Every cell _populate_project_table would emit is inert.
    for value in (
        record.get("projectId"),
        record.get("artifactKind"),
        record.get("signingStatus"),
        record.get("archiveStatus"),
        app._short_digest(record.get("manifestDigest")),
        record.get("createdAt"),
    ):
        _assert_inert(app._safe(value))

    # End-to-end through the real widget render path.
    stub = _StubSources(project_pages=[_Page([record])])
    pilot_app = _app(stub)

    async def scenario() -> None:
        async with pilot_app.run_test() as pilot:
            await pilot_app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import Input

            pilot_app.query_one("#tenant-id", Input).value = "tnt-a"
            pilot_app.query_one("#proj-id", Input).value = hostile_id
            pilot_app.on_input_submitted(
                Input.Submitted(pilot_app.query_one("#proj-id", Input), hostile_id)
            )
            await pilot_app.workers.wait_for_complete()
            await pilot.pause()
            # The view composed and rendered without crashing on hostile bytes.
            for value in (record.get("projectId"), record.get("artifactKind")):
                _assert_inert(pilot_app._safe(value))

    _run_async(scenario)


def test_project_view_no_scan_and_paginates_to_exhaustion() -> None:
    # Threat 1 / scenario 3: the project listing rides only the project-scoped
    # key-condition query (no Scan) AND walks the pagination token across MORE
    # than one page, surfacing ALL items rather than truncating to page 1.
    page1 = [_record(manifestDigest="sha256:" + "a" * 64)]
    page2 = [_record(manifestDigest="sha256:" + "b" * 64)]
    client = _RecordingReadOnlyClient(
        ecr_pages=[{"repositories": []}],
        # Two pages for the (tenant, project) partition key; the table emits a
        # LastEvaluatedKey for page 0 so the data layer fetches page 1. The
        # partition key is the opaque {tenant-id}.
        pages_by_pk={"tnt-a": [{"Items": page1}, {"Items": page2}]},
    )
    sources = ArtifactSources(target=TARGET, client=client, region=REGION)  # type: ignore[arg-type]
    app = ArtifactStoreApp(sources, deployment_target=TARGET)

    items = app._fetch_project_bundles("tnt-a", "proj-a")

    # (a) ALL items across BOTH pages appear — none truncated.
    assert len(items) == 2
    digests = {it["manifestDigest"] for it in items}
    assert digests == {"sha256:" + "a" * 64, "sha256:" + "b" * 64}
    # Two query calls means the token loop fetched the second page.
    assert client.calls.count("query") == 2

    # (b) Only the project-scoped key-condition query path was exercised — no
    # Scan, no other non-allowlisted op.
    illegal = set(client.calls) - _ALLOWLISTED_OPS
    assert not illegal, f"project listing issued non-allowlisted ops: {illegal}"
    assert "scan" not in client.calls
    assert "receive_message" not in client.calls
    assert "get_queue_url" not in client.calls


def test_project_view_blank_input_issues_no_query() -> None:
    # A blank/whitespace project ID is dropped on submit — no query issued.
    stub = _StubSources()
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            stub.calls.clear()

            from textual.widgets import Input

            inp = app.query_one("#proj-id", Input)
            inp.value = "   "
            app.on_input_submitted(Input.Submitted(inp, "   "))
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert "list_project_bundles" not in stub.calls

    _run_async(scenario)


def test_watch_refresh_on_project_tab_reruns_project_query() -> None:
    # The watch/refresh dispatch (_refresh_all_workers) re-runs the direct
    # project query when the project tab is active — reads-only, same
    # allowlisted call type, no new EventBridge/subscription.
    stub = _StubSources(project_pages=[_Page([_record()])])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import Input, TabbedContent

            app.query_one(TabbedContent).active = "tab-project"
            app.query_one("#tenant-id", Input).value = "tnt-a"
            app.query_one("#proj-id", Input).value = "proj-a"
            stub.calls.clear()
            stub.project_ids_queried.clear()

            app.action_refresh()
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert "list_project_bundles" in stub.calls
            assert stub.project_ids_queried == ["proj-a"]

    _run_async(scenario)


# --------------------------------------------------------------------------
# 6. Regression guards: assert the success/pagination paths are not disturbed by
#    the query error boundary.
# --------------------------------------------------------------------------


def test_project_query_success_renders_n_rows_render_safe() -> None:
    # 3.1 — a successful project query with N>=1 records renders exactly N rows
    # in #project-table, and every cell is render-safe. Drive it through the
    # project worker (set #proj-id, submit, wait), assert row_count == N, and
    # (mirroring test_hostile_bundle_inert_in_control_view) assert a hostile
    # projectId record rendered into the project table carries no control byte.
    # Covers Unchanged clause #1.
    n = 3
    hostile = _hostile_record()
    records = [_record(manifestDigest="sha256:" + ch * 64) for ch in ("a", "b")] + [hostile]
    stub = _StubSources(project_pages=[_Page(records)])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable, Input

            app.query_one("#tenant-id", Input).value = "tnt-a"
            app.query_one("#proj-id", Input).value = "proj-a"
            app.on_input_submitted(Input.Submitted(app.query_one("#proj-id", Input), "proj-a"))
            await app.workers.wait_for_complete()
            await pilot.pause()

            table = app.query_one("#project-table", DataTable)
            assert table.row_count == n
            assert "list_project_bundles" in stub.calls

            # Render-safety choke point: every cell _populate_project_table emits
            # for the hostile record routes through self._safe and is inert.
            for value in (
                hostile.get("projectId"),
                hostile.get("artifactKind"),
                hostile.get("signingStatus"),
                hostile.get("archiveStatus"),
                app._short_digest(hostile.get("manifestDigest")),
                hostile.get("createdAt"),
            ):
                _assert_inert(app._safe(value))

    _run_async(scenario)


def test_project_query_multipage_drains_all_rows_via_project_listing_only() -> None:
    # 3.2 — a multi-_Page project result (first page carries a next_token) is
    # fully drained: all rows across pages appear AND only the project-scoped
    # listing was used (the stub has no scan method; assert >=2
    # list_project_bundles calls and no scan-like call). Covers Unchanged
    # clause #2; guards _fetch_project_bundles' token-exhaustion loop.
    page1 = _Page(
        [_record(manifestDigest="sha256:" + "a" * 64)],
        next_token="tok-1",
    )
    page2 = _Page([_record(manifestDigest="sha256:" + "b" * 64)])
    stub = _StubSources(project_pages=[page1, page2])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable, Input

            app.query_one("#tenant-id", Input).value = "tnt-a"
            app.query_one("#proj-id", Input).value = "proj-a"
            app.on_input_submitted(Input.Submitted(app.query_one("#proj-id", Input), "proj-a"))
            await app.workers.wait_for_complete()
            await pilot.pause()

            table = app.query_one("#project-table", DataTable)
            # Both pages surfaced — none truncated.
            assert table.row_count == 2
            # The pagination loop followed the token: >=2 project-scoped calls.
            assert stub.calls.count("list_project_bundles") >= 2
            # Only the sanctioned project listing was used — no scan-like call
            # (the stub surface has no scan method at all).
            assert not any("scan" in call for call in stub.calls)

    _run_async(scenario)


def test_project_query_zero_records_leaves_table_empty_without_raising() -> None:
    # 3.3 — a project query that succeeds with zero records leaves
    # #project-table at row_count == 0 and does not raise. Covers Expected
    # clause #3 (success side). Deliberately does NOT assert on #proj-status so
    # the guard's intent (the empty success path) stays independent of the
    # fix's new status widget.
    stub = _StubSources(project_pages=[_Page([])])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable, Input

            app.query_one("#tenant-id", Input).value = "tnt-a"
            app.query_one("#proj-id", Input).value = "proj-a"
            app.on_input_submitted(Input.Submitted(app.query_one("#proj-id", Input), "proj-a"))
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert app.is_running  # zero records is a success, not a crash
            table = app.query_one("#project-table", DataTable)
            assert table.row_count == 0
            assert "list_project_bundles" in stub.calls

    _run_async(scenario)


# --------------------------------------------------------------------------
# Watch-mode toggle + interval clamping.
# --------------------------------------------------------------------------


def test_watch_toggle_starts_and_stops_interval() -> None:
    app = _app(_StubSources(bundles=[_record()]))

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.is_watching is False
            app.action_toggle_watch()
            assert app.is_watching is True
            assert app._watch_timer is not None
            app.action_toggle_watch()
            assert app.is_watching is False
            assert app._watch_timer is None

    _run_async(scenario)


def test_watch_interval_is_clamped_to_minimum() -> None:
    # A punishing sub-minimum interval is clamped up to the floor.
    app = ArtifactStoreApp(
        _StubSources(),  # type: ignore[arg-type]
        watch_interval=0.05,
        min_watch_interval=1.0,
        deployment_target=TARGET,
    )
    assert app._watch_interval >= 1.0


def test_dlq_text_flags_nonzero_and_marks_unknown() -> None:
    app = _app(_StubSources())
    depths = [
        DlqDepth(stage="validate", queue_name="q", approximate_messages=0),
        DlqDepth(stage="sign", queue_name="q", approximate_messages=5),
        DlqDepth(stage="archive", queue_name="q", approximate_messages=None),
    ]
    text = app.render_dlq_text(depths)
    assert "validate: 0 (approx)" in text
    assert "sign: 5 (approx) [!]" in text
    assert "archive: unknown" in text


# --------------------------------------------------------------------------
# 7. Navigation (design review): drill DOWN with Enter from ANY list,
#    drill UP with Esc, jump with the number keys, drill ACROSS with `p`.
#    These reuse only the existing read-only workers — no new AWS call types.
# --------------------------------------------------------------------------


def _select_row(app: ArtifactStoreApp, table_id: str, row: int = 0) -> None:
    """Fire a DataTable RowSelected for ``table_id`` exactly as a keypress would."""
    from textual.widgets import DataTable

    table = app.query_one(f"#{table_id}", DataTable)
    table.move_cursor(row=row)
    row_key = table.coordinate_to_cell_key((row, 0)).row_key
    app.on_data_table_row_selected(DataTable.RowSelected(table, cursor_row=row, row_key=row_key))


def test_selecting_bundle_row_auto_activates_detail_tab() -> None:
    # Drill-down in one keystroke: selecting a bundle row both fetches the
    # detail AND switches to the Detail tab.
    record = _record()
    stub = _StubSources(bundles=[record])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import TabbedContent

            _select_row(app, "bundle-table")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert app.query_one(TabbedContent).active == "tab-detail"
            assert "get_bundle_detail" in stub.calls
            assert app._selected is record

    _run_async(scenario)


def test_selecting_control_result_row_opens_detail() -> None:
    # Drill-down from a control search: a bundle found by control-search is no
    # longer a dead end — selecting its row opens the same Detail view.
    record = _record()
    stub = _StubSources(control_items=[record])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable, Input, TabbedContent

            app.query_one(TabbedContent).active = "tab-control"
            app.query_one("#ctl-tenant-id", Input).value = "tnt-a"
            app.query_one("#ctl-uuid", Input).value = "11111111-1111-1111-1111-111111111111"
            app.query_one("#ctl-id", Input).value = "ac-2"
            app.on_input_submitted(Input.Submitted(app.query_one("#ctl-id", Input), "ac-2"))
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#control-table", DataTable).row_count == 1

            _select_row(app, "control-table")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert app.query_one(TabbedContent).active == "tab-detail"
            assert app._selected is record
            # Esc drills back UP to the control search it came from.
            app.action_back()
            await pilot.pause()
            assert app.query_one(TabbedContent).active == "tab-control"

    _run_async(scenario)


def test_selecting_project_result_row_opens_detail_and_esc_returns() -> None:
    record = _record()
    stub = _StubSources(project_pages=[_Page([record])])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable, Input, TabbedContent

            app.query_one(TabbedContent).active = "tab-project"
            app.query_one("#tenant-id", Input).value = "tnt-a"
            app.query_one("#proj-id", Input).value = "proj-a"
            app.on_input_submitted(Input.Submitted(app.query_one("#proj-id", Input), "proj-a"))
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#project-table", DataTable).row_count == 1

            _select_row(app, "project-table")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one(TabbedContent).active == "tab-detail"

            app.action_back()
            await pilot.pause()
            assert app.query_one(TabbedContent).active == "tab-project"

    _run_async(scenario)


def test_esc_from_search_tab_returns_to_bundles() -> None:
    app = _app(_StubSources())

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import TabbedContent

            tabs = app.query_one(TabbedContent)
            tabs.active = "tab-control"
            await pilot.pause()
            app.action_back()
            await pilot.pause()
            assert tabs.active == "tab-list"

    _run_async(scenario)


def test_number_keys_jump_between_tabs() -> None:
    app = _app(_StubSources())

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import TabbedContent

            tabs = app.query_one(TabbedContent)
            app.action_show_detail()
            await pilot.pause()
            assert tabs.active == "tab-detail"
            app.action_focus_control()
            await pilot.pause()
            assert tabs.active == "tab-control"
            app.action_focus_project()
            await pilot.pause()
            assert tabs.active == "tab-project"
            app.action_show_bundles()
            await pilot.pause()
            assert tabs.active == "tab-list"

    _run_async(scenario)


def test_project_drill_from_selected_bundle_runs_query() -> None:
    # Drill ACROSS: `p` on a selected bundle lists its project without the
    # operator re-typing the id, riding the same query_by_project path.
    record = _record(projectId="proj-a")
    stub = _StubSources(bundles=[record], project_pages=[_Page([record])])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import Input, TabbedContent

            _select_row(app, "bundle-table")
            await app.workers.wait_for_complete()
            await pilot.pause()
            stub.calls.clear()
            stub.project_ids_queried.clear()

            app.action_project_drill()
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert app.query_one(TabbedContent).active == "tab-project"
            assert app.query_one("#proj-id", Input).value == "proj-a"
            assert "list_project_bundles" in stub.calls
            assert stub.project_ids_queried == ["proj-a"]

    _run_async(scenario)


def test_project_drill_without_selection_issues_no_query() -> None:
    stub = _StubSources()
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            stub.calls.clear()
            app.action_project_drill()  # nothing selected
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "list_project_bundles" not in stub.calls

    _run_async(scenario)


def test_project_drill_rejects_malformed_project_id() -> None:
    # Defense in depth: a stored projectId carrying escape bytes is NOT placed
    # into the search Input (which would be a raw-render path) and issues no
    # query — it fails the canonical grammar guard.
    hostile = _record(projectId=f"proj{_HOSTILE}")
    stub = _StubSources()
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import Input

            app._selected = hostile
            stub.calls.clear()
            app.action_project_drill()
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert "list_project_bundles" not in stub.calls
            # The Input was never populated with the unsafe value.
            assert app.query_one("#proj-id", Input).value == ""

    _run_async(scenario)


def test_validated_coordinates_accepts_canonical_rejects_garbage() -> None:
    app = _app(_StubSources())
    # A record carries valid tenant/project (and org) -> (tenant_id, project_id).
    assert app._validated_coordinates(_record(projectId="proj-a")) == ("tnt-a", "proj-a")
    assert app._validated_coordinates(_record(projectId=f"proj{_HOSTILE}")) is None
    assert app._validated_coordinates(_record(projectId="Has Spaces")) is None
    # A hostile/invalid TENANT id is also rejected (it is the partition key).
    assert app._validated_coordinates(_record(tenantId="Bad Tenant")) is None
    # Missing coordinates -> None (not all three are present strings).
    assert app._validated_coordinates({"projectId": None}) is None
    assert app._validated_coordinates({"projectId": "proj-a"}) is None  # no tenantId/orgId


def test_validated_coordinates_rejects_trailing_newline_bypass() -> None:
    # The opaque-id grammar anchors with `\\Z`, but a stored value placed into the
    # #tenant-id / #proj-id Input renders RAW (no _safe). The guard additionally
    # requires render-safety byte-for-byte and rejects a trailing control byte
    # rather than smuggle it to the terminal.
    app = _app(_StubSources())
    assert app._validated_coordinates(_record(projectId="proj-a\n")) is None
    assert app._validated_coordinates(_record(projectId="proj-a\r")) is None
    assert app._validated_coordinates(_record(tenantId="tnt-a\n")) is None
    # And the drill itself must not populate the Input nor query for such a value.
    stub = _StubSources()
    drill_app = _app(stub)

    async def scenario() -> None:
        async with drill_app.run_test() as pilot:
            await drill_app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import Input

            drill_app._selected = _record(projectId="proj-a\n")
            stub.calls.clear()
            drill_app.action_project_drill()
            await drill_app.workers.wait_for_complete()
            await pilot.pause()
            assert "list_project_bundles" not in stub.calls
            assert drill_app.query_one("#proj-id", Input).value == ""

    _run_async(scenario)


def test_number_key_and_p_routing_via_real_keypresses() -> None:
    # Pin the ACTUAL key-routing contract (not just the action bodies): with the
    # bundle table focused (no Input), digit keys jump tabs and `p` drills; and
    # Esc frees the operator from a focused search Input. This guards the
    # deliberate decision NOT to make those bindings priority (which would break
    # typing digits/`p` into the UUID and project-id search boxes).
    record = _record(projectId="proj-a")
    stub = _StubSources(bundles=[record], project_pages=[_Page([record])])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import TabbedContent

            tabs = app.query_one(TabbedContent)
            # Bundle table is focused on mount, so a digit key jumps tabs.
            await pilot.press("4")
            await pilot.pause()
            assert tabs.active == "tab-project"

            # The project-id Input is now focused; a digit is TYPED, not a jump.
            from textual.widgets import Input

            await pilot.press("3")
            await pilot.pause()
            assert tabs.active == "tab-project"  # unchanged — Input swallowed it
            assert "3" in app.query_one("#proj-id", Input).value

            # Esc is NOT swallowed by the Input: it drills up to the bundle list.
            await pilot.press("escape")
            await pilot.pause()
            assert tabs.active == "tab-list"

            # With the table focused again, `p` drills to the selected bundle's
            # project. Select a row first so there is a selection.
            _select_row(app, "bundle-table")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("escape")  # back to the list, table focused
            await pilot.pause()
            await pilot.press("p")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert tabs.active == "tab-project"
            assert app.query_one("#proj-id", Input).value == "proj-a"

    _run_async(scenario)


# --------------------------------------------------------------------------
# 8. OCI referrer visibility + bundle ⇄ referrer navigation.
#    Render every referrer field through the _safe/_literal choke point,
#    classify by artifactType WITHOUT implying verification, distinguish
#    "no referrers" from "referrers unavailable", and drive the
#    bundle→referrer→back navigation via the pilot.
# --------------------------------------------------------------------------


def _referrer(
    *,
    artifact_type: str,
    digest: str = "sha256:" + "c" * 64,
    tag: str | None = None,
    annotations: dict[str, str] | None = None,
) -> Referrer:
    return Referrer(
        artifact_type=artifact_type,
        digest=digest,
        tag=tag,
        annotations=annotations or {},
    )


def _signature_and_receipt_listing() -> ReferrerListing:
    return ReferrerListing(
        referrers=[
            _referrer(
                artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE,
                digest="sha256:" + "1" * 64,
                tag="sig-tag",
            ),
            _referrer(
                artifact_type=WORM_RECEIPT_ARTIFACT_TYPE,
                digest="sha256:" + "2" * 64,
                tag="receipt-tag",
            ),
        ],
        available=True,
    )


def test_referrer_lines_surface_signature_and_receipt() -> None:
    # (a) A selected bundle's signature + receipt referrers surface, classified
    # by artifactType, with digest and tag — driven directly on the pure compose
    # method (no event loop).
    app = _app(_StubSources())
    listing = _signature_and_receipt_listing()
    blob = "\n".join(app.render_referrer_lines(listing))

    # Both referrers appear, classified.
    assert "signature" in blob
    assert "worm-receipt" in blob
    # Each referrer's artifactType, digest, and tag render.
    assert NOTARY_SIGNATURE_ARTIFACT_TYPE in blob
    assert WORM_RECEIPT_ARTIFACT_TYPE in blob
    assert "sha256:" + "1" * 64 in blob
    assert "sha256:" + "2" * 64 in blob
    assert "sig-tag" in blob and "receipt-tag" in blob


def test_referrer_render_through_pilot_populates_table() -> None:
    # (a) End-to-end: selecting a bundle fetches its referrers lazily and
    # populates the referrer table in the detail flow.
    record = _record()
    stub = _StubSources(bundles=[record], referrers=_signature_and_receipt_listing())
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable

            _select_row(app, "bundle-table")
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import Static

            assert "get_bundle_referrers" in stub.calls
            referrer_table = app.query_one("#referrer-table", DataTable)
            assert referrer_table.row_count == 2
            status = app.query_one("#referrers-status", Static)
            assert "2 referrers" in status.content.plain

    _run_async(scenario)


def _hostile_referrer() -> Referrer:
    return _referrer(
        artifact_type=f"type{_HOSTILE}",
        digest="sha256:" + "c" * 64,
        tag=f"tag{_HOSTILE}",
        annotations={f"ann{_HOSTILE}": f"val{_HOSTILE}"},
    )


def test_hostile_referrer_inert_across_referrer_view() -> None:
    # (b) A referrer whose artifactType/tag/annotation carries ANSI/OSC/DCS +
    # C0/C1 bytes renders inert across BOTH the referrer list AND the focused
    # referrer detail view (mirrors the hostile-bundle tests).
    app = _app(_StubSources())
    hostile = _hostile_referrer()
    listing = ReferrerListing(referrers=[hostile], available=True)

    # List rows + status line.
    for line in app.render_referrer_lines(listing):
        _assert_inert(line)
    _assert_inert(app.render_referrer_status(listing))

    # Focused referrer detail body (identity + subject bundle + annotations).
    app._selected = _hostile_record()
    for line in app.render_referrer_detail_lines(hostile):
        _assert_inert(line)


def test_hostile_referrer_inert_through_pilot_render() -> None:
    # (b) End-to-end: a hostile referrer flows through the real widget render
    # path (table + focused detail) without leaking a control byte.
    record = _record()
    hostile = _hostile_referrer()
    stub = _StubSources(
        bundles=[record], referrers=ReferrerListing(referrers=[hostile], available=True)
    )
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            _select_row(app, "bundle-table")
            await app.workers.wait_for_complete()
            await pilot.pause()

            for line in app.render_referrer_lines(app._referrers):
                _assert_inert(line)
            # Drill into the hostile referrer; the focused body is inert too.
            _select_row(app, "referrer-table")
            await pilot.pause()
            for line in app.render_referrer_detail_lines(hostile):
                _assert_inert(line)

    _run_async(scenario)


def test_counterfeit_signature_referrer_carries_no_verification_affordance() -> None:
    # (c) A counterfeit referrer whose artifactType == the signature constant is
    # classified/shown as `signature`, but the rendered text MUST carry NO
    # verification-implying claim: no "verified", and a neutral
    # "not verified — run cairn verify" affordance instead.
    app = _app(_StubSources())
    counterfeit = _referrer(artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE)
    listing = ReferrerListing(referrers=[counterfeit], available=True)

    line = app.render_referrer_lines(listing)[0]
    assert "signature" in line
    # Neutral, non-verification wording present; no claim of validation.
    lowered = line.lower()
    assert "not verified" in lowered
    assert "run cairn verify" in lowered
    # MUST NOT imply cryptographic validation.
    assert "verified" not in lowered.replace("not verified", "")
    assert "valid" not in lowered

    # The focused referrer view carries the same neutral label.
    app._selected = _record()
    focused = "\n".join(app.render_referrer_detail_lines(counterfeit))
    assert "not verified" in focused.lower()
    assert "valid" not in focused.lower()


def test_no_referrers_and_unavailable_render_distinctly() -> None:
    # (d) "no referrers" (available, empty) and "referrers unavailable"
    # (unavailable) render as DISTINCT, non-crashing indications.
    app = _app(_StubSources())
    empty = ReferrerListing(referrers=[], available=True)
    unavailable = ReferrerListing(referrers=[], available=False)

    empty_status = app.render_referrer_status(empty)
    unavailable_status = app.render_referrer_status(unavailable)

    assert "no referrers" in empty_status
    assert "referrers unavailable" in unavailable_status
    # Distinguishable from each other AND both inert.
    assert empty_status != unavailable_status
    _assert_inert(empty_status)
    _assert_inert(unavailable_status)
    # Neither populates the list rows.
    assert app.render_referrer_lines(empty) == []
    assert app.render_referrer_lines(unavailable) == []
    # A None (not-yet-fetched) listing is a benign empty string, not a crash.
    assert app.render_referrer_status(None) == ""
    assert app.render_referrer_lines(None) == []


def test_unavailable_referrers_render_through_pilot_without_crash() -> None:
    # (d) End-to-end: an "unavailable" listing leaves the session interactive,
    # shows the distinct status, and adds no rows.
    record = _record()
    stub = _StubSources(bundles=[record], referrers=ReferrerListing(referrers=[], available=False))
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable

            _select_row(app, "bundle-table")
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import Static

            assert app.is_running
            status = app.query_one("#referrers-status", Static)
            assert "referrers unavailable" in status.content.plain
            assert app.query_one("#referrer-table", DataTable).row_count == 0

    _run_async(scenario)


def test_bundle_to_referrer_and_back_navigation_via_pilot() -> None:
    # (e) bundle→referrer→back navigation: from a selected bundle's detail, drill
    # into a referrer (focused view names the subject bundle), and Esc returns to
    # the BUNDLE detail (NOT out of the detail tab); a second Esc leaves to the
    # origin tab.
    record = _record()
    stub = _StubSources(bundles=[record], referrers=_signature_and_receipt_listing())
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import Static, TabbedContent

            _select_row(app, "bundle-table")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one(TabbedContent).active == "tab-detail"
            assert app._referrer_focus is None

            # Drill INTO the first referrer — focused sub-state within the tab.
            _select_row(app, "referrer-table")
            await pilot.pause()
            assert app._referrer_focus is not None
            assert app.query_one(TabbedContent).active == "tab-detail"
            # Focused body names the subject bundle it refers back to.
            body = app.query_one("#detail-body", Static)
            assert "Subject bundle" in body.content.plain
            assert record["repositoryName"] in body.content.plain

            # Esc returns to the BUNDLE detail (clears focus) WITHOUT leaving tab.
            app.action_back()
            await pilot.pause()
            assert app._referrer_focus is None
            assert app.query_one(TabbedContent).active == "tab-detail"
            assert "== Lifecycle ==" in app.query_one("#detail-body", Static).content.plain

            # A second Esc drills up to the origin tab (the bundle list).
            app.action_back()
            await pilot.pause()
            assert app.query_one(TabbedContent).active == "tab-list"

    _run_async(scenario)


def test_selecting_new_bundle_clears_prior_referrer_focus() -> None:
    # Selecting a different bundle resets the referrer drill-down sub-state so a
    # stale focused referrer never bleeds across selections.
    record_a = _record(projectId="proj-a")
    record_b = _record(projectId="proj-b", repositoryName=f"artifact-{QUALIFIER}/proj-b")
    stub = _StubSources(bundles=[record_a, record_b], referrers=_signature_and_receipt_listing())
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            _select_row(app, "bundle-table", row=0)
            await app.workers.wait_for_complete()
            await pilot.pause()
            _select_row(app, "referrer-table", row=0)
            await pilot.pause()
            assert app._referrer_focus is not None

            # Selecting another bundle clears the focus.
            _select_row(app, "bundle-table", row=1)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._referrer_focus is None

    _run_async(scenario)


def test_open_referrer_out_of_range_index_is_noop() -> None:
    # Edge case: a referrer row index beyond the listing bounds (or negative)
    # must NOT raise and must NOT set a focus — the index guard in _open_referrer
    # keeps a stale/oversized row key from indexing past the list. (These all hit
    # the guard before _rerender_detail_body, so no mounted widgets are needed.)
    app = _app(_StubSources())
    app._referrers = _signature_and_receipt_listing()  # 2 referrers
    assert app._referrer_focus is None

    app._open_referrer(2)  # one past the end
    assert app._referrer_focus is None
    app._open_referrer(99)
    assert app._referrer_focus is None
    app._open_referrer(-1)
    assert app._referrer_focus is None


def test_open_referrer_valid_index_sets_focus_via_pilot() -> None:
    # The valid-index branch re-renders the detail body, so it needs a mounted
    # app: a within-bounds selection focuses that referrer.
    record = _record()
    stub = _StubSources(bundles=[record], referrers=_signature_and_receipt_listing())
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            _select_row(app, "bundle-table")
            await app.workers.wait_for_complete()
            await pilot.pause()

            app._open_referrer(0)
            await pilot.pause()
            assert app._referrer_focus is app._referrers.referrers[0]

    _run_async(scenario)


def test_open_referrer_when_no_listing_or_unavailable_is_noop() -> None:
    # Selecting a referrer row when the listing is None (not yet fetched),
    # empty ("no referrers"), or unavailable ("referrers unavailable") must be a
    # no-op — there is no referrer to focus and the sub-state must stay clear.
    # All three hit the guard before any widget query, so no mount is needed.
    app = _app(_StubSources())
    app._referrers = None
    app._open_referrer(0)
    assert app._referrer_focus is None

    app._referrers = ReferrerListing(referrers=[], available=True)  # none
    app._open_referrer(0)
    assert app._referrer_focus is None

    app._referrers = ReferrerListing(referrers=[], available=False)  # unavailable
    app._open_referrer(0)
    assert app._referrer_focus is None


def test_esc_on_empty_referrer_table_drills_up_normally() -> None:
    # Pressing Esc in the detail tab when the referrer table is empty (the
    # "no referrers" case, so no referrer can be focused) must drill UP to the
    # origin tab in ONE step — the bundle⇄referrer sub-state only intercepts Esc
    # when a referrer is actually focused.
    record = _record()
    stub = _StubSources(bundles=[record], referrers=ReferrerListing(referrers=[], available=True))
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable, TabbedContent

            _select_row(app, "bundle-table")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one(TabbedContent).active == "tab-detail"
            assert app.query_one("#referrer-table", DataTable).row_count == 0
            assert app._referrer_focus is None

            # Esc drills straight up to the origin tab (no focused sub-state to
            # intercept it, because the empty table has no row to focus).
            app.action_back()
            await pilot.pause()
            assert app.query_one(TabbedContent).active == "tab-list"

    _run_async(scenario)


def test_esc_on_unavailable_referrer_table_drills_up_normally() -> None:
    # Same drill-up contract when referrers are UNAVAILABLE (read failed): no
    # referrer can be focused, so a single Esc leaves the detail tab.
    record = _record()
    stub = _StubSources(bundles=[record], referrers=ReferrerListing(referrers=[], available=False))
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable, TabbedContent

            _select_row(app, "bundle-table")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#referrer-table", DataTable).row_count == 0
            assert app._referrer_focus is None

            app.action_back()
            await pilot.pause()
            assert app.query_one(TabbedContent).active == "tab-list"

    _run_async(scenario)


def test_referrer_annotation_key_escape_bytes_render_inert() -> None:
    # (b) Sharpen the hostile-field coverage: a referrer whose annotation KEY
    # (not just value) carries escape bytes must render inert in the focused
    # detail body — the key is producer-influenceable and routes through _safe
    # just like the value. A benign value with a hostile key isolates the key
    # path so a regression that sanitized only values would be caught.
    app = _app(_StubSources())
    app._selected = _record()
    hostile_key_referrer = _referrer(
        artifact_type=WORM_RECEIPT_ARTIFACT_TYPE,
        annotations={f"k{_HOSTILE}": "benign-value"},
    )
    for line in app.render_referrer_detail_lines(hostile_key_referrer):
        _assert_inert(line)
    # The hostile key's benign value still surfaces (only control bytes stripped).
    blob = "\n".join(app.render_referrer_detail_lines(hostile_key_referrer))
    assert "benign-value" in blob


def test_refresh_while_referrer_focused_keeps_session_interactive() -> None:
    # Watch/refresh edge case: a refresh of the selected bundle's detail while a
    # referrer is focused re-runs the lazy referrer read on the same worker. The
    # session MUST stay interactive, the referrer listing MUST be refreshed, and
    # the body MUST stay render-safe.
    #
    # It ALSO guards the watch/refresh ⇄ referrer-focus seam: a fresh detail
    # re-fetch resets the body to the BUNDLE detail, so _render_detail clears
    # _referrer_focus. After the refresh the visible body and the sub-state agree
    # (no referrer focused, bundle detail shown), and a SINGLE Esc drills up to
    # the list — the refresh must not leave a stale focus that swallows an Esc.
    record = _record()
    stub = _StubSources(bundles=[record], referrers=_signature_and_receipt_listing())
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable, Static, TabbedContent

            _select_row(app, "bundle-table")
            await app.workers.wait_for_complete()
            await pilot.pause()
            _select_row(app, "referrer-table", row=0)
            await pilot.pause()
            assert app._referrer_focus is not None
            assert "Subject bundle" in app.query_one("#detail-body", Static).content.plain

            # A refresh while the detail tab is active re-fetches THIS bundle.
            app.action_refresh()
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert app.is_running
            assert app.query_one(TabbedContent).active == "tab-detail"
            # The referrer listing was re-read and the table re-populated.
            assert stub.calls.count("get_bundle_referrers") >= 2
            assert app.query_one("#referrer-table", DataTable).row_count == 2

            # The fresh re-fetch cleared the stale focus AND reverted the body to
            # the bundle detail — the two now agree. Body stays render-safe.
            body_after = app.query_one("#detail-body", Static)
            assert app._referrer_focus is None
            assert "== Lifecycle ==" in body_after.content.plain
            for line in body_after.content.plain.splitlines():
                _assert_inert(line)

            # With no stale focus to intercept it, a SINGLE Esc drills up to the
            # list (no swallowed keystroke).
            app.action_back()
            await pilot.pause()
            assert app.is_running
            assert app.query_one(TabbedContent).active == "tab-list"

    _run_async(scenario)


# --------------------------------------------------------------------------
# 9. List grouping: referrers nest under their primary; orphans -> unattached;
#    referrer-less primary is a leaf; expand/collapse rebuilds rows; expansion
#    persists across a watch-tick rebuild.
# --------------------------------------------------------------------------

from cairn_cli.tui.app import (  # noqa: E402
    _CHILD_PREFIX,
    _COLLAPSED_MARKER,
    _EXPANDED_MARKER,
    _UNATTACHED_GROUP_LABEL,
)

_PRIMARY_DIGEST = "sha256:" + "a" * 64


def _primary_record(**extra: Any) -> dict[str, Any]:
    """A validated primary bundle the referrers will nest under."""
    return _record(manifestDigest=_PRIMARY_DIGEST, projectId="proj-a", **extra)


def _referrer_child(
    *,
    classification: str,
    subject_digest: str | None,
    digest: str,
    **extra: Any,
) -> dict[str, Any]:
    """A synthetic pipeline-minted referrer entry (unindexed, has subjectDigest)."""
    return _unindexed(
        classification=classification,
        subjectDigest=subject_digest,
        manifestDigest=digest,
        **extra,
    )


def _project_col(app: ArtifactStoreApp) -> list[str]:
    """The rendered projectId column (index 0) of #bundle-table, including prefix."""
    from textual.coordinate import Coordinate
    from textual.widgets import DataTable

    table = app.query_one("#bundle-table", DataTable)
    return [table.get_cell_at(Coordinate(row, 0)).plain for row in range(table.row_count)]


def test_referrer_children_nest_under_primary_when_expanded() -> None:
    # 3.1 — a primary plus its three pipeline-minted referrers (subjectDigest ==
    # primary's manifestDigest) render as collapsible children. Collapsed by
    # default (one row, with an expand marker); after a toggle the three nest.
    primary = _primary_record()
    sig = _referrer_child(
        classification="signature", subject_digest=_PRIMARY_DIGEST, digest="sha256:" + "1" * 64
    )
    receipt = _referrer_child(
        classification="worm-receipt", subject_digest=_PRIMARY_DIGEST, digest="sha256:" + "2" * 64
    )
    resource = _referrer_child(
        classification="resource-ref",
        subject_digest=_PRIMARY_DIGEST,
        digest="sha256:" + "3" * 64,
    )
    stub = _StubSources(bundles=[primary, sig, receipt, resource])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            # Collapsed default: only the primary row, carrying an expand marker.
            assert table.row_count == 1
            assert _project_col(app)[0].startswith(_COLLAPSED_MARKER)

            # Expand the primary's group (Space on the selected primary row).
            table.move_cursor(row=0)
            app.action_toggle_group()
            await pilot.pause()

            col = _project_col(app)
            # Primary + 3 nested children.
            assert table.row_count == 4
            assert col[0].startswith(_EXPANDED_MARKER)
            for child_row in col[1:]:
                assert child_row.startswith(_CHILD_PREFIX)

    _run_async(scenario)


def test_attestation_referrer_and_its_signature_nest_under_content_not_unattached() -> None:
    # The producer-claim Producer_Attestation_Referrer (subject = content digest) is a
    # referrer child of the content bundle, and the Notation signature OVER the
    # attestation (subject = the attestation's OWN digest — a referrer-of-a-referrer)
    # must resolve transitively up to the content bundle rather than dangling in
    # the "unattached" group. The attestation rolls up to the content bundle (not
    # the unattached group); its signature is not surfaced as unattached.
    att_digest = "sha256:" + "e" * 64
    primary = _primary_record()
    attestation = _referrer_child(
        classification="attestation", subject_digest=_PRIMARY_DIGEST, digest=att_digest
    )
    content_sig = _referrer_child(
        classification="signature", subject_digest=_PRIMARY_DIGEST, digest="sha256:" + "1" * 64
    )
    # The attestation's own signature points at the ATTESTATION digest, not the
    # content digest — the transitive-resolution case.
    attestation_sig = _referrer_child(
        classification="signature", subject_digest=att_digest, digest="sha256:" + "2" * 64
    )
    stub = _StubSources(bundles=[primary, attestation, content_sig, attestation_sig])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            # Collapsed default: one primary row, expandable (it has children).
            assert table.row_count == 1
            assert _project_col(app)[0].startswith(_COLLAPSED_MARKER)

            table.move_cursor(row=0)
            app.action_toggle_group()
            await pilot.pause()

            col = _project_col(app)
            # Primary + all THREE referrers (attestation, content sig, attestation
            # sig) nested — and crucially NO "unattached" group row.
            assert table.row_count == 4
            assert col[0].startswith(_EXPANDED_MARKER)
            for child_row in col[1:]:
                assert child_row.startswith(_CHILD_PREFIX)
            assert _UNATTACHED_GROUP_LABEL not in col

    _run_async(scenario)


def test_referrerless_primary_is_leaf_with_no_expand_affordance() -> None:
    # 3.1 — a primary with no referrer children renders as a plain leaf row: no
    # expand/collapse marker, and toggling it is a no-op.
    primary = _primary_record()
    stub = _StubSources(bundles=[primary])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            assert table.row_count == 1
            col = _project_col(app)
            assert not col[0].startswith(_COLLAPSED_MARKER)
            assert not col[0].startswith(_EXPANDED_MARKER)

            # Toggling a leaf is a no-op (still one row, still no marker).
            table.move_cursor(row=0)
            app.action_toggle_group()
            await pilot.pause()
            assert app.query_one("#bundle-table", DataTable).row_count == 1

    _run_async(scenario)


def test_orphan_referrer_lands_in_unattached_group_not_dropped() -> None:
    # 3.1 — a referrer whose subjectDigest matches no listed primary (here: a
    # different digest) AND one whose subjectDigest is None both surface in the
    # "unattached" group rather than being silently dropped.
    primary = _primary_record()
    orphan = _referrer_child(
        classification="signature",
        subject_digest="sha256:" + "f" * 64,  # no listed primary has this digest
        digest="sha256:" + "9" * 64,
    )
    none_subject = _referrer_child(
        classification="resource-ref",
        subject_digest=None,  # missing subject -> stays out of any primary
        digest="sha256:" + "8" * 64,
    )
    stub = _StubSources(bundles=[primary, orphan, none_subject])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            col = _project_col(app)
            # The unattached header row is present and labelled.
            assert any(_UNATTACHED_GROUP_LABEL in cell for cell in col), col
            # Two distinct "surfaced not dropped" cases, both present (4 rows):
            #   - the unattached header + the genuine orphan (subjectDigest matches
            #     no listed primary) under it;
            #   - the None-subject referrer, which per design is "not a child" and
            #     so stays a TOP-LEVEL leaf row (NOT in the unattached group);
            #   - plus the real primary, itself a leaf (no children).
            assert table.row_count == 4
            # Neither the genuine orphan nor the None-subject entry is dropped —
            # both their digests appear in a digest cell.
            from textual.coordinate import Coordinate

            digests = [table.get_cell_at(Coordinate(r, 5)).plain for r in range(table.row_count)]
            assert any("8" * 12 in d for d in digests)
            assert any("9" * 12 in d for d in digests)

    _run_async(scenario)


def test_expansion_state_persists_across_watch_tick_rebuild() -> None:
    # 3.1 (design risk note) — expanding a group then triggering a list rebuild
    # (as a watch tick does) must NOT collapse it. Drive a second
    # _populate_bundle_table with the same records and assert the children remain.
    primary = _primary_record()
    sig = _referrer_child(
        classification="signature", subject_digest=_PRIMARY_DIGEST, digest="sha256:" + "1" * 64
    )
    stub = _StubSources(bundles=[primary, sig])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            table.move_cursor(row=0)
            app.action_toggle_group()  # expand
            await pilot.pause()
            assert app.query_one("#bundle-table", DataTable).row_count == 2

            # Simulate a watch-tick rebuild with the same record set.
            app._populate_bundle_table(list(stub._bundles))
            await pilot.pause()
            # Still expanded — the child survived the rebuild.
            assert app.query_one("#bundle-table", DataTable).row_count == 2
            assert _project_col(app)[0].startswith(_EXPANDED_MARKER)

    _run_async(scenario)


def test_child_row_selection_opens_referrer_record_detail() -> None:
    # 3.1 — selecting a nested CHILD row resolves to that referrer's record and
    # opens its detail (the grouped table's index-keyed selection extends to
    # children, not just primaries).
    primary = _primary_record()
    child_digest = "sha256:" + "1" * 64
    sig = _referrer_child(
        classification="signature", subject_digest=_PRIMARY_DIGEST, digest=child_digest
    )
    stub = _StubSources(bundles=[primary, sig])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable, TabbedContent

            table = app.query_one("#bundle-table", DataTable)
            table.move_cursor(row=0)
            app.action_toggle_group()  # expand so the child row exists
            await pilot.pause()

            _select_row(app, "bundle-table", row=1)  # the child
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert app.query_one(TabbedContent).active == "tab-detail"
            assert app._selected is sig  # the child's referrer record

    _run_async(scenario)


def test_selecting_unattached_header_row_is_noop() -> None:
    # 3.1 — the synthetic "unattached" header row backs no record, so selecting
    # it neither opens a detail nor raises.
    primary = _primary_record()
    orphan = _referrer_child(
        classification="signature", subject_digest="sha256:" + "f" * 64, digest="sha256:" + "9" * 64
    )
    stub = _StubSources(bundles=[primary, orphan])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.coordinate import Coordinate
            from textual.widgets import DataTable, TabbedContent

            table = app.query_one("#bundle-table", DataTable)
            header_row = next(
                r
                for r in range(table.row_count)
                if _UNATTACHED_GROUP_LABEL in table.get_cell_at(Coordinate(r, 0)).plain
            )
            stub.calls.clear()
            _select_row(app, "bundle-table", row=header_row)
            await pilot.pause()
            # No detail opened (header backs no record); still on the list.
            assert app.query_one(TabbedContent).active == "tab-list"
            assert "get_bundle_detail" not in stub.calls

    _run_async(scenario)


def test_index_entry_is_not_grouped_as_referrer_child() -> None:
    # 3.1 — an "index" classification is NOT a minted referrer (it carries no
    # subject), so even with a stray subjectDigest it stays a top-level row, never
    # nested. Guards the _REFERRER_CLASSIFICATIONS membership (index excluded).
    primary = _primary_record()
    index_entry = _unindexed(
        classification="index",
        subjectDigest=_PRIMARY_DIGEST,  # even if present, index never nests
        manifestDigest="sha256:" + "7" * 64,
    )
    stub = _StubSources(bundles=[primary, index_entry])
    app = _app(stub)
    app._show_indexes = True  # show the index so we can assert it stays top-level

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            # Two top-level leaf rows, no nesting, no unattached group.
            assert table.row_count == 2
            col = _project_col(app)
            assert not any(c.startswith(_CHILD_PREFIX) for c in col)
            assert not any(_UNATTACHED_GROUP_LABEL in c for c in col)

    _run_async(scenario)


# --------------------------------------------------------------------------
# 10. Raw-manifest drill-down: bundle (reused fetch) + focused referrer (lazy),
#     whole-blob render safety, Esc-unwind levels.
# --------------------------------------------------------------------------

_BUNDLE_MANIFEST = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "config": {"mediaType": "application/vnd.cairnproofs.artifact.config.v1+json"},
    "layers": [{"digest": "sha256:" + "d" * 64, "size": 42}],
    "annotations": {"org.cairn.note": "hello"},
}


def test_bundle_raw_manifest_renders_pretty_printed_no_new_call() -> None:
    # 3.2 — drilling into the bundle's raw manifest pretty-prints it, reusing the
    # manifest already fetched on selection (NO get_referrer_manifest call).
    record = _record()
    stub = _StubSources(bundles=[record], bundle_manifest=_BUNDLE_MANIFEST)
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import Static

            _select_row(app, "bundle-table")
            await app.workers.wait_for_complete()
            await pilot.pause()

            app.action_raw_manifest()
            await pilot.pause()

            body = app.query_one("#detail-body", Static).content.plain
            assert "Raw OCI manifest" in body
            assert '"schemaVersion": 2' in body  # pretty-printed (indent=2)
            assert "org.cairn.note" in body
            # No focused-referrer read for the BUNDLE manifest path.
            assert "get_referrer_manifest" not in stub.calls

    _run_async(scenario)


def test_focused_referrer_raw_manifest_is_lazy_batch_get_image() -> None:
    # 3.2 — drilling into a FOCUSED referrer's raw manifest issues exactly one
    # lazy get_referrer_manifest (ecr:BatchGetImage), keyed by the referrer's
    # digest, and renders it pretty-printed.
    record = _record()
    ref_digest = "sha256:" + "1" * 64
    ref_manifest = {
        "artifactType": NOTARY_SIGNATURE_ARTIFACT_TYPE,
        "subject": {"digest": ref_digest},
    }
    listing = ReferrerListing(
        referrers=[
            Referrer(
                artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE,
                digest=ref_digest,
                tag="sig",
                annotations={},
            )
        ],
        available=True,
    )
    stub = _StubSources(
        bundles=[record], referrers=listing, referrer_manifests={ref_digest: ref_manifest}
    )
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import Static

            _select_row(app, "bundle-table")
            await app.workers.wait_for_complete()
            await pilot.pause()
            _select_row(app, "referrer-table", row=0)  # focus the referrer
            await pilot.pause()

            app.action_raw_manifest()
            await pilot.pause()

            assert stub.calls.count("get_referrer_manifest") == 1
            # Read was keyed by the referrer's digest, riding the subject bundle's
            # registry/repository.
            assert stub.referrer_manifest_args[0][2] == ref_digest
            body = app.query_one("#detail-body", Static).content.plain
            assert "Raw OCI manifest" in body
            assert NOTARY_SIGNATURE_ARTIFACT_TYPE in body

    _run_async(scenario)


def test_unavailable_manifest_shows_render_safe_message_not_crash() -> None:
    # 3.2 — a None/unavailable manifest degrades to a fixed render-safe message
    # rather than crashing the session.
    record = _record()
    stub = _StubSources(bundles=[record], bundle_manifest=None)  # no manifest
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import Static

            _select_row(app, "bundle-table")
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_raw_manifest()
            await pilot.pause()

            assert app.is_running
            body = app.query_one("#detail-body", Static).content.plain
            assert "unavailable" in body.lower()

    _run_async(scenario)


def test_raw_manifest_esc_unwinds_one_level_at_a_time() -> None:
    # 3.2 / 3.3 — Esc unwinds exactly one level per press: raw-manifest -> focus
    # (referrer) -> bundle detail -> origin tab. Open a focused referrer, drill
    # into its raw manifest, then Esc three times and assert each intermediate
    # state.
    record = _record()
    ref_digest = "sha256:" + "1" * 64
    listing = ReferrerListing(
        referrers=[
            Referrer(
                artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE,
                digest=ref_digest,
                tag="sig",
                annotations={},
            )
        ],
        available=True,
    )
    stub = _StubSources(
        bundles=[record],
        referrers=listing,
        referrer_manifests={ref_digest: {"artifactType": NOTARY_SIGNATURE_ARTIFACT_TYPE}},
    )
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import Static, TabbedContent

            _select_row(app, "bundle-table")
            await app.workers.wait_for_complete()
            await pilot.pause()
            _select_row(app, "referrer-table", row=0)
            await pilot.pause()
            assert app._referrer_focus is not None

            app.action_raw_manifest()
            await pilot.pause()
            assert app._showing_raw_manifest is True
            assert "Raw OCI manifest" in app.query_one("#detail-body", Static).content.plain

            # Esc 1: raw-manifest -> referrer focus (still in detail tab).
            app.action_back()
            await pilot.pause()
            assert app._showing_raw_manifest is False
            assert app._referrer_focus is not None
            assert app.query_one(TabbedContent).active == "tab-detail"
            assert "== Referrer ==" in app.query_one("#detail-body", Static).content.plain

            # Esc 2: referrer focus -> bundle detail (still in detail tab).
            app.action_back()
            await pilot.pause()
            assert app._referrer_focus is None
            assert app.query_one(TabbedContent).active == "tab-detail"
            assert "== Lifecycle ==" in app.query_one("#detail-body", Static).content.plain

            # Esc 3: bundle detail -> origin tab (the bundle list).
            app.action_back()
            await pilot.pause()
            assert app.query_one(TabbedContent).active == "tab-list"

    _run_async(scenario)


def test_bundle_raw_manifest_esc_returns_to_bundle_detail() -> None:
    # 3.2 — the raw-manifest level opened from the BUNDLE (no referrer focus)
    # unwinds in one Esc back to the bundle detail, then a second Esc to origin.
    record = _record()
    stub = _StubSources(bundles=[record], bundle_manifest=_BUNDLE_MANIFEST)
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import Static, TabbedContent

            _select_row(app, "bundle-table")
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_raw_manifest()
            await pilot.pause()
            assert app._showing_raw_manifest is True

            app.action_back()  # raw -> bundle detail
            await pilot.pause()
            assert app._showing_raw_manifest is False
            assert app.query_one(TabbedContent).active == "tab-detail"
            assert "== Lifecycle ==" in app.query_one("#detail-body", Static).content.plain

            app.action_back()  # bundle detail -> list
            await pilot.pause()
            assert app.query_one(TabbedContent).active == "tab-list"

    _run_async(scenario)


# --------------------------------------------------------------------------
# 11. Grouping edge cases (coverage gaps): nesting under an UNVALIDATED primary,
#     two-primary cross-attribution + child off-by-one, expansion-state
#     independence / disappearing-primary, focused-referrer manifest None.
# --------------------------------------------------------------------------


def test_referrer_nests_under_unvalidated_primary_not_only_validated() -> None:
    # "A referrer whose subject is itself unindexed": the grouping key
    # is manifestDigest equality against ANY listed primary — validated OR
    # unvalidated. A referrer whose subjectDigest equals an UNVALIDATED (also
    # unindexed) primary's manifestDigest must nest under it, NOT fall to the
    # unattached group.
    unvalidated_primary_digest = "sha256:" + "c" * 64
    unvalidated_primary = _unindexed(
        classification="unvalidated bundle",
        manifestDigest=unvalidated_primary_digest,
        subjectDigest=None,  # a primary, not a referrer child
    )
    child = _referrer_child(
        classification="signature",
        subject_digest=unvalidated_primary_digest,
        digest="sha256:" + "1" * 64,
    )
    stub = _StubSources(bundles=[unvalidated_primary, child])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            # Collapsed: just the unvalidated primary, carrying an expand marker
            # (it HAS a child) — no unattached group.
            assert table.row_count == 1
            col = _project_col(app)
            assert col[0].startswith(_COLLAPSED_MARKER)
            assert not any(_UNATTACHED_GROUP_LABEL in c for c in col)

            # Expanding it nests the child.
            table.move_cursor(row=0)
            app.action_toggle_group()
            await pilot.pause()
            assert app.query_one("#bundle-table", DataTable).row_count == 2
            assert _project_col(app)[1].startswith(_CHILD_PREFIX)

    _run_async(scenario)


def test_two_primaries_interleaved_children_no_cross_attribution() -> None:
    # Two primaries with interleaved children: each child must nest under its OWN
    # parent (subjectDigest match), never the other primary. Selecting a specific
    # child row must resolve to THAT child's record (off-by-one guard on the
    # rebuilt _bundle_row_records index map).
    digest_a = "sha256:" + "a" * 64
    digest_b = "sha256:" + "b" * 64
    primary_a = _record(manifestDigest=digest_a, projectId="proj-a")
    primary_b = _record(manifestDigest=digest_b, projectId="proj-b")
    child_a = _referrer_child(
        classification="signature", subject_digest=digest_a, digest="sha256:" + "1" * 64
    )
    child_b = _referrer_child(
        classification="worm-receipt", subject_digest=digest_b, digest="sha256:" + "2" * 64
    )
    # Interleave the input order so a naive positional grouping would misattribute.
    stub = _StubSources(bundles=[primary_a, child_b, primary_b, child_a])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable, TabbedContent

            table = app.query_one("#bundle-table", DataTable)
            # Two collapsed primaries, no unattached group (both children matched).
            assert table.row_count == 2
            col = _project_col(app)
            assert not any(_UNATTACHED_GROUP_LABEL in c for c in col)

            # Expand BOTH primaries. Rows: A(0) child_a(1) B(2) child_b(3).
            table.move_cursor(row=0)
            app.action_toggle_group()
            await pilot.pause()
            table = app.query_one("#bundle-table", DataTable)
            # After expanding A, B moved to row 2.
            table.move_cursor(row=2)
            app.action_toggle_group()
            await pilot.pause()

            assert app.query_one("#bundle-table", DataTable).row_count == 4
            # The backing model maps each child row to the RIGHT referrer record.
            assert app._bundle_row_records[1] is child_a
            assert app._bundle_row_records[3] is child_b

            # Selecting child_b's row (index 3) opens child_b — not child_a.
            _select_row(app, "bundle-table", row=3)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one(TabbedContent).active == "tab-detail"
            assert app._selected is child_b

    _run_async(scenario)


def test_collapse_then_reexpand_restores_same_children() -> None:
    # Collapsing then re-expanding a group restores the same child set (the model
    # is rebuilt from the persisted records, not a one-shot expansion).
    primary = _primary_record()
    sig = _referrer_child(
        classification="signature", subject_digest=_PRIMARY_DIGEST, digest="sha256:" + "1" * 64
    )
    receipt = _referrer_child(
        classification="worm-receipt", subject_digest=_PRIMARY_DIGEST, digest="sha256:" + "2" * 64
    )
    stub = _StubSources(bundles=[primary, sig, receipt])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            table.move_cursor(row=0)
            app.action_toggle_group()  # expand
            await pilot.pause()
            assert app.query_one("#bundle-table", DataTable).row_count == 3

            app.query_one("#bundle-table", DataTable).move_cursor(row=0)
            app.action_toggle_group()  # collapse
            await pilot.pause()
            assert app.query_one("#bundle-table", DataTable).row_count == 1
            assert _project_col(app)[0].startswith(_COLLAPSED_MARKER)

            app.query_one("#bundle-table", DataTable).move_cursor(row=0)
            app.action_toggle_group()  # re-expand
            await pilot.pause()
            # Same two children return, both as child-styled rows.
            assert app.query_one("#bundle-table", DataTable).row_count == 3
            assert all(c.startswith(_CHILD_PREFIX) for c in _project_col(app)[1:])

    _run_async(scenario)


def test_expansion_state_for_one_primary_unaffected_by_toggling_another() -> None:
    # Toggling digest B's expansion must not change digest A's expansion state.
    digest_a = "sha256:" + "a" * 64
    digest_b = "sha256:" + "b" * 64
    primary_a = _record(manifestDigest=digest_a, projectId="proj-a")
    primary_b = _record(manifestDigest=digest_b, projectId="proj-b")
    child_a = _referrer_child(
        classification="signature", subject_digest=digest_a, digest="sha256:" + "1" * 64
    )
    child_b = _referrer_child(
        classification="signature", subject_digest=digest_b, digest="sha256:" + "2" * 64
    )
    stub = _StubSources(bundles=[primary_a, child_a, primary_b, child_b])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            # Expand A (row 0).
            table.move_cursor(row=0)
            app.action_toggle_group()
            await pilot.pause()
            assert digest_a in app._expanded_primaries
            assert digest_b not in app._expanded_primaries

            # Now expand B. A must stay expanded. With A expanded, rows are:
            # A(0) child_a(1) B(2) ...
            app.query_one("#bundle-table", DataTable).move_cursor(row=2)
            app.action_toggle_group()
            await pilot.pause()
            assert digest_a in app._expanded_primaries
            assert digest_b in app._expanded_primaries

            # Collapse B again; A is still expanded (independent state).
            # Rows now: A(0) child_a(1) B(2) child_b(3); B is at row 2.
            app.query_one("#bundle-table", DataTable).move_cursor(row=2)
            app.action_toggle_group()
            await pilot.pause()
            assert digest_a in app._expanded_primaries
            assert digest_b not in app._expanded_primaries

    _run_async(scenario)


def test_disappearing_expanded_primary_clears_stale_state_no_crash() -> None:
    # A primary expanded on one tick that VANISHES on the next watch-tick rebuild
    # must not leave stale expansion state (set must shrink) and must not crash.
    primary = _primary_record()
    sig = _referrer_child(
        classification="signature", subject_digest=_PRIMARY_DIGEST, digest="sha256:" + "1" * 64
    )
    stub = _StubSources(bundles=[primary, sig])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            table.move_cursor(row=0)
            app.action_toggle_group()  # expand
            await pilot.pause()
            assert _PRIMARY_DIGEST in app._expanded_primaries

            # Next tick: the primary (and its child) are gone entirely.
            app._populate_bundle_table([])
            await pilot.pause()
            assert app.is_running
            # Stale expansion state for the vanished primary was pruned.
            assert _PRIMARY_DIGEST not in app._expanded_primaries
            assert app.query_one("#bundle-table", DataTable).row_count == 0

    _run_async(scenario)


def test_focused_referrer_raw_manifest_none_shows_safe_message_not_crash() -> None:
    # Focused-referrer raw manifest when get_referrer_manifest returns None
    # (manifest unavailable for the referrer): a fixed render-safe message, no
    # crash. Distinct from the BUNDLE-manifest-None path already covered.
    record = _record()
    ref_digest = "sha256:" + "1" * 64
    listing = ReferrerListing(
        referrers=[
            Referrer(
                artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE,
                digest=ref_digest,
                tag="sig",
                annotations={},
            )
        ],
        available=True,
    )
    # referrer_manifests is empty -> get_referrer_manifest returns None.
    stub = _StubSources(bundles=[record], referrers=listing, referrer_manifests={})
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import Static

            _select_row(app, "bundle-table")
            await app.workers.wait_for_complete()
            await pilot.pause()
            _select_row(app, "referrer-table", row=0)  # focus the referrer
            await pilot.pause()

            app.action_raw_manifest()
            await pilot.pause()

            assert app.is_running
            assert stub.calls.count("get_referrer_manifest") == 1
            body = app.query_one("#detail-body", Static).content.plain
            assert "Raw OCI manifest" in body
            assert "unavailable" in body.lower()

    _run_async(scenario)


# --------------------------------------------------------------------------
# 10. Image-index entries hidden by default behind the `i` toggle.
# --------------------------------------------------------------------------


def _index_note(app: ArtifactStoreApp) -> str:
    from textual.widgets import Static

    return app.query_one("#index-note", Static).content.plain


def test_index_entries_hidden_by_default_with_count_note() -> None:
    # An `index`-classified entry is registry machinery; it is excluded from the
    # rendered list by default, and the #index-note reports how many are hidden.
    validated = _record()
    idx1 = _unindexed(classification="index", manifestDigest="sha256:" + "c" * 64)
    idx2 = _unindexed(classification="index", manifestDigest="sha256:" + "d" * 64)
    stub = _StubSources(bundles=[validated, idx1, idx2])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            # Only the validated bundle is rendered; both index entries hidden.
            assert table.row_count == 1
            assert "index" not in _artifact_kind_cells(app)
            note = _index_note(app)
            assert "2" in note and "hidden" in note.lower() and "press i" in note.lower()
            # The underlying fetched set is NOT pruned — both indexes still present.
            assert sum(1 for r in app._records if r.get("classification") == "index") == 2

    _run_async(scenario)


def test_toggle_reveals_then_rehides_indexes_without_refetch() -> None:
    validated = _record()
    idx = _unindexed(classification="index", manifestDigest="sha256:" + "c" * 64)
    stub = _StubSources(bundles=[validated, idx])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            assert table.row_count == 1  # hidden by default
            calls_before = stub.calls.count("list_all_bundles")

            app.action_toggle_indexes()  # show
            await pilot.pause()
            assert table.row_count == 2
            assert "index" in _artifact_kind_cells(app)
            assert _index_note(app) == ""  # nothing hidden now

            app.action_toggle_indexes()  # hide again
            await pilot.pause()
            assert table.row_count == 1
            assert "hidden" in _index_note(app).lower()

            # No re-fetch: the toggle re-renders from the cached record set.
            assert stub.calls.count("list_all_bundles") == calls_before

    _run_async(scenario)


def test_hidden_index_choice_persists_across_rebuild_and_spares_nesting() -> None:
    # Hiding indexes must not disturb referrer nesting, and the choice survives a
    # watch-tick rebuild (re-populate from the same records).
    primary = _primary_record()
    sig = _referrer_child(
        classification="signature", subject_digest=_PRIMARY_DIGEST, digest="sha256:" + "1" * 64
    )
    idx = _unindexed(classification="index", manifestDigest="sha256:" + "e" * 64)
    stub = _StubSources(bundles=[primary, sig, idx])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            # Default: index hidden; primary present (collapsed, has a child).
            assert table.row_count == 1
            assert _project_col(app)[0].startswith(_COLLAPSED_MARKER)
            assert "1" in _index_note(app)

            # Simulate a watch-tick rebuild from the same records.
            app._populate_bundle_table(app._records)
            await pilot.pause()
            assert table.row_count == 1  # still hidden after rebuild
            assert "1" in _index_note(app)

            # Expanding the primary still nests its referrer child (nesting intact).
            table.move_cursor(row=0)
            app.action_toggle_group()
            await pilot.pause()
            assert table.row_count == 2
            assert _project_col(app)[1].startswith(_CHILD_PREFIX)

    _run_async(scenario)


# --------------------------------------------------------------------------
# 10. Create-date column is present on EVERY row.
#     The synthetic "unattached" header backs no record but must still carry
#     the full 7-column count (placeholder in the create-date column) so the
#     rows below it stay column-aligned; every backed row renders its own
#     ``createdAt`` in the create-date column (index 6).
# --------------------------------------------------------------------------

_CREATE_DATE_COL = 6  # 7-column bundle table: createdAt is the last column.


def _row_cell_counts(app: ArtifactStoreApp) -> list[int]:
    """The number of cells in each #bundle-table row, top to bottom."""
    from textual.widgets import DataTable

    table = app.query_one("#bundle-table", DataTable)
    return [len(table.get_row_at(row)) for row in range(table.row_count)]


def _create_date_col(app: ArtifactStoreApp) -> list[str]:
    """The rendered create-date column (index 6) of #bundle-table, per row."""
    from textual.coordinate import Coordinate
    from textual.widgets import DataTable

    table = app.query_one("#bundle-table", DataTable)
    return [
        table.get_cell_at(Coordinate(row, _CREATE_DATE_COL)).plain for row in range(table.row_count)
    ]


def test_unattached_header_carries_full_column_count_with_placeholder_date() -> None:
    # The synthetic "unattached" group-header backs no record, but it must
    # emit all 7 cells (placeholder in the create-date column) so every row below
    # it stays column-aligned.
    primary = _primary_record()
    orphan = _referrer_child(
        classification="signature",
        subject_digest="sha256:" + "f" * 64,  # no listed primary -> unattached
        digest="sha256:" + "9" * 64,
    )
    stub = _StubSources(bundles=[primary, orphan])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.coordinate import Coordinate
            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            # Every row — including the synthetic header — carries 7 cells.
            assert _row_cell_counts(app) == [7] * table.row_count

            header_row = next(
                r
                for r in range(table.row_count)
                if _UNATTACHED_GROUP_LABEL in table.get_cell_at(Coordinate(r, 0)).plain
            )
            # The header's create-date cell is the placeholder, not a shifted value
            # nor a missing cell.
            assert table.get_cell_at(Coordinate(header_row, _CREATE_DATE_COL)).plain == _PLACEHOLDER

    _run_async(scenario)


def test_indexed_primary_row_renders_its_create_date() -> None:
    # 2.2(b) — a validated, indexed primary renders its ``createdAt`` in the
    # create-date column (index 6), not an empty cell.
    primary = _primary_record(createdAt="2026-06-01T00:00:00Z")
    stub = _StubSources(bundles=[primary])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert _create_date_col(app) == ["2026-06-01T00:00:00Z"]

    _run_async(scenario)


def test_unvalidated_entry_row_renders_its_create_date() -> None:
    # 2.2(b) — an ECR-present-but-unindexed (``unvalidated``) entry renders its
    # ``createdAt`` in the create-date column.
    entry = _unindexed(
        classification="unvalidated bundle",
        createdAt="2026-06-02T00:00:00Z",
        manifestDigest="sha256:" + "c" * 64,
    )
    stub = _StubSources(bundles=[entry])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert _create_date_col(app) == ["2026-06-02T00:00:00Z"]

    _run_async(scenario)


def test_referrer_child_row_renders_its_create_date() -> None:
    # 2.2(b) — an expanded referrer CHILD row renders its OWN ``createdAt`` in the
    # create-date column (distinct from the primary's), so no real row is blank.
    primary = _primary_record(createdAt="2026-06-03T00:00:00Z")
    sig = _referrer_child(
        classification="signature",
        subject_digest=_PRIMARY_DIGEST,
        digest="sha256:" + "1" * 64,
        createdAt="2026-06-04T00:00:00Z",
    )
    stub = _StubSources(bundles=[primary, sig])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            table.move_cursor(row=0)
            app.action_toggle_group()  # expand so the child row exists
            await pilot.pause()

            # Primary's date on row 0, the child's own date on row 1.
            assert _create_date_col(app) == ["2026-06-03T00:00:00Z", "2026-06-04T00:00:00Z"]

    _run_async(scenario)


def test_backed_row_without_created_at_renders_placeholder_column_aligned() -> None:
    # 2.2(b) edge — when a backed (real) row has NO createdAt (the realistic case
    # where AWS imagePushedAt was genuinely missing, so the data layer left the key
    # absent), the create-date column shows the placeholder, NOT an empty/shifted
    # cell, and the row still carries all 7 cells so columns stay aligned.
    entry = _unindexed(manifestDigest="sha256:" + "d" * 64)
    entry.pop("createdAt")  # genuinely-missing push time
    stub = _StubSources(bundles=[entry])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert _row_cell_counts(app) == [7]
            assert _create_date_col(app) == [_PLACEHOLDER]

    _run_async(scenario)


def test_backed_row_with_empty_created_at_renders_placeholder() -> None:
    # 2.2(b) edge — an empty-string createdAt (falsy) hits the ``or _PLACEHOLDER``
    # fallback at the render site and shows the placeholder, not a blank cell.
    entry = _unindexed(createdAt="", manifestDigest="sha256:" + "e" * 64)
    stub = _StubSources(bundles=[entry])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert _create_date_col(app) == [_PLACEHOLDER]

    _run_async(scenario)


def test_bundle_table_defines_createdat_as_seventh_column() -> None:
    # Column-definition completeness: the create-date column is the 7th (and last)
    # column header, matching the 7 cells every header/data row emits. Guards
    # against a column being added/removed without the row builders following.
    primary = _primary_record(createdAt="2026-06-07T00:00:00Z")
    stub = _StubSources(bundles=[primary])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            labels = [str(c.label.plain) for c in table.columns.values()]
            assert len(labels) == 7
            assert labels[_CREATE_DATE_COL] == "createdAt"

    _run_async(scenario)


def test_unattached_orphan_row_renders_its_create_date() -> None:
    # 2.2(b) — an orphaned referrer surfaced under the "unattached" header renders
    # its ``createdAt`` in the create-date column; only the synthetic header above
    # it shows the placeholder.
    primary = _primary_record(createdAt="2026-06-05T00:00:00Z")
    orphan = _referrer_child(
        classification="signature",
        subject_digest="sha256:" + "f" * 64,  # no listed primary -> unattached
        digest="sha256:" + "9" * 64,
        createdAt="2026-06-06T00:00:00Z",
    )
    stub = _StubSources(bundles=[primary, orphan])
    app = _app(stub)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            from textual.coordinate import Coordinate
            from textual.widgets import DataTable

            table = app.query_one("#bundle-table", DataTable)
            dates = _create_date_col(app)
            # The orphan's own date is present; the placeholder appears ONLY on the
            # synthetic header row (no real row shows an empty create-date cell).
            assert "2026-06-06T00:00:00Z" in dates
            header_row = next(
                r
                for r in range(table.row_count)
                if _UNATTACHED_GROUP_LABEL in table.get_cell_at(Coordinate(r, 0)).plain
            )
            assert [d for d in dates if d == _PLACEHOLDER] == [dates[header_row]]

    _run_async(scenario)

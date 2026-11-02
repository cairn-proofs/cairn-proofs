"""Textual application for the read-only Artifact_TUI (``cairn tui``).

This is the view layer over :class:`~cairn_cli.tui._sources.ArtifactSources`.
It owns four views — an ECR-derived bundle list, a per-bundle detail pane
(lifecycle timeline + cross-references + OSCAL), a control-scoped list, and a
DLQ health bar — plus a watch mode that re-polls the active view on an
interval.

Three security-critical disciplines are baked into the structure rather than
left to reviewer vigilance:

- **Render safety.** Every producer-influenced string
  is routed through :func:`~cairn_cli.tui._render_safety.render_safe` via the
  single :meth:`ArtifactStoreApp._safe` helper before it reaches a widget.
  There is no display path that writes a raw record value to the terminal.
- **Surfaced verify command.** The detail pane offers a
  ``cairn verify`` string the operator runs MANUALLY. It is composed ONLY from
  format-validated identifiers (digest against ``^sha256:[0-9a-f]{64}$``, repo
  name against the canonical ``cairn_oci`` grammar) and rendered through
  ``render_safe``. The app NEVER executes it — there is no ``subprocess`` /
  ``os.system`` / shell-out anywhere in this module.
- **Read-only via thread workers.** All AWS reads run
  on Textual thread workers (``@work(thread=True)``). The fetch logic lives in
  plain synchronous ``_fetch_*`` methods that call ``self._sources....`` and
  return plain data; thin worker methods call those and push results to widgets
  via ``self.call_from_thread(...)``. This keeps the fetch logic unit-testable
  without fighting the event loop, and watch mode re-runs only those
  same allowlisted reads — no new call types, no EventBridge.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from botocore.exceptions import BotoCoreError, ClientError
from cairn_oci import RETAIN_UNTIL_ANNOTATION
from cairn_oci.categories import (
    ArtifactLocator,
    InvalidArtifactKindError,
    InvalidIdError,
    normalize_artifact_kind,
)
from cairn_signing import bundle_trust_from_projection
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)

from cairn_cli._pending import TAG_PENDING_LABEL, is_tag_pending
from cairn_cli._prune_eligibility import format_rfc3339_utc, parse_rfc3339_utc
from cairn_cli.tui._projects import project_id_from_repo_name
from cairn_cli.tui._render_safety import render_safe
from cairn_cli.tui._sources import alias_tag_of, classify_referrer, manifest_dict_from_response

if TYPE_CHECKING:
    from textual.timer import Timer

    from cairn_cli._aws import DeploymentTarget
    from cairn_cli.tui._sources import (
        ArtifactSources,
        BundleDetail,
        DlqDepth,
        Referrer,
        ReferrerListing,
    )

# --- format-validation for the surfaced verify command ---
#
# The verify string is interpolated ONLY from identifiers that pass these
# checks; anything failing degrades to a safe "cannot construct" message rather
# than a malformed/possibly-injected string.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# Short manifest-digest length shown in the list (after the "sha256:" prefix).
_SHORT_DIGEST_LEN = 12

_PLACEHOLDER = "—"

# Marker shown for an ECR-present-but-unindexed bundle (status == "unvalidated"):
# it was pushed but never completed validate→sign→archive→metadata, so it has no
# Metadata_Index row. Used only as an artifactKind fallback when the data layer
# could not attach a classification at all.
_UNVALIDATED_MARKER = "unvalidated"

# Classification label for an OCI image index (registry machinery — see
# ``_sources.classify_unindexed_artifact``). These are hidden from the
# all-bundles list by default behind the `i` toggle.
_INDEX_CLASSIFICATION = "index"

# Signing/archive column text for unindexed entries, derived from the data
# layer's classification. A pipeline byproduct — a Notation
# signature, a WORM_Receipt, an OSCAL-resource referrer, or an image index
# (labels from ``_sources.classify_unindexed_artifact``) — is never itself signed
# or archived as an artifact, so its signing/archive columns read "not applicable"
# (an expected state, not a missing step). Every other unindexed entry (a
# did-not-land ``unvalidated bundle``, an ``unclassified`` image, or a missing
# classification) has no Metadata_Index row to read a status from, so the columns
# read "unknown". An unrecognized future label fails safe to "unknown". The view
# never invents a determinate signed/unsigned for an unindexed entry — there is
# no metadata row behind it.
_PIPELINE_ARTIFACT_CLASSIFICATIONS = frozenset(
    {"signature", "worm-receipt", "resource-ref", "attestation", "index"}
)
_NOT_APPLICABLE = "n/a"
_UNKNOWN = "unknown"

# Classifications that identify an OCI referrer (a Notation signature, a
# WORM_Receipt, a resource-reference referrer, or the producer-claim
# Producer_Attestation_Referrer). An unindexed entry carrying one of these AND a
# ``subjectDigest`` is a referrer CHILD: in the all-bundles list it nests under
# the primary it ultimately refers to. The match is transitive (see
# ``_build_group_model``): the Notation signature OVER the Producer_Attestation_Referrer
# has the attestation's digest as its ``subjectDigest`` — itself a child, not a
# primary — so its parent is resolved by following the subject chain up to the
# content bundle rather than dropping it into the "unattached" group. An
# ``index`` is NOT a referrer (it carries no ``subject``) and stays a top-level row.
_REFERRER_CLASSIFICATIONS = frozenset({"signature", "worm-receipt", "resource-ref", "attestation"})

# Bound on the referrer subject-chain walk (``_build_group_model``). A real chain
# is at most depth 2 (content ← Producer_Attestation_Referrer ← its Notation signature);
# the bound is generous and exists only to fail closed on a producer-crafted
# subject-pointer cycle rather than loop forever.
_MAX_REFERRER_CHAIN = 8

# List-grouping row decorations. The prefix is purely visual; the underlying cell
# values still pass through ``_literal(_safe(...))`` so a referrer's
# producer-controlled fields cannot inject control bytes.
_EXPANDED_MARKER = "▼ "
_COLLAPSED_MARKER = "▶ "
_LEAF_MARKER = "  "
_CHILD_PREFIX = "  └─ "

# Label for referrer children whose ``subjectDigest`` matches no listed primary
# (or is absent): they are surfaced here rather than dropped (spec: "unattached"
# group). The label is a synthetic row that carries no underlying record.
_UNATTACHED_GROUP_LABEL = "── unattached referrers (subject not in this list) ──"

# Fallback message when the botocore error carries no resolvable error code
# (e.g. a transport-level ``BotoCoreError`` subclass).
_GENERIC_READ_ERROR_CODE = "ReadError"

# botocore error codes grouped by operator-actionable cause. The code token is a
# server-emitted enum, never an echo of operator/producer input.
_NOT_FOUND_CODES = frozenset({"ResourceNotFoundException"})
_ACCESS_CODES = frozenset({"AccessDeniedException", "AccessDenied", "UnrecognizedClientException"})
_THROTTLE_CODES = frozenset(
    {"ThrottlingException", "ProvisionedThroughputExceededException", "RequestLimitExceeded"}
)


def _error_code(exc: ClientError | BotoCoreError) -> str:
    """Extract the botocore error *code* token, or a generic fallback (PURE).

    boto3 always populates ``ClientError.response`` as a dict, but guard the
    access anyway: a malformed/mocked error with a non-dict ``response`` must
    NOT raise here — that would propagate out of the error handler and crash the
    very session this boundary exists to keep alive.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        # Narrow the inner value too: a degenerate/mocked exception whose
        # ``Error`` is a non-dict must NOT raise here (that would re-crash the
        # session this boundary protects).
        if isinstance(error, dict) and error.get("Code"):
            return error["Code"]
    return _GENERIC_READ_ERROR_CODE


def _read_target_phrase(resource: str | None, region: str | None) -> str:
    """A render-safe noun phrase naming WHAT was read and WHERE (PURE).

    ``resource`` (the DynamoDB table name) and ``region`` come from the
    operator's CLI args / AWS config, never from stored producer data — but the
    caller still routes the whole message through ``self._safe`` for uniform
    render-safety.
    """
    if resource and region:
        return f"table '{resource}' in {region}"
    if resource:
        return f"table '{resource}'"
    if region:
        return f"the metadata table in {region}"
    return "the metadata table"


def format_read_error(
    exc: ClientError | BotoCoreError,
    *,
    resource: str | None = None,
    region: str | None = None,
) -> str:
    """Map a botocore read error to a one-line, operator-actionable message (PURE).

    The message is built ONLY from the fixed botocore error *code* token plus
    the operator-supplied ``resource``/``region`` — it NEVER interpolates the
    raw boto3 message string or any stored producer data, so the render-safety
    invariant cannot be defeated through a crafted error payload.
    The wording is tailored to the code so the operator gets the right next
    step: a not-found names the table and points at account/region/stage; an
    access denial says so; a throttle says retry. The caller still routes the
    result through ``self._safe`` for defense in depth.
    """
    code = _error_code(exc)
    target = _read_target_phrase(resource, region)
    if code in _NOT_FOUND_CODES:
        return (
            f"read failed: {code} — {target} not found; "
            "check the AWS account/region and --stage/--region-code"
        )
    if code in _ACCESS_CODES:
        return f"read failed: {code} — you lack read access to {target}"
    if code in _THROTTLE_CODES:
        return f"read failed: {code} — {target} is throttling reads; retry shortly"
    return f"read failed: {code} — could not read {target}"


def compose_verify_command(
    record: dict[str, Any],
    *,
    stage: str,
    region_code: str,
) -> str | None:
    """Compose the ``cairn verify`` string from FORMAT-VALIDATED identifiers only.

    Returns the command string, or ``None`` when an identifier fails its grammar
    check (the caller renders a safe "cannot construct" message). The reference
    is emitted in the SHORT form ``cairn verify`` actually accepts
    (``<project-id>/<artifact-kind>@<digest>`` — see
    ``cairn_cli._refs.resolve_reference``). The full ECR repo path is NOT a valid
    reference (a path with no registry host is rejected), so we derive the
    project-id and emit it with the artifact-kind:

        cairn verify {projectId}/{artifactKind}@{manifestDigest}
            --stage {stage} --region-code {region}

    The collapsed repo name does not carry the category, so the
    project-id comes from the repo name and the artifact-kind comes from the
    record's ``artifactKind`` (validated against the controlled vocabulary).
    ``stage`` / ``region_code`` come from the deployment target the operator
    launched against (not from the untrusted record), so they need no validation
    here. The caller MUST still route the returned string through
    :func:`render_safe` before display (defense in depth — validated input
    cannot carry control bytes, but the single render boundary is uniform).
    """
    repository_name = record.get("repositoryName")
    manifest_digest = record.get("manifestDigest")
    artifact_kind = record.get("artifactKind")
    if not isinstance(repository_name, str) or not isinstance(manifest_digest, str):
        return None
    if not _DIGEST_RE.match(manifest_digest):
        return None
    # Validate the artifact-kind via the single resolver so a stored alias (not
    # just a canonical) is accepted; the verify string keeps the supplied spelling
    # verbatim (consistent with the tag/path producer-surface preservation). An
    # unresolvable kind degrades to a safe "cannot construct" message.
    if not isinstance(artifact_kind, str):
        return None
    try:
        normalize_artifact_kind(artifact_kind)
    except InvalidArtifactKindError:
        return None
    project_id = project_id_from_repo_name(
        repository_name, deployment_qualifier=f"{region_code}-{stage}"
    )
    if project_id is None:
        return None
    return (
        f"cairn verify {project_id}/{artifact_kind}@{manifest_digest} "
        f"--stage {stage} --region-code {region_code}"
    )


#: Opaque id grammar (``cairn_oci.categories`` ``_ID_RE``) for the org / tenant /
#: project segments the surfaced ``cairn prune`` command interpolates.
_OPAQUE_ID_RE = re.compile(r"\A[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?\Z")

RETENTION_EXPIRED = "expired"
RETENTION_IN_FORCE = "in force"
RETENTION_UNKNOWN = "unknown"


def retention_status(listing: ReferrerListing | None, now: datetime) -> tuple[datetime | None, str]:
    """Retain_Until and its status from the WORM_Receipt referrer annotation ([TUI-016]).

    Reads ONLY the ``dev.cairnproofs.artifact.retain-until`` annotation carried
    on the ``ListImageReferrers`` result the detail view already fetched — no
    extra AWS call. The annotation is producer-writable, so this is a display
    value, never an eligibility decision ([RETAIN-012]). Returns
    ``(None, "unknown")`` when the read failed, there is not exactly one
    WORM_Receipt referrer, or the value is absent or not strict RFC 3339 UTC.
    """
    if listing is None or not listing.available:
        return None, RETENTION_UNKNOWN
    receipts = [
        r for r in listing.referrers if classify_referrer(r.artifact_type) == "worm-receipt"
    ]
    if len(receipts) != 1:
        return None, RETENTION_UNKNOWN
    retain_until = parse_rfc3339_utc(receipts[0].annotations.get(RETAIN_UNTIL_ANNOTATION))
    if retain_until is None:
        return None, RETENTION_UNKNOWN
    return retain_until, RETENTION_EXPIRED if now >= retain_until else RETENTION_IN_FORCE


def compose_prune_report_command(
    record: dict[str, Any], *, stage: str, region_code: str
) -> str | None:
    """The ``cairn prune`` REPORT command for the bundle's project ([TUI-016.03]).

    Report mode only (no ``--digest``): the TUI never offers or composes a
    delete. The org / tenant / project come from the record's repository path
    and must each match the opaque-id grammar, else ``None``.
    """
    repository_name = record.get("repositoryName")
    if not isinstance(repository_name, str):
        return None
    segments = repository_name.split("/")
    if len(segments) < 4:
        return None
    org_id, tenant_id, project_id = segments[-3:]
    if not all(_OPAQUE_ID_RE.match(x) for x in (org_id, tenant_id, project_id)):
        return None
    return (
        f"cairn prune --target {org_id}/{tenant_id}/{project_id} "
        f"--stage {stage} --region-code {region_code}"
    )


def compute_lifecycle(record: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Derive the (stage, state, detail) lifecycle timeline from the record.

    Returns one row per pipeline stage in order. ``state`` is one of
    ``done`` / ``pending`` / ``failed`` / ``unknown``; ``detail`` is the
    associated timestamp or status text (UNSAFE — caller renders via
    ``render_safe``). Pure function: no AWS, no widgets.
    """
    validated_at = record.get("validatedAt")
    signing_status = record.get("signingStatus")
    signed_at = record.get("signedAt")
    archive_status = record.get("archiveStatus")
    archived_at = record.get("archivedAt")
    created_at = record.get("createdAt")

    rows: list[tuple[str, str, str]] = []

    # validate
    if validated_at:
        rows.append(("validate", "done", str(validated_at)))
    else:
        rows.append(("validate", "unknown", ""))

    # sign
    if signing_status == "signed":
        rows.append(("sign", "done", str(signed_at) if signed_at else ""))
    elif signing_status == "unsigned":
        rows.append(("sign", "failed", "unsigned"))
    elif signing_status == "pending":
        rows.append(("sign", "pending", "pending"))
    else:
        rows.append(("sign", "unknown", str(signing_status) if signing_status else ""))

    # archive
    if archive_status == "archived":
        rows.append(("archive", "done", str(archived_at) if archived_at else ""))
    elif archive_status == "failed":
        rows.append(("archive", "failed", "failed"))
    elif archive_status == "pending":
        rows.append(("archive", "pending", "pending"))
    else:
        rows.append(("archive", "unknown", str(archive_status) if archive_status else ""))

    # metadata — the row's very existence means the metadata stage ran.
    rows.append(("metadata", "done", str(created_at) if created_at else "present"))

    return rows


def projection_trust_label(record: dict[str, Any]) -> str:
    """Two-signature trust label for a record, read from the projection (PURE).

    A bundle is fully trusted only when BOTH the content signature and the
    attestation signature are present and valid. The Metadata_Index carries only
    the coarse projected ``signingStatus``,
    so this maps that projection onto a ``fully-trusted`` / ``partial`` /
    ``unsigned`` label. An unindexed entry (``status ==
    "unvalidated"``) has no Metadata_Index row to read a determinate status
    from, so its trust is ``unknown`` — the view never invents full trust for an
    entry with no projection behind it. The authoritative two-signature verdict
    is a live re-verification via ``cairn verify``; this projection label is a
    non-authoritative index read. Value is fixed vocabulary (no producer bytes)
    but still routed through the render-safety choke point by the caller.
    """
    if record.get("status") == _UNVALIDATED_MARKER:
        return _UNKNOWN
    return bundle_trust_from_projection(record.get("signingStatus")).value


class ArtifactStoreApp(App[None]):
    """Read-only Textual app over an injected :class:`ArtifactSources`.

    Dependency injection (the ``sources`` argument) lets tests pass a stub data
    layer and drive the fetch/compose methods without touching AWS. ``watch``
    mode polls the active view + DLQ health on ``watch_interval`` seconds,
    clamped to at least ``min_watch_interval`` so a punishing sub-second hammer
    is impossible.
    """

    CSS = """
    #dlq-bar { height: 3; border: round $primary; padding: 0 1; }
    #verify-cmd { color: $text-muted; }
    #limitation { color: $warning; }
    #project-caption { color: $text-muted; }
    #control-caption { color: $text-muted; }
    #referrers-caption { color: $text-muted; }
    #referrers-status { color: $text-muted; }
    #index-note { color: $text-muted; }
    DataTable { height: 1fr; }
    """

    # Navigation model (design review): drill DOWN with Enter on any table row,
    # drill UP with Esc, jump directly with the number keys, and drill ACROSS to
    # the selected bundle's project with `p`. Every action reuses the existing
    # read-only workers — no new AWS call types, so the read-only allowlist
    # is unchanged.
    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("escape", "back", "Back"),
        ("1", "show_bundles", "Bundles"),
        ("2", "show_detail", "Detail"),
        ("3", "focus_control", "By control"),
        ("4", "focus_project", "By project"),
        ("p", "project_drill", "Sel. project"),
        ("space", "toggle_group", "Expand/collapse"),
        ("m", "raw_manifest", "Raw manifest"),
        ("i", "toggle_indexes", "Show/hide indexes"),
        ("w", "toggle_watch", "Watch"),
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        sources: ArtifactSources,
        *,
        watch_interval: float = 3.0,
        min_watch_interval: float = 1.0,
        deployment_target: DeploymentTarget | None = None,
        region: str | None = None,
        title: str = "Cairn Proofs (read-only)",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__()
        self._sources = sources
        # Wall clock for the retention status (expired / in force); injectable
        # so tests pin "now".
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._min_watch_interval = max(0.1, float(min_watch_interval))
        self._watch_interval = max(float(watch_interval), self._min_watch_interval)
        self._deployment_target = deployment_target
        # AWS region the read clients target — surfaced in read-error messages so
        # a `ResourceNotFoundException` names where the table was looked for.
        self._region = region
        self._app_title = title
        # The records backing each bundle-bearing table, keyed by the DataTable
        # row key (the row index), so selecting a row in ANY of the three tables
        # can recover the underlying record and open its detail.
        self._records: list[dict[str, Any]] = []
        self._control_records: list[dict[str, Any]] = []
        self._project_records: list[dict[str, Any]] = []
        self._selected: dict[str, Any] | None = None
        # The tab a detail view was opened FROM, so Esc returns there (drill-up).
        self._detail_origin_tab: str = "tab-list"
        # Read-time Identity_Registry join ([IDREG-001], [TUI-002.04]): opaque
        # {org-id}/{tenant-id}/{project-id} -> human display name, resolved by a
        # refresh worker before it repopulates a table. A missing id falls back
        # to the opaque id itself; the registry read degrading to {} leaves every
        # cell showing its opaque id (never a crash). Both the resolved name and
        # the opaque id are rendered render-safe ([TUI-003]).
        self._identity_names: dict[str, str] = {}
        # The selected bundle's lazily-fetched OCI referrer listing (None until a
        # bundle's detail worker has fetched it). Backs the referrer table rows.
        self._referrers: ReferrerListing | None = None
        # Referrer drill-down SUB-STATE within the detail tab: when an operator
        # selects a referrer row, this holds it so the detail body re-renders to
        # the referrer's identity + subject bundle. Esc from this focused state
        # returns to the BUNDLE detail (clears this) BEFORE leaving the tab — so
        # the detail flow has two Esc levels (referrer → bundle → origin tab).
        self._referrer_focus: Referrer | None = None
        # All-bundles list grouping. ``_records`` is the flat record
        # list the data layer returns; ``_bundle_row_records`` is the per-row
        # record list the grouped DataTable is built from (None for the synthetic
        # "unattached" header row, which backs no record). Expansion state is
        # keyed by primary ``manifestDigest`` and PERSISTS across a watch-tick
        # rebuild so a refresh never collapses what the operator expanded.
        self._bundle_row_records: list[dict[str, Any] | None] = []
        self._expanded_primaries: set[str] = set()
        # Manifest digests of primaries that actually have referrer children,
        # captured on each table build so the Space toggle knows whether a primary
        # row is expandable without re-deriving the whole group model.
        self._primaries_with_children: set[str] = set()
        # Raw-manifest drill-down SUB-STATE within the detail tab. When set, the
        # detail body shows the pretty-printed raw OCI manifest of either the
        # focused referrer (when ``_referrer_focus`` is set) or the selected
        # bundle. Esc from this state returns to whichever of bundle/referrer was
        # in focus BEFORE leaving the tab — so the detail flow now has three Esc
        # levels (raw-manifest → referrer-focus|bundle → origin tab).
        self._showing_raw_manifest = False
        # The selected bundle's raw OCI manifest, captured from the detail
        # worker's ``BatchGetImage`` result on selection so the raw-manifest
        # drill-down reuses it with NO additional AWS call. None when
        # the manifest was unavailable.
        self._bundle_manifest: dict[str, Any] | None = None
        # The focused referrer's raw OCI manifest, fetched lazily on the
        # raw-manifest drill-in. None until drilled into / when
        # unavailable.
        self._focused_referrer_manifest: dict[str, Any] | None = None
        self._watch_timer: Timer | None = None
        self._watching = False
        # All-bundles list filter: OCI image-index entries (the `index`
        # classification — registry-machinery manifests the pipeline does NOT mint
        # itself; suppressing them is tracked in the backlog) are HIDDEN by
        # default to keep the operator's artifact view uncluttered. The
        # `i` hotkey toggles them on; ``_records`` always holds the unfiltered set,
        # so toggling re-renders without a re-fetch and the choice persists across
        # watch-tick rebuilds.
        self._show_indexes = False

    # --- render-safety choke point -------------------------------

    def _safe(self, value: object) -> str:
        """THE single render boundary: every producer string passes through here.

        Call this at EVERY render site — list cells, detail fields, control
        rows, OSCAL/annotation values, and the verify string — so there is no
        path that writes a raw record value to the terminal. Strips C0/C1
        control bytes and ANSI/OSC/DCS escape sequences.
        """
        return render_safe(value)

    @staticmethod
    def _literal(text: str) -> Text:
        """Wrap a render-safe string as a literal Rich ``Text`` (no markup parse).

        Textual widgets (``Static`` / ``DataTable``) interpret a bare ``str`` as
        Rich/Textual console markup, so a producer value containing ``[...]``
        could be parsed as a style/link tag and corrupt the render even after
        control-byte stripping. ``Text(str)`` renders the characters verbatim,
        closing that markup-injection gap. Pair every ``self._safe(...)`` that
        reaches a widget with this wrapper.
        """
        return Text(text)

    # --- Identity_Registry display-name join ([IDREG-001], [TUI-002.04]) ------

    def _resolve_identity_names(self, records: list[dict[str, Any]]) -> dict[str, str]:
        """Resolve the opaque ids in ``records`` to display names (read-time join).

        Collects the distinct opaque ``orgId``/``tenantId``/``projectId`` across
        the records and resolves them in one batched Identity_Registry read.
        Runs on a refresh worker thread (never the render thread), and the data
        layer degrades a failed registry read to ``{}`` — so a missing registry
        only loses display names, never blocks the list ([IDREG-001.03]).
        """
        ids: set[str] = set()
        for record in records:
            for key in ("orgId", "tenantId", "projectId"):
                value = record.get(key)
                if isinstance(value, str) and value:
                    ids.add(value)
        if not ids:
            return {}
        return self._sources.resolve_identity_names(ids)

    def _display_identity(self, opaque_id: object) -> str:
        """Render-safe display string for an opaque org/tenant/project id.

        Resolves the opaque id to its Identity_Registry display name when known,
        formatting it as ``{display name} ({opaque-id})`` so the human label is
        visible WITHOUT losing the opaque id (the queryable key). Falls back to
        the bare opaque id when no name is resolved. The display name is
        producer/operator-influenced control-plane content, so the CALLER still
        routes the result through :meth:`_safe` before any terminal write
        ([TUI-002.04], [TUI-003]).
        """
        if not isinstance(opaque_id, str) or not opaque_id:
            return _PLACEHOLDER
        name = self._identity_names.get(opaque_id)
        if name:
            return f"{name} ({opaque_id})"
        return opaque_id

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="dlq-bar")
        with TabbedContent(initial="tab-list"):
            with TabPane("Bundles (ECR-derived)", id="tab-list"):
                yield DataTable(id="bundle-table", cursor_type="row")
            with TabPane("Detail", id="tab-detail"):
                with VerticalScroll(id="detail-pane", can_focus=True):
                    yield Static(
                        self._literal(
                            self._safe(
                                "Select a bundle (Enter) in any list to view its detail. "
                                "Esc returns to that list."
                            )
                        ),
                        id="detail-body",
                    )
                    yield Label("", id="verify-cmd")
                    yield Static(self._literal(self._safe("")), id="retention-body")
                    yield Static(
                        self._literal(self._safe("== Referrers (OCI) ==")),
                        id="referrers-caption",
                    )
                    # Render-safe summary of the referrer-read outcome: it
                    # DISTINGUISHES "referrers unavailable" (read failed) from
                    # "no referrers" (read succeeded, zero referrers) — see
                    # render_referrer_lines.
                    yield Static(self._literal(self._safe("")), id="referrers-status")
                    # Selectable referrer list. Selection routes through the
                    # existing on_data_table_row_selected handler (distinct id);
                    # every cell is render-safe via _literal(_safe(...)).
                    yield DataTable(id="referrer-table", cursor_type="row")
            with TabPane("Control", id="tab-control"):
                with Vertical():
                    yield Static(
                        self._literal(
                            self._safe(
                                "Lists bundles citing a control. The ControlClaims projection "
                                "is a tenant-PK plane keyed by the opaque {tenant-id} partition "
                                "and a {catalogOrProfileUuid}#{controlId}#{manifestDigest} sort "
                                "condition, so ALL THREE fields are required: enter the tenant "
                                "id, the catalog/profile UUID, AND the control id, then press "
                                "Enter."
                            )
                        ),
                        id="control-caption",
                    )
                    yield Input(placeholder="tenant id (e.g. tnt-a)", id="ctl-tenant-id")
                    yield Input(placeholder="catalog-or-profile UUID", id="ctl-uuid")
                    yield Input(placeholder="control id (e.g. ac-2)", id="ctl-id")
                    yield Label("", id="ctl-status")
                    yield DataTable(id="control-table", cursor_type="row")
            with TabPane("By Project", id="tab-project"):
                with Vertical():
                    yield Static(
                        self._literal(
                            self._safe(
                                "Direct Metadata_Index query by tenant + project id "
                                "(tenant-PK key-condition, NOT ECR-gated). The index is keyed "
                                "by the opaque {tenant-id} partition and a "
                                "begins_with({project-id}#) sort condition, so BOTH the tenant "
                                "id AND the project id are required; enter both, then press "
                                "Enter. Lists the (tenant, project)'s persisted records even "
                                "when the ECR-derived bundle list would omit it - so a zero-row "
                                "result here means no Metadata_Index records, NOT that an id "
                                "was wrong."
                            )
                        ),
                        id="project-caption",
                    )
                    yield Input(placeholder="tenant id (e.g. tnt-a)", id="tenant-id")
                    yield Input(placeholder="project id (e.g. proj-a)", id="proj-id")
                    yield Label("", id="proj-status")
                    yield DataTable(id="project-table", cursor_type="row")
        # Persistent indication of the index-hide filter (no silent caps): shows
        # how many image-index entries are hidden and how to reveal them. Empty
        # when nothing is hidden. Populated by _populate_bundle_table.
        yield Static(self._literal(self._safe("")), id="index-note")
        yield Static(
            self._literal(
                self._safe(
                    "Limitation (T7): the bundle list shows only bundles with a CURRENT "
                    "ECR copy. A bundle whose ECR image was lifecycle-expired may not appear "
                    "even though its Metadata_Index record persists - absence here is NOT "
                    "proof a bundle never existed."
                )
            ),
            id="limitation",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.title = self._safe(self._app_title)
        table = self.query_one("#bundle-table", DataTable)
        table.add_columns(
            "projectId", "artifactKind", "signing", "trust", "archive", "digest", "createdAt"
        )
        control_table = self.query_one("#control-table", DataTable)
        control_table.add_columns("projectId", "artifactKind", "digest", "createdAt")
        project_table = self.query_one("#project-table", DataTable)
        project_table.add_columns(
            "projectId", "artifactKind", "signing", "trust", "archive", "digest", "createdAt"
        )
        referrer_table = self.query_one("#referrer-table", DataTable)
        referrer_table.add_columns("type", "artifactType", "digest", "tag")
        # Focus the bundle list immediately so arrow-keys + Enter (drill-down)
        # work without first clicking the table.
        table.focus()
        self._refresh_all_workers()

    # --- list view (plain fetch + thin worker) -------------------------------

    def _fetch_bundles(self) -> list[dict[str, Any]]:
        """Synchronous, unit-testable: list all bundles via the data layer."""
        return self._sources.list_all_bundles()

    @work(thread=True, group="bundles", exclusive=True)
    def _refresh_bundles_worker(self) -> None:
        records = self._fetch_bundles()
        self._identity_names = self._resolve_identity_names(records)
        self.call_from_thread(self._populate_bundle_table, records)
        self._notify_refresh_complete()

    @staticmethod
    def _is_referrer_child(record: dict[str, Any]) -> bool:
        """True iff a record is a pipeline-minted referrer eligible to nest.

        A referrer child is an unindexed entry (``status == "unvalidated"``)
        whose ``classification`` names a minted OCI referrer (signature /
        worm-receipt / resource-ref) AND that carries a well-formed
        ``subjectDigest`` (the content-addressed pointer to the primary it refers
        to). Anything else — a validated bundle, an ``index``, an unvalidated
        producer bundle, an unclassified entry, or a referrer whose subject is
        missing — stays a top-level row. The grouping key is exact-match digest
        equality, NOT a verified relationship: nesting declares, it
        does not validate.
        """
        if record.get("status") != "unvalidated":
            return False
        if record.get("classification") not in _REFERRER_CLASSIFICATIONS:
            return False
        subject = record.get("subjectDigest")
        return isinstance(subject, str) and bool(subject)

    def _build_group_model(
        self, records: list[dict[str, Any]]
    ) -> tuple[
        list[tuple[dict[str, Any], list[dict[str, Any]]]],
        list[dict[str, Any]],
    ]:
        """Partition the flat record list into (primaries-with-children, orphans).

        Returns ``(groups, unattached)`` where ``groups`` preserves the input
        order of the primaries (every record that is not a referrer child) and
        pairs each with its referrer children (a child nests under the primary
        whose ``manifestDigest`` equals the child's ``subjectDigest``). A child
        whose subject matches no listed primary lands in ``unattached`` rather
        than being dropped (spec: "unattached" group). Pure: no widgets, no AWS.
        """
        primaries: list[dict[str, Any]] = []
        children: list[dict[str, Any]] = []
        for record in records:
            (children if self._is_referrer_child(record) else primaries).append(record)

        # Index primaries by their content-addressed manifest digest. A digest
        # collision (two listed primaries sharing a digest) is degenerate; the
        # first wins — content-addressing makes this effectively impossible.
        by_digest: dict[str, dict[str, Any]] = {}
        children_of: dict[int, list[dict[str, Any]]] = {}
        for primary in primaries:
            digest = primary.get("manifestDigest")
            if isinstance(digest, str) and digest and digest not in by_digest:
                by_digest[digest] = primary
            children_of[id(primary)] = []

        # Index EVERY record by digest so a referrer-of-a-referrer can resolve
        # transitively up to the top primary. The Notation signature OVER the
        # Producer_Attestation_Referrer points its ``subject`` at the attestation's digest
        # — itself a child, not a primary — so a single by-primary lookup would
        # strand it as "unattached". Walking the subject chain attaches it under
        # the content bundle it ultimately belongs to.
        record_by_digest: dict[str, dict[str, Any]] = {}
        for record in records:
            digest = record.get("manifestDigest")
            if isinstance(digest, str) and digest and digest not in record_by_digest:
                record_by_digest[digest] = record

        def _resolve_primary(child: dict[str, Any]) -> dict[str, Any] | None:
            """Follow ``subjectDigest`` up to the owning primary, or None.

            Bounded walk (guards against a producer-crafted subject-pointer
            cycle): land on a primary → that's the owner; hop through an
            intermediate child referrer (e.g. the Producer_Attestation_Referrer) and keep
            walking; a missing/cyclic/dangling pointer yields None (unattached).
            """
            seen: set[str] = set()
            cursor: dict[str, Any] | None = child
            for _ in range(_MAX_REFERRER_CHAIN):
                if cursor is None:
                    return None
                subject = cursor.get("subjectDigest")
                if not isinstance(subject, str) or not subject or subject in seen:
                    return None
                seen.add(subject)
                primary = by_digest.get(subject)
                if primary is not None:
                    return primary
                cursor = record_by_digest.get(subject)
            return None

        unattached: list[dict[str, Any]] = []
        for child in children:
            parent = _resolve_primary(child)
            if parent is None:
                unattached.append(child)
            else:
                children_of[id(parent)].append(child)

        groups = [(primary, children_of[id(primary)]) for primary in primaries]
        return groups, unattached

    @staticmethod
    def _classified_status_columns(record: dict[str, Any]) -> tuple[Any, Any, Any]:
        """The (artifactKind, signing, archive) column values for one record (PURE).

        A validated bundle (no ``status``) keeps its stored ``artifactKind`` and
        pipeline status columns. An unindexed entry (``status == "unvalidated"``)
        surfaces its ``classification`` label (what the artifact IS), falling
        back to the unvalidated marker if absent; its signing/archive columns are
        derived from that classification: ``n/a`` for a
        pipeline-internal byproduct that is never signed/archived as an artifact,
        else ``unknown`` (no Metadata_Index row to read a determinate status
        from). Values are raw (UNSAFE) — the caller routes each through
        ``self._safe``.
        """
        if record.get("status") != "unvalidated":
            return (
                record.get("artifactKind"),
                record.get("signingStatus"),
                record.get("archiveStatus"),
            )
        classification = record.get("classification")
        artifact_kind = classification or _UNVALIDATED_MARKER
        if classification in _PIPELINE_ARTIFACT_CLASSIFICATIONS:
            return artifact_kind, _NOT_APPLICABLE, _NOT_APPLICABLE
        return artifact_kind, _UNKNOWN, _UNKNOWN

    def _update_index_note(self, hidden_count: int) -> None:
        """Set the persistent index-hide indication (render-SAFE; fixed text).

        Shows how many image-index entries are hidden and how to reveal them, or
        clears the line when none are hidden (indexes shown, or none present).
        Fixed text + an integer — nothing producer-controlled — but still routed
        through the render-safety choke point for uniformity.
        """
        if hidden_count:
            noun = "entry" if hidden_count == 1 else "entries"
            text = f"{hidden_count} image-index {noun} hidden — press i to show"
        else:
            text = ""
        self.query_one("#index-note", Static).update(self._literal(self._safe(text)))

    def _populate_bundle_table(self, records: list[dict[str, Any]]) -> None:
        """Render the GROUPED bundle list. EVERY cell routes through ``self._safe``.

        Pipeline-minted referrers (signature / WORM_Receipt / OSCAL-resource)
        nest as collapsible CHILDREN under the primary they refer to — matched on
        ``manifestDigest == subjectDigest``. A primary with no
        children renders as a leaf with no expand affordance; a referrer whose
        subject matches no listed primary surfaces in an "unattached" group
        rather than being dropped. Expansion state persists across a rebuild
        (keyed by the primary's manifest digest), so a watch-tick refresh does
        not collapse what the operator expanded.

        An unindexed entry shows the data layer's ``classification`` label in its
        ``artifactKind`` column and derives signing/archive from it.
        The expand/child decoration is a visual prefix only — every cell still
        passes through ``self._literal(self._safe(...))``.

        OCI image-index entries (registry machinery the pipeline does not mint)
        are HIDDEN unless ``_show_indexes`` is set (the `i` toggle). ``_records``
        keeps the UNFILTERED set so toggling re-renders without a re-fetch; the
        hidden count is surfaced in the ``#index-note`` line (no silent caps).
        """
        self._records = records
        hidden_indexes = sum(1 for r in records if r.get("classification") == _INDEX_CLASSIFICATION)
        if self._show_indexes:
            visible = records
            hidden_indexes = 0
        else:
            visible = [r for r in records if r.get("classification") != _INDEX_CLASSIFICATION]
        self._update_index_note(hidden_indexes)
        groups, unattached = self._build_group_model(visible)
        # Drop expansion state for primaries no longer present so the set cannot
        # grow unbounded across watch ticks; surviving digests keep their state.
        live_digests = {
            primary.get("manifestDigest")
            for primary, _children in groups
            if isinstance(primary.get("manifestDigest"), str)
        }
        self._expanded_primaries &= live_digests  # type: ignore[arg-type]

        table = self.query_one("#bundle-table", DataTable)
        table.clear()
        self._bundle_row_records = []
        self._primaries_with_children = set()
        for primary, child_records in groups:
            digest = primary.get("manifestDigest")
            has_children = bool(child_records)
            if has_children and isinstance(digest, str):
                self._primaries_with_children.add(digest)
            expanded = has_children and digest in self._expanded_primaries
            if not has_children:
                marker = _LEAF_MARKER
            elif expanded:
                marker = _EXPANDED_MARKER
            else:
                marker = _COLLAPSED_MARKER
            self._add_bundle_row(table, primary, prefix=marker)
            if expanded:
                for child in child_records:
                    self._add_bundle_row(table, child, prefix=_CHILD_PREFIX)

        if unattached:
            # Synthetic, non-selectable header row (backs no record), then each
            # orphaned referrer as a child-styled row that still resolves to its
            # underlying record on selection.
            self._add_header_row(table, _UNATTACHED_GROUP_LABEL)
            for child in unattached:
                self._add_bundle_row(table, child, prefix=_CHILD_PREFIX)

    def _add_bundle_row(self, table: DataTable, record: dict[str, Any], *, prefix: str) -> None:
        """Append one render-safe bundle row, recording its backing record.

        The ``prefix`` (expand marker or child indent) is decoration prepended to
        the render-safe projectId cell; the underlying value still passes through
        ``self._literal(self._safe(...))`` so producer bytes stay neutralized.
        """
        artifact_kind, signing, archive = self._classified_status_columns(record)
        index = len(self._bundle_row_records)
        self._bundle_row_records.append(record)
        project_cell = Text(prefix) + self._literal(
            self._safe(self._display_identity(record.get("projectId")))
        )
        table.add_row(
            project_cell,
            self._literal(self._safe(artifact_kind or _PLACEHOLDER)),
            self._literal(self._safe(signing or _PLACEHOLDER)),
            self._literal(self._safe(projection_trust_label(record))),
            self._literal(self._safe(archive or _PLACEHOLDER)),
            self._literal(self._safe(self._short_digest(record.get("manifestDigest")))),
            self._literal(self._safe(record.get("createdAt") or _PLACEHOLDER)),
            key=str(index),
        )

    def _add_header_row(self, table: DataTable, label: str) -> None:
        """Append a synthetic, non-selectable group header (backs no record).

        Its row index maps to ``None`` in ``_bundle_row_records`` so selecting it
        is a no-op. The label still routes through the render-safety step.
        """
        index = len(self._bundle_row_records)
        self._bundle_row_records.append(None)
        table.add_row(
            self._literal(self._safe(label)),
            self._literal(self._safe("")),
            self._literal(self._safe("")),
            self._literal(self._safe("")),
            self._literal(self._safe("")),
            self._literal(self._safe("")),
            self._literal(self._safe(_PLACEHOLDER)),
            key=str(index),
        )

    @staticmethod
    def _short_digest(digest: object) -> str:
        if not isinstance(digest, str) or not digest:
            return _PLACEHOLDER
        body = digest.split(":", 1)[1] if ":" in digest else digest
        return body[:_SHORT_DIGEST_LEN]

    # The flat search-result tables back their rows with one of these record
    # lists, keyed by the integer row key. Selecting a row in either drills into
    # the same Detail view — so a bundle found by control- or project-search is
    # no longer a dead end. The all-bundles ``bundle-table`` is GROUPED and
    # resolves its rows through ``_bundle_row_records`` instead (see
    # ``on_data_table_row_selected``), so it is intentionally absent here.
    _RECORDS_BY_TABLE: ClassVar[dict[str, str]] = {
        "control-table": "_control_records",
        "project-table": "_project_records",
    }

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        table_id = event.data_table.id or ""
        try:
            index = int(event.row_key.value) if event.row_key.value is not None else -1
        except (TypeError, ValueError):
            return
        # Referrer rows are NOT bundle records — selecting one drills INTO the
        # referrer sub-view of the current bundle's detail (no new AWS call).
        if table_id == "referrer-table":
            self._open_referrer(index)
            return
        # The bundle table is GROUPED: its row index keys ``_bundle_row_records``
        # (one entry per visible row — a primary, a child, or ``None`` for a
        # synthetic group header), NOT the flat ``_records`` list. A child/primary
        # row resolves to its referrer/bundle record; a header row backs no record
        # and is a no-op.
        if table_id == "bundle-table":
            if 0 <= index < len(self._bundle_row_records):
                record = self._bundle_row_records[index]
                if record is not None:
                    self._open_detail(record, origin=table_id)
            return
        records_attr = self._RECORDS_BY_TABLE.get(table_id)
        if records_attr is None:
            return
        records: list[dict[str, Any]] = getattr(self, records_attr)
        if 0 <= index < len(records):
            self._open_detail(records[index], origin=table_id)

    def _open_detail(self, record: dict[str, Any], *, origin: str | None) -> None:
        """Drill into a bundle's Detail view from any list (single keystroke).

        Remembers the originating tab so Esc returns there (drill-up), fetches
        the detail on the lazy per-bundle worker, and activates the Detail tab so
        the operator sees the result without a second navigation step.
        """
        self._selected = record
        # A fresh bundle selection clears any prior referrer drill-down, raw-
        # manifest drill-down, and listing; the detail worker re-fetches the new
        # bundle's referrers and manifest.
        self._referrer_focus = None
        self._referrers = None
        self._showing_raw_manifest = False
        self._bundle_manifest = None
        self._focused_referrer_manifest = None
        # Map the originating table id to its owning tab id so Esc returns there.
        self._detail_origin_tab = {
            "bundle-table": "tab-list",
            "control-table": "tab-control",
            "project-table": "tab-project",
        }.get(origin or "", "tab-list")
        self._open_detail_worker(record)
        self.query_one(TabbedContent).active = "tab-detail"
        # Move keyboard focus onto the Detail pane once the tab activation has
        # rendered. Without this, focus stays on the origin list's widget (e.g.
        # ``#project-table``), now inside a hidden tab; Textual reconciles by
        # making that focused widget visible again — reverting the active tab and
        # producing the "detail flashes then returns to the list" bug. Focusing
        # the pane synchronously here would no-op (it is not displayed yet), so we
        # defer to ``call_after_refresh``: by then the pane is visible, the focus
        # sticks, and the activation holds. _open_detail is only reached on an
        # explicit drill-down, so anchoring focus on Detail is always the intent.
        self.call_after_refresh(self._focus_detail_pane)

    def _focus_detail_pane(self) -> None:
        """Anchor focus on the Detail tab (deferred so it runs once it is visible).

        Runs one refresh after :meth:`_open_detail` switched to the Detail tab —
        only ever reached on an explicit drill-down. It both PREVENTS the flip
        (moving focus off the origin list's now-hidden widget, so Textual has no
        hidden-but-focused widget to make visible again) and CORRECTS it
        (re-asserting the active tab if Textual already reverted it during
        reconciliation). The re-assert window is a single refresh — far shorter
        than any manual navigate-away — so it cannot fight a real operator action.
        Degrades to a no-op rather than raising if the widgets are momentarily
        absent.
        """
        try:
            tabs = self.query_one(TabbedContent)
            if tabs.active != "tab-detail":
                tabs.active = "tab-detail"
            self.query_one("#detail-pane", VerticalScroll).focus()
        except Exception:
            return

    # --- detail view (lazy ECR/S3 fetch on selection) ------------------------

    def _fetch_detail(self, record: dict[str, Any]) -> BundleDetail:
        """Synchronous, unit-testable: LAZY detail fetch for one selected bundle."""
        return self._sources.get_bundle_detail(record)

    def _fetch_referrers(self, record: dict[str, Any]) -> ReferrerListing:
        """Synchronous, unit-testable: LAZY referrer fetch for one selected bundle.

        Rides ONLY ``ArtifactSources.get_bundle_referrers`` (the sole
        ``ecr:ListImageReferrers`` path); it never raises, returning a
        ``ReferrerListing`` whose ``available`` flag distinguishes the
        unavailable/empty degradation cases.
        """
        return self._sources.get_bundle_referrers(record)

    @work(thread=True, group="detail", exclusive=True)
    def _open_detail_worker(self, record: dict[str, Any]) -> None:
        detail = self._fetch_detail(record)
        # Lazy, same-worker referrer read — only for THIS selected bundle (D2/D4),
        # off the event loop like the detail read; never a new AWS call type.
        referrers = self._fetch_referrers(record)
        self.call_from_thread(self._render_detail, detail, referrers)
        self._notify_refresh_complete()

    def render_detail_lines(self, record: dict[str, Any]) -> list[str]:
        """Compose the detail-pane body as render-SAFE lines (unit-testable).

        Every value is funneled through ``self._safe``. The verify command is
        appended via :meth:`render_verify_line`. Returns a list of lines (the
        widget render joins them); this is the authoritative producer-string
        surface the hostile-bundle test inspects.
        """
        lines: list[str] = []

        lines.append("== Lifecycle ==")
        for stage, state, detail in compute_lifecycle(record):
            marker = {"done": "[x]", "pending": "[~]", "failed": "[!]"}.get(state, "[ ]")
            lines.append(f"{marker} {self._safe(stage)}: {self._safe(state)} {self._safe(detail)}")

        # Two-signature trust verdict, derived from the coarse projected
        # signingStatus: a
        # bundle is fully trusted only with BOTH content and attestation
        # signatures valid. This is a non-authoritative projection read — the
        # authoritative re-verification is the `cairn verify` command rendered
        # below — so it is labelled "(projection)".
        lines.append(f"Trust:       {self._safe(projection_trust_label(record))} (projection)")

        lines.append("")
        lines.append("== Cross-references ==")
        lines.append(f"ECR repo:    {self._safe(record.get('repositoryName') or _PLACEHOLDER)}")
        lines.append(f"ECR registry:{self._safe(record.get('registryId') or _PLACEHOLDER)}")
        manifest_digest = record.get("manifestDigest")
        lines.append(
            f"ECR digest:  {self._safe(manifest_digest or _PLACEHOLDER)} (immutable identity)"
        )
        # A bundle resolvable by digest but with no canonical tag yet is in the
        # pushed-but-not-yet-tagged window — label it "tag pending"
        # rather than an empty placeholder, so the operator reads a transient
        # state, not a missing/broken tag.
        image_tag = record.get("imageTag")
        alias_tag = alias_tag_of(image_tag)
        if is_tag_pending(record):
            lines.append(f"ECR tag:     {self._safe(TAG_PENDING_LABEL)}")
        elif alias_tag is not None:
            # An alias is a MUTABLE label over the immutable digest — render it
            # VISUALLY DISTINCT from a canonical `_ref` identity so the operator
            # never mistakes a movable pointer for the verified identity, which
            # is always the digest above. The tag is producer-
            # influenceable, so it still passes through ``self._safe``.
            lines.append(
                f"ECR tag:     {self._safe(alias_tag)} (MUTABLE ALIAS — "
                f"reached via mutable alias -> the digest above is the identity)"
            )
        else:
            lines.append(f"ECR tag:     {self._safe(image_tag or _PLACEHOLDER)}")
        lines.append(f"S3 bucket:   {self._safe(record.get('s3Bucket') or _PLACEHOLDER)}")
        lines.append(f"S3 key:      {self._safe(record.get('s3KeyPrefix') or _PLACEHOLDER)}")
        lines.append(f"Signing ARN: {self._safe(record.get('signingProfileArn') or _PLACEHOLDER)}")
        lines.append(f"Signature:   {self._safe(record.get('signatureDigest') or _PLACEHOLDER)}")
        lines.append(f"WORM receipt:{self._safe(record.get('wormReceiptDigest') or _PLACEHOLDER)}")

        # OSCAL section — shown ONLY when the bundle carries OSCAL references on
        # its Metadata_Index record. The record holds only system-bounded facts;
        # the producer's control claim (controls, catalog/profile) is not on the
        # record — it is the signed Producer_Attestation_Referrer's, projected
        # for query into the producer-claims control-claims projection
        # ([PCLAIM-001]) and surfaced through the control-scoped list view, not
        # this per-bundle detail pane. A bundle with no OSCAL references (a
        # non-OSCAL artifact, or a drilled-into referrer child) omits the section
        # entirely rather than render empty placeholders.
        oscal_refs = record.get("oscalReferences")
        if oscal_refs:
            lines.append("")
            lines.append("== OSCAL ==")
            if isinstance(oscal_refs, (list, tuple)):
                rendered_refs = ", ".join(self._safe(r) for r in oscal_refs)
            elif isinstance(oscal_refs, dict):
                rendered_refs = ", ".join(
                    f"{self._safe(k)}={self._safe(v)}" for k, v in oscal_refs.items()
                )
            else:
                rendered_refs = self._safe(oscal_refs)
            lines.append(f"OSCAL refs:      {rendered_refs or _PLACEHOLDER}")

        annotations = record.get("manifestAnnotations")
        if isinstance(annotations, dict) and annotations:
            lines.append("")
            lines.append("== Annotations ==")
            for key, value in annotations.items():
                lines.append(f"{self._safe(key)}: {self._safe(value)}")

        return lines

    def render_verify_line(self, record: dict[str, Any]) -> str:
        """Compose the surfaced ``cairn verify`` line (render-SAFE; never executed).

        Built only from format-validated identifiers via
        :func:`compose_verify_command`; the result is routed through
        ``self._safe`` (defense in depth). When validation fails, a safe
        explanatory message is returned instead of a malformed string. THE APP
        NEVER EXECUTES this — it is presented for the operator to copy/paste.
        """
        stage, region_code = self._stage_and_region()
        command = compose_verify_command(record, stage=stage, region_code=region_code)
        if command is None:
            return self._safe(
                "verify: cannot construct verify command for this bundle "
                "(missing or malformed repository/digest)"
            )
        return self._safe(f"verify (run manually): {command}")

    def render_retention_lines(
        self, record: dict[str, Any], listing: ReferrerListing | None
    ) -> list[str]:
        """Compose the retention section as render-SAFE lines ([TUI-016]).

        Retain_Until and its status come from :func:`retention_status` (the
        already-fetched referrer listing). An unparseable annotation shows
        *unknown* and its raw value is never rendered as a date. The section
        offers no delete action; it surfaces the ``cairn prune`` REPORT command
        for manual use.
        """
        retain_until, status = retention_status(listing, self._clock())
        stage, region_code = self._stage_and_region()
        command = compose_prune_report_command(record, stage=stage, region_code=region_code)
        shown = format_rfc3339_utc(retain_until) if retain_until else RETENTION_UNKNOWN
        prune_line = (
            "prune report: cannot construct command for this bundle"
            if command is None
            else f"prune report (run manually): {command}"
        )
        return [
            "",
            "== Retention ==",
            f"Retain_Until: {self._safe(shown)}",
            f"Retention:    {self._safe(status)}",
            self._safe(prune_line),
        ]

    def _stage_and_region(self) -> tuple[str, str]:
        target = self._deployment_target
        if target is not None:
            return target.stage, target.region_code
        return "alpha", "cmh"

    # --- referrer surface (lazy; rendered IN the detail flow) ------

    # Neutral status strings for the two degradation cases. They MUST
    # read differently so an unreadable referrer set is never mistaken for an
    # empty one.
    _REFERRERS_UNAVAILABLE = "referrers unavailable"
    _REFERRERS_NONE = "no referrers"

    def render_referrer_status(self, listing: ReferrerListing | None) -> str:
        """Compose the render-SAFE one-line referrer-read summary (unit-testable).

        Distinguishes the two degradation cases: ``available=False``
        renders "referrers unavailable" (the ``ecr:ListImageReferrers`` read
        failed or the record lacked identifiers); ``available=True`` with an
        empty list renders "no referrers" (read succeeded, zero referrers). A
        populated listing summarizes the count. ``None`` (not yet fetched)
        renders an empty string. Every token is fixed text or an integer, so
        nothing producer-controlled reaches the terminal here.
        """
        if listing is None:
            return self._safe("")
        if not listing.available:
            return self._safe(self._REFERRERS_UNAVAILABLE)
        if not listing.referrers:
            return self._safe(self._REFERRERS_NONE)
        count = len(listing.referrers)
        noun = "referrer" if count == 1 else "referrers"
        return self._safe(f"{count} {noun}")

    def render_referrer_lines(self, listing: ReferrerListing | None) -> list[str]:
        """Compose the referrer LIST rows as render-SAFE strings (unit-testable).

        One line per referrer: ``classification | artifactType | digest | tag``.
        Every field is routed through ``self._safe``. The
        classification is the pure :func:`classify_referrer` string match — NOT a
        verification claim: a ``signature``-classified referrer is
        labelled neutrally ("not verified — run cairn verify") so nothing implies
        the signature has been cryptographically validated. Returns an empty list
        for the unavailable/empty/unfetched cases (the status line covers those).
        """
        if listing is None or not listing.available:
            return []
        return [self._format_referrer_row(referrer) for referrer in listing.referrers]

    def _safe_referrer_fields(self, referrer: Referrer) -> tuple[str, str, str]:
        """The (artifactType, digest, tag) cells, each through the render-safety
        choke point with the placeholder fallback. Single source for
        the three sites that surface a referrer's fields — list row, focused
        detail body, and the selectable table — so they cannot drift.
        """
        return (
            self._safe(referrer.artifact_type) or _PLACEHOLDER,
            self._safe(referrer.digest) or _PLACEHOLDER,
            self._safe(referrer.tag) or _PLACEHOLDER,
        )

    def _format_referrer_row(self, referrer: Referrer) -> str:
        """One render-SAFE summary line for a referrer (classification + fields)."""
        classification = self._referrer_classification_label(referrer)
        artifact_type, digest, tag = self._safe_referrer_fields(referrer)
        return f"{classification} | type={artifact_type} | digest={digest} | tag={tag}"

    def _referrer_classification_label(self, referrer: Referrer) -> str:
        """Render-SAFE classification label that NEVER implies verification (M4).

        ``classify_referrer`` is a pure ``artifactType`` string match. A
        ``signature``-classified referrer is shown as
        ``signature (not verified — run cairn verify)`` so the operator cannot
        read the classification as a cryptographic-validation claim; verification
        stays the manually-surfaced ``cairn verify`` line.
        """
        classification = classify_referrer(referrer.artifact_type)
        if classification == "signature":
            return self._safe("signature (not verified — run cairn verify)")
        return self._safe(classification)

    def render_referrer_detail_lines(self, referrer: Referrer) -> list[str]:
        """Compose the FOCUSED referrer view body as render-SAFE lines.

        Shows the selected referrer's identity (classification / artifactType /
        digest / tag, plus any annotations) AND names the SUBJECT bundle it
        refers back to (the bundle's repo + manifest digest). Every value flows
        through ``self._safe``; the classification carries the same
        no-verification-implied label.
        """
        artifact_type, digest, tag = self._safe_referrer_fields(referrer)
        lines: list[str] = []
        lines.append("== Referrer ==")
        lines.append(f"Classification: {self._referrer_classification_label(referrer)}")
        lines.append(f"artifactType:   {artifact_type}")
        lines.append(f"Digest:         {digest}")
        lines.append(f"Tag:            {tag}")
        if referrer.annotations:
            lines.append("")
            lines.append("== Referrer annotations ==")
            for key, value in referrer.annotations.items():
                lines.append(f"{self._safe(key)}: {self._safe(value)}")
        lines.append("")
        lines.append("== Subject bundle ==")
        record = self._selected or {}
        lines.append(f"ECR repo:   {self._safe(record.get('repositoryName') or _PLACEHOLDER)}")
        lines.append(f"ECR digest: {self._safe(record.get('manifestDigest') or _PLACEHOLDER)}")
        lines.append("")
        lines.append(self._safe("(Esc returns to the bundle detail.)"))
        return lines

    # --- raw-manifest drill-down ---------------------------------

    # Render-safe message when the manifest could not be fetched/parsed. A fixed
    # string, so nothing producer-controlled reaches the terminal in this case.
    _MANIFEST_UNAVAILABLE = "raw manifest unavailable for this artifact"

    def render_raw_manifest_lines(self, manifest: dict[str, Any] | None) -> list[str]:
        """Compose the pretty-printed raw OCI manifest as render-SAFE lines.

        The ENTIRE producer-controlled manifest is pretty-printed
        (``json.dumps(indent=2)``) and EACH resulting line is passed through the
        single ``self._safe()`` choke point so no escape/control byte in any KEY,
        VALUE, or STRUCTURE can reach the terminal. Sanitizing
        per line — rather than the whole blob in one pass — is essential: a
        single-pass ``self._safe(blob)`` would strip the structural newlines (a
        newline is a C0 control byte) and collapse the pretty-print to one line.
        ``json.dumps`` has already escaped any in-value control byte to a
        printable backslash-escape, so the only real newlines are the structural
        ones; the caller's newline-join re-inserts them around the
        per-line-sanitized content, while indentation (spaces) survives untouched.
        An unavailable/None manifest degrades to a fixed render-safe message
        rather than crashing. Returned lines are joined and wrapped in
        ``self._literal`` by the caller.
        """
        lines: list[str] = ["== Raw OCI manifest =="]
        if manifest is None:
            lines.append(self._safe(self._MANIFEST_UNAVAILABLE))
        else:
            # json.dumps over a parsed dict cannot itself raise. Split on the
            # structural newlines and neutralize each line independently so the
            # pretty-printed structure survives the render-safety sweep.
            blob = json.dumps(manifest, indent=2, sort_keys=False, default=str)
            lines.extend(self._safe(line) for line in blob.split("\n"))
        lines.append("")
        lines.append(self._safe("(Esc returns to the previous view.)"))
        return lines

    def action_raw_manifest(self) -> None:
        """Drill into the raw OCI manifest of the focused referrer or bundle (m).

        Only meaningful in the Detail tab with a selection. When a referrer is
        focused, fetches that referrer's manifest lazily via
        ``get_referrer_manifest`` (one allowlisted ``ecr:BatchGetImage`` — design
        D5); otherwise it reuses the bundle manifest already captured on
        selection (NO new AWS call). Sets the raw-manifest sub-state and
        re-renders the body. A no-op outside the detail tab or with nothing
        selected.
        """
        if self._active_tab() != "tab-detail" or self._selected is None:
            return
        if self._referrer_focus is not None:
            # Lazy, single allowlisted BatchGetImage for the focused referrer.
            self._focused_referrer_manifest = self._fetch_referrer_manifest(self._referrer_focus)
        self._showing_raw_manifest = True
        self._rerender_detail_body()

    def _fetch_referrer_manifest(self, referrer: Referrer) -> dict[str, Any] | None:
        """Lazy raw manifest of a focused referrer via ``get_referrer_manifest``.

        Rides the SAME allowlisted ``ecr:BatchGetImage`` as the bundle/list
        paths (no new call type). The referrer's registry/repository are the
        SUBJECT bundle's (a referrer lives in the same repository as its subject);
        the referrer's own ``digest`` selects the image. Degrade-don't-crash:
        ``get_referrer_manifest`` returns ``None`` on any error, never raising.
        """
        record = self._selected or {}
        return self._sources.get_referrer_manifest(
            registry_id=record.get("registryId"),
            repository_name=record.get("repositoryName"),
            manifest_digest=referrer.digest,
        )

    def _open_referrer(self, index: int) -> None:
        """Drill INTO a referrer row of the current bundle's detail (sub-state).

        Sets ``_referrer_focus`` to the selected referrer and re-renders the
        detail body to its identity + subject bundle. No AWS call — the referrer
        was already fetched lazily with the bundle's detail.
        """
        listing = self._referrers
        if listing is None or not listing.available:
            return
        if not (0 <= index < len(listing.referrers)):
            return
        self._referrer_focus = listing.referrers[index]
        # Drilling into a referrer leaves any raw-manifest view (e.g. the
        # bundle's) and clears the previous referrer's lazily-fetched manifest.
        self._showing_raw_manifest = False
        self._focused_referrer_manifest = None
        self._rerender_detail_body()

    def _rerender_detail_body(self) -> None:
        """Re-render the detail body for the current bundle/referrer-focus state.

        When a referrer is focused, the body shows that referrer's identity +
        subject bundle; otherwise it shows the bundle detail. The verify line,
        referrer status, and referrer table are unaffected (they belong to the
        bundle, not the focused referrer).
        """
        body = self.query_one("#detail-body", Static)
        if self._showing_raw_manifest:
            # The raw-manifest level sits BENEATH whichever of referrer/bundle is
            # in focus: a focused referrer shows its (lazily-fetched) manifest;
            # otherwise the bundle's manifest captured on selection.
            manifest = (
                self._focused_referrer_manifest
                if self._referrer_focus is not None
                else self._bundle_manifest
            )
            body.update(self._literal("\n".join(self.render_raw_manifest_lines(manifest))))
        elif self._referrer_focus is not None:
            body.update(
                self._literal("\n".join(self.render_referrer_detail_lines(self._referrer_focus)))
            )
        elif self._selected is not None:
            body.update(self._literal("\n".join(self.render_detail_lines(self._selected))))

    def _render_detail(self, detail: BundleDetail, referrers: ReferrerListing) -> None:
        # A fresh detail fetch (initial open OR a watch/refresh re-tick) shows the
        # BUNDLE detail and a freshly-fetched referrer set, so any prior
        # referrer-focus is stale — clear it. Otherwise a tick that fired while a
        # referrer was focused would leave the body on the bundle detail but
        # ``_referrer_focus`` still set, and the operator's next Esc would be
        # swallowed clearing that invisible focus instead of drilling up.
        self._referrer_focus = None
        # A fresh fetch also drops any raw-manifest drill-down sub-state (a watch
        # tick that fired while the raw manifest was shown reverts to the bundle
        # detail) and captures THIS bundle's manifest for the drill-down.
        self._showing_raw_manifest = False
        self._bundle_manifest = manifest_dict_from_response(detail.manifest)
        self._referrers = referrers
        body = self.query_one("#detail-body", Static)
        body.update(self._literal("\n".join(self.render_detail_lines(detail.record))))
        verify_label = self.query_one("#verify-cmd", Label)
        verify_label.update(self._literal(self.render_verify_line(detail.record)))
        self.query_one("#retention-body", Static).update(
            self._literal("\n".join(self.render_retention_lines(detail.record, referrers)))
        )
        self._populate_referrer_table(referrers)
        # Focus the scrollable pane so PageUp/Down + arrows scroll long detail
        # without first clicking it. Only steal focus while Detail is the active
        # tab — a watch-mode re-render of the selected bundle must not yank focus
        # away from a list the operator has since navigated to.
        if self._active_tab() == "tab-detail":
            self.query_one("#detail-pane", VerticalScroll).scroll_home(animate=False)
            self.query_one("#detail-pane", VerticalScroll).focus()

    def _populate_referrer_table(self, listing: ReferrerListing) -> None:
        """Render the referrer list + status. EVERY cell routes through ``self._safe``.

        The status line distinguishes "referrers unavailable" from "no
        referrers"; the table holds one selectable row per referrer,
        each cell wrapped in ``self._literal(self._safe(...))``. A
        ``signature``-classified referrer's type cell carries the neutral
        no-verification label.
        """
        self.query_one("#referrers-status", Static).update(
            self._literal(self.render_referrer_status(listing))
        )
        table = self.query_one("#referrer-table", DataTable)
        table.clear()
        if not listing.available:
            return
        for index, referrer in enumerate(listing.referrers):
            artifact_type, digest, tag = self._safe_referrer_fields(referrer)
            table.add_row(
                self._literal(self._referrer_classification_label(referrer)),
                self._literal(artifact_type),
                self._literal(digest),
                self._literal(tag),
                key=str(index),
            )

    # --- control view --------------------------------------------------------

    def _fetch_control_bundles(
        self, tenant_id: str, uuid: str, control_id: str
    ) -> list[dict[str, Any]]:
        """Synchronous, unit-testable: list ALL bundles citing a control.

        Exhausts the pagination token so a control cited by more than one page
        of bundles is never silently truncated — the *Paginated listing without
        truncation* requirement names both the project AND control result sets.
        The ControlClaims projection is tenant-partitioned, so ``tenant_id`` is
        the partition key threaded into every page query.
        """
        items: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            page = self._sources.list_control_bundles(
                tenant_id=tenant_id,
                catalog_or_profile_uuid=uuid,
                control_id=control_id,
                page_token=token,
            )
            items.extend(page.items)
            token = page.next_token
            if not token:
                break
        return items

    @work(thread=True, group="control", exclusive=True)
    def _refresh_control_worker(self, tenant_id: str, uuid: str, control_id: str) -> None:
        try:
            records = self._fetch_control_bundles(tenant_id, uuid, control_id)
        except (ClientError, BotoCoreError) as exc:
            self.call_from_thread(self._render_query_error, "#control-table", "#ctl-status", exc)
            self._notify_refresh_complete()
            return
        self._identity_names = self._resolve_identity_names(records)
        self.call_from_thread(self._populate_control_table, records)
        self._notify_refresh_complete()

    def _populate_control_table(self, records: list[dict[str, Any]]) -> None:
        self.query_one("#ctl-status", Label).update("")
        self._control_records = records
        table = self.query_one("#control-table", DataTable)
        table.clear()
        for index, record in enumerate(records):
            table.add_row(
                self._literal(self._safe(self._display_identity(record.get("projectId")))),
                self._literal(self._safe(record.get("artifactKind") or _PLACEHOLDER)),
                self._literal(self._safe(self._short_digest(record.get("manifestDigest")))),
                self._literal(self._safe(record.get("createdAt") or _PLACEHOLDER)),
                key=str(index),
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("ctl-tenant-id", "ctl-uuid", "ctl-id"):
            tenant_id = self.query_one("#ctl-tenant-id", Input).value.strip()
            uuid = self.query_one("#ctl-uuid", Input).value.strip()
            control_id = self.query_one("#ctl-id", Input).value.strip()
            if tenant_id and uuid and control_id:
                self._refresh_control_worker(tenant_id, uuid, control_id)
        elif event.input.id in ("proj-id", "tenant-id"):
            tenant_id = self.query_one("#tenant-id", Input).value.strip()
            project_id = self.query_one("#proj-id", Input).value.strip()
            if tenant_id and project_id:
                self._refresh_project_worker(tenant_id, project_id)

    # --- navigation (drill down / up / across; reads-only) -------------------

    def action_show_bundles(self) -> None:
        """Jump to the all-bundles list and focus it for arrow/Enter nav."""
        self.query_one(TabbedContent).active = "tab-list"
        self.query_one("#bundle-table", DataTable).focus()

    def action_show_detail(self) -> None:
        self.query_one(TabbedContent).active = "tab-detail"

    def action_focus_control(self) -> None:
        self.query_one(TabbedContent).active = "tab-control"
        self.query_one("#ctl-tenant-id", Input).focus()

    def action_focus_project(self) -> None:
        self.query_one(TabbedContent).active = "tab-project"
        self.query_one("#proj-id", Input).focus()

    def action_back(self) -> None:
        """Drill UP one level (Esc).

        From Detail, return to the tab the bundle was opened from (the
        drilled-from origin, defaulting to the list); from a search tab, return
        to the all-bundles list. Lands focus on the destination's table so the
        keyboard stays live. A no-op on the list itself.

        Esc is the universal escape hatch: unlike the number-key jumps and ``p``
        (which a focused search ``Input`` swallows as ordinary text — intended,
        since project ids / UUIDs contain those characters), Esc is NOT consumed
        by an ``Input``, so it always frees the operator from a search box
        without reaching for the mouse.
        """
        tabs = self.query_one(TabbedContent)
        # Detail-tab SUB-STATE (deepest first). The raw-manifest drill-down is the
        # bottom level: the first Esc clears it and re-renders to whichever of
        # referrer-focus / bundle detail it was opened from, WITHOUT leaving the
        # tab. So the detail flow now has three Esc levels: raw-manifest →
        # (referrer-focus | bundle) → origin tab.
        if tabs.active == "tab-detail" and self._showing_raw_manifest:
            self._showing_raw_manifest = False
            self._rerender_detail_body()
            return
        # When a referrer is focused, the next Esc returns to the BUNDLE detail
        # (clears the focus + re-renders) WITHOUT leaving the tab; only a
        # subsequent Esc (no referrer focused) drills up to the origin tab. This
        # keeps bundle ⇄ referrer navigation inside the detail flow (D4).
        if tabs.active == "tab-detail" and self._referrer_focus is not None:
            self._referrer_focus = None
            self._rerender_detail_body()
            self.query_one("#referrer-table", DataTable).focus()
            return
        if tabs.active == "tab-detail":
            destination = self._detail_origin_tab
        elif tabs.active in ("tab-control", "tab-project"):
            destination = "tab-list"
        else:
            return
        tabs.active = destination
        focus_id = {
            "tab-list": "#bundle-table",
            "tab-control": "#control-table",
            "tab-project": "#project-table",
        }.get(destination)
        if focus_id is not None:
            self.query_one(focus_id, DataTable).focus()

    def action_toggle_group(self) -> None:
        """Expand or collapse the referrer children of the selected primary (Space).

        Resolves the primary digest from the currently-selected bundle row: a
        primary row toggles its own group; a referrer CHILD row toggles its
        parent (so collapsing from a child still works). A leaf primary (no
        children), a header row, or a non-bundle context is a no-op. Toggling
        flips the digest's membership in ``_expanded_primaries`` and rebuilds the
        grouped table from the persisted model — no AWS read.
        """
        if self._active_tab() != "tab-list":
            return
        digest = self._primary_digest_for_selected_bundle_row()
        if digest is None:
            return
        if digest in self._expanded_primaries:
            self._expanded_primaries.discard(digest)
        else:
            self._expanded_primaries.add(digest)
        self._populate_bundle_table(self._records)

    def action_toggle_indexes(self) -> None:
        """Show or hide OCI image-index entries in the all-bundles list (`i`).

        Image indexes are registry machinery the pipeline does not mint
        (suppressing them is tracked in the backlog) and are hidden by
        default. This flips ``_show_indexes`` and re-renders from the unfiltered
        ``_records`` — no AWS read — then surfaces the resulting state. Works
        regardless of the active tab so the operator can flip the filter without
        first focusing the list.
        """
        self._show_indexes = not self._show_indexes
        self._populate_bundle_table(self._records)
        state = "shown" if self._show_indexes else "hidden"
        self.notify(self._safe(f"Image indexes {state}"))

    def _primary_digest_for_selected_bundle_row(self) -> str | None:
        """The toggle-able primary digest for the selected bundle row, or None.

        A primary row that HAS children returns its own ``manifestDigest``; a
        referrer child returns its ``subjectDigest`` (its parent). A leaf primary
        (no children), a synthetic header row, or no/invalid selection returns
        ``None`` (the toggle is then a no-op).
        """
        table = self.query_one("#bundle-table", DataTable)
        try:
            index = table.cursor_row
        except Exception:
            return None
        if not (0 <= index < len(self._bundle_row_records)):
            return None
        record = self._bundle_row_records[index]
        if record is None:
            return None
        if self._is_referrer_child(record):
            subject = record.get("subjectDigest")
            return subject if isinstance(subject, str) and subject else None
        # A primary row: only toggle-able when it actually has referrer children,
        # recorded by the last table build (no need to re-derive the group model).
        digest = record.get("manifestDigest")
        if isinstance(digest, str) and digest in self._primaries_with_children:
            return digest
        return None

    def action_project_drill(self) -> None:
        """Drill ACROSS: list the SELECTED bundle's (tenant, project) (`p`).

        Saves hand-typing the tenant + project ids that are already on screen.
        Both are validated against the canonical ``cairn_oci`` segment grammar
        before they are placed in the search boxes — that grammar admits only
        ``[a-z0-9-]``, so neither can carry a terminal-control byte
        and the subsequent query rides the same sanctioned tenant-scoped
        ``query_by_project`` path. An unselected or malformed bundle
        degrades to a notification rather than a broken query.
        """
        if self._selected is None:
            self.notify(self._safe("Select a bundle first (Enter), then press p"))
            return
        coordinates = self._validated_coordinates(self._selected)
        if coordinates is None:
            self.notify(self._safe("Selected bundle has no valid tenant/project id to drill into"))
            return
        tenant_id, project_id = coordinates
        self.query_one("#tenant-id", Input).value = tenant_id
        self.query_one("#proj-id", Input).value = project_id
        self.query_one(TabbedContent).active = "tab-project"
        self._refresh_project_worker(tenant_id, project_id)

    def _validated_coordinates(self, record: dict[str, Any]) -> tuple[str, str] | None:
        """Return ``(tenant_id, project_id)`` iff the record is safe to drill into.

        Reuses ``cairn_oci``'s opaque-id rule (via the repo-name builder) rather
        than re-implementing it, so the drill cannot drift from the grammar.
        The grammar alone is NOT relied on for render-safety: ``_ID_RE`` anchors
        with ``\\Z`` but a stored value placed into the ``#tenant-id`` / ``#proj-id``
        ``Input`` renders WITHOUT ``_safe``, so each opaque id MUST ALSO survive
        ``render_safe`` byte-for-byte — i.e. carry no control byte at all —
        before it is accepted; anything stripped is rejected rather than silently
        sanitized (a sanitized id would query a different scope). The org id is
        validated alongside (it shares the path grammar) even though the query
        keys on tenant + project. Defense in depth for render-safety.
        """
        org_id = record.get("orgId")
        tenant_id = record.get("tenantId")
        project_id = record.get("projectId")
        if not all(isinstance(v, str) for v in (org_id, tenant_id, project_id)):
            return None
        if any(render_safe(v) != v for v in (org_id, tenant_id, project_id)):
            return None
        stage, region_code = self._stage_and_region()
        try:
            ArtifactLocator().repo_name(
                deployment_qualifier=f"{region_code}-{stage}",
                org_id=org_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )
        except InvalidIdError:
            return None
        return tenant_id, project_id

    # --- project-ID view (direct query_by_project, NOT ECR-gated) ------------

    def _fetch_project_bundles(self, tenant_id: str, project_id: str) -> list[dict[str, Any]]:
        """Synchronous, unit-testable: list ALL bundles for a (tenant, project).

        Rides ONLY the sanctioned tenant-scoped ``query_by_project`` path via
        ``ArtifactSources.list_project_bundles`` (partition = exact-match
        ``{tenant-id}``, sort = ``begins_with('{project-id}#')``; never a
        ``Scan``). Unlike the control view, this EXHAUSTS the pagination token so
        a multi-page project is never silently truncated (spec scenario "Project
        listing rides only sanctioned access paths").
        """
        items: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            page = self._sources.list_project_bundles(
                tenant_id=tenant_id, project_id=project_id, page_token=token
            )
            items.extend(page.items)
            token = page.next_token
            if not token:
                break
        return items

    @work(thread=True, group="project", exclusive=True)
    def _refresh_project_worker(self, tenant_id: str, project_id: str) -> None:
        try:
            records = self._fetch_project_bundles(tenant_id, project_id)
        except (ClientError, BotoCoreError) as exc:
            self.call_from_thread(self._render_query_error, "#project-table", "#proj-status", exc)
            self._notify_refresh_complete()
            return
        self._identity_names = self._resolve_identity_names(records)
        self.call_from_thread(self._populate_project_table, records)
        self._notify_refresh_complete()

    def _populate_project_table(self, records: list[dict[str, Any]]) -> None:
        """Render the project list. EVERY cell routes through ``self._safe``."""
        self.query_one("#proj-status", Label).update("")
        self._project_records = records
        table = self.query_one("#project-table", DataTable)
        table.clear()
        for index, record in enumerate(records):
            table.add_row(
                self._literal(self._safe(self._display_identity(record.get("projectId")))),
                self._literal(self._safe(record.get("artifactKind") or _PLACEHOLDER)),
                self._literal(self._safe(record.get("signingStatus") or _PLACEHOLDER)),
                self._literal(self._safe(projection_trust_label(record))),
                self._literal(self._safe(record.get("archiveStatus") or _PLACEHOLDER)),
                self._literal(self._safe(self._short_digest(record.get("manifestDigest")))),
                self._literal(self._safe(record.get("createdAt") or _PLACEHOLDER)),
                key=str(index),
            )

    def _render_query_error(
        self, table_id: str, status_id: str, exc: ClientError | BotoCoreError
    ) -> None:
        """Surface a sanctioned-read error WITHOUT terminating the session.

        Runs on the main thread (dispatched via ``call_from_thread`` from the
        query workers). Clears the target DataTable so a stale success set can't
        masquerade as the error's result, then writes a non-fatal, render-safe
        message — built from the fixed botocore error code plus the resolved
        table name and region — into the named status Label. The message still
        passes through ``self._safe`` / ``self._literal`` for uniform
        render-safety.
        """
        self.query_one(table_id, DataTable).clear()
        message = format_read_error(exc, resource=self._table_for(table_id), region=self._region)
        self.query_one(status_id, Label).update(self._literal(self._safe(message)))

    def _table_for(self, table_id: str) -> str | None:
        """Resolve the DynamoDB table name backing a view's DataTable, if known.

        Returns ``None`` when no deployment target was injected (some tests), so
        the error message degrades to an unqualified phrase rather than guessing.
        """
        target = self._deployment_target
        if target is None:
            return None
        if table_id == "#project-table":
            return target.metadata_table
        if table_id == "#control-table":
            return target.control_claims_table
        return None

    # --- DLQ health bar ------------------------------------------------------

    def _fetch_dlq_health(self) -> list[DlqDepth]:
        """Synchronous, unit-testable: counts-only DLQ depth (never ReceiveMessage)."""
        return self._sources.dlq_health()

    @work(thread=True, group="dlq", exclusive=True)
    def _refresh_dlq_worker(self) -> None:
        depths = self._fetch_dlq_health()
        self.call_from_thread(self._render_dlq, depths)

    def render_dlq_text(self, depths: list[DlqDepth]) -> str:
        """Compose the DLQ bar text (render-SAFE). Flags non-zero; marks unknown."""
        parts: list[str] = []
        for depth in depths:
            stage = self._safe(depth.stage)
            if depth.approximate_messages is None:
                parts.append(f"{stage}: unknown")
            elif depth.approximate_messages > 0:
                parts.append(f"{stage}: {depth.approximate_messages} (approx) [!]")
            else:
                parts.append(f"{stage}: 0 (approx)")
        return "DLQ depth — " + "  |  ".join(parts) if parts else "DLQ depth — unknown"

    def _render_dlq(self, depths: list[DlqDepth]) -> None:
        self.query_one("#dlq-bar", Static).update(self._literal(self.render_dlq_text(depths)))

    # --- refresh + watch mode ------------------------------------

    def _refresh_all_workers(self) -> bool:
        """Re-run the active view's reads + DLQ health on the bounded worker pool.

        Reads ONLY — the same allowlisted operations the views already issue.
        No new call types, no EventBridge subscription.

        Returns ``True`` when a view worker was dispatched (that worker fires the
        completion notification off-thread via ``_notify_refresh_complete``), and
        ``False`` when none was — e.g. the control/project tab with an empty
        input. The caller uses this to still emit a completion indication so the
        refresh affordance always shows invocation *and* completion.
        """
        active = self._active_tab()
        dispatched = True
        if active == "tab-control":
            tenant_id = self.query_one("#ctl-tenant-id", Input).value.strip()
            uuid = self.query_one("#ctl-uuid", Input).value.strip()
            control_id = self.query_one("#ctl-id", Input).value.strip()
            if tenant_id and uuid and control_id:
                self._refresh_control_worker(tenant_id, uuid, control_id)
            else:
                dispatched = False
        elif active == "tab-project":
            tenant_id = self.query_one("#tenant-id", Input).value.strip()
            project_id = self.query_one("#proj-id", Input).value.strip()
            if tenant_id and project_id:
                self._refresh_project_worker(tenant_id, project_id)
            else:
                dispatched = False
        elif active == "tab-detail" and self._selected is not None:
            # Load-on-demand: refresh only the selected bundle's detail, not the
            # whole list, when the detail view is active.
            self._open_detail_worker(self._selected)
        else:
            self._refresh_bundles_worker()
        self._refresh_dlq_worker()
        return dispatched

    def _active_tab(self) -> str | None:
        try:
            return self.query_one(TabbedContent).active
        except Exception:
            return None

    def action_refresh(self) -> None:
        # Visible feedback (Fix 4): the invocation notification fires
        # synchronously on the main thread; the matching completion
        # notification fires off-thread from the view worker once its read
        # finishes (see ``_notify_refresh_complete``). This adds only UI
        # notifications — the read set issued by ``_refresh_all_workers`` is
        # unchanged.
        self.notify(self._safe("Refreshing…"))
        if not self._refresh_all_workers():
            # No view worker was dispatched (e.g. control/project tab with an
            # empty input), so no off-thread completion will fire. Emit it
            # synchronously here — on the main thread — so the operator still
            # sees the matching completion indication. No reads are issued.
            self.notify(self._safe("Refresh complete"))

    def action_toggle_watch(self) -> None:
        if self._watching:
            self._stop_watch()
            self.notify(self._safe("Watch disabled"))
        else:
            self._start_watch()
            self.notify(self._safe(f"Watch enabled (every {self._watch_interval:g}s)"))

    def _notify_refresh_complete(self) -> None:
        """Fire the refresh/watch-tick completion notification OFF-THREAD.

        Called from a thread worker after it has pushed its results to widgets
        (consistent with how the tables are updated via ``call_from_thread``).
        Watch ticks reuse this same completion notification. Adds only a visible
        indication; it issues no reads.
        """
        self.call_from_thread(self.notify, self._safe("Refresh complete"))

    def _start_watch(self) -> None:
        if self._watching:
            return
        self._watching = True
        # Clamp to the minimum so watch mode can never become a sub-second hammer.
        interval = max(self._watch_interval, self._min_watch_interval)
        self._watch_timer = self.set_interval(interval, self._refresh_all_workers)

    def _stop_watch(self) -> None:
        self._watching = False
        if self._watch_timer is not None:
            self._watch_timer.stop()
            self._watch_timer = None

    @property
    def is_watching(self) -> bool:
        return self._watching


__all__ = [
    "ArtifactStoreApp",
    "compose_verify_command",
    "compute_lifecycle",
    "format_read_error",
]

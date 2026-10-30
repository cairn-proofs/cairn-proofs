"""Data-source layer for the Artifact_TUI — thin views over sanctioned reads.

Sits between the Textual app and the read-only AWS surface. Every
AWS call routes through :class:`~cairn_cli.tui._readonly_client.ReadOnlyAwsClient`
so the read-only invariant is structural, not conventional — this
module cannot reach a mutating or message-consuming operation because the
client does not expose one.

Access patterns:

- The all-bundles list is assembled by enumerating ECR repositories,
  deriving the distinct opaque ``{org-id}``/``{tenant-id}``/``{project-id}``
  coordinates from their three-segment repo paths, and running the tenant-scoped
  ``query_by_project`` for each. Never a ``Scan``. Display names are resolved by
  joining the opaque ids against the control-plane Identity_Registry at read
  time ([IDREG-001], [TUI-002.04]) — never read off the path.
- The list path renders entirely from the Metadata_Index rows; the ECR
  manifest read and the S3 ``HeadObject`` happen lazily, only for the bundle
  the operator selects (see :meth:`ArtifactSources.get_bundle_detail`).
- DLQ health is queue-depth only (``GetQueueAttributes``); never
  ``ReceiveMessage``.

Pagination is exhausted via ``Page.next_token`` in the all-bundles path so
nothing is silently truncated; the project / control list methods return a
single ``Page`` and leave on-demand paging to the caller (the UI loads the
next page when the operator scrolls).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from botocore.exceptions import BotoCoreError, ClientError
from cairn_metadata import ControlClaims, IdentityRegistry, MetadataIndex
from cairn_oci import (
    NOTARY_SIGNATURE_ARTIFACT_TYPE,
    PRODUCER_ATTESTATION_ARTIFACT_TYPE,
    RESOURCE_ARTIFACT_TYPE,
    WORM_RECEIPT_ARTIFACT_TYPE,
    SkipReason,
    alias_tag_of,
    classify_pipeline_internal,
)

from cairn_cli._aws import PIPELINE_DLQ_RESOURCES
from cairn_cli.tui._projects import (
    RepoCoordinates,
    coordinates_from_repo_name,
    derive_coordinates,
)

# `alias_tag_of` is the single pure-grammar string-level alias classifier
# (`cairn_oci`); re-exported here so the TUI labels an alias-reached handle
# identically to the CLI read paths. The tag is producer-influenceable, so
# callers MUST still route the returned value through ``render_safe`` before any
# terminal write.

if TYPE_CHECKING:
    from cairn_metadata.pagination import Page

    from cairn_cli._aws import DeploymentTarget
    from cairn_cli.tui._readonly_client import ReadOnlyAwsClient

# The pipeline dead-letter queues, in pipeline order. Both the (stage, resource)
# pairs AND the physical-name composition live in cairn_cli._aws — the single
# source of truth that mirrors the CDK nameFor() scheme
# (cdk/lib/constructs/dead-letter-queues.ts) — so the queue names here cannot
# drift from the deployed names (T-13). The full appName-prefixed physical name
# is built per-target via ``DeploymentTarget.dlq_name``.
_DLQ_STAGES: tuple[tuple[str, str], ...] = PIPELINE_DLQ_RESOURCES


@dataclass
class DlqDepth:
    """Approximate depth of one pipeline dead-letter queue."""

    stage: str  # "validate" | "sign" | "archive" | "metadata"
    queue_name: str
    approximate_messages: int | None  # None when unresolved (e.g. account id unknown)


@dataclass
class BundleDetail:
    """The lazily-fetched cross-reference detail for one selected bundle."""

    record: dict[str, Any]
    manifest: dict[str, Any] | None  # ECR batch_get_image result (None if unavailable)
    s3_head: dict[str, Any] | None  # S3 head_object result (None if not archived / absent)


# OCI referrer ``artifactType`` vocabulary lives in ``libs/oci`` as the single
# source of truth: the three pipeline-minted types — Notation
# signature, WORM_Receipt, and OSCAL-resource — are imported above. A drift-guard
# test in ``tests/unit/cairn_oci`` pins the signature and WORM_Receipt tokens to
# their ``lambdas/`` mint sites by value (the one-way dependency rule forbids
# importing ``lambdas/``). The names are re-exported from this module.


def classify_referrer(artifact_type: str) -> str:
    """Classify an OCI referrer by its ``artifactType`` string.

    Returns ``"signature"`` for the Notation signature type, ``"worm-receipt"``
    for the WORM_Receipt type, ``"resource-ref"`` for the resource-reference
    referrer, ``"attestation"`` for the producer-claim Producer_Attestation_Referrer, and
    ``"other"`` for anything else. This is a pure string match
    against the pinned constants — NOT a verification claim.
    A counterfeit referrer whose ``artifactType`` merely *looks* like a
    known type is matched exactly or falls through to ``"other"``; live
    cryptographic verification stays the manually-run ``cairn verify``.

    The ``resource-ref`` label mirrors the wire ``artifactType`` token
    (``…artifact.resource.v1`` — "resource", not "oscal") and names the
    artifact's ROLE: it is the bundle's archived-location reference (an OSCAL
    ``Resource`` envelope minted for EVERY bundle, not only OSCAL content).
    Labelling it ``oscal-…`` would falsely imply OSCAL control content.
    """
    if artifact_type == NOTARY_SIGNATURE_ARTIFACT_TYPE:
        return "signature"
    if artifact_type == WORM_RECEIPT_ARTIFACT_TYPE:
        return "worm-receipt"
    if artifact_type == RESOURCE_ARTIFACT_TYPE:
        return "resource-ref"
    if artifact_type == PRODUCER_ATTESTATION_ARTIFACT_TYPE:
        return "attestation"
    return "other"


# Map from the pipeline's own ``SkipReason`` to the TUI classification label.
# Reusing ``classify_pipeline_internal`` (a ``libs/`` function) keeps the TUI's
# identification bit-for-bit aligned with what the ingestion pipeline skips.
# ``OCI_EMPTY`` that is NOT a receipt is an index-like
# placeholder, so it maps to ``"index"`` alongside a true image index.
_SKIP_REASON_LABELS: dict[SkipReason, str] = {
    SkipReason.NOTARY_SIGNATURE: "signature",
    SkipReason.IMAGE_INDEX: "index",
    SkipReason.OCI_EMPTY: "index",
}


def classify_unindexed_artifact(manifest: dict[str, Any]) -> str:
    """Classify an unindexed ECR image from its OCI manifest content.

    Pure function over a parsed manifest dict. Returns one of ``"signature"``,
    ``"worm-receipt"``, ``"resource-ref"``, ``"attestation"``, ``"index"``, or
    ``"unvalidated bundle"``.

    Classification ORDER matters: the WORM_Receipt, the resource-reference
    referrer, AND the producer-claim Producer_Attestation_Referrer manifests all carry an
    oci-empty ``config.mediaType`` (the Producer_Attestation_Referrer is pushed via
    ``oras attach``, which leaves an empty scratch config) and so would otherwise
    be mis-bucketed as ``"index"`` by :func:`classify_pipeline_internal` (which
    deliberately keeps ``artifactType`` out of its skip decision). Each is
    distinguished ONLY by its top-level ``artifactType``
    (``WORM_RECEIPT_ARTIFACT_TYPE`` / ``RESOURCE_ARTIFACT_TYPE`` /
    ``PRODUCER_ATTESTATION_ARTIFACT_TYPE``), so those checks run FIRST
    — the WORM_Receipt, resource-ref, and Producer_Attestation_Referrer referrers are
    classified by ``artifactType`` and not mis-bucketed as ``index``. Otherwise
    the manifest is classified by reusing the pipeline's own
    ``classify_pipeline_internal`` skip rule; a ``None`` result (no
    pipeline-internal media type matched) means a genuine producer
    Artifact_Bundle that was pushed but did not complete the
    validate→sign→archive→metadata pipeline — an ``"unvalidated bundle"``.

    This is a content match, NOT a verification claim: a producer that crafts a
    pipeline-internal media type / ``artifactType`` only affects this cosmetic
    label; the tag and digest stay visible regardless.
    """
    if manifest.get("artifactType") == WORM_RECEIPT_ARTIFACT_TYPE:
        return "worm-receipt"
    if manifest.get("artifactType") == RESOURCE_ARTIFACT_TYPE:
        return "resource-ref"
    if manifest.get("artifactType") == PRODUCER_ATTESTATION_ARTIFACT_TYPE:
        return "attestation"
    skip_reason = classify_pipeline_internal(manifest)
    if skip_reason is None:
        return "unvalidated bundle"
    return _SKIP_REASON_LABELS[skip_reason]


def manifest_dict_from_response(response: dict[str, Any] | None) -> dict[str, Any] | None:
    """Parse the OCI manifest dict out of an ``ecr:BatchGetImage`` response (PURE).

    A ``batch_get_image`` response carries the OCI manifest as a JSON STRING at
    ``images[0].imageManifest``. Parse it to a ``dict``, degrade-don't-crash on
    anything malformed: a non-dict response, an empty/non-dict ``images`` element,
    a missing / non-string / unparseable ``imageManifest``, or a parsed value
    that is not a ``dict`` all return ``None`` — this NEVER raises. The single
    manifest-parse boundary shared by the list classifier, the lazy referrer
    fetch, and the app's raw-manifest drill-down so the degradation rules cannot
    drift between them. No AWS.
    """
    if not isinstance(response, dict):
        return None
    images = response.get("images") or []
    if not images:
        return None
    first = images[0]
    if not isinstance(first, dict):
        return None
    try:
        # json.loads raises TypeError on a missing/non-string manifest and
        # ValueError on malformed JSON — both degrade to None.
        manifest = json.loads(first.get("imageManifest"))
    except (ValueError, TypeError):
        return None
    return manifest if isinstance(manifest, dict) else None


def _subject_digest_of(manifest: dict[str, Any]) -> str | None:
    """Extract a referrer manifest's ``subject.digest``.

    Pure, degrade-don't-crash read of the OCI ``subject`` referrer pointer used
    to group a referrer under the primary it refers to. Returns the
    subject digest string when ``subject`` is a dict carrying a non-empty string
    ``digest``; otherwise ``None`` — a missing, non-dict, or non-string/empty
    ``subject.digest`` is "no subject" (the entry stays a top-level row), NEVER
    a raise. The value is producer-influenced and content-addressed; it is only
    ever used for an exact-match grouping key (it never reaches the terminal
    un-neutralized).
    """
    subject = manifest.get("subject")
    if not isinstance(subject, dict):
        return None
    digest = subject.get("digest")
    if isinstance(digest, str) and digest:
        return digest
    return None


def _normalize_pushed_at(pushed_at: Any) -> str | None:
    """Normalize an AWS ``imagePushedAt`` to the indexed ``createdAt`` form (PURE).

    The indexed ``createdAt`` (set by ``lambdas/update_metadata`` from the AWS
    ``ecrPushedAt``) is a ``Z``-suffixed UTC string like
    ``"2026-05-26T12:05:00Z"``. ``DescribeImages`` returns ``imagePushedAt`` as a
    timezone-aware ``datetime``; convert it to UTC and format it as
    ``%Y-%m-%dT%H:%M:%SZ`` (a trailing ``Z``, NOT ``+00:00``) so the table reads
    the identical string form for indexed and unindexed rows alike.

    Tolerant of either input shape: a ``datetime`` is formatted; a ``str`` is
    passed through unchanged (a fixture may already provide the wire form);
    anything else — including ``None`` (genuinely-missing ``imagePushedAt``) —
    returns ``None`` so the caller leaves the ``createdAt`` key absent and the
    table renders its placeholder. NEVER raises.

    The value is AWS-set provenance ONLY — it MUST come from ``imagePushedAt`` and
    NEVER from a producer-controlled OCI manifest ``created`` annotation.
    """
    if isinstance(pushed_at, str):
        return pushed_at
    if isinstance(pushed_at, datetime):
        return pushed_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


@dataclass
class ImageRef:
    """One ECR image reference from ``DescribeImages`` (digest + tag + push time).

    ``pushed_at`` is the AWS-set ``imagePushedAt`` retained off the SAME
    ``DescribeImages`` page that yields the digest/tag — no extra AWS call. It is
    the ONLY timestamp source for an unindexed entry's ``createdAt``;
    a producer-controlled OCI manifest ``created`` annotation is never used.
    ``None`` when AWS did not set it.
    """

    digest: str
    tag: str | None
    pushed_at: Any  # AWS-set imagePushedAt (timezone-aware datetime) or None


@dataclass
class Referrer:
    """One OCI Referrer attached to a bundle's manifest.

    ``annotations`` and ``artifact_type`` are producer-influenceable and so
    untrusted — the view routes every field through its render-safety choke
    point before display.
    """

    artifact_type: str
    digest: str
    tag: str | None
    annotations: dict[str, str]


@dataclass
class ReferrerListing:
    """Result of a lazy referrer read, distinguishing unavailable from empty.

    ``available=False`` means the ``ecr:ListImageReferrers`` read failed (a
    ``ClientError``/``BotoCoreError``, e.g. ``AccessDeniedException``, or a
    missing identifier on the record) — "referrers unavailable". ``available=
    True`` with an empty ``referrers`` list means the read succeeded and the
    bundle simply has zero referrers — "no referrers". The two cases must read
    differently in the detail view.
    """

    referrers: list[Referrer]
    available: bool


class ArtifactSources:
    """Read-only data source feeding the Artifact_TUI views.

    All AWS access goes through the injected ``ReadOnlyAwsClient``; the
    ``cairn_metadata`` facades are constructed with that client's read-only
    DynamoDB resource so even their queries cannot reach a mutator.
    """

    def __init__(
        self,
        *,
        target: DeploymentTarget,
        client: ReadOnlyAwsClient,
        region: str,
        max_workers: int = 4,
    ) -> None:
        self._target = target
        self._client = client
        self._region = region
        self._max_workers = max(1, max_workers)
        # The AWS account id is resolved from the ECR ``registryId`` purely to
        # build SQS DLQ URLs and ECR ``batch_get_image`` calls — it is NEVER a
        # Metadata_Index partition-key component (the partition key is the opaque
        # {tenant-id} derived from the repo path).
        self._account_id: str | None = None
        self._account_id_resolved = False
        self._metadata = MetadataIndex(
            target.metadata_table,
            dynamodb_resource=client.dynamodb_resource,
        )
        # The Control Claims table (ControlClaims, [PCLAIM-001]); its physical
        # name is the SSM-resolved auto-name (falling back to name_for) on the
        # deployment target ([TUI-004]).
        self._controls = ControlClaims(
            target.control_claims_table,
            dynamodb_resource=client.dynamodb_resource,
        )
        # The control-plane Identity_Registry ([IDREG-001]) resolves an opaque
        # {org-id}/{tenant-id}/{project-id} to its human display name at read
        # time (a join; never denormalized onto a bundle row). Its physical name
        # is the SSM-resolved auto-name (falling back to name_for) on the
        # deployment target ([TUI-004]).
        self._registry = IdentityRegistry(
            target.identity_registry_table,
            dynamodb_resource=client.dynamodb_resource,
        )

    # --- ECR enumeration (D3) ------------------------------------------------

    def list_repository_names(self) -> list[str]:
        """Enumerate every artifact repository name via ``DescribeRepositories``.

        Follows the ECR ``nextToken`` to exhaustion so no repository is missed
        (which would silently drop a project from the all-bundles list). Caches
        the registry/account id off the first page for deterministic DLQ-URL
        construction (see :meth:`dlq_health`).
        """
        names: list[str] = []
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {}
            if next_token:
                kwargs["nextToken"] = next_token
            response = self._client.describe_repositories(**kwargs)
            repositories = response.get("repositories") or []
            for repo in repositories:
                name = repo.get("repositoryName")
                if name is not None:
                    names.append(name)
                self._capture_registry_id(repo)
            next_token = response.get("nextToken")
            if not next_token:
                break
        return names

    def _capture_registry_id(self, repo: dict[str, Any]) -> None:
        """Cache the account id from an ECR repository's ``registryId`` field."""
        if self._account_id is None:
            registry_id = repo.get("registryId")
            if registry_id:
                self._account_id = str(registry_id)

    # --- all-bundles list (D3) -----------------------------------------------

    def list_all_bundles(self) -> list[dict[str, Any]]:
        """List every bundle by ECR-derived project fan-out — never a ``Scan``.

        Enumerates repositories, derives the distinct project IDs from their
        repo-path prefixes, then runs ``query_by_project`` for each project on a
        bounded thread pool, exhausting every page per project so nothing is
        truncated. Results are merged into a single list.

        Beyond the validated bundles sourced from the Metadata_Index, this also
        surfaces ECR-present-but-unindexed bundles (those that were pushed but
        never completed validate→sign→archive→metadata, so have no metadata
        row): for each artifact repository it reads the image references via the
        allowlisted ``ecr:DescribeImages`` and appends a synthetic
        ``status="unvalidated"`` entry for every image digest absent from its
        project's Metadata_Index ``manifestDigest`` set. Validated digests are
        NOT re-emitted as unvalidated. No ``dynamodb:Scan`` anywhere.
        """
        repo_names = self.list_repository_names()
        coordinates = derive_coordinates(
            repo_names,
            deployment_qualifier=self._target.qualifier,
            app_name=self._target.app_name,
        )
        if not coordinates:
            return []

        # Validated bundles: one tenant-scoped query fan-out per distinct
        # coordinate (bounded concurrency). Each worker exhausts that
        # coordinate's pages before returning. Track the digests per coordinate
        # so the ECR diff below can tell which ECR images already landed.
        merged: list[dict[str, Any]] = []
        digests_by_coord: dict[RepoCoordinates, set[str]] = {c: set() for c in coordinates}
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            # executor.map preserves input order and yields exactly one result
            # per coordinate, so the zip pairs each coordinate with its items.
            for coord, items in zip(
                coordinates,
                executor.map(self._all_pages_for_coordinate, coordinates),
                strict=True,
            ):
                merged.extend(items)
                for item in items:
                    digest = item.get("manifestDigest")
                    if isinstance(digest, str) and digest:
                        digests_by_coord[coord].add(digest)

        merged.extend(self._unvalidated_entries(repo_names, digests_by_coord))
        return merged

    def _unvalidated_entries(
        self,
        repo_names: list[str],
        digests_by_coord: dict[RepoCoordinates, set[str]],
    ) -> list[dict[str, Any]]:
        """Synthetic entries for ECR images with no Metadata_Index row.

        For each artifact repository whose name maps to a known coordinate,
        reads the repository's image references (``ecr:DescribeImages``) and
        yields one ``status="unvalidated"`` entry per image digest absent from
        that coordinate's validated-digest set. Each synthetic entry carries the
        opaque ``orgId``/``tenantId``/``projectId`` so the view can resolve their
        display names via the Identity_Registry join. Reads only; never a
        ``Scan`` and never a mutating or message-consuming call.
        """
        entries: list[dict[str, Any]] = []
        for repo_name in repo_names:
            coord = coordinates_from_repo_name(
                repo_name,
                deployment_qualifier=self._target.qualifier,
                app_name=self._target.app_name,
            )
            if coord is None:
                continue
            validated = digests_by_coord.get(coord, set())
            try:
                image_refs = self.list_image_refs(repo_name)
            except (ClientError, BotoCoreError):
                # One repo's image read failing — a delete race
                # (RepositoryNotFoundException) or an operator whose principal
                # lacks ecr:DescribeImages — must NOT blank the whole all-bundles
                # view (which already holds the validated bundles). Degrade to
                # "validated-only for this repo" and move on.
                continue
            for ref in image_refs:
                if ref.digest in validated:
                    continue
                classification, subject_digest = self._classify_unindexed(repo_name, ref.digest)
                entry: dict[str, Any] = {
                    "orgId": coord.org_id,
                    "tenantId": coord.tenant_id,
                    "projectId": coord.project_id,
                    "repositoryName": repo_name,
                    "registryId": self._account_id,
                    "manifestDigest": ref.digest,
                    "imageTag": ref.tag,
                    "status": "unvalidated",
                    "classification": classification,
                    # The referrer's subject digest: drives the
                    # grouping of a referrer under its primary. None
                    # when the manifest is unreadable or carries no
                    # well-formed ``subject`` — the entry then stays a
                    # top-level row.
                    "subjectDigest": subject_digest,
                }
                # createdAt from the AWS-set imagePushedAt ONLY,
                # normalized to the same Z-suffixed UTC form the indexed createdAt
                # uses so the table renders both row kinds identically. Left ABSENT
                # (→ table placeholder) only when imagePushedAt is genuinely
                # missing — NEVER sourced from a manifest ``created`` annotation.
                created_at = _normalize_pushed_at(ref.pushed_at)
                if created_at is not None:
                    entry["createdAt"] = created_at
                entries.append(entry)
        return entries

    def _classify_unindexed(self, repository_name: str, digest: str) -> tuple[str, str | None]:
        """Classify ONE unindexed image AND extract its subject.

        Reads the image's OCI manifest via the already-allowlisted
        ``ecr:BatchGetImage`` (mirroring :meth:`_fetch_manifest`), parses
        ``images[0].imageManifest`` (a JSON string) to a dict, and returns a
        ``(classification, subject_digest)`` pair: the
        :func:`classify_unindexed_artifact` label and the manifest's
        ``subject.digest`` (a referrer points at its primary via ``subject``).
        Both come from the SAME manifest read — extracting the subject is free
        relative to the read the classification already requires.
        Confined to the unindexed set — validated bundles are NOT re-read.

        Degradation is the default: ANY
        ``ClientError``/``BotoCoreError`` (incl. ``AccessDenied`` or a delete
        race), or a missing / non-string / parse-failing manifest, returns
        ``("unclassified", None)`` — never raises, so one image's read failure
        cannot drop the other unindexed entries. A readable manifest whose
        ``subject`` is absent / non-dict / carries a non-string digest yields a
        ``None`` subject (the entry stays top-level) while still classifying.
        """
        try:
            response = self._client.batch_get_image(
                registryId=str(self._account_id),
                repositoryName=repository_name,
                imageIds=[{"imageDigest": digest}],
            )
        except (ClientError, BotoCoreError):
            return "unclassified", None
        # Any malformed/hostile response shape degrades to None here
        # — a read failure for one image never drops the others.
        manifest = manifest_dict_from_response(response)
        if manifest is None:
            return "unclassified", None
        return classify_unindexed_artifact(manifest), _subject_digest_of(manifest)

    def list_image_refs(self, repo_name: str) -> list[ImageRef]:
        """Return an :class:`ImageRef` for every image in one repository.

        Uses the allowlisted ``ecr:DescribeImages`` (an image read — not a
        manifest fetch, not a mutation), following the ``nextToken`` to
        exhaustion so no image is missed. The tag is the first
        ``imageTags`` entry (or ``None`` for an untagged image). Each ref also
        retains the AWS-set ``imagePushedAt`` off the SAME ``imageDetail`` it
        already reads — no extra AWS call — so the caller can map it onto the
        synthetic entry's ``createdAt`` (the push time is AWS-set provenance, not
        a producer-controlled manifest annotation). Images without
        a resolvable digest are skipped; ``imagePushedAt`` may be absent.
        """
        refs: list[ImageRef] = []
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"repositoryName": repo_name}
            if next_token:
                kwargs["nextToken"] = next_token
            response = self._client.describe_images(**kwargs)
            for detail in response.get("imageDetails") or []:
                digest = detail.get("imageDigest")
                if not digest:
                    continue
                tags = detail.get("imageTags") or []
                tag = tags[0] if tags else None
                refs.append(
                    ImageRef(
                        digest=str(digest),
                        tag=str(tag) if tag is not None else None,
                        pushed_at=detail.get("imagePushedAt"),
                    )
                )
            next_token = response.get("nextToken")
            if not next_token:
                break
        return refs

    def _all_pages_for_coordinate(self, coord: RepoCoordinates) -> list[dict[str, Any]]:
        """Drain every Metadata_Index page for one coordinate via the page token.

        Issues a tenant-scoped ``query_by_project`` — the partition key is the
        exact-match opaque ``{tenant-id}`` and the sort-key condition is
        ``begins_with('{project-id}#')``. Never a ``Scan``.
        """
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            page = self._metadata.query_by_project(
                tenant_id=coord.tenant_id,
                project_id=coord.project_id,
                page_token=page_token,
            )
            items.extend(page.items)
            page_token = page.next_token
            if not page_token:
                break
        return items

    # --- read-time Identity_Registry display-name join ([IDREG-001]) ---------

    def resolve_identity_names(self, opaque_ids: Iterable[str]) -> dict[str, str]:
        """Resolve opaque org/tenant/project ids -> display names (read-time join).

        Batch-resolves through the control-plane Identity_Registry ([IDREG-001],
        [TUI-002.04]). Unknown ids are simply absent from the returned mapping;
        callers render the opaque id itself when a name is missing. Degrade-
        don't-crash: any ``ClientError``/``BotoCoreError`` (e.g. the registry
        table is absent, or the operator's principal lacks the read grant)
        returns an empty mapping so the list still renders the opaque ids rather
        than failing the whole view. The registry is DISTINCT from the
        rebuildable Metadata_Index — losing it costs only display-name
        resolution, never identity or verifiability ([IDREG-001.03]).
        """
        try:
            return self._registry.resolve_display_names(opaque_ids)
        except (ClientError, BotoCoreError):
            return {}

    # --- load-on-demand project / control pages ------------------------------

    def list_project_bundles(
        self,
        *,
        tenant_id: str,
        project_id: str,
        page_token: str | None = None,
        limit: int = 100,
    ) -> Page:
        """One Metadata_Index page for a (tenant, project) (caller paginates)."""
        return self._metadata.query_by_project(
            tenant_id=tenant_id,
            project_id=project_id,
            page_token=page_token,
            limit=limit,
        )

    def list_control_bundles(
        self,
        *,
        tenant_id: str,
        catalog_or_profile_uuid: str,
        control_id: str,
        page_token: str | None = None,
        limit: int = 100,
    ) -> Page:
        """One Control Claims page for a control in a tenant (caller paginates).

        The control-claims projection is a tenant-PK plane ([PCLAIM-001]): the
        partition key is the exact-match opaque ``{tenant-id}`` and the sort-key
        condition is ``begins_with('{catalogOrProfileUuid}#{controlId}#')``, so
        the listing is scoped to one tenant.
        """
        return self._controls.query_by_control(
            tenant_id,
            catalog_or_profile_uuid=catalog_or_profile_uuid,
            control_id=control_id,
            page_token=page_token,
            limit=limit,
        )

    # --- lazy bundle detail (D4) ---------------------------------------------

    def get_bundle_detail(self, record: dict[str, Any]) -> BundleDetail:
        """Fetch the ECR manifest + S3 head for THIS bundle only (lazy).

        This is the sole method that issues ``batch_get_image`` / ``head_object``
        — the list path never does. Missing identifiers or AWS errors degrade
        each fetch to ``None`` so a selection can never crash the view.
        """
        return BundleDetail(
            record=record,
            manifest=self._fetch_manifest(record),
            s3_head=self._fetch_s3_head(record),
        )

    def _fetch_manifest(self, record: dict[str, Any]) -> dict[str, Any] | None:
        registry_id = record.get("registryId")
        repository_name = record.get("repositoryName")
        manifest_digest = record.get("manifestDigest")
        if not (registry_id and repository_name and manifest_digest):
            return None
        try:
            return self._client.batch_get_image(
                registryId=str(registry_id),
                repositoryName=str(repository_name),
                imageIds=[{"imageDigest": str(manifest_digest)}],
            )
        except (ClientError, BotoCoreError):
            return None

    def _fetch_s3_head(self, record: dict[str, Any]) -> dict[str, Any] | None:
        if record.get("archiveStatus") != "archived":
            return None
        bucket = record.get("s3Bucket")
        key_prefix = record.get("s3KeyPrefix")
        if not (bucket and key_prefix):
            return None
        try:
            return self._client.head_object(
                Bucket=str(bucket), Key=self._object_key(str(key_prefix))
            )
        except (ClientError, BotoCoreError):
            # A missing object (404) or any transport error yields None rather
            # than crashing the detail view.
            return None

    @staticmethod
    def _object_key(key_prefix: str) -> str:
        """Derive the head_object target from the archive key prefix.

        The archive Lambda writes ``{prefix}manifest.json`` under a prefix that
        ends in ``/``; head that canonical object. If the stored value already
        names an object (no trailing slash) head it as given and tolerate a 404.
        """
        if key_prefix.endswith("/"):
            return f"{key_prefix}manifest.json"
        return key_prefix

    # --- lazy referrer fetch (D2 / D5) ---------------------------------------

    def get_bundle_referrers(self, record: dict[str, Any]) -> ReferrerListing:
        """Read the OCI Referrers attached to THIS bundle's manifest (lazy).

        Issues ``ecr:ListImageReferrers`` against the bundle's manifest digest
        as the subject, walking ``nextToken`` to exhaustion so no referrer is
        silently truncated (matching ``sign_bundle/referrers.py``). Each
        returned referrer is built into a classified :class:`Referrer`.

        Degradation is the default, not an exception path: any
        ``ClientError``/``BotoCoreError`` (incl. ``AccessDeniedException`` for a
        principal lacking the grant), or a record missing the required
        identifiers (so AWS is never called with ``None``), returns
        ``ReferrerListing(available=False)`` — "referrers unavailable". A
        successful read of zero referrers returns ``available=True`` with an
        empty list — "no referrers". No exception ever escapes. Reads only:
        never a ``Scan``, mutation, or message-consuming call.
        """
        registry_id = record.get("registryId")
        repository_name = record.get("repositoryName")
        manifest_digest = record.get("manifestDigest")
        if not (registry_id and repository_name and manifest_digest):
            return ReferrerListing(referrers=[], available=False)
        referrers: list[Referrer] = []
        next_token: str | None = None
        try:
            while True:
                kwargs: dict[str, Any] = {
                    "registryId": str(registry_id),
                    "repositoryName": str(repository_name),
                    "subjectId": {"imageDigest": str(manifest_digest)},
                }
                if next_token:
                    kwargs["nextToken"] = next_token
                response = self._client.list_image_referrers(**kwargs)
                for ref in response.get("referrers") or []:
                    # A malformed/hostile response may carry a non-dict entry
                    # (None, a string, a list). Skip it rather than let
                    # ``_build_referrer``'s ``.get`` raise ``AttributeError`` and
                    # escape this degradation boundary (no exception
                    # ever escapes).
                    if not isinstance(ref, dict):
                        continue
                    referrers.append(self._build_referrer(ref))
                next_token = response.get("nextToken")
                if not next_token:
                    break
        except (ClientError, BotoCoreError):
            return ReferrerListing(referrers=[], available=False)
        return ReferrerListing(referrers=referrers, available=True)

    def get_referrer_manifest(
        self,
        *,
        registry_id: str | None,
        repository_name: str | None,
        manifest_digest: str | None,
    ) -> dict[str, Any] | None:
        """Fetch ONE focused referrer's raw OCI manifest, parsed (lazy).

        Backs the focused-referrer raw-manifest drill-down. Reads the
        referrer image's manifest via the allowlisted ``ecr:BatchGetImage`` —
        the SAME op the list and bundle-detail paths already use, introducing no
        new call type — and returns ``images[0].imageManifest`` parsed to a
        ``dict``.

        Degrade-don't-crash, mirroring :meth:`_fetch_manifest`:
        a missing identifier (so AWS is never called with ``None``) or ANY
        ``ClientError``/``BotoCoreError`` returns ``None``, as does any malformed
        response shape (via :func:`manifest_dict_from_response`) — this NEVER
        raises. The returned manifest is fully producer-controlled; the caller
        routes it through the render-safety choke point before any terminal write.
        """
        if not (registry_id and repository_name and manifest_digest):
            return None
        try:
            response = self._client.batch_get_image(
                registryId=str(registry_id),
                repositoryName=str(repository_name),
                imageIds=[{"imageDigest": str(manifest_digest)}],
            )
        except (ClientError, BotoCoreError):
            return None
        return manifest_dict_from_response(response)

    @staticmethod
    def _build_referrer(ref: dict[str, Any]) -> Referrer:
        """Build a classified :class:`Referrer` from one Referrers API entry."""
        artifact_type = str(ref.get("artifactType") or "")
        digest = ref.get("imageDigest") or ref.get("digest")
        tags = ref.get("imageTags") or []
        tag = str(tags[0]) if tags else None
        # Guard the type: a non-mapping ``annotations`` (e.g. a list) must not
        # make ``dict(...)`` raise out of this degradation boundary.
        annotations = ref.get("annotations")
        if not isinstance(annotations, dict):
            annotations = {}
        return Referrer(
            artifact_type=artifact_type,
            digest=str(digest) if digest else "",
            tag=tag,
            annotations=dict(annotations),
        )

    # --- DLQ health (D5) -----------------------------------------------------

    def dlq_health(self) -> list[DlqDepth]:
        """Approximate depth of the four pipeline DLQs — counts only, never consume.

        ``GetQueueAttributes`` needs a QueueUrl, but ``GetQueueUrl`` is NOT on
        the read-only allowlist (it is a non-allowlisted op and would widen the
        client's surface). So we CONSTRUCT the URL deterministically from the
        region + account id + queue name — the standard SQS URL shape — keeping
        every call inside the allowlist. The account id comes from the ECR
        ``registryId`` (resolved/cached here if not already seen). If no account
        id can be resolved (e.g. zero repositories), each depth degrades to
        ``None`` rather than raising.
        """
        account_id = self._resolve_account_id()
        depths: list[DlqDepth] = []
        for stage, resource in _DLQ_STAGES:
            queue_name = self._target.dlq_name(resource)
            depths.append(self._dlq_depth(stage, queue_name, account_id))
        return depths

    def _dlq_depth(self, stage: str, queue_name: str, account_id: str | None) -> DlqDepth:
        if account_id is None:
            return DlqDepth(stage=stage, queue_name=queue_name, approximate_messages=None)
        # Deterministic SQS URL — see dlq_health for why GetQueueUrl is avoided.
        url = f"https://sqs.{self._region}.amazonaws.com/{account_id}/{queue_name}"
        try:
            response = self._client.get_queue_attributes(
                QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"]
            )
        except (ClientError, BotoCoreError):
            return DlqDepth(stage=stage, queue_name=queue_name, approximate_messages=None)
        count = (response.get("Attributes") or {}).get("ApproximateNumberOfMessages")
        return DlqDepth(
            stage=stage,
            queue_name=queue_name,
            approximate_messages=int(count) if count is not None else None,
        )

    def _resolve_account_id(self) -> str | None:
        """Resolve the AWS account id from ECR ``registryId``, caching the result.

        Calls ``describe_repositories`` once if the id has not already been
        captured (e.g. ``list_repository_names`` was never run). Zero
        repositories -> no id -> None (the caller degrades gracefully).
        """
        if self._account_id is not None:
            return self._account_id
        if self._account_id_resolved:
            return None
        self._account_id_resolved = True
        response = self._client.describe_repositories()
        for repo in response.get("repositories") or []:
            self._capture_registry_id(repo)
            if self._account_id is not None:
                break
        return self._account_id


__all__ = [
    "NOTARY_SIGNATURE_ARTIFACT_TYPE",
    "RESOURCE_ARTIFACT_TYPE",
    "WORM_RECEIPT_ARTIFACT_TYPE",
    "ArtifactSources",
    "BundleDetail",
    "DlqDepth",
    "ImageRef",
    "Referrer",
    "ReferrerListing",
    "alias_tag_of",
    "classify_referrer",
    "classify_unindexed_artifact",
    "manifest_dict_from_response",
]

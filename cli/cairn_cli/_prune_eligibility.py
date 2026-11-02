"""Prune_Candidate eligibility classifier for ``cairn prune``.

Implements retention-lifecycle [RETAIN-004] (expired bundle), [RETAIN-008]
(unarchived manifest), [RETAIN-009] (Retain_Until for a receipt that does not
record it), and [RETAIN-012] (authoritative retention inputs).

Trust model. Producers hold repo-scoped ``ecr:PutImage``, so every ECR referrer
and annotation can be forged. Producers have no S3 write. So:

- The S3 prefix and Metadata_Index key are derived from the deployment's
  org / tenant / project coordinates and the digest — never read from a receipt
  body or an annotation ([RETAIN-012]).
- The authoritative Retain_Until is the pipeline-written S3 receipt copy
  (``{prefix}worm-receipt.json``) and, under ``WORM_Enabled=true``, the Object
  Lock dates of the prefix's object versions. The ECR receipt referrer and its
  annotation are only a cross-check: more than one receipt referrer, or an ECR
  value that disagrees with the S3 copy, refuses the item.
- Every absence that makes an item eligible is proven by a positive not-found
  response (``ImageNotFoundException`` for that digest, a strongly consistent
  ``GetItem`` with no item, an empty ``ListObjectVersions``). Any other error
  refuses the item and names the failed check ([RETAIN-008.05]).

The classifier only reads. Deletion lives in :mod:`cairn_cli.prune`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from cairn_oci import (
    OCI_IMAGE_INDEX_MEDIA_TYPE,
    RETAIN_UNTIL_ANNOTATION,
    WORM_RECEIPT_ARTIFACT_TYPE,
)

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
#: OCI referrers tag schema: the fallback index for subject ``sha256:<hex>``.
_TAG_INDEX_RE = re.compile(r"^sha256-([0-9a-f]{64})$")
#: Strict RFC 3339 UTC: ``Z`` or ``+00:00``, optional fractional seconds.
_RFC3339_UTC_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(?:Z|\+00:00)$"
)
RECEIPT_FILE = "worm-receipt.json"

#: Every exception a boto3 call raises: an AWS error response (ClientError) or
#: a failure with no response, such as a network error, a timeout, or expired
#: credentials (BotoCoreError). A BotoCoreError is never a positive answer.
AWS_ERRORS: tuple[type[Exception], ...] = (ClientError, BotoCoreError)

_IMAGE_NOT_FOUND = "ImageNotFoundException"
_NO_SUCH_KEY = frozenset({"NoSuchKey", "404", "NotFound"})
_NO_LOCK_CONFIG = "NoSuchObjectLockConfiguration"


class ItemClass(StrEnum):
    """The two Prune_Candidate classes ([RETAIN-004], [RETAIN-008])."""

    EXPIRED_BUNDLE = "expired-bundle"
    UNARCHIVED_MANIFEST = "unarchived-manifest"


class CheckError(Exception):
    """An existence or retention check returned something other than a positive answer."""

    def __init__(self, check: str, error: Exception | str) -> None:
        detail = _error_code(error) if isinstance(error, AWS_ERRORS) else str(error)
        super().__init__(f"{check} failed: {detail}")
        self.check = check


def _error_code(error: Exception) -> str:
    """The AWS error code, or the exception class name for a BotoCoreError.

    A BotoCoreError (network failure, timeout, credential error) carries no
    error code, so it never matches a positive not-found code.
    """
    if isinstance(error, ClientError):
        return str(error.response.get("Error", {}).get("Code") or "ClientError")
    return type(error).__name__


def parse_rfc3339_utc(value: object) -> datetime | None:
    """Parse a strict RFC 3339 UTC timestamp; ``None`` for anything else.

    Used for every value that crosses a trust boundary (receipt bodies and ECR
    annotations). Offsets other than UTC, missing seconds, and non-strings are
    rejected rather than guessed.
    """
    if not isinstance(value, str):
        return None
    match = _RFC3339_UTC_RE.match(value)
    if not match:
        return None
    year, month, day, hour, minute, second, frac = match.groups()
    micro = int((frac or "0")[:6].ljust(6, "0"))
    try:
        return datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second), micro, tzinfo=UTC
        )
    except ValueError:
        return None


def format_rfc3339_utc(value: datetime) -> str:
    """Second-precision RFC 3339 UTC (``YYYY-MM-DDTHH:MM:SSZ``)."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Deployment:
    """Deployment coordinates and settings — the only source of derived keys."""

    org_id: str
    tenant_id: str
    project_id: str
    repository_name: str
    bucket: str
    metadata_table: str
    controls_table: str
    worm_enabled: bool
    retention_period_days: int
    window_days: int

    @property
    def project_prefix(self) -> str:
        return f"{self.org_id}/{self.tenant_id}/{self.project_id}/"

    def prefix(self, digest: str) -> str:
        """The bundle's canonical S3 prefix ([WORM-002]), from coordinates + digest."""
        _require_digest(digest)
        return f"{self.project_prefix}{digest}/"

    def index_key(self, digest: str) -> dict[str, dict[str, str]]:
        """The Metadata_Index key ([META-001]): pk ``{tenant}``, sk ``{project}#{digest}``."""
        _require_digest(digest)
        return {"pk": {"S": self.tenant_id}, "sk": {"S": f"{self.project_id}#{digest}"}}


@dataclass(frozen=True)
class Clients:
    ecr: Any
    s3: Any
    ddb: Any


@dataclass
class RunCache:
    """Per-run read cache, created by the caller and passed to :func:`classify`.

    ``named_digests`` maps every digest that a Metadata_Index record of the
    project names to the digest of that record ([RETAIN-008] item 2). It is
    read on first use and kept for the rest of the run. A failed read is not
    cached, so the next item retries it.
    """

    named_digests: dict[str, str] | None = None


#: Metadata_Index attributes that name a digest of the bundle ([RETAIN-008] item 2).
_NAMING_ATTRIBUTES = (
    "manifestDigest",
    "attestationDigest",
    "signatureDigest",
    "attestationSignatureDigest",
)
_NAMING_SET_ATTRIBUTE = "validatedAttestationDigests"


@dataclass(frozen=True)
class S3Version:
    key: str
    version_id: str
    is_delete_marker: bool


@dataclass(frozen=True)
class Classification:
    """The outcome of classifying one digest."""

    digest: str
    eligible: bool
    reason: str
    item_class: ItemClass | None = None
    #: True when a check failed (not a positive answer) rather than the item
    #: being plainly ineligible ([RETAIN-008.05]).
    check_failed: bool = False
    retain_until: datetime | None = None
    ecr_present: bool = False
    #: ECR images to delete before the manifest, in a safe order: referrers-tag
    #: schema indexes first, then referrers children-before-parent.
    referrer_digests: tuple[str, ...] = field(default_factory=tuple)
    s3_versions: tuple[S3Version, ...] = field(default_factory=tuple)


def _require_digest(digest: str) -> None:
    if not DIGEST_RE.fullmatch(digest):
        raise ValueError(f"not a sha256:<64-hex> digest: {digest!r}")


def _refuse(digest: str, reason: str, **kw: Any) -> Classification:
    return Classification(digest=digest, eligible=False, reason=reason, **kw)


def _refuse_unproven_coordinates(digest: str, coordinates_unproven: str) -> Classification:
    """Refuse an ECR-present manifest whose absences rest on unproven coordinates."""
    return _refuse(
        digest,
        f"coordinates unproven: {coordinates_unproven}",
        ecr_present=True,
        check_failed=True,
    )


# ── boto3 boundary (each returns a positive answer or raises CheckError) ────────


def describe_image(
    clients: Clients, dep: Deployment, *, digest: str | None = None, tag: str | None = None
) -> dict[str, Any] | None:
    """``DescribeImages`` for one digest or tag; ``None`` only on ImageNotFoundException."""
    image_id = {"imageDigest": digest} if digest else {"imageTag": tag}
    try:
        resp = clients.ecr.describe_images(repositoryName=dep.repository_name, imageIds=[image_id])
    except AWS_ERRORS as e:
        if _error_code(e) == _IMAGE_NOT_FOUND:
            return None
        raise CheckError(f"ECR DescribeImages {digest or tag}", e) from e
    details = resp.get("imageDetails") or []
    if not details:
        raise CheckError(f"ECR DescribeImages {digest or tag}", "empty response")
    return details[0]


def _get_manifest(clients: Clients, dep: Deployment, digest: str) -> dict[str, Any]:
    try:
        resp = clients.ecr.batch_get_image(
            repositoryName=dep.repository_name,
            imageIds=[{"imageDigest": digest}],
            acceptedMediaTypes=[
                "application/vnd.oci.image.manifest.v1+json",
                OCI_IMAGE_INDEX_MEDIA_TYPE,
                "application/vnd.docker.distribution.manifest.v2+json",
            ],
        )
    except AWS_ERRORS as e:
        raise CheckError(f"ECR BatchGetImage {digest}", e) from e
    images = resp.get("images") or []
    if not images or not images[0].get("imageManifest"):
        raise CheckError(f"ECR BatchGetImage {digest}", f"failures={resp.get('failures')}")
    try:
        manifest = json.loads(images[0]["imageManifest"])
    except json.JSONDecodeError as e:
        raise CheckError(f"ECR BatchGetImage {digest}", f"unparseable manifest: {e}") from e
    if not isinstance(manifest, dict):
        raise CheckError(f"ECR BatchGetImage {digest}", "manifest is not a JSON object")
    manifest.setdefault("mediaType", images[0].get("imageManifestMediaType"))
    return manifest


def list_referrers(clients: Clients, dep: Deployment, subject: str) -> list[dict[str, Any]]:
    """Every OCI referrer of ``subject`` (paginated; boto3 has no paginator)."""
    out: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "repositoryName": dep.repository_name,
            "subjectId": {"imageDigest": subject},
        }
        if token:
            kwargs["nextToken"] = token
        try:
            resp = clients.ecr.list_image_referrers(**kwargs)
        except AWS_ERRORS as e:
            raise CheckError(f"ECR ListImageReferrers {subject}", e) from e
        out.extend(resp.get("referrers") or [])
        token = resp.get("nextToken")
        if not token:
            return out


def referrer_digest(referrer: dict[str, Any]) -> str:
    return str(referrer.get("imageDigest") or referrer.get("digest") or "")


def list_versions(clients: Clients, dep: Deployment, prefix: str) -> list[S3Version]:
    """Every object version AND delete marker under ``prefix`` (paginated to the end)."""
    out: list[S3Version] = []
    kwargs: dict[str, Any] = {"Bucket": dep.bucket, "Prefix": prefix}
    while True:
        try:
            resp = clients.s3.list_object_versions(**kwargs)
        except AWS_ERRORS as e:
            raise CheckError(f"S3 ListObjectVersions {prefix}", e) from e
        for v in resp.get("Versions") or []:
            out.append(S3Version(v["Key"], v["VersionId"], is_delete_marker=False))
        for m in resp.get("DeleteMarkers") or []:
            out.append(S3Version(m["Key"], m["VersionId"], is_delete_marker=True))
        if not resp.get("IsTruncated"):
            return out
        kwargs["KeyMarker"] = resp.get("NextKeyMarker")
        kwargs["VersionIdMarker"] = resp.get("NextVersionIdMarker")


def index_record(clients: Clients, dep: Deployment, digest: str) -> dict[str, Any] | None:
    """Strongly consistent ``GetItem`` on the derived Metadata_Index key."""
    try:
        resp = clients.ddb.get_item(
            TableName=dep.metadata_table, Key=dep.index_key(digest), ConsistentRead=True
        )
    except AWS_ERRORS as e:
        raise CheckError(f"Metadata_Index GetItem {digest}", e) from e
    item = resp.get("Item")
    return item if isinstance(item, dict) else None


def named_digests(clients: Clients, dep: Deployment, cache: RunCache) -> dict[str, str]:
    """Every digest a Metadata_Index record of the project names, mapped to that record.

    One strongly consistent Query of the project's rows in the tenant
    partition, paginated to the end and projected to the digest attributes.
    The map value is the record's ``manifestDigest`` (its sort-key digest
    when the attribute is not yet written). A read error raises
    :class:`CheckError` ([RETAIN-008.05]).
    """
    if cache.named_digests is not None:
        return cache.named_digests
    sk_prefix = f"{dep.project_id}#"
    found: dict[str, str] = {}
    kwargs: dict[str, Any] = {
        "TableName": dep.metadata_table,
        "KeyConditionExpression": "pk = :pk AND begins_with(sk, :sk)",
        "ExpressionAttributeValues": {":pk": {"S": dep.tenant_id}, ":sk": {"S": sk_prefix}},
        "ProjectionExpression": ", ".join(("sk", *_NAMING_ATTRIBUTES, _NAMING_SET_ATTRIBUTE)),
        "ConsistentRead": True,
    }
    while True:
        try:
            resp = clients.ddb.query(**kwargs)
        except AWS_ERRORS as e:
            raise CheckError("Metadata_Index Query", e) from e
        for item in resp.get("Items") or []:
            sk = _ddb_string(item, "sk") or ""
            for d, record in _record_names(item, sk, sk_prefix):
                found.setdefault(d, record)
        if not resp.get("LastEvaluatedKey"):
            cache.named_digests = found
            return found
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _record_names(item: dict[str, Any], sk: str, sk_prefix: str) -> list[tuple[str, str]]:
    """Every ``(named_digest, record_digest)`` pair of one Metadata_Index record.

    A naming attribute that is present must be well formed: a String holding a
    digest, or for ``validatedAttestationDigests`` a String Set of digests.
    Anything else raises :class:`CheckError`, because skipping it would drop a
    name and could make a held manifest look like an orphan ([RETAIN-008.05]).
    """

    def malformed(attr: str) -> CheckError:
        return CheckError("Metadata_Index Query", f"malformed {attr} in {sk}")

    named: list[str] = []
    for name in _NAMING_ATTRIBUTES:
        if name not in item:
            continue
        value = _ddb_string(item, name)
        if value is None or not DIGEST_RE.fullmatch(value):
            raise malformed(name)
        named.append(value)
    if _NAMING_SET_ATTRIBUTE in item:
        raw = item[_NAMING_SET_ATTRIBUTE]
        members = raw.get("SS") if isinstance(raw, dict) else None
        if not isinstance(members, list) or not all(
            isinstance(m, str) and DIGEST_RE.fullmatch(m) for m in members
        ):
            raise malformed(_NAMING_SET_ATTRIBUTE)
        named.extend(members)
    record = _ddb_string(item, "manifestDigest") or sk.removeprefix(sk_prefix)
    if not DIGEST_RE.fullmatch(record):
        raise malformed("sk")
    return [(d, record) for d in (record, *named)]


def _require_repository(clients: Clients, dep: Deployment) -> None:
    """Positive-found check that the derived repository exists."""
    try:
        clients.ecr.describe_repositories(repositoryNames=[dep.repository_name])
    except AWS_ERRORS as e:
        raise CheckError(f"ECR DescribeRepositories {dep.repository_name}", e) from e


def _read_s3_receipt(clients: Clients, dep: Deployment, prefix: str) -> dict[str, Any] | None:
    key = f"{prefix}{RECEIPT_FILE}"
    try:
        resp = clients.s3.get_object(Bucket=dep.bucket, Key=key)
    except AWS_ERRORS as e:
        if _error_code(e) in _NO_SUCH_KEY:
            return None
        raise CheckError(f"S3 GetObject {key}", e) from e
    try:
        doc = json.loads(resp["Body"].read())
    except (ValueError, KeyError, BotoCoreError) as e:
        raise CheckError(f"S3 GetObject {key}", f"unparseable receipt copy: {e}") from e
    if not isinstance(doc, dict):
        raise CheckError(f"S3 GetObject {key}", "receipt copy is not a JSON object")
    return doc


def _object_lock_date(clients: Clients, dep: Deployment, v: S3Version) -> datetime | None:
    try:
        resp = clients.s3.get_object_retention(Bucket=dep.bucket, Key=v.key, VersionId=v.version_id)
    except AWS_ERRORS as e:
        if _error_code(e) == _NO_LOCK_CONFIG:
            return None
        raise CheckError(f"S3 GetObjectRetention {v.key}?versionId={v.version_id}", e) from e
    date = (resp.get("Retention") or {}).get("RetainUntilDate")
    if isinstance(date, datetime):
        return date.astimezone(UTC)
    return parse_rfc3339_utc(date)


def _legal_hold_on(clients: Clients, dep: Deployment, v: S3Version) -> bool:
    try:
        resp = clients.s3.get_object_legal_hold(
            Bucket=dep.bucket, Key=v.key, VersionId=v.version_id
        )
    except AWS_ERRORS as e:
        if _error_code(e) == _NO_LOCK_CONFIG:
            return False
        raise CheckError(f"S3 GetObjectLegalHold {v.key}?versionId={v.version_id}", e) from e
    return (resp.get("LegalHold") or {}).get("Status") == "ON"


def prove_coordinates(clients: Clients, dep: Deployment) -> str | None:
    """Positive-found check that the coordinates address real data (M11, M32).

    The unarchived-manifest class rests on three absences. A wrong coordinate
    would make each absence vacuously true, so before trusting any absence the
    repository must exist, and the project prefix and index partition must each
    hold at least one entry. Returns ``None`` when proven, else the reason.
    """
    try:
        clients.ecr.describe_repositories(repositoryNames=[dep.repository_name])
    except AWS_ERRORS as e:
        return f"ECR repository {dep.repository_name} not found ({_error_code(e)})"
    try:
        listing = clients.s3.list_objects_v2(
            Bucket=dep.bucket, Prefix=dep.project_prefix, MaxKeys=1
        )
    except AWS_ERRORS as e:
        return f"S3 project prefix {dep.project_prefix} unreadable ({_error_code(e)})"
    if not listing.get("KeyCount"):
        return f"no S3 object under project prefix {dep.project_prefix}"
    try:
        rows = clients.ddb.query(
            TableName=dep.metadata_table,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :sk)",
            ExpressionAttributeValues={
                ":pk": {"S": dep.tenant_id},
                ":sk": {"S": f"{dep.project_id}#"},
            },
            Limit=1,
            ConsistentRead=True,
        )
    except AWS_ERRORS as e:
        return f"Metadata_Index partition unreadable ({_error_code(e)})"
    if not rows.get("Count"):
        return f"no Metadata_Index record for {dep.tenant_id}/{dep.project_id}"
    return None


# ── classification ─────────────────────────────────────────────────────────────


def _subject_of(
    manifest: dict[str, Any], image: dict[str, Any]
) -> tuple[str | None, bool, tuple[str, ...]]:
    """Return ``(subject_digest, is_index, schema_tags)`` for a manifest.

    A referrer names its subject in ``subject``. A referrers-tag-schema image
    index names it through its ``sha256-<hex>`` tag. ``schema_tags`` is every
    such tag of an index. Tags are producer-writable, so when an index has
    more than one, ``subject_digest`` is ``None`` and the caller refuses: an
    added tag must not redirect the subject check to an absent digest.
    """
    subject = manifest.get("subject")
    if isinstance(subject, dict) and isinstance(subject.get("digest"), str):
        return subject["digest"], False, ()
    is_index = manifest.get("mediaType") == OCI_IMAGE_INDEX_MEDIA_TYPE
    if not is_index:
        return None, False, ()
    tags = tuple(sorted(t for t in image.get("imageTags") or [] if _TAG_INDEX_RE.fullmatch(t)))
    if len(tags) != 1:
        return None, True, tags
    return f"sha256:{tags[0].removeprefix('sha256-')}", True, tags


def _index_children(digest: str, manifest: dict[str, Any]) -> list[str] | Classification:
    """The ``manifests[]`` digests of an image index, or a refusal if malformed."""
    entries = manifest.get("manifests", [])
    if not isinstance(entries, list):
        return _refuse(digest, "image index manifests[] is not a list", ecr_present=True)
    children: list[str] = []
    for entry in entries:
        child = entry.get("digest") if isinstance(entry, dict) else None
        if not isinstance(child, str) or not DIGEST_RE.fullmatch(child):
            return _refuse(
                digest, "image index manifests[] entry is not a sha256 digest", ecr_present=True
            )
        children.append(child)
    return children


def _deletion_order(clients: Clients, dep: Deployment, root: str) -> list[str]:
    """Every ECR image hanging off ``root``, in safe deletion order.

    Referrers-tag-schema indexes come first (ECR refuses to delete an image an
    index still references), then referrers children-before-parent, so no step
    leaves a referrer without its subject while it still has referrers.
    """
    indexes: list[str] = []
    referrers: list[str] = []
    seen: set[str] = {root}

    def visit(subject: str) -> None:
        index = describe_image(clients, dep, tag=f"sha256-{subject.removeprefix('sha256:')}")
        if index is not None and index.get("imageDigest") not in seen:
            seen.add(index["imageDigest"])
            indexes.append(index["imageDigest"])
        for ref in list_referrers(clients, dep, subject):
            d = referrer_digest(ref)
            if not d or d in seen:
                continue
            seen.add(d)
            visit(d)
            referrers.append(d)

    visit(root)
    return indexes + referrers


def _referrer_with_own_archive(
    clients: Clients, dep: Deployment, digests: tuple[str, ...]
) -> str | None:
    """The refusal reason if any image in ``digests`` is an archived bundle itself.

    ``subject`` is producer-writable, so a producer can push a content manifest
    B with ``subject`` naming bundle A. B then appears in A's referrer tree,
    and deleting A's tree would delete B's ECR parts while B's own S3 copy may
    still be under retention ([RETAIN-012]). The pipeline archives a bundle's
    referrers under the bundle's own prefix, never under the referrer's
    digest, so a referrer with S3 object versions or a Metadata_Index record
    of its own is not a part of A. Each absence is a positive not-found.
    """
    for d in digests:
        prefix = dep.prefix(d)
        if list_versions(clients, dep, prefix):
            return (
                f"referrer {d} has S3 object versions under its own prefix {prefix}; "
                "prune it on its own first"
            )
        if index_record(clients, dep, d) is not None:
            return f"referrer {d} has a Metadata_Index record of its own; prune it on its own first"
    return None


def _pushed_within_window(image: dict[str, Any], dep: Deployment, now: datetime) -> bool:
    pushed = image.get("imagePushedAt")
    if not isinstance(pushed, datetime):
        # Without a push time the window cannot be proven to have elapsed.
        return True
    return now - pushed.astimezone(UTC) < timedelta(days=dep.window_days)


def _classify_expired(
    clients: Clients,
    dep: Deployment,
    digest: str,
    *,
    versions: list[S3Version],
    ecr_present: bool,
    direct_referrers: list[dict[str, Any]],
    referrer_digests: tuple[str, ...],
    now: datetime,
) -> Classification:
    """[RETAIN-004] with the [RETAIN-012] / [RETAIN-009] inputs."""
    prefix = dep.prefix(digest)
    common = {
        "ecr_present": ecr_present,
        "referrer_digests": referrer_digests,
        "s3_versions": tuple(versions),
    }
    receipt = _read_s3_receipt(clients, dep, prefix)
    if receipt is None:
        return _refuse(digest, f"no S3 receipt copy at {prefix}{RECEIPT_FILE}", **common)
    if receipt.get("manifestDigest") != digest or receipt.get("s3KeyPrefix") != prefix:
        return _refuse(digest, "S3 receipt copy names a different bundle or prefix", **common)
    has_s3_value = "retainUntil" in receipt
    s3_retain = parse_rfc3339_utc(receipt.get("retainUntil"))
    if has_s3_value and s3_retain is None:
        return _refuse(digest, "S3 receipt copy retainUntil is not RFC 3339 UTC", **common)

    # [RETAIN-012.02] ECR cross-check: one receipt referrer, agreeing with S3.
    if ecr_present:
        receipts = [
            r for r in direct_referrers if r.get("artifactType") == WORM_RECEIPT_ARTIFACT_TYPE
        ]
        if len(receipts) > 1:
            listed = ", ".join(sorted(referrer_digest(r) for r in receipts))
            return _refuse(digest, f"multiple WORM_Receipt referrers: {listed}", **common)
        if receipts:
            annotations = receipts[0].get("annotations") or {}
            has_ann = RETAIN_UNTIL_ANNOTATION in annotations
            ann = parse_rfc3339_utc(annotations.get(RETAIN_UNTIL_ANNOTATION))
            if has_ann != has_s3_value or (has_ann and ann != s3_retain):
                return _refuse(
                    digest,
                    "ECR WORM_Receipt Retain_Until disagrees with the S3 receipt copy",
                    **common,
                )

    # Object Lock dates and legal holds (WORM only; a delete marker has neither).
    lock_dates: list[datetime] = []
    if dep.worm_enabled:
        for v in versions:
            if v.is_delete_marker:
                continue
            if _legal_hold_on(clients, dep, v):
                return _refuse(
                    digest, f"legal hold ON for {v.key} version {v.version_id}", **common
                )
            date = _object_lock_date(clients, dep, v)
            if date is None:
                return _refuse(
                    digest, f"no Object Lock retention on {v.key} version {v.version_id}", **common
                )
            lock_dates.append(date)

    # [RETAIN-009] receipt without retainUntil.
    retain = s3_retain
    if retain is None:
        if dep.worm_enabled:
            retain = max(lock_dates) if lock_dates else None
        else:
            archived = parse_rfc3339_utc(receipt.get("archivedAt"))
            if archived is not None:
                retain = archived + timedelta(days=dep.retention_period_days)
    if retain is None:
        return _refuse(digest, "cannot establish Retain_Until from the S3 receipt copy", **common)
    authoritative = max([retain, *lock_dates])
    if now < authoritative:
        return _refuse(
            digest,
            f"retention in force until {format_rfc3339_utc(authoritative)}",
            retain_until=authoritative,
            **common,
        )
    archived_referrer = _referrer_with_own_archive(clients, dep, referrer_digests)
    if archived_referrer:
        return _refuse(digest, archived_referrer, retain_until=authoritative, **common)
    return Classification(
        digest=digest,
        eligible=True,
        item_class=ItemClass.EXPIRED_BUNDLE,
        reason=f"Retain_Until {format_rfc3339_utc(authoritative)} has passed",
        retain_until=authoritative,
        **common,
    )


def classify(
    clients: Clients,
    dep: Deployment,
    digest: str,
    *,
    now: datetime,
    coordinates_unproven: str | None,
    cache: RunCache | None = None,
) -> Classification:
    """Classify one digest. Never raises for a failed check; refuses instead.

    ``coordinates_unproven`` is :func:`prove_coordinates`' result: when set,
    the unarchived-manifest class is refused with that reason. ``cache`` is
    shared by every item of one run; without it, reads are cached only for
    this item.
    """
    _require_digest(digest)
    try:
        return _classify(
            clients,
            dep,
            digest,
            now=now,
            coordinates_unproven=coordinates_unproven,
            cache=cache if cache is not None else RunCache(),
        )
    except CheckError as e:
        return _refuse(digest, str(e), check_failed=True)


def _classify(
    clients: Clients,
    dep: Deployment,
    digest: str,
    *,
    now: datetime,
    coordinates_unproven: str | None,
    cache: RunCache,
) -> Classification:
    image = describe_image(clients, dep, digest=digest)
    ecr_present = image is not None
    direct: list[dict[str, Any]] = []
    order: tuple[str, ...] = ()
    versions: list[S3Version] | None = None
    if image is not None:
        manifest = _get_manifest(clients, dep, digest)
        subject, is_index, schema_tags = _subject_of(manifest, image)
        if len(schema_tags) > 1:
            return _refuse(
                digest,
                "image index has more than one referrers-tag-schema tag: " + ", ".join(schema_tags),
                ecr_present=True,
            )
        if subject is not None:
            # A manifest with S3 object versions under its own prefix is an
            # archived bundle whatever its producer-writable subject says, and
            # is classified only as an expired bundle ([RETAIN-008.06]).
            versions = list_versions(clients, dep, dep.prefix(digest))
            if not versions:
                return _classify_orphan(
                    clients,
                    dep,
                    digest,
                    image=image,
                    subject=subject,
                    index_manifest=manifest if is_index else None,
                    now=now,
                    coordinates_unproven=coordinates_unproven,
                    cache=cache,
                )
        elif is_index:
            return _refuse(
                digest, "image index that is not a referrers-tag-schema index", ecr_present=True
            )
        direct = list_referrers(clients, dep, digest)
        order = tuple(_deletion_order(clients, dep, digest))

    if versions is None:
        versions = list_versions(clients, dep, dep.prefix(digest))
    if versions:
        return _classify_expired(
            clients,
            dep,
            digest,
            versions=versions,
            ecr_present=ecr_present,
            direct_referrers=direct,
            referrer_digests=order,
            now=now,
        )
    if image is not None:
        return _classify_unarchived(
            clients,
            dep,
            digest,
            image=image,
            direct=direct,
            now=now,
            coordinates_unproven=coordinates_unproven,
        )
    # ECR and S3 parts are gone. A Metadata_Index record left by an interrupted
    # prune is the last step of [RETAIN-010] and resumes as an expired bundle,
    # but only on positive evidence (see _classify_index_remnant).
    row = index_record(clients, dep, digest)
    if row is not None:
        return _classify_index_remnant(clients, dep, digest, row=row, now=now)
    return _refuse(digest, "nothing to prune: no ECR manifest, S3 object, or index record")


def _ddb_string(row: dict[str, Any], name: str) -> str | None:
    value = row.get(name)
    if isinstance(value, dict) and isinstance(value.get("S"), str):
        return value["S"]
    return None


def _classify_index_remnant(
    clients: Clients,
    dep: Deployment,
    digest: str,
    *,
    row: dict[str, Any],
    now: datetime,
) -> Classification:
    """Index rows left after an interrupted prune deleted every ECR and S3 part.

    The absences of the ECR manifest and the S3 versions are trusted only when
    the coordinates are proven: the repository must exist, and the
    pipeline-written index record must name the derived bucket and prefix as
    the bundle's archive. Retain_Until evidence is the record's archive
    timestamp plus the configured retention period; the item is refused until
    that has passed. The S3 receipt copy is gone, so this is the only evidence
    left, and it can only delay eligibility relative to S3 ([RETAIN-012.03]
    is about S3 showing retention in force, which an empty prefix cannot).
    """
    _require_repository(clients, dep)
    prefix = dep.prefix(digest)
    if (
        _ddb_string(row, "archiveStatus") != "archived"
        or _ddb_string(row, "s3Bucket") != dep.bucket
        or _ddb_string(row, "s3KeyPrefix") != prefix
    ):
        return _refuse(
            digest,
            f"index record does not name {dep.bucket}/{prefix} as its archive; "
            "cannot prove an interrupted prune",
        )
    archived = parse_rfc3339_utc(_ddb_string(row, "archivedAt"))
    if archived is None:
        return _refuse(digest, "index record archivedAt is not RFC 3339 UTC")
    retain = archived + timedelta(days=dep.retention_period_days)
    if now < retain:
        return _refuse(
            digest,
            f"retention in force until {format_rfc3339_utc(retain)} (index record)",
            retain_until=retain,
        )
    return Classification(
        digest=digest,
        eligible=True,
        item_class=ItemClass.EXPIRED_BUNDLE,
        reason=(
            f"Retain_Until {format_rfc3339_utc(retain)} has passed; no ECR manifest or "
            "S3 object version remains; only index records"
        ),
        retain_until=retain,
    )


def _classify_unarchived(
    clients: Clients,
    dep: Deployment,
    digest: str,
    *,
    image: dict[str, Any],
    direct: list[dict[str, Any]],
    now: datetime,
    coordinates_unproven: str | None,
) -> Classification:
    """[RETAIN-008] item 1: a content manifest with no referrer, record, or archive."""
    if direct:
        return _refuse(digest, f"has {len(direct)} referrer(s)", ecr_present=True)
    if _pushed_within_window(image, dep, now):
        return _refuse(digest, f"pushed within the {dep.window_days}-day window", ecr_present=True)
    if coordinates_unproven:
        return _refuse_unproven_coordinates(digest, coordinates_unproven)
    if index_record(clients, dep, digest) is not None:
        return _refuse(digest, "has a Metadata_Index record", ecr_present=True)
    return Classification(
        digest=digest,
        eligible=True,
        item_class=ItemClass.UNARCHIVED_MANIFEST,
        reason="no referrer, no Metadata_Index record, no S3 object",
        ecr_present=True,
    )


def _classify_orphan(
    clients: Clients,
    dep: Deployment,
    digest: str,
    *,
    image: dict[str, Any],
    subject: str,
    index_manifest: dict[str, Any] | None,
    now: datetime,
    coordinates_unproven: str | None,
    cache: RunCache,
) -> Classification:
    """[RETAIN-008] item 2: a referrer or tag-schema index whose subject is gone.

    ``index_manifest`` is the manifest of a tag-schema index, else ``None``.
    The index's tag is producer-writable, so its subject alone does not tie
    it to a bundle. Every digest its ``manifests[]`` lists must also be
    absent from ECR and named by no Metadata_Index record.

    ``subject`` is producer-writable, so a dangling subject alone never makes
    a manifest eligible: an archived content manifest pushed with a forged
    subject would otherwise skip the retention checks ([RETAIN-012]). The
    manifest must also have no S3 object version and no Metadata_Index record
    of its own, and its subject no S3 object version under the subject's
    derived prefix ([RETAIN-008.07]), each proven by a positive not-found
    under proven coordinates. Last, no Metadata_Index record of the project
    may name the manifest's digest or its subject's ([RETAIN-008.08]): the
    pipeline archives an attestation and its signature under the content
    manifest's prefix, so their own prefixes are empty while that bundle is
    still held.
    """
    if _pushed_within_window(image, dep, now):
        return _refuse(digest, f"pushed within the {dep.window_days}-day window", ecr_present=True)
    if not DIGEST_RE.fullmatch(subject):
        return _refuse(digest, "referrer subject is not a sha256 digest", ecr_present=True)
    if describe_image(clients, dep, digest=subject) is not None:
        return _refuse(digest, f"referrer subject {subject} is present", ecr_present=True)
    children: list[str] = []
    if index_manifest is not None:
        listed = _index_children(digest, index_manifest)
        if isinstance(listed, Classification):
            return listed
        children = listed
        for child in children:
            if describe_image(clients, dep, digest=child) is not None:
                return _refuse(
                    digest, f"index lists {child}, which is present in ECR", ecr_present=True
                )
    if coordinates_unproven:
        return _refuse_unproven_coordinates(digest, coordinates_unproven)
    prefix = dep.prefix(digest)
    if list_versions(clients, dep, prefix):
        return _refuse(
            digest,
            f"has S3 object versions under {prefix}; a referrer subject cannot bypass retention",
            ecr_present=True,
        )
    subject_prefix = dep.prefix(subject)
    if list_versions(clients, dep, subject_prefix):
        return _refuse(
            digest,
            f"referrer subject {subject} is archived: S3 object versions under {subject_prefix}",
            ecr_present=True,
        )
    if index_record(clients, dep, digest) is not None:
        return _refuse(digest, "has a Metadata_Index record", ecr_present=True)
    named = named_digests(clients, dep, cache)
    if digest in named:
        return _refuse(
            digest, f"{digest} is named by the index record of {named[digest]}", ecr_present=True
        )
    if subject in named:
        return _refuse(
            digest,
            f"subject {subject} is named by the index record of {named[subject]}",
            ecr_present=True,
        )
    for child in children:
        if child in named:
            return _refuse(
                digest,
                f"index lists {child}, which is named by the index record of {named[child]}",
                ecr_present=True,
            )
    return Classification(
        digest=digest,
        eligible=True,
        item_class=ItemClass.UNARCHIVED_MANIFEST,
        reason=f"subject {subject} is absent from the repository; no S3 object or index record",
        ecr_present=True,
    )


def project_digests(clients: Clients, dep: Deployment) -> list[str]:
    """Every digest the report must classify ([RETAIN-005] item 1).

    The union of every ECR image, every S3 bundle prefix that holds any object
    version or delete marker (``ListObjectVersions``, so a prefix with only
    noncurrent versions is not missed), and every Metadata_Index record of the
    project. The S3 and index sides catch a bundle whose ECR parts, or ECR and
    S3 parts, an interrupted prune already deleted ([RETAIN-010]).
    """
    found: set[str] = set()
    try:
        for page in clients.ecr.get_paginator("list_images").paginate(
            repositoryName=dep.repository_name
        ):
            for image_id in page.get("imageIds") or []:
                d = image_id.get("imageDigest")
                if d and DIGEST_RE.fullmatch(d):
                    found.add(d)
    except AWS_ERRORS as e:
        raise CheckError("ECR ListImages", e) from e
    try:
        for page in clients.s3.get_paginator("list_object_versions").paginate(
            Bucket=dep.bucket, Prefix=dep.project_prefix, Delimiter="/"
        ):
            for cp in page.get("CommonPrefixes") or []:
                d = cp["Prefix"][len(dep.project_prefix) :].rstrip("/")
                if DIGEST_RE.fullmatch(d):
                    found.add(d)
    except AWS_ERRORS as e:
        raise CheckError("S3 ListObjectVersions", e) from e
    found.update(_index_digests(clients, dep))
    return sorted(found)


def _index_digests(clients: Clients, dep: Deployment) -> set[str]:
    """Every digest with a Metadata_Index record in the project (paginated)."""
    sk_prefix = f"{dep.project_id}#"
    found: set[str] = set()
    kwargs: dict[str, Any] = {
        "TableName": dep.metadata_table,
        "KeyConditionExpression": "pk = :pk AND begins_with(sk, :sk)",
        "ExpressionAttributeValues": {":pk": {"S": dep.tenant_id}, ":sk": {"S": sk_prefix}},
        "ProjectionExpression": "sk",
        "ConsistentRead": True,
    }
    while True:
        try:
            resp = clients.ddb.query(**kwargs)
        except AWS_ERRORS as e:
            raise CheckError("Metadata_Index Query", e) from e
        for item in resp.get("Items") or []:
            d = (_ddb_string(item, "sk") or "").removeprefix(sk_prefix)
            if DIGEST_RE.fullmatch(d):
                found.add(d)
        if not resp.get("LastEvaluatedKey"):
            return found
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

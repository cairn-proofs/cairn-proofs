"""In-memory fakes at the boto3 boundary for the `cairn prune` tests.

One shared operation log (``World.ops``) records every mutating call across ECR,
S3, DynamoDB, and CloudWatch Logs, so tests can assert the cross-service delete
order ([RETAIN-010]) and the audit-before-delete interleaving ([RETAIN-011]).
Errors are injected per call with ``World.fail(method, key, code)``.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError, EndpointConnectionError
from cairn_cli._prune_eligibility import Clients, Deployment
from cairn_oci import (
    NOTARY_SIGNATURE_ARTIFACT_TYPE,
    OCI_IMAGE_INDEX_MEDIA_TYPE,
    PRODUCER_ATTESTATION_ARTIFACT_TYPE,
    RETAIN_UNTIL_ANNOTATION,
    WORM_RECEIPT_ARTIFACT_TYPE,
    compute_sha256_digest,
)

ACCOUNT = "111111111111"
ORG, TENANT, PROJECT = "mvp-org", "mvp-tenant", "mvp-project"
APP = "cairnproofs4af"
REPO = f"{APP}-artifact-cmh-alpha/{ORG}/{TENANT}/{PROJECT}"
BUCKET = f"{APP}-artifact-archive-cmh-alpha"
METADATA_TABLE = "Stack-MetadataTable-ABC"
CONTROLS_TABLE = "Stack-ControlClaimsTable-DEF"
PRUNER_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{APP}-pruner-cmh-alpha"
PRUNER_SESSION_ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/{APP}-pruner-cmh-alpha/dana"
AUDIT_GROUP = f"/{APP}/cmh-alpha/prune-audit"
NOW = datetime(2034, 1, 1, tzinfo=UTC)
PERIOD = 2555
WINDOW = 30
#: Error code for ``World.fail`` that raises a BotoCoreError (no HTTP response).
NETWORK_ERROR = "<EndpointConnectionError>"


def client_error(code: str, op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


def digest_of(label: str) -> str:
    return compute_sha256_digest(label.encode())


def rfc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Image:
    digest: str
    manifest: dict[str, Any]
    pushed_at: datetime
    tags: list[str] = field(default_factory=list)


@dataclass
class Version:
    key: str
    version_id: str
    body: bytes = b""
    delete_marker: bool = False
    retain_until: datetime | None = None
    legal_hold: bool = False


@dataclass
class World:
    worm: bool = False
    images: dict[str, Image] = field(default_factory=dict)
    versions: list[Version] = field(default_factory=list)
    items: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    ops: list[tuple[str, str]] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)
    errors: dict[tuple[str, str], str] = field(default_factory=dict)
    caller_arn: str = PRUNER_SESSION_ARN
    params: dict[str, str] = field(default_factory=dict)
    assume_calls: list[dict[str, Any]] = field(default_factory=list)
    _vid: int = 0

    def __post_init__(self) -> None:
        self.params = {
            "/cairnproofs/cmh-alpha/app-name": APP,
            f"/{APP}/alpha/tables/metadata": METADATA_TABLE,
            f"/{APP}/alpha/tables/control-claims": CONTROLS_TABLE,
            f"/{APP}/alpha/retention/period-days": str(PERIOD),
            f"/{APP}/alpha/retention/unarchived-manifest-window-days": str(WINDOW),
            f"/{APP}/alpha/prune/pruner-role-arn": PRUNER_ROLE_ARN,
            f"/{APP}/alpha/prune/audit-log-group": AUDIT_GROUP,
        }

    # ── error injection ─────────────────────────────────────────────────
    def fail(self, method: str, key: str, code: str) -> None:
        self.errors[(method, key)] = code

    def _maybe_fail(self, method: str, key: str) -> None:
        code = self.errors.get((method, key)) or self.errors.get((method, "*"))
        if code == NETWORK_ERROR:
            raise EndpointConnectionError(endpoint_url=f"https://{method}.example")
        if code:
            raise client_error(code, method)

    # ── builders ────────────────────────────────────────────────────────
    def add_image(
        self,
        label: str,
        *,
        subject: str | None = None,
        artifact_type: str | None = None,
        annotations: dict[str, str] | None = None,
        pushed_at: datetime | None = None,
        media_type: str = "application/vnd.oci.image.manifest.v1+json",
        tags: list[str] | None = None,
    ) -> str:
        manifest: dict[str, Any] = {"schemaVersion": 2, "mediaType": media_type, "label": label}
        if artifact_type:
            manifest["artifactType"] = artifact_type
        if subject:
            manifest["subject"] = {"digest": subject, "mediaType": media_type, "size": 1}
        if annotations:
            manifest["annotations"] = annotations
        digest = digest_of(json.dumps(manifest, sort_keys=True))
        self.images[digest] = Image(
            digest, manifest, pushed_at or NOW - timedelta(days=400), tags or []
        )
        return digest

    def put_version(self, key: str, body: bytes = b"x", **kw: Any) -> Version:
        self._vid += 1
        v = Version(key=key, version_id=f"v{self._vid}", body=body, **kw)
        self.versions.append(v)
        return v

    def add_bundle(
        self,
        label: str = "bundle",
        *,
        archived_at: datetime = NOW - timedelta(days=PERIOD + 10),
        retain_until: datetime | None | str = "auto",
        annotation: str | None | object = "auto",
        lock_until: datetime | None = None,
        legal_hold: bool = False,
        receipt_in_ecr: bool = True,
        controls: tuple[str, ...] = ("ac-2", "au-4"),
    ) -> str:
        """An archived bundle with the full referrer tree, S3 objects, and index rows."""
        digest = self.add_image(label)
        prefix = f"{ORG}/{TENANT}/{PROJECT}/{digest}/"
        if retain_until == "auto":
            retain_until = archived_at + timedelta(days=PERIOD)
        receipt: dict[str, Any] = {
            "manifestDigest": digest,
            "archivedAt": archived_at.isoformat(),
            "s3Bucket": BUCKET,
            "s3KeyPrefix": prefix,
            "wormEnabled": self.worm,
            "blobCount": 3,
            "integrityVerified": True,
            "schemaVersion": "v1",
        }
        if retain_until is not None:
            receipt["retainUntil"] = rfc(retain_until)  # type: ignore[arg-type]
        lock = (
            lock_until
            if lock_until is not None
            else (retain_until if isinstance(retain_until, datetime) else None)
        )
        for name in ("manifest.json", "attestation.json", "blobs/sha256/aaa"):
            self.put_version(
                prefix + name,
                retain_until=lock if self.worm else None,
                legal_hold=legal_hold,
            )
        self.put_version(
            prefix + "worm-receipt.json",
            json.dumps(receipt).encode(),
            retain_until=lock if self.worm else None,
        )
        att = self.add_image(
            f"{label}-att", subject=digest, artifact_type=PRODUCER_ATTESTATION_ARTIFACT_TYPE
        )
        sig = self.add_image(
            f"{label}-sig", subject=digest, artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE
        )
        self.add_image(
            f"{label}-att-sig", subject=att, artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE
        )
        self.add_image(
            f"{label}-sig-index",
            media_type=OCI_IMAGE_INDEX_MEDIA_TYPE,
            tags=[f"sha256-{digest.removeprefix('sha256:')}"],
        )
        if receipt_in_ecr:
            ann: dict[str, str] = {}
            if annotation == "auto":
                if "retainUntil" in receipt:
                    ann[RETAIN_UNTIL_ANNOTATION] = receipt["retainUntil"]
            elif isinstance(annotation, str):
                ann[RETAIN_UNTIL_ANNOTATION] = annotation
            self.add_image(
                f"{label}-receipt",
                subject=digest,
                artifact_type=WORM_RECEIPT_ARTIFACT_TYPE,
                annotations=ann,
            )
        _ = sig
        self.add_row(digest, archived_at=archived_at)
        for control in controls:
            self.items[(CONTROLS_TABLE, TENANT, f"cat#{control}#{digest}")] = {
                "pk": {"S": TENANT},
                "sk": {"S": f"cat#{control}#{digest}"},
                "manifestDigest": {"S": digest},
            }
        return digest

    def add_row(self, digest: str, *, archived_at: datetime | None = None) -> None:
        """A Metadata_Index row; with ``archived_at``, the archive stage's fields too."""
        row: dict[str, Any] = {"pk": {"S": TENANT}, "sk": {"S": f"{PROJECT}#{digest}"}}
        if archived_at is not None:
            row.update(
                {
                    "archiveStatus": {"S": "archived"},
                    "s3Bucket": {"S": BUCKET},
                    "s3KeyPrefix": {"S": f"{ORG}/{TENANT}/{PROJECT}/{digest}/"},
                    "archivedAt": {"S": rfc(archived_at)},
                }
            )
        self.items[(METADATA_TABLE, TENANT, f"{PROJECT}#{digest}")] = row

    def add_other_project_row(self) -> None:
        # Keeps the index partition non-empty for the coordinate proof.
        self.items[(METADATA_TABLE, TENANT, f"{PROJECT}#sha256:{'f' * 64}")] = {
            "pk": {"S": TENANT},
            "sk": {"S": f"{PROJECT}#sha256:{'f' * 64}"},
        }
        self.put_version(f"{ORG}/{TENANT}/{PROJECT}/sha256:{'f' * 64}/manifest.json")

    # ── views ───────────────────────────────────────────────────────────
    def prefix_versions(self, digest: str) -> list[Version]:
        prefix = f"{ORG}/{TENANT}/{PROJECT}/{digest}/"
        return [v for v in self.versions if v.key.startswith(prefix)]

    def row_present(self, digest: str) -> bool:
        return (METADATA_TABLE, TENANT, f"{PROJECT}#{digest}") in self.items

    def deletes(self) -> list[tuple[str, str]]:
        return [op for op in self.ops if op[0].startswith("delete:")]

    def deployment(self) -> Deployment:
        return Deployment(
            org_id=ORG,
            tenant_id=TENANT,
            project_id=PROJECT,
            repository_name=REPO,
            bucket=BUCKET,
            metadata_table=METADATA_TABLE,
            controls_table=CONTROLS_TABLE,
            worm_enabled=self.worm,
            retention_period_days=PERIOD,
            window_days=WINDOW,
        )

    def clients(self) -> Clients:
        return Clients(ecr=FakeEcr(self), s3=FakeS3(self), ddb=FakeDdb(self))

    def session(self, *, expiry: datetime | None = NOW + timedelta(minutes=50)) -> FakeSession:
        return FakeSession(self, expiry)


class _Paginator:
    def __init__(self, fn: Any) -> None:
        self._fn = fn

    def paginate(self, **kw: Any) -> list[dict[str, Any]]:
        return [self._fn(**kw)]


class FakeEcr:
    def __init__(self, w: World) -> None:
        self.w = w

    def describe_repositories(self, *, repositoryNames: list[str]) -> dict[str, Any]:
        self.w._maybe_fail("describe_repositories", repositoryNames[0])
        if repositoryNames != [REPO]:
            raise client_error("RepositoryNotFoundException")
        return {"repositories": [{"repositoryName": REPO}]}

    def _check_repo(self, name: str) -> None:
        if name != REPO:
            raise client_error("RepositoryNotFoundException")

    def describe_images(self, *, repositoryName: str, imageIds: list[dict]) -> dict:
        self._check_repo(repositoryName)
        ident = imageIds[0]
        key = ident.get("imageDigest") or ident.get("imageTag")
        self.w._maybe_fail("describe_images", key)
        for img in self.w.images.values():
            if img.digest == ident.get("imageDigest") or ident.get("imageTag") in img.tags:
                return {
                    "imageDetails": [
                        {
                            "imageDigest": img.digest,
                            "imageTags": img.tags,
                            "imagePushedAt": img.pushed_at,
                        }
                    ]
                }
        raise client_error("ImageNotFoundException")

    def batch_get_image(self, *, repositoryName: str, imageIds: list[dict], **_: Any) -> dict:
        self._check_repo(repositoryName)
        d = imageIds[0]["imageDigest"]
        self.w._maybe_fail("batch_get_image", d)
        img = self.w.images.get(d)
        if img is None:
            return {"images": [], "failures": [{"failureCode": "ImageNotFound"}]}
        return {
            "images": [
                {
                    "imageManifest": json.dumps(img.manifest),
                    "imageManifestMediaType": img.manifest["mediaType"],
                }
            ]
        }

    def list_image_referrers(self, *, repositoryName: str, subjectId: dict, **_: Any) -> dict:
        self._check_repo(repositoryName)
        subject = subjectId["imageDigest"]
        self.w._maybe_fail("list_image_referrers", subject)
        refs = [
            {
                "digest": img.digest,
                "artifactType": img.manifest.get("artifactType"),
                "annotations": img.manifest.get("annotations") or {},
            }
            for img in self.w.images.values()
            if (img.manifest.get("subject") or {}).get("digest") == subject
        ]
        return {"referrers": refs}

    def batch_delete_image(self, *, repositoryName: str, imageIds: list[dict]) -> dict:
        self._check_repo(repositoryName)
        d = imageIds[0]["imageDigest"]
        self.w._maybe_fail("batch_delete_image", d)
        self.w.ops.append(("delete:ecr", d))
        if self.w.images.pop(d, None) is None:
            return {
                "imageIds": [],
                "failures": [{"imageId": {"imageDigest": d}, "failureCode": "ImageNotFound"}],
            }
        return {"imageIds": [{"imageDigest": d}], "failures": []}

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "list_images"
        return _Paginator(lambda **kw: {"imageIds": [{"imageDigest": d} for d in self.w.images]})


class FakeS3:
    def __init__(self, w: World) -> None:
        self.w = w

    def list_object_versions(self, *, Bucket: str, Prefix: str, **_: Any) -> dict:
        self.w._maybe_fail("list_object_versions", Prefix)
        vs = [v for v in self.w.versions if v.key.startswith(Prefix)]
        return {
            "Versions": [
                {"Key": v.key, "VersionId": v.version_id} for v in vs if not v.delete_marker
            ],
            "DeleteMarkers": [
                {"Key": v.key, "VersionId": v.version_id} for v in vs if v.delete_marker
            ],
            "IsTruncated": False,
        }

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        self.w._maybe_fail("get_object", Key)
        latest = [v for v in self.w.versions if v.key == Key]
        if not latest or latest[-1].delete_marker:
            raise client_error("NoSuchKey")
        return {"Body": io.BytesIO(latest[-1].body)}

    def _version(self, key: str, vid: str) -> Version:
        for v in self.w.versions:
            if v.key == key and v.version_id == vid:
                return v
        raise client_error("NoSuchVersion")

    def get_object_retention(self, *, Bucket: str, Key: str, VersionId: str) -> dict:
        self.w._maybe_fail("get_object_retention", Key)
        v = self._version(Key, VersionId)
        if v.retain_until is None:
            raise client_error("NoSuchObjectLockConfiguration")
        return {"Retention": {"Mode": "COMPLIANCE", "RetainUntilDate": v.retain_until}}

    def get_object_legal_hold(self, *, Bucket: str, Key: str, VersionId: str) -> dict:
        self.w._maybe_fail("get_object_legal_hold", Key)
        v = self._version(Key, VersionId)
        return {"LegalHold": {"Status": "ON" if v.legal_hold else "OFF"}}

    def delete_object(self, *, Bucket: str, Key: str, VersionId: str) -> dict:
        self.w._maybe_fail("delete_object", Key)
        self.w.ops.append(("delete:s3", f"{Key}?{VersionId}"))
        self.w.versions = [
            v for v in self.w.versions if not (v.key == Key and v.version_id == VersionId)
        ]
        return {}

    def list_objects_v2(self, *, Bucket: str, Prefix: str, MaxKeys: int = 1000, **_: Any) -> dict:
        self.w._maybe_fail("list_objects_v2", Prefix)
        keys = sorted(
            {v.key for v in self.w.versions if v.key.startswith(Prefix) and not v.delete_marker}
        )
        return {"KeyCount": min(len(keys), MaxKeys)}

    def get_paginator(self, name: str) -> _Paginator:
        # ListObjectVersions: a prefix with only noncurrent versions or delete
        # markers still appears as a CommonPrefix.
        assert name == "list_object_versions"

        def page(*, Bucket: str, Prefix: str, Delimiter: str) -> dict:
            self.w._maybe_fail("list_object_versions", Prefix)
            prefixes = sorted(
                {
                    Prefix + v.key[len(Prefix) :].split("/", 1)[0] + "/"
                    for v in self.w.versions
                    if v.key.startswith(Prefix)
                }
            )
            return {"CommonPrefixes": [{"Prefix": p} for p in prefixes]}

        return _Paginator(page)

    def get_object_lock_configuration(self, *, Bucket: str) -> dict:
        if not self.w.worm:
            raise client_error("ObjectLockConfigurationNotFoundError")
        return {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}}


class FakeDdb:
    def __init__(self, w: World) -> None:
        self.w = w

    def get_item(self, *, TableName: str, Key: dict, ConsistentRead: bool = False) -> dict:
        assert ConsistentRead, "existence checks must be strongly consistent"
        self.w._maybe_fail("get_item", Key["sk"]["S"])
        item = self.w.items.get((TableName, Key["pk"]["S"], Key["sk"]["S"]))
        return {"Item": item} if item else {}

    def query(self, *, TableName: str, ExpressionAttributeValues: dict, **kw: Any) -> dict:
        self.w._maybe_fail("query", TableName)
        pk = ExpressionAttributeValues[":pk"]["S"]
        rows = [i for (t, p, _s), i in self.w.items.items() if t == TableName and p == pk]
        if ":sk" in ExpressionAttributeValues:
            rows = [
                r for r in rows if r["sk"]["S"].startswith(ExpressionAttributeValues[":sk"]["S"])
            ]
        if ":d" in ExpressionAttributeValues:
            d = ExpressionAttributeValues[":d"]["S"]
            rows = [r for r in rows if r.get("manifestDigest", {}).get("S") == d]
        if kw.get("Limit"):
            rows = rows[: kw["Limit"]]
        return {"Items": rows, "Count": len(rows)}

    def delete_item(self, *, TableName: str, Key: dict) -> dict:
        self.w._maybe_fail("delete_item", Key["sk"]["S"])
        self.w.ops.append(
            ("delete:" + ("index" if TableName == METADATA_TABLE else "claims"), Key["sk"]["S"])
        )
        self.w.items.pop((TableName, Key["pk"]["S"], Key["sk"]["S"]), None)
        return {}


class FakeLogs:
    def __init__(self, w: World) -> None:
        self.w = w

    def create_log_stream(self, *, logGroupName: str, logStreamName: str) -> dict:
        self.w._maybe_fail("create_log_stream", logGroupName)
        assert logGroupName == AUDIT_GROUP
        return {}

    def put_log_events(self, *, logGroupName: str, logStreamName: str, logEvents: list) -> dict:
        record = json.loads(logEvents[0]["message"])
        self.w._maybe_fail("put_log_events", f"{record['step']}/{record['outcome']}")
        self.w._maybe_fail("put_log_events", "*")
        self.w.audit.append(record)
        self.w.ops.append(("audit", f"{record['digest']}:{record['step']}:{record['outcome']}"))
        return {}


class FakeSsm:
    def __init__(self, w: World) -> None:
        self.w = w

    def get_parameter(self, *, Name: str) -> dict:
        if Name not in self.w.params:
            raise client_error("ParameterNotFound")
        return {"Parameter": {"Value": self.w.params[Name]}}


class FakeSts:
    def __init__(self, w: World, caller_arn: str | None = None) -> None:
        self.w = w
        self.caller_arn = caller_arn or w.caller_arn

    def get_caller_identity(self) -> dict:
        return {"Arn": self.caller_arn, "UserId": "AROAEXAMPLE:dana", "Account": ACCOUNT}

    def assume_role(self, **kw: Any) -> dict:
        self.w.assume_calls.append(kw)
        self.w._maybe_fail("assume_role", kw["RoleArn"])
        return {
            "Credentials": {
                "AccessKeyId": "ASIAEXAMPLE",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": NOW + timedelta(seconds=kw["DurationSeconds"]),
            }
        }


@dataclass
class _Creds:
    _expiry_time: datetime | None


class FakeSession:
    def __init__(self, w: World, expiry: datetime | None, *, caller_arn: str | None = None) -> None:
        self.w = w
        self.expiry = expiry
        self.caller_arn = caller_arn
        self.clients_requested: list[str] = []

    def client(self, name: str) -> Any:
        self.clients_requested.append(name)
        if name == "sts":
            return FakeSts(self.w, self.caller_arn)
        return {
            "ecr": FakeEcr,
            "s3": FakeS3,
            "dynamodb": FakeDdb,
            "logs": FakeLogs,
            "ssm": FakeSsm,
        }[name](self.w)

    def get_credentials(self) -> _Creds:
        return _Creds(self.expiry)

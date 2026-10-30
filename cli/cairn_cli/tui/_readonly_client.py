"""Read-only AWS access layer for the Artifact_TUI (hard constraint).

WHY: the operator's own IAM credentials may well permit writes
to the Cairn Proofs's DynamoDB tables, ECR repositories, S3 archive, and
DLQs. The guarantee that *the TUI tool* never issues a mutating or
message-consuming call must NOT rest on reviewer vigilance or "we just don't
call those methods" — it has to live in the STRUCTURE of this access layer
and be testable. This module is the single AWS access surface for the whole
TUI; every read the UI performs goes through ``ReadOnlyAwsClient``.

The invariant is enforced structurally, not by convention:

- Each allowlisted operation is its OWN explicit method that forwards to a
  privately-held boto3 client. There is NO generic passthrough — no
  ``__getattr__``, no ``getattr(client, name)``, no "issue this operation by
  name" dispatcher. There is simply no code path through this class that can
  reach a non-allowlisted operation, regardless of caller credentials.
- DynamoDB is the subtle case: the ``cairn_metadata`` facades
  (``MetadataIndex`` / ``ControlClaims`` / ``IdentityRegistry``) take a
  ``dynamodb_resource`` and internally call ``resource.Table(name).query(...)``
  / ``.get_item(...)`` and (for the Identity_Registry display-name join)
  ``resource.batch_get_item(...)``. To keep the invariant structural *through*
  those facades, the resource exposed here is a thin wrapper
  (``_ReadOnlyResource``) whose ``.Table(name)`` yields a ``_ReadOnlyTable``
  exposing ONLY ``query`` and ``get_item`` and which itself forwards ONLY the
  read-only ``batch_get_item``. A facade (or anything else) that reaches for
  ``put_item`` / ``update_item`` / ``delete_item`` / ``batch_writer`` /
  ``batch_write_item`` gets an ``AttributeError`` — the mutator is structurally
  absent, not merely "not called."

Allowlist (and nothing else):
  DynamoDB  : Query, GetItem, BatchGetItem  (via the read-only resource wrapper;
              BatchGetItem backs the Identity_Registry opaque-id → display-name
              join — a pure read, never a write)
  ECR       : DescribeRepositories, DescribeImages, BatchGetImage, ListImageReferrers
              (image/manifest read; ListImageReferrers is the OCI Referrers read API)
  S3        : GetObject, HeadObject
  SQS       : GetQueueAttributes
  SSM       : GetParameter          (appName discovery — the bootstrap read that
              resolves the deployment's appName before any table/queue name is
              composed; pure read, no mutation)
"""

from __future__ import annotations

from typing import Any

import boto3


class _ReadOnlyTable:
    """A DynamoDB Table surface exposing ONLY ``query`` and ``get_item``.

    Wraps a real boto3 ``Table`` resource and forwards exactly the two
    read operations the ``cairn_metadata`` facades use. Mutating methods
    (``put_item``, ``update_item``, ``delete_item``, ``batch_writer``, ...)
    are structurally absent: there is no attribute for them, so reaching for
    one raises ``AttributeError`` rather than forwarding to AWS.
    """

    __slots__ = ("_table",)

    def __init__(self, table: Any) -> None:
        self._table = table

    def query(self, **kwargs: Any) -> dict[str, Any]:
        return self._table.query(**kwargs)

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        return self._table.get_item(**kwargs)


class _ReadOnlyResource:
    """A DynamoDB resource surface exposing ONLY read operations.

    Mirrors the methods the ``cairn_metadata`` facades call on a boto3 DynamoDB
    resource: ``Table`` (whose returned wrapper is itself read-only) and the
    read-only ``batch_get_item`` (used by ``IdentityRegistry`` to resolve
    opaque ids → display names in one batched read). Both survive the handoff
    into ``MetadataIndex`` / ``ControlClaims`` / ``IdentityRegistry`` without
    exposing any mutator: ``batch_write_item`` and every per-item write are
    structurally absent.
    """

    __slots__ = ("_resource",)

    def __init__(self, resource: Any) -> None:
        self._resource = resource

    def Table(self, name: str) -> _ReadOnlyTable:  # boto3 API name
        return _ReadOnlyTable(self._resource.Table(name))

    def batch_get_item(self, **kwargs: Any) -> dict[str, Any]:
        """Forward the read-only ``BatchGetItem`` (the Identity_Registry join).

        BatchGetItem is purely a read; the write-side ``batch_write_item`` is
        deliberately NOT exposed, so the read-only invariant holds.
        """
        return self._resource.batch_get_item(**kwargs)


class ReadOnlyAwsClient:
    """The single read-only AWS access surface for the Artifact_TUI.

    Exposes ONLY the allowlisted read operations; there is no code path to a
    mutating or message-consuming call. Real boto3 clients/resources are
    constructed lazily so unit tests can inject stubs and never touch AWS;
    when an injected dependency is provided it is used as-is.
    """

    def __init__(
        self,
        *,
        region_name: str | None = None,
        dynamodb_resource: Any | None = None,
        ecr_client: Any | None = None,
        s3_client: Any | None = None,
        sqs_client: Any | None = None,
        ssm_client: Any | None = None,
    ) -> None:
        self._region_name = region_name
        # Stored raw; wrapped/used only through the explicit methods below.
        self._dynamodb_resource_raw = dynamodb_resource
        self._ecr_client = ecr_client
        self._s3_client = s3_client
        self._sqs_client = sqs_client
        self._ssm_client = ssm_client
        # Memoize the read-only resource wrapper so repeated property reads
        # hand back the same object (and we build the boto3 resource once).
        self._readonly_resource: _ReadOnlyResource | None = None

    # --- DynamoDB: a read-only resource the cairn_metadata facades consume ---

    @property
    def dynamodb_resource(self) -> _ReadOnlyResource:
        """A read-only DynamoDB resource wrapper for the metadata facades.

        ``.Table(name)`` yields a surface exposing ONLY ``query`` /
        ``get_item`` — mutators are structurally absent — and the resource
        itself forwards only the read-only ``batch_get_item``. Pass this
        straight into ``MetadataIndex`` / ``ControlClaims`` / ``IdentityRegistry``
        to get read-only behavior.
        """
        if self._readonly_resource is None:
            raw = self._dynamodb_resource_raw
            if raw is None:
                raw = boto3.resource("dynamodb", region_name=self._region_name)
            self._readonly_resource = _ReadOnlyResource(raw)
        return self._readonly_resource

    # --- ECR ---

    def describe_repositories(self, **kwargs: Any) -> dict[str, Any]:
        return self._ecr().describe_repositories(**kwargs)

    def describe_images(self, **kwargs: Any) -> dict[str, Any]:
        return self._ecr().describe_images(**kwargs)

    def batch_get_image(self, **kwargs: Any) -> dict[str, Any]:
        return self._ecr().batch_get_image(**kwargs)

    def list_image_referrers(self, **kwargs: Any) -> dict[str, Any]:
        return self._ecr().list_image_referrers(**kwargs)

    # --- S3 ---

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        return self._s3().head_object(**kwargs)

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        return self._s3().get_object(**kwargs)

    # --- SQS ---

    def get_queue_attributes(self, **kwargs: Any) -> dict[str, Any]:
        return self._sqs().get_queue_attributes(**kwargs)

    # --- SSM (appName discovery only) ---

    def get_parameter(self, **kwargs: Any) -> dict[str, Any]:
        """Read a single SSM parameter — used only for appName discovery.

        Forwarded to satisfy the ``resolve_app_name(ssm_client, ...)`` contract
        so the TUI's bootstrap appName lookup goes through the same structurally
        read-only surface as every other read.
        """
        return self._ssm().get_parameter(**kwargs)

    # --- lazy boto3 client construction (real clients built on first use) ---

    def _ecr(self) -> Any:
        if self._ecr_client is None:
            self._ecr_client = boto3.client("ecr", region_name=self._region_name)
        return self._ecr_client

    def _s3(self) -> Any:
        if self._s3_client is None:
            self._s3_client = boto3.client("s3", region_name=self._region_name)
        return self._s3_client

    def _sqs(self) -> Any:
        if self._sqs_client is None:
            self._sqs_client = boto3.client("sqs", region_name=self._region_name)
        return self._sqs_client

    def _ssm(self) -> Any:
        if self._ssm_client is None:
            self._ssm_client = boto3.client("ssm", region_name=self._region_name)
        return self._ssm_client


__all__ = ["ReadOnlyAwsClient"]

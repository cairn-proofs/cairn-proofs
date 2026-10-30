"""Helpers for parsing the ECR PutImage EventBridge event envelope.

The ``validate-bundle`` Lambda is triggered by the CloudTrail-via-
EventBridge form of the ECR ``PutImage`` API call. The relevant fields
live under ``detail``:

  - ``detail.eventTime``                — RFC 3339 ISO timestamp set by AWS
    (the canonical ``ecrPushedAt`` value, [PIPELINE-009])
  - ``detail.userIdentity``             — AWS-verified push principal
    ([PIPELINE-010], extracted by cairn_validation.extract_authenticated_push_principal)
  - ``detail.requestParameters.repositoryName`` — ECR repo path
  - ``detail.responseElements.image.imageId.imageDigest`` — manifest digest
  - ``detail.recipientAccountId``       — AWS account that received the push
  - ``detail.awsRegion``                — region the push landed in

EventBridge also surfaces ``time`` at the envelope level; the spec
calls out ``detail.eventTime`` as the canonical authenticated push
timestamp, so we prefer it and fall back to ``time`` only when
``eventTime`` is absent (defensive).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class MalformedEcrEvent(ValueError):
    """The EventBridge event payload does not look like an ECR PutImage event."""


@dataclass(frozen=True)
class EcrPushEvent:
    """Structured projection of the fields the handler needs from the event."""

    repository_name: str
    image_digest: str
    registry_id: str
    region: str
    ecr_pushed_at: str  # RFC 3339 ISO 8601 string


def parse_ecr_push_event(event: dict[str, Any]) -> EcrPushEvent:
    """Project the EventBridge ``PutImage`` payload into the fields we need.

    Raises ``MalformedEcrEvent`` if any required field is missing. The
    handler maps that to ``artifact.validation-failed`` with
    ``criterion="event_envelope"``.
    """
    detail = event.get("detail")
    if not isinstance(detail, dict):
        raise MalformedEcrEvent("event.detail missing or not a dict")

    request_params = detail.get("requestParameters") or {}
    response_elems = detail.get("responseElements") or {}
    image = (response_elems.get("image") if isinstance(response_elems, dict) else None) or {}
    image_id = (image.get("imageId") if isinstance(image, dict) else None) or {}

    repository_name = (
        request_params.get("repositoryName") if isinstance(request_params, dict) else None
    )
    image_digest = image_id.get("imageDigest") if isinstance(image_id, dict) else None
    registry_id = (
        request_params.get("registryId") if isinstance(request_params, dict) else None
    ) or detail.get("recipientAccountId")
    region = detail.get("awsRegion") or event.get("region")
    ecr_pushed_at = detail.get("eventTime") or event.get("time")

    missing = [
        name
        for name, value in (
            ("repositoryName", repository_name),
            ("imageDigest", image_digest),
            ("registryId/recipientAccountId", registry_id),
            ("awsRegion", region),
            ("eventTime", ecr_pushed_at),
        )
        if not value
    ]
    if missing:
        raise MalformedEcrEvent(f"ECR PutImage event missing required field(s): {missing}")

    return EcrPushEvent(
        repository_name=str(repository_name),
        image_digest=str(image_digest),
        registry_id=str(registry_id),
        region=str(region),
        ecr_pushed_at=str(ecr_pushed_at),
    )


def ecr_image_uri(*, registry_id: str, region: str, repository_name: str, digest: str) -> str:
    """Canonical ``{acct}.dkr.ecr.{region}.amazonaws.com/{repo}@{digest}`` URI."""
    return f"{registry_id}.dkr.ecr.{region}.amazonaws.com/{repository_name}@{digest}"

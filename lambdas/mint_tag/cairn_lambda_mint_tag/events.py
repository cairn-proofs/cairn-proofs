"""EventBridge emit helpers for artifact.tagged / artifact.tag-failed."""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
from cairn_observability import inject_trace_context

EVENT_SOURCE = "dev.cairnproofs.artifact"
DETAIL_TYPE_TAGGED = "artifact.tagged"
DETAIL_TYPE_TAG_FAILED = "artifact.tag-failed"

_EVENTS_CLIENT: Any = None


def _events() -> Any:
    global _EVENTS_CLIENT
    if _EVENTS_CLIENT is None:
        _EVENTS_CLIENT = boto3.client("events")
    return _EVENTS_CLIENT


def _event_bus_name() -> str:
    name = os.environ.get("EVENT_BUS_NAME")
    if not name:
        raise RuntimeError("EVENT_BUS_NAME env var is required")
    return name


def emit(detail_type: str, detail: dict[str, Any]) -> None:
    # Inject the live W3C trace context into a shallow copy so the caller's
    # `detail` is untouched — the carrier keys (traceparent/baggage) only
    # appear in the serialized payload (D2: flat keys under Detail; D3:
    # inject from the live current context at emit time).
    enriched = inject_trace_context(dict(detail))
    _events().put_events(
        Entries=[
            {
                "Source": EVENT_SOURCE,
                "DetailType": detail_type,
                "Detail": json.dumps(enriched, default=str),
                "EventBusName": _event_bus_name(),
            }
        ]
    )


def emit_tagged(detail: dict[str, Any]) -> None:
    emit(DETAIL_TYPE_TAGGED, detail)


def emit_tag_failed(detail: dict[str, Any]) -> None:
    emit(DETAIL_TYPE_TAG_FAILED, detail)

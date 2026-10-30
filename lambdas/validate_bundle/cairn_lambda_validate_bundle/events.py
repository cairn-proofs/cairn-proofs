"""EventBridge emit helpers for artifact.validated / artifact.validation-failed.

The custom event bus name comes from ``EVENT_BUS_NAME`` (CDK-injected
env var). Source for all pipeline events is ``dev.cairnproofs.artifact``
so a single rule pattern can match the whole namespace.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
from cairn_observability import inject_trace_context

EVENT_SOURCE = "dev.cairnproofs.artifact"

DETAIL_TYPE_VALIDATED = "artifact.validated"
DETAIL_TYPE_VALIDATION_FAILED = "artifact.validation-failed"

_EVENTS_CLIENT: Any = None


class EventEmitError(RuntimeError):
    """A put_events entry was rejected (FailedEntryCount>0) — never silent."""


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
    """PutEvents one event onto the configured custom bus.

    EventBridge returns HTTP 200 with ``FailedEntryCount>0`` (it does NOT
    raise) when an entry is rejected or oversized; surface that as an
    ``EventEmitError`` so a dropped event becomes a Lambda failure rather
    than vanishing silently.
    """
    # Inject the live W3C trace context into a shallow copy so the caller's
    # `detail` is untouched — the carrier keys (traceparent/baggage) only
    # appear in the serialized payload (D2: flat keys under Detail; D3:
    # inject from the live current context at emit time). The carrier is
    # bounded short strings, well under the handler's MAX_EVENT_DETAIL_BYTES
    # ceiling, so injection cannot push a within-ceiling detail over it.
    enriched = inject_trace_context(dict(detail))
    resp = _events().put_events(
        Entries=[
            {
                "Source": EVENT_SOURCE,
                "DetailType": detail_type,
                "Detail": json.dumps(enriched, default=str),
                "EventBusName": _event_bus_name(),
            }
        ]
    )
    if resp.get("FailedEntryCount", 0) > 0:
        entry = (resp.get("Entries") or [{}])[0]
        raise EventEmitError(
            f"put_events dropped {detail_type}: "
            f"{entry.get('ErrorCode')} {entry.get('ErrorMessage')}"
        )


def emit_validated(detail: dict[str, Any]) -> None:
    emit(DETAIL_TYPE_VALIDATED, detail)


def emit_validation_failed(detail: dict[str, Any]) -> None:
    emit(DETAIL_TYPE_VALIDATION_FAILED, detail)

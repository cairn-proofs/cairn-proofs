"""Exponential-backoff waiters for downstream-signal observation.

Integration tests poll DynamoDB rows, SQS DLQ messages, CloudWatch alarm
states, and EventBridge audit events. Each helper waits up to a hard
wall-clock cap with exponential backoff and returns the observed value
(or raises ``TimeoutError`` with the elapsed seconds for triage).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from cairn_metadata import keys as _mkeys


def wait_for[T](
    probe: Callable[[], T | None],
    *,
    timeout_seconds: float,
    initial_interval: float = 1.0,
    max_interval: float = 30.0,
    description: str,
) -> T:
    """Poll ``probe`` until it returns a truthy value or ``timeout_seconds``
    elapses. Raises ``TimeoutError`` with the probe description on expiry.
    """
    start = time.monotonic()
    interval = initial_interval
    last_observation: Any = None
    while True:
        observation = probe()
        if observation:
            return observation
        last_observation = observation
        elapsed = time.monotonic() - start
        if elapsed >= timeout_seconds:
            raise TimeoutError(
                f"timed out after {elapsed:.1f}s waiting for {description} "
                f"(last observation: {last_observation!r})"
            )
        time.sleep(min(interval, timeout_seconds - elapsed))
        interval = min(interval * 2, max_interval)


def fetch_metadata_row(
    ddb_client: Any,
    table_name: str,
    tenant_id: str,
    project_id: str,
    manifest_digest: str,
) -> dict[str, Any] | None:
    """Fetch a Metadata_Index row by its tenant-PK key ([META-001]).

    Keys are built via ``cairn_metadata.keys`` (pk = ``{tenant-id}``,
    sk = ``{project-id}#{manifest-digest}``) so the harness can never drift from
    the canonical schema.
    """
    response = ddb_client.get_item(
        TableName=table_name,
        Key={
            "pk": {"S": _mkeys.partition_key(tenant_id)},
            "sk": {"S": _mkeys.sort_key(project_id, manifest_digest)},
        },
    )
    item = response.get("Item")
    if not item:
        return None
    return item


def wait_for_pipeline_complete(
    ddb_client: Any,
    table_name: str,
    tenant_id: str,
    project_id: str,
    manifest_digest: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Block until the Metadata_Index row reaches the terminal pipeline state.

    The completion gate is ``signingStatus=signed`` AND
    ``archiveStatus=archived``; returns the observed row or raises
    ``TimeoutError`` on expiry.
    """

    def _probe() -> dict[str, Any] | None:
        item = fetch_metadata_row(ddb_client, table_name, tenant_id, project_id, manifest_digest)
        if not item:
            return None
        if (
            item.get("signingStatus", {}).get("S") == "signed"
            and item.get("archiveStatus", {}).get("S") == "archived"
        ):
            return item
        return None

    return wait_for(
        _probe,
        timeout_seconds=timeout_seconds,
        description=(
            f"Metadata_Index row {tenant_id}/{project_id}#{manifest_digest} to reach "
            "signingStatus=signed AND archiveStatus=archived"
        ),
    )


def drain_dlq_messages(
    sqs_client: Any,
    queue_url: str,
    *,
    visibility_timeout: int = 120,
    max_receives: int = 25,
) -> list[dict[str, Any]]:
    """Receive (without deleting) a broad swath of a DLQ in one call.

    Failure-path tests assert the *content* of the rejection message matching a
    specific Manifest_Digest, so the scan must reliably surface that one needle
    even when the validate DLQ is polluted with unrelated messages — e.g. the
    async dead-letter envelopes the validate self-trigger loop deposits when the
    pipeline's own archive/signature ``PutImage`` events fail validation.

    A single ``receive_message`` on a standard queue samples only a random
    subset (often 1-3 messages), so one call easily misses the target. This
    loops ``receive_message`` until the queue yields nothing more (or a receive
    cap is hit), and uses a ``VisibilityTimeout`` longer than the scan so each
    message is seen ONCE per scan and not immediately re-sampled. The serial
    integration suite (``-n 0``) and per-test unique digests make the long
    in-flight window harmless. Returns parsed JSON bodies (``{"raw": body}`` for
    a non-JSON body).
    """
    messages: list[dict[str, Any]] = []
    for _ in range(max_receives):
        response = sqs_client.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=1,
            VisibilityTimeout=visibility_timeout,
            MessageAttributeNames=["All"],
            AttributeNames=["All"],
        )
        batch = response.get("Messages", [])
        if not batch:
            break
        for message in batch:
            body = message.get("Body", "")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {"raw": body}
            messages.append(parsed)
    return messages


def alarm_state(cloudwatch_client: Any, alarm_name: str) -> str:
    response = cloudwatch_client.describe_alarms(AlarmNames=[alarm_name])
    alarms = response.get("MetricAlarms", [])
    if not alarms:
        return "MISSING"
    return alarms[0].get("StateValue", "UNKNOWN")

"""In-band dead-letter delivery for permanent validation failures.

Permanent validation failures route *directly* to the validate DLQ ([PIPELINE-006]:
"route the event directly to the dead letter queue without further retries.
Permanent failures MUST NOT be retried"). Rather than rely on Lambda's
async-invoke dead-letter destination — which only fires *after* the default
two async retries are exhausted, and deposits the raw triggering ECR event with
no failure reason — the handler sends a reason-bearing rejection record to the
DLQ in the same invocation via :func:`send_rejection`.

The queue URL comes from ``VALIDATE_DLQ_URL`` (CDK-injected env var). The lazy
``_sqs()`` client mirrors ``events.py``'s ``_events()`` pattern so tests inject
a fake SQS client the same way they inject the events recorder.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3

_SQS_CLIENT: Any = None


def _sqs() -> Any:
    global _SQS_CLIENT
    if _SQS_CLIENT is None:
        _SQS_CLIENT = boto3.client("sqs")
    return _SQS_CLIENT


def _dlq_url() -> str:
    url = os.environ.get("VALIDATE_DLQ_URL")
    if not url:
        raise RuntimeError("VALIDATE_DLQ_URL env var is required")
    return url


def send_rejection(detail: dict[str, Any]) -> None:
    """Deposit a permanent-failure rejection record on the validate DLQ.

    ``detail`` is the same byte-ceiling-enforced rejection record emitted to
    EventBridge (``manifestDigest`` + bounded ``failedCriteria`` + ``errorKind``),
    so the SQS body inherits the same 256 KB bound. Serialized with
    ``default=str`` to match ``events.emit``'s tolerance for non-JSON-native
    values.
    """
    _sqs().send_message(QueueUrl=_dlq_url(), MessageBody=json.dumps(detail, default=str))

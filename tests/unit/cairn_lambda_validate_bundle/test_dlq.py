"""Unit tests for ``cairn_lambda_validate_bundle.dlq`` in isolation.

These characterize the ``dlq`` module's own seams directly. The handler-driven
suites drive ``dlq.send_rejection`` only through the handler chokepoint (with a
``FakeSqs`` injected via the ``reload_handler`` fixture); they never exercise the
module's own seams:

  - ``_dlq_url()`` raises ``RuntimeError`` when ``VALIDATE_DLQ_URL`` is unset (and
    returns the URL when set);
  - ``send_rejection`` calls ``send_message`` with the right ``QueueUrl`` and a
    JSON-serialized ``MessageBody``;
  - the ``_SQS_CLIENT`` lazy-singleton is built once and is resettable for tests
    (mirrors the ``test_events.py`` ``reset_client`` pattern for ``_EVENTS_CLIENT``).

These run without the handler/``reload_handler`` fixture so they characterize the
module contract directly, the same way ``test_events.py`` characterizes
``events.py``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import cairn_lambda_validate_bundle.dlq as dlq
import pytest


@pytest.fixture(autouse=True)
def reset_client() -> None:
    """Snapshot/restore the lazy SQS singleton per test (xdist + leak safe)."""
    dlq._SQS_CLIENT = None
    yield
    dlq._SQS_CLIENT = None


# --- _dlq_url() ---------------------------------------------------------------


def test_dlq_url_returns_env_value_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://sqs.us-east-2.amazonaws.com/123456789012/cairn-validate-dlq"
    monkeypatch.setenv("VALIDATE_DLQ_URL", url)

    assert dlq._dlq_url() == url


def test_dlq_url_raises_runtime_error_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VALIDATE_DLQ_URL", raising=False)

    with pytest.raises(RuntimeError, match="VALIDATE_DLQ_URL"):
        dlq._dlq_url()


def test_dlq_url_raises_runtime_error_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # An empty string is falsy — treated as "unset", not a valid queue URL.
    monkeypatch.setenv("VALIDATE_DLQ_URL", "")

    with pytest.raises(RuntimeError, match="VALIDATE_DLQ_URL"):
        dlq._dlq_url()


# --- send_rejection() ---------------------------------------------------------


def test_send_rejection_calls_send_message_with_queue_url_and_json_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://sqs.us-east-2.amazonaws.com/123456789012/cairn-validate-dlq"
    monkeypatch.setenv("VALIDATE_DLQ_URL", url)
    fake = MagicMock()
    monkeypatch.setattr(dlq, "_sqs", lambda: fake)

    detail = {
        "manifestDigest": "sha256:" + "a" * 64,
        "errorKind": "malformed_event",
        "failedCriteria": [{"criterion": "a", "field": "x", "message": "boom"}],
    }
    dlq.send_rejection(detail)

    fake.send_message.assert_called_once()
    kwargs = fake.send_message.call_args.kwargs
    assert kwargs["QueueUrl"] == url
    assert json.loads(kwargs["MessageBody"]) == detail


def test_send_rejection_serializes_non_json_native_values_with_default_str(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``json.dumps(..., default=str)`` must not raise on a non-JSON-native value
    (mirrors ``events.emit``'s tolerance) — it stringifies instead."""
    monkeypatch.setenv("VALIDATE_DLQ_URL", "https://sqs.example/q")
    fake = MagicMock()
    monkeypatch.setattr(dlq, "_sqs", lambda: fake)

    class _Weird:
        def __str__(self) -> str:
            return "weird-value"

    dlq.send_rejection({"manifestDigest": "sha256:0", "extra": _Weird()})

    body = json.loads(fake.send_message.call_args.kwargs["MessageBody"])
    assert body["extra"] == "weird-value"


def test_send_rejection_raises_when_dlq_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing ``VALIDATE_DLQ_URL`` propagates the ``RuntimeError`` out of
    ``send_rejection`` (no silent drop of a permanent-failure rejection)."""
    monkeypatch.delenv("VALIDATE_DLQ_URL", raising=False)
    fake = MagicMock()
    monkeypatch.setattr(dlq, "_sqs", lambda: fake)

    with pytest.raises(RuntimeError, match="VALIDATE_DLQ_URL"):
        dlq.send_rejection({"manifestDigest": "sha256:0"})

    fake.send_message.assert_not_called()


# --- _sqs() lazy singleton ----------------------------------------------------


def test_sqs_client_is_lazily_built_once_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    calls: list[str] = []

    def _fake_boto3_client(service: str):
        calls.append(service)
        return sentinel

    monkeypatch.setattr(dlq.boto3, "client", _fake_boto3_client)

    assert dlq._SQS_CLIENT is None
    first = dlq._sqs()
    second = dlq._sqs()

    assert first is sentinel
    assert second is sentinel
    # Built exactly once for the SQS service, then cached on the module global.
    assert calls == ["sqs"]
    assert dlq._SQS_CLIENT is sentinel


def test_sqs_client_resets_to_none_between_tests() -> None:
    """The ``reset_client`` fixture must leave ``_SQS_CLIENT`` cleared so a stale
    client cannot leak across tests (the leak class the conftest reset guards)."""
    assert dlq._SQS_CLIENT is None

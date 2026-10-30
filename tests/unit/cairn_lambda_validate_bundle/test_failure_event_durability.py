"""Tests for durable validation-failure notifications.

The validate-bundle Lambda must not SILENTLY DROP the mandated
``artifact.validation-failed`` EventBridge event for attacker-influenced
manifests. Two compounding mechanisms are guarded:

1. ``handler._emit_failure_event`` puts the full ``failed_criteria`` list
   verbatim into ``detail["failedCriteria"]``. A producer-controlled OCI
   manifest with a huge ``layers[]`` array drives the entry count 1:1 with
   attacker volume, which would push the serialized detail past EventBridge's
   256KB per-entry limit without bounding.
2. ``events.emit`` does ``json.dumps`` + a single ``put_events``. EventBridge
   returns HTTP 200 with ``FailedEntryCount>0`` (it does NOT raise) when an
   entry is rejected, so an oversized failure event could be dropped silently
   absent a response check.

These tests assert the observable contract via behavior, not by importing
internal bounding symbols (``_bound_failed_criteria``, ``MAX_FAILED_CRITERIA``,
``MAX_CRITERION_STR_LEN``, ``EventEmitError``):

- cap/truncation: feed a large input and assert the emitted list is shorter
  than the input AND carries a ``{criterion: "truncated", ...}`` marker.
- string bound: feed an oversized field/message and assert the emitted strings
  are truncated.
- raise-on-failure: stub ``put_events`` to report ``FailedEntryCount>0`` and
  assert ``emit()`` raises ``EventEmitError`` (a ``RuntimeError`` subclass)
  carrying the rejected entry's ErrorCode/ErrorMessage.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

EVENT_BUS_NAME = "cairnproofs4af-artifact-cmh-alpha"


@pytest.fixture(autouse=True)
def reset_events_client() -> None:
    import cairn_lambda_validate_bundle.events as mod

    mod._EVENTS_CLIENT = None
    yield
    mod._EVENTS_CLIENT = None


def _failing_put_events_client(
    error_code: str = "EntrySizeExceeded",
    error_message: str = "Total size of the entries in the request is over the limit.",
) -> MagicMock:
    """A boto3 ``events`` stub whose ``put_events`` mirrors the real AWS contract
    for a rejected entry: HTTP 200, ``FailedEntryCount>0``, per-entry error in
    ``Entries`` — and crucially DOES NOT raise."""
    client = MagicMock()
    client.put_events.return_value = {
        "FailedEntryCount": 1,
        "Entries": [{"ErrorCode": error_code, "ErrorMessage": error_message}],
    }
    return client


# --- 1.1: failedCriteria count is capped + truncation marker appended --------


def test_emit_failure_event_caps_failed_criteria_and_appends_marker(
    reload_handler: Any,
) -> None:
    """A huge ``failed_criteria`` list is bounded in the emitted detail and the
    last entry is a truncation marker recording the omitted count."""
    handler = reload_handler

    input_len = 10_000
    failed = [
        {
            "criterion": "manifest_schema",
            "field": f"layers.{i}.digest",
            "message": "Field required",
        }
        for i in range(input_len)
    ]

    captured: dict[str, Any] = {}

    def _capture(detail: dict[str, Any]) -> None:
        captured["detail"] = detail

    handler.emit_validation_failed = _capture

    handler._emit_failure_event(
        manifest_digest="sha256:" + "0" * 64,
        repository_name="artifact-cmh-alpha/alpha-bank",
        failed_criteria=failed,
        error_kind="malformed_manifest",
    )

    emitted = captured["detail"]["failedCriteria"]
    assert len(emitted) < input_len, (
        f"emitted failedCriteria was not capped: {len(emitted)} entries for an "
        f"{input_len}-entry input (attacker-controlled volume reaches EventBridge verbatim)"
    )

    marker = emitted[-1]
    assert marker.get("criterion") == "truncated", (
        f"last emitted entry is not a truncation marker: {marker!r}"
    )
    # The marker message must record how many were omitted so an operator can
    # see the failure event was bounded rather than fully reported.
    omitted = input_len - (len(emitted) - 1)
    assert str(omitted) in marker.get("message", "") or str(input_len) in marker.get(
        "message", ""
    ), f"truncation marker message does not record the omitted/total count: {marker!r}"


# --- 1.2: per-criterion field/message strings are bounded --------------------


def test_emit_failure_event_bounds_long_criterion_strings(
    reload_handler: Any,
) -> None:
    """An oversized ``field``/``message`` is truncated to the per-string bound
    in the emitted detail."""
    handler = reload_handler

    long_field = "f" * 5000
    long_message = "m" * 5000
    failed = [
        {
            "criterion": "manifest_schema",
            "field": long_field,
            "message": long_message,
        }
    ]

    captured: dict[str, Any] = {}
    handler.emit_validation_failed = lambda detail: captured.__setitem__("detail", detail)

    handler._emit_failure_event(
        manifest_digest="sha256:" + "0" * 64,
        repository_name="artifact-cmh-alpha/alpha-bank",
        failed_criteria=failed,
        error_kind="malformed_manifest",
    )

    emitted_entry = captured["detail"]["failedCriteria"][0]
    # The bound is MAX_CRITERION_STR_LEN = 1024; allow a small margin for
    # any truncation-marker suffix appended to each bounded string.
    assert len(emitted_entry["field"]) <= 1100, (
        f"emitted field string was not bounded: {len(emitted_entry['field'])} chars"
    )
    assert len(emitted_entry["message"]) <= 1100, (
        f"emitted message string was not bounded: {len(emitted_entry['message'])} chars"
    )
    assert len(emitted_entry["field"]) < len(long_field)
    assert len(emitted_entry["message"]) < len(long_message)


# --- 1.3: emit() raises when put_events reports FailedEntryCount > 0 ----------


def test_emit_raises_on_failed_entry_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``FailedEntryCount>0`` response (HTTP 200, no boto exception) is surfaced
    as a raise carrying the rejected entry's ErrorCode/ErrorMessage."""
    import cairn_lambda_validate_bundle.events as events

    monkeypatch.setenv("EVENT_BUS_NAME", EVENT_BUS_NAME)
    error_code = "EntrySizeExceeded"
    error_message = "Total size of the entries in the request is over the limit."
    client = _failing_put_events_client(error_code, error_message)
    monkeypatch.setattr(events, "_events", lambda: client)

    with pytest.raises(RuntimeError) as excinfo:
        events.emit(events.DETAIL_TYPE_VALIDATION_FAILED, {"manifestDigest": "sha256:" + "1" * 64})

    raised = str(excinfo.value)
    assert error_code in raised, (
        f"raised error did not carry the rejected entry's ErrorCode: {raised!r}"
    )
    assert error_message in raised, (
        f"raised error did not carry the rejected entry's ErrorMessage: {raised!r}"
    )


# --- 1.4: BOTH emit_validated and emit_validation_failed raise (shared path) --


@pytest.mark.parametrize("emit_fn_name", ["emit_validated", "emit_validation_failed"])
def test_both_emit_helpers_raise_on_failed_entry_count(
    emit_fn_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``emit_validated`` and ``emit_validation_failed`` both surface a
    ``FailedEntryCount>0`` response as a raise, proving the guard lives in the
    shared ``emit()`` chokepoint."""
    import cairn_lambda_validate_bundle.events as events

    monkeypatch.setenv("EVENT_BUS_NAME", EVENT_BUS_NAME)
    monkeypatch.setattr(events, "_events", lambda: _failing_put_events_client())

    emit_fn = getattr(events, emit_fn_name)

    with pytest.raises(RuntimeError):
        emit_fn({"manifestDigest": "sha256:" + "2" * 64})

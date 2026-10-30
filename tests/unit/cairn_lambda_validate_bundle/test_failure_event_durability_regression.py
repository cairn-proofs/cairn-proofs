"""Regression guards for durable validation-failure notifications.

These tests pin the durability invariants (payload bounding +
raise-on-FailedEntryCount) so the behavior callers rely on cannot silently
regress:

- ``emit()`` returns ``None`` without raising when ``put_events`` reports
  ``FailedEntryCount==0`` (Hypothesis over small valid details). Guards
  ``events.py`` ``emit()``.
- a ``failed_criteria`` list at or below ``MAX_FAILED_CRITERIA`` with in-bound
  strings is emitted verbatim with NO truncation marker appended. Drives
  ``handler._emit_failure_event`` with ``emit_validation_failed`` monkeypatched
  to capture the detail.
- ``_emit_failure_event`` logs the FULL unbounded ``failedCriteria`` via
  ``LOG.warning(..., extra={"failedCriteria": ...})`` even when the EventBridge
  detail is truncated. Guards the log-forensics contract that shares this
  chokepoint.

The mocked ``put_events`` mirrors the real AWS contract: HTTP 200 with a
``FailedEntryCount``/``Entries`` shape, never raising.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

EVENT_BUS_NAME = "cairnproofs4af-artifact-cmh-alpha"


@pytest.fixture(autouse=True)
def reset_events_client() -> None:
    import cairn_lambda_validate_bundle.events as mod

    mod._EVENTS_CLIENT = None
    yield
    mod._EVENTS_CLIENT = None


def _ok_put_events_client() -> MagicMock:
    """A boto3 ``events`` stub whose ``put_events`` mirrors the real AWS
    success contract: HTTP 200, ``FailedEntryCount==0``, one accepted entry."""
    client = MagicMock()
    client.put_events.return_value = {
        "FailedEntryCount": 0,
        "Entries": [{"EventId": "evt-0"}],
    }
    return client


# --- 3.1: emit() returns None (no raise) when FailedEntryCount == 0 -----------


@settings(max_examples=50)
@given(
    detail=st.dictionaries(
        keys=st.text(min_size=1, max_size=16),
        values=st.one_of(
            st.text(max_size=64),
            st.integers(),
            st.booleans(),
        ),
        max_size=6,
    )
)
def test_emit_returns_none_without_raising_on_success(detail: dict[str, Any]) -> None:
    """A ``FailedEntryCount==0`` response leaves ``emit()`` returning ``None`` and
    never raising, for any small valid detail.

    Guards against the raise-on-partial-failure check over-firing on the success
    path. Patching is done per-example via ``MonkeyPatch.context()`` so the
    Hypothesis function-scoped-fixture health check stays satisfied (a
    function-scoped ``monkeypatch`` would not reset between generated inputs).
    """
    import cairn_lambda_validate_bundle.events as events

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("EVENT_BUS_NAME", EVENT_BUS_NAME)
        client = _ok_put_events_client()
        mp.setattr(events, "_events", lambda: client)

        result = events.emit(events.DETAIL_TYPE_VALIDATED, detail)

        assert result is None
        client.put_events.assert_called_once()


# --- 3.2: <= cap, in-bound strings -> verbatim, NO truncation marker ----------


def test_emit_failure_event_at_or_below_cap_is_verbatim_no_marker(
    reload_handler: Any,
) -> None:
    """A ``failed_criteria`` list of length ``MAX_FAILED_CRITERIA`` with in-bound
    strings is emitted verbatim — every criterion preserved, no truncation
    marker appended.
    """
    handler = reload_handler

    input_len = handler.MAX_FAILED_CRITERIA
    assert input_len <= handler.MAX_CRITERION_STR_LEN  # in-bound string sanity
    failed = [
        {
            "criterion": "manifest_schema",
            "field": f"layers.{i}.digest",
            "message": "Field required",
        }
        for i in range(input_len)
    ]

    captured: dict[str, Any] = {}
    handler.emit_validation_failed = lambda detail: captured.__setitem__("detail", detail)

    handler._emit_failure_event(
        manifest_digest="sha256:" + "0" * 64,
        repository_name="artifact-cmh-alpha/alpha-bank",
        failed_criteria=failed,
        error_kind="malformed_manifest",
    )

    emitted = captured["detail"]["failedCriteria"]
    assert len(emitted) == input_len, (
        f"expected all {input_len} criteria emitted verbatim, got {len(emitted)}"
    )
    assert emitted == failed, "criteria were not emitted verbatim at/below the cap"
    assert all(c.get("criterion") != "truncated" for c in emitted), (
        "a truncation marker was appended for an at-cap input that needs none"
    )


# --- 3.3: full unbounded failedCriteria still logged even when detail bounded --


def test_emit_failure_event_logs_full_criteria_when_detail_truncated(
    reload_handler: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log-forensics contract: when the
    EventBridge detail is truncated, ``LOG.warning(..., extra={...})`` still
    carries the FULL unbounded ``failedCriteria`` so the log line remains the
    complete forensic record.
    """
    handler = reload_handler

    input_len = handler.MAX_FAILED_CRITERIA + 25
    assert input_len > handler.MAX_FAILED_CRITERIA  # guarantees detail truncation
    failed = [
        {
            "criterion": "manifest_schema",
            "field": f"layers.{i}.digest",
            "message": "Field required",
        }
        for i in range(input_len)
    ]

    captured: dict[str, Any] = {}
    handler.emit_validation_failed = lambda detail: captured.__setitem__("detail", detail)

    with caplog.at_level(logging.WARNING, logger=handler.LOG.name):
        handler._emit_failure_event(
            manifest_digest="sha256:" + "0" * 64,
            repository_name="artifact-cmh-alpha/alpha-bank",
            failed_criteria=failed,
            error_kind="malformed_manifest",
        )

    # The emitted EventBridge detail is bounded (cap + 1 truncation marker).
    emitted = captured["detail"]["failedCriteria"]
    assert len(emitted) == handler.MAX_FAILED_CRITERIA + 1
    assert emitted[-1].get("criterion") == "truncated"

    # The log record, however, keeps the FULL unbounded list verbatim.
    records = [r for r in caplog.records if getattr(r, "failedCriteria", None) is not None]
    assert records, "no warning log record carried a failedCriteria extra"
    logged = records[-1].failedCriteria
    assert len(logged) == input_len, (
        f"log extra.failedCriteria was truncated to {len(logged)}; the forensic "
        f"record must keep all {input_len} criteria"
    )
    assert logged == failed, "log extra.failedCriteria diverged from the full input"
    assert len(logged) > len(emitted), (
        "log record must carry strictly more criteria than the bounded EventBridge detail"
    )


# --- Gap-fill: exact boundaries of _bound_failed_criteria --------------------
#
# These pin the EXACT boundaries and the pass-through edges of
# _bound_failed_criteria that an off-by-one in the slice/marker logic would
# silently break.


def test_bound_failed_criteria_empty_list_no_marker(reload_handler: Any) -> None:
    """An empty failed-criteria list bounds to an empty list with no marker —
    omitted is 0, so the truncation marker branch must not fire (no IndexError,
    no spurious marker)."""
    handler = reload_handler

    assert handler._bound_failed_criteria([]) == []


def test_bound_failed_criteria_exactly_at_cap_no_marker(reload_handler: Any) -> None:
    """Exactly ``MAX_FAILED_CRITERIA`` entries: every entry preserved, omitted
    == 0, NO marker. The boundary the off-by-one lives on."""
    handler = reload_handler

    cap = handler.MAX_FAILED_CRITERIA
    criteria = [{"criterion": "c", "field": f"f{i}", "message": "m"} for i in range(cap)]

    bounded = handler._bound_failed_criteria(criteria)

    assert len(bounded) == cap
    assert bounded == criteria
    assert all(c.get("criterion") != "truncated" for c in bounded)


def test_bound_failed_criteria_one_over_cap_marker_omits_one(reload_handler: Any) -> None:
    """``MAX_FAILED_CRITERIA + 1`` entries: kept list is the cap, a single
    marker is appended, and the marker records omitted == 1 and the true total.
    The minimal case that must still produce a marker."""
    handler = reload_handler

    cap = handler.MAX_FAILED_CRITERIA
    total = cap + 1
    criteria = [{"criterion": "c", "field": f"f{i}", "message": "m"} for i in range(total)]

    bounded = handler._bound_failed_criteria(criteria)

    # cap real entries + exactly one marker.
    assert len(bounded) == cap + 1
    assert bounded[:cap] == criteria[:cap]
    marker = bounded[-1]
    assert marker["criterion"] == "truncated"
    assert marker["field"] == "failedCriteria"
    # omitted is exactly 1; the true total is reported so an operator can tell
    # the detail was bounded by one entry, not many.
    assert "1 additional" in marker["message"]
    assert str(total) in marker["message"]


def test_bound_failed_criteria_string_exactly_at_limit_not_truncated(
    reload_handler: Any,
) -> None:
    """A string of exactly ``MAX_CRITERION_STR_LEN`` is passed through intact —
    the slice ``v[:MAX_CRITERION_STR_LEN]`` is a no-op at the boundary, so no
    silent off-by-one truncation of an in-bound value."""
    handler = reload_handler

    limit = handler.MAX_CRITERION_STR_LEN
    at_limit = "x" * limit
    criteria = [{"criterion": "c", "field": at_limit, "message": at_limit}]

    bounded = handler._bound_failed_criteria(criteria)

    assert bounded[0]["field"] == at_limit
    assert bounded[0]["message"] == at_limit
    assert len(bounded[0]["field"]) == limit


def test_bound_failed_criteria_string_one_over_limit_truncated(reload_handler: Any) -> None:
    """A string one char over ``MAX_CRITERION_STR_LEN`` is truncated to exactly
    the limit — the first length at which truncation must fire."""
    handler = reload_handler

    limit = handler.MAX_CRITERION_STR_LEN
    over = "y" * (limit + 1)
    criteria = [{"criterion": "c", "field": over, "message": over}]

    bounded = handler._bound_failed_criteria(criteria)

    assert len(bounded[0]["field"]) == limit
    assert len(bounded[0]["message"]) == limit
    assert bounded[0]["field"] == "y" * limit


def test_bound_failed_criteria_non_string_values_pass_through(reload_handler: Any) -> None:
    """Non-string criterion values (ints, lists, dicts, None, bool) pass through
    untouched — the ``isinstance(v, str)`` guard must not coerce or crash on a
    non-string value, and only string values are length-bounded."""
    handler = reload_handler

    long_str = "z" * (handler.MAX_CRITERION_STR_LEN + 100)
    criteria = [
        {
            "criterion": "c",
            "count": 7,
            "loc": ["layers", 3, "digest"],
            "nested": {"k": "v"},
            "flag": True,
            "absent": None,
            "message": long_str,
        }
    ]

    bounded = handler._bound_failed_criteria(criteria)
    entry = bounded[0]

    # Non-string values are preserved by identity/value, untouched.
    assert entry["count"] == 7
    assert entry["loc"] == ["layers", 3, "digest"]
    assert entry["nested"] == {"k": "v"}
    assert entry["flag"] is True
    assert entry["absent"] is None
    # Only the string value is bounded.
    assert len(entry["message"]) == handler.MAX_CRITERION_STR_LEN


# --- Gap-fill: emit() fallback when Entries is missing/empty -----------------


def _failing_no_entries_put_events_client() -> MagicMock:
    """AWS contract corner: ``FailedEntryCount>0`` but ``Entries`` absent. The
    ``(resp.get("Entries") or [{}])[0]`` fallback must not KeyError/IndexError."""
    client = MagicMock()
    client.put_events.return_value = {"FailedEntryCount": 1}
    return client


def test_emit_raises_when_failed_count_but_entries_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``FailedEntryCount>0`` response with NO ``Entries`` key still raises
    ``EventEmitError`` (never silent) — the fallback yields ``{}`` and the
    error message carries ``None`` ErrorCode/ErrorMessage rather than crashing."""
    import cairn_lambda_validate_bundle.events as events

    monkeypatch.setenv("EVENT_BUS_NAME", EVENT_BUS_NAME)
    monkeypatch.setattr(events, "_events", lambda: _failing_no_entries_put_events_client())

    with pytest.raises(events.EventEmitError) as excinfo:
        events.emit(events.DETAIL_TYPE_VALIDATION_FAILED, {"manifestDigest": "sha256:" + "3" * 64})

    # Drops are surfaced even with no per-entry detail; the message names the
    # detail type so the dropped event is identifiable.
    assert events.DETAIL_TYPE_VALIDATION_FAILED in str(excinfo.value)


def test_emit_raises_when_failed_count_but_entries_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``FailedEntryCount>0`` with ``Entries: []`` (empty list, not missing):
    the ``or [{}]`` fallback covers the falsy-empty-list case too — no
    IndexError on ``[0]``."""
    import cairn_lambda_validate_bundle.events as events

    client = MagicMock()
    client.put_events.return_value = {"FailedEntryCount": 1, "Entries": []}
    monkeypatch.setenv("EVENT_BUS_NAME", EVENT_BUS_NAME)
    monkeypatch.setattr(events, "_events", lambda: client)

    with pytest.raises(events.EventEmitError):
        events.emit(events.DETAIL_TYPE_VALIDATED, {"manifestDigest": "sha256:" + "4" * 64})


# --- H1: byte-true ceiling (_enforce_detail_byte_ceiling) --------------------
#
# The count/length caps (_bound_failed_criteria) bound code-point COUNT and
# per-string code-point LENGTH, but emit() serializes via
# json.dumps(detail, default=str) with the default ensure_ascii=True, which
# escapes every non-ASCII code point to \uXXXX / surrogate-pair sequences. A
# single astral code point is one code point under the length cap but six bytes
# after escaping, so a fully count/length-bounded detail can still blow past
# the EventBridge per-entry byte limit. These tests pin the byte-true backstop
# added by H1: measured the SAME way emit() does
# (json.dumps(detail, default=str).encode("utf-8")), the emitted detail is
# always <= MAX_EVENT_DETAIL_BYTES.


def _serialized_byte_len(detail: dict[str, Any]) -> int:
    """Serialized byte length measured exactly as ``events.emit`` measures it."""
    return len(json.dumps(detail, default=str).encode("utf-8"))


def test_emit_failure_event_non_ascii_detail_within_byte_budget(
    reload_handler: Any,
) -> None:
    """End-to-end (H1 motivating gap): a ``failed_criteria`` list whose
    ``field``/``message`` are full-length non-ASCII (mixed BMP + astral) drives
    the COUNT/LENGTH-bounded detail over the byte ceiling because ensure_ascii
    expansion inflates each non-ASCII code point to its ``\\uXXXX`` escape. The
    emitted detail, serialized the SAME way ``emit()`` does, must still be
    ``<= MAX_EVENT_DETAIL_BYTES``, with ``failedCriteria`` collapsed to a single
    summary marker that records the true total count.

    This is the assertion the count-based RED suite lacked: count + per-string
    length caps are necessary but NOT byte-sufficient against non-ASCII.
    """
    handler = reload_handler

    # Mixed BMP ("é", 2 bytes UTF-8 -> 6 bytes as é) and astral
    # ("\U0001F4A3" bomb, 4 bytes UTF-8 -> 12 bytes as a \uXXXX surrogate pair),
    # repeated to the FULL per-string code-point cap so every criterion is
    # in-bound by length yet maximally byte-amplified.
    unit = "é\U0001f4a3"
    blob = (unit * handler.MAX_CRITERION_STR_LEN)[: handler.MAX_CRITERION_STR_LEN]
    assert len(blob) == handler.MAX_CRITERION_STR_LEN  # in-bound by code-point length

    # At-cap count of maximally-amplified entries: count-bounded but oversized.
    total = handler.MAX_FAILED_CRITERIA
    failed = [{"criterion": "c", "field": blob, "message": blob} for _ in range(total)]

    captured: dict[str, Any] = {}
    handler.emit_validation_failed = lambda detail: captured.__setitem__("detail", detail)

    handler._emit_failure_event(
        manifest_digest="sha256:" + "0" * 64,
        repository_name="artifact-cmh-alpha/alpha-bank",
        failed_criteria=failed,
        error_kind="malformed_manifest",
    )

    detail = captured["detail"]

    # Sanity: without the byte ceiling this detail WOULD be oversized — confirm
    # the count/length caps alone did not keep it in budget (the gap H1 closes).
    bounded_only = {
        "manifestDigest": detail["manifestDigest"],
        "repositoryName": detail["repositoryName"],
        "failedCriteria": handler._bound_failed_criteria(failed),
        "errorKind": "malformed_manifest",
    }
    assert _serialized_byte_len(bounded_only) > handler.MAX_EVENT_DETAIL_BYTES, (
        "test premise broken: count/length-bounded non-ASCII detail did not "
        "exceed the byte ceiling, so this case no longer exercises H1"
    )

    # The emitted detail fits the byte budget, measured as emit() measures it.
    assert _serialized_byte_len(detail) <= handler.MAX_EVENT_DETAIL_BYTES

    # failedCriteria collapsed to exactly one marker recording the true total.
    emitted = detail["failedCriteria"]
    assert len(emitted) == 1
    marker = emitted[0]
    assert marker["criterion"] == "truncated"
    assert marker["field"] == "failedCriteria"
    assert str(total) in marker["message"]


def test_enforce_detail_byte_ceiling_collapses_when_over(reload_handler: Any) -> None:
    """Helper unit, over ceiling: a detail whose ``failedCriteria`` serializes
    over ``MAX_EVENT_DETAIL_BYTES`` collapses to exactly one marker echoing
    ``total_criteria``, and the returned detail serializes under the ceiling."""
    handler = reload_handler

    # A failedCriteria list whose serialized size exceeds the ceiling. The
    # helper enforces only the BYTE ceiling — it does not re-apply the count
    # cap — so we size the list directly off the ceiling to guarantee overflow
    # regardless of the configured MAX_EVENT_DETAIL_BYTES. Plain ASCII is enough
    # here; the helper measures the whole serialized detail, not non-ASCII alone.
    big = "q" * handler.MAX_CRITERION_STR_LEN
    entry_bytes = _serialized_byte_len({"criterion": "c", "field": big, "message": big})
    n_entries = handler.MAX_EVENT_DETAIL_BYTES // entry_bytes + 2
    detail: dict[str, Any] = {
        "manifestDigest": "sha256:" + "0" * 64,
        "repositoryName": "artifact-cmh-alpha/alpha-bank",
        "failedCriteria": [
            {"criterion": "c", "field": big, "message": big} for _ in range(n_entries)
        ],
        "errorKind": "malformed_manifest",
    }
    assert _serialized_byte_len(detail) > handler.MAX_EVENT_DETAIL_BYTES  # premise

    total_criteria = 137
    out = handler._enforce_detail_byte_ceiling(detail, total_criteria=total_criteria)

    emitted = out["failedCriteria"]
    assert len(emitted) == 1
    marker = emitted[0]
    assert marker["criterion"] == "truncated"
    assert marker["field"] == "failedCriteria"
    assert str(total_criteria) in marker["message"]
    # The collapsed detail now fits the ceiling.
    assert _serialized_byte_len(out) <= handler.MAX_EVENT_DETAIL_BYTES
    # Non-amplifiable fields are left intact.
    assert out["manifestDigest"] == detail["manifestDigest"]
    assert out["errorKind"] == "malformed_manifest"


def test_enforce_detail_byte_ceiling_passthrough_when_under(reload_handler: Any) -> None:
    """Helper unit, under ceiling = passthrough: a small in-bound detail is
    returned unchanged — failedCriteria untouched, NO marker added. Guards
    against over-eager collapse of a perfectly-deliverable detail."""
    handler = reload_handler

    failed = [
        {"criterion": "manifest_schema", "field": f"layers.{i}.digest", "message": "Field required"}
        for i in range(3)
    ]
    detail: dict[str, Any] = {
        "manifestDigest": "sha256:" + "0" * 64,
        "repositoryName": "artifact-cmh-alpha/alpha-bank",
        "failedCriteria": failed,
        "errorKind": "malformed_manifest",
    }
    assert _serialized_byte_len(detail) <= handler.MAX_EVENT_DETAIL_BYTES  # premise

    out = handler._enforce_detail_byte_ceiling(detail, total_criteria=len(failed))

    # Returned unchanged: same criteria, no marker appended.
    assert out["failedCriteria"] == failed
    assert all(c.get("criterion") != "truncated" for c in out["failedCriteria"])


def test_enforce_detail_byte_ceiling_reads_module_constant(
    reload_handler: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tunability: a detail that fits the DEFAULT ceiling collapses once
    ``MAX_EVENT_DETAIL_BYTES`` is lowered — proving the limit is read from the
    (env-overridable) module constant, not hard-coded. Monkeypatches
    ``handler.MAX_EVENT_DETAIL_BYTES`` low and confirms the same detail that was
    a passthrough at the default now collapses."""
    handler = reload_handler

    failed = [
        {"criterion": "manifest_schema", "field": f"layers.{i}.digest", "message": "Field required"}
        for i in range(3)
    ]

    def _fresh_detail() -> dict[str, Any]:
        return {
            "manifestDigest": "sha256:" + "0" * 64,
            "repositoryName": "artifact-cmh-alpha/alpha-bank",
            "failedCriteria": [dict(c) for c in failed],
            "errorKind": "malformed_manifest",
        }

    # At the default ceiling this detail is a passthrough (no collapse).
    under = handler._enforce_detail_byte_ceiling(_fresh_detail(), total_criteria=len(failed))
    assert under["failedCriteria"] == failed

    # Lower the ceiling below the detail's serialized size; same input now
    # collapses, proving the helper reads the module-level constant.
    detail = _fresh_detail()
    tiny = _serialized_byte_len(detail) - 1
    monkeypatch.setattr(handler, "MAX_EVENT_DETAIL_BYTES", tiny)

    out = handler._enforce_detail_byte_ceiling(detail, total_criteria=len(failed))
    assert len(out["failedCriteria"]) == 1
    assert out["failedCriteria"][0]["criterion"] == "truncated"
    assert str(len(failed)) in out["failedCriteria"][0]["message"]

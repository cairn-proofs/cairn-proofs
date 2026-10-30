"""Tests for the re-seen-digest short-circuit granularity.

The validate stage's re-seen short-circuit keys its skip on the
(content Manifest_Digest, triggering Producer_Attestation_Referrer) pair gated
on the row's terminal-success state (``signingStatus``/``archiveStatus``), not on
the mere existence of a Metadata_Index row with ``validateStatus == "validated"``.
Two situations that share a content digest with an existing row MUST still
PROCESS:

  1. a re-push to retry a bundle parked at a non-terminal state, and
  2. a re-push carrying a genuinely NEW, distinct Producer_Attestation_Referrer.

The seeded Metadata_Index row carries the keys the predicate consults
(``signingStatus``, ``archiveStatus``, ``validatedAttestationDigests``); these
tests assert each gate forces a PROCESS (emitting ``artifact.validated``) rather
than a ``re-seen-digest`` skip.
"""

from __future__ import annotations

import pytest

from tests.unit.cairn_lambda_validate_bundle.conftest import (
    build_bundle,
    seed_metadata_index,
    submit,
)


def test_non_terminal_row_re_push_is_not_skipped(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A content row parked non-terminal (validated but signingStatus=pending /
    archiveStatus=pending after a transient sign failure) MUST be processed on
    re-push so it can progress, NOT skipped. ``_already_validated`` consults the
    terminal-success state, not ``validateStatus`` alone, so the parked row
    advances.
    """
    handler = reload_handler
    bundle = build_bundle()
    seed_metadata_index(
        handler,
        monkeypatch,
        manifest_digest=bundle.manifest_digest,
        record={
            "validateStatus": "validated",
            "signingStatus": "pending",
            "archiveStatus": "pending",
        },
    )
    _attestation, event = submit(handler, bundle=bundle)

    result = handler.lambda_handler(event, lambda_context)

    # The fix processes the bundle (mirrors the success-path assertions).
    assert result is not handler._SKIPPED
    assert handler._test_recorder.detail_types() == ["artifact.validated"]


def test_new_attestation_over_terminal_subject_is_not_skipped(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NEW, distinct Producer_Attestation_Referrer over an already-terminal-success
    content subject MUST be processed ("one content, many attestations"), NOT
    skipped. The dedup unit is the (content digest, Producer_Attestation_Referrer
    digest) pair: ``_already_validated`` consults the triggering attestation
    digest, so a second referrer over the same content digest is validated.
    """
    handler = reload_handler
    bundle = build_bundle()
    # Terminal-success row, but the recorded referrer set holds a DIFFERENT
    # attestation digest than the one this submission will push.
    seed_metadata_index(
        handler,
        monkeypatch,
        manifest_digest=bundle.manifest_digest,
        record={
            "validateStatus": "validated",
            "signingStatus": "signed",
            "archiveStatus": "archived",
            "validatedAttestationDigests": {
                "sha256:0000000000000000000000000000000000000000000000000000000000000000"
            },
        },
    )
    attestation, event = submit(handler, bundle=bundle)
    # Pin the premise: the triggering referrer digest is NEW vs. the recorded set.
    assert attestation.attestation_digest not in {
        "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    }

    result = handler.lambda_handler(event, lambda_context)

    # The fix processes the new attestation (mirrors the success-path assertions).
    assert result is not handler._SKIPPED
    assert handler._test_recorder.detail_types() == ["artifact.validated"]


# ---------------------------------------------------------------------------
# Partial-terminal boundary: the terminal gate is the CONJUNCTION
# signingStatus=signed AND archiveStatus=archived. Each independent gate must
# force a PROCESS when it fails, even with the OTHER gate satisfied AND the
# triggering referrer already a member of the recorded set — so neither gate
# can be dropped without a test going red. These isolate handler.py:314 and :316.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("signing_status", "archive_status"),
    [
        ("signed", "pending"),  # archived gate fails (handler.py:316)
        ("unsigned", "archived"),  # signed gate fails — sign-failed + archived (handler.py:314)
        ("signed", "failed"),  # archived gate fails on a failed archive transition
        ("pending", "archived"),  # signed gate fails — parked-pending sign
    ],
    ids=["signed+pending", "unsigned+archived", "signed+failed", "pending+archived"],
)
def test_partial_terminal_row_is_processed_not_skipped(
    reload_handler,
    lambda_context,
    monkeypatch: pytest.MonkeyPatch,
    signing_status: str,
    archive_status: str,
) -> None:
    """A content row that is validated and whose recorded set CONTAINS the
    triggering referrer but is only PARTIALLY terminal (exactly one of
    signed/archived) MUST be processed, not skipped. Seeding the referrer into the
    set isolates the terminal gate as the sole reason to process: a predicate that
    dropped either status check (or the AND) would wrongly skip here, so each
    parametrization guards one gate."""
    handler = reload_handler
    bundle = build_bundle()
    attestation, event = submit(handler, bundle=bundle)
    # Set membership is satisfied — only the partial-terminal status can force
    # a process. (If the terminal gate were absent, this would skip.)
    seed_metadata_index(
        handler,
        monkeypatch,
        manifest_digest=bundle.manifest_digest,
        record={
            "validateStatus": "validated",
            "signingStatus": signing_status,
            "archiveStatus": archive_status,
            "validatedAttestationDigests": {attestation.attestation_digest},
        },
    )

    result = handler.lambda_handler(event, lambda_context)

    assert result is not handler._SKIPPED
    assert handler._test_recorder.detail_types() == ["artifact.validated"]


# ---------------------------------------------------------------------------
# Set-membership edge: a terminal-success row whose validatedAttestationDigests
# is ABSENT or EMPTY must PROCESS — the triggering referrer is not a member, so
# the (content, referrer)-pair dedup unit does not match. Guards handler.py:318
# (`record.get(...) or ()`), the forward-compat path: an already-terminal bundle
# with no recorded set reprocesses once, and re-records the attestation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "set_value",
    [None, set()],
    ids=["absent", "empty"],
)
def test_terminal_row_with_absent_or_empty_set_is_processed(
    reload_handler,
    lambda_context,
    monkeypatch: pytest.MonkeyPatch,
    set_value: set[str] | None,
) -> None:
    """A terminal-success row whose recorded attestation set is absent or empty
    does NOT match the triggering referrer, so the bundle is PROCESSED (and the
    referrer recorded on the way through). A terminal row with no recorded
    attestation set re-validates its attestations on the next re-push."""
    handler = reload_handler
    bundle = build_bundle()
    record: dict[str, object] = {
        "validateStatus": "validated",
        "signingStatus": "signed",
        "archiveStatus": "archived",
    }
    if set_value is not None:
        record["validatedAttestationDigests"] = set_value
    seed_metadata_index(
        handler,
        monkeypatch,
        manifest_digest=bundle.manifest_digest,
        record=record,
    )
    _attestation, event = submit(handler, bundle=bundle)

    result = handler.lambda_handler(event, lambda_context)

    assert result is not handler._SKIPPED
    assert handler._test_recorder.detail_types() == ["artifact.validated"]

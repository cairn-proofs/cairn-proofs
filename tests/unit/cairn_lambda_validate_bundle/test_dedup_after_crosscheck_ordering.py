"""Regression: the re-seen dedup short-circuit is gated behind a PASSING
org+tenant+project cross-check ([PIPELINE-013]; security review finding).

The re-seen / idempotency short-circuit (``_re_seen_terminal_duplicate``) keys
on the tenant-PK schema using the producer-claimed ``tenantId`` / ``projectId``.
If it ran on UNVERIFIED producer-asserted coordinates (before the cross-check),
a tenant-A producer could set ``claim.tenantId = B`` plus a guessed manifest
digest and read skip-vs-DLQ as a boolean cross-tenant existence oracle. The
handler therefore runs ``crosscheck_identity`` (the ``if failed_criteria`` gate)
BEFORE the dedup lookup, so dedup keys only on cross-check-verified coordinates.

These tests pin that ordering:

1. A claim whose coordinates mismatch the IAM-enforced path NEVER reaches the
   dedup lookup — the cross-check rejects it to a permanent failure first.
2. A legitimately re-delivered bundle whose claim MATCHES the path still
   short-circuits as a re-seen skip (idempotency preserved).
"""

from __future__ import annotations

from tests.unit.cairn_lambda_validate_bundle.conftest import (
    REPO_NAME,
    attestation_claim,
    attestation_push_event,
    build_attestation,
    build_bundle,
)


def _submit(handler, lambda_context, claim_bytes):
    bundle = build_bundle(repo_name=REPO_NAME)
    handler._test_fake_ecr.add_bundle(bundle)
    attestation = build_attestation(
        subject_digest=bundle.manifest_digest,
        repo_name=REPO_NAME,
        claim_bytes=claim_bytes,
    )
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)
    return handler.lambda_handler(event, lambda_context)


def test_mismatched_claim_never_reaches_dedup_lookup(
    reload_handler, lambda_context, monkeypatch
) -> None:
    """A cross-tenant-probing claim (mismatched tenant/project) must be rejected
    by the cross-check BEFORE the dedup predicate is consulted — closing the
    existence oracle. We assert the dedup function is never called."""
    handler = reload_handler

    def _boom(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError(
            "_re_seen_terminal_duplicate was reached on an unverified claim — "
            "cross-tenant existence oracle re-opened"
        )

    monkeypatch.setattr(handler, "_re_seen_terminal_duplicate", _boom)

    result = _submit(
        handler,
        lambda_context,
        attestation_claim(tenant_id="tnt-victim", project_id="prj-victim"),
    )

    assert result is handler._REJECTED
    # No skip was recorded; the dedup path was never taken.
    assert not handler._test_recorder.details("artifact.validation-skipped")


def test_verified_duplicate_still_short_circuits(
    reload_handler, lambda_context, monkeypatch
) -> None:
    """Idempotency preserved: a claim whose coordinates MATCH the path reaches
    the dedup lookup, and a genuine re-seen terminal duplicate short-circuits to
    a skip (no re-processing side effects)."""
    handler = reload_handler

    calls: list[tuple] = []

    def _seen(*, content_digest, tenant_id, project_id, attestation_referrer_digest):
        calls.append((content_digest, tenant_id, project_id, attestation_referrer_digest))
        return True

    monkeypatch.setattr(handler, "_re_seen_terminal_duplicate", _seen)

    result = _submit(handler, lambda_context, attestation_claim())

    # The dedup predicate WAS consulted (claim matched the path) and reported a
    # re-seen duplicate, so the handler short-circuited before emitting a normal
    # artifact.validated event.
    assert calls, "verified duplicate must reach the dedup lookup"
    assert result is not handler._REJECTED
    assert not handler._test_recorder.details("artifact.validated")

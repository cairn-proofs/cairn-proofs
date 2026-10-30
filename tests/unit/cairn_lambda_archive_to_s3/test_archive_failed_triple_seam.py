"""Cross-wave seam: the ``artifact.archive-failed`` event MUST carry the opaque
identity triple the tenant-PK ``update_metadata`` consumer needs to address the
row ([META-001]; failure-path events).

The actor-model tenancy re-key made ``tenantId`` the Metadata_Index partition
key and ``projectId`` the sort-key sub-hierarchy, so EVERY post-validated event
that mutates a row has to thread the producer claim (the
``orgId``/``tenantId``/``projectId`` triple) for the consumer to compute the
``(pk, sk)``. The sign stage's ``artifact.signing-failed`` emit was updated to
thread ``attestation`` for exactly this reason (see
``sign_bundle/handler.py`` — "Thread the signed claim forward so update_metadata
can source the orgId/tenantId/projectId triple"); the success
``artifact.archived`` emit threads both the top-level triple AND ``attestation``.

The consumer is ``update_metadata._handle_archive_failed`` ->
``_conditional_update``, which sources the triple via
``cairn_oci.identity_triple(detail)`` (the DEFAULT call — reads
``detail["attestation"]``) and raises ``ValueError`` when ``tenantId`` /
``projectId`` are absent, so ``archiveStatus`` never advances to ``"failed"``
and the update_metadata Lambda errors out to its own retry/DLQ.

This guard drives the REAL ``archive_to_s3`` emit (so it cannot drift from a
hand-built fixture) and asserts the consumer's exact extractor can resolve the
row key from the emitted detail.

NOTE: this is ``xfail(strict=True)`` because ``archive_to_s3``'s
``emit_archive_failed`` (handler.py ~line 554) omits ``attestation`` / the
triple. Once the emit threads the claim (mirroring ``signing-failed``), the
strict-xfail flips to xpass and FAILS the gate, forcing this marker's removal.
"""

from __future__ import annotations

import pytest
from cairn_oci import identity_triple

from tests.unit.cairn_lambda_archive_to_s3.conftest import build_bundle, signed_event


def _drive_real_archive_failed_detail(handler, lambda_context) -> dict:
    """Drive a genuine ``artifact.archive-failed`` emit and return its detail.

    Tampers the config blob so ``verify_digest`` raises ``IntegrityError`` —
    the same permanent-failure chokepoint production routes through — then
    captures exactly what the handler put on the bus (drift-proof against the
    real emit payload).
    """
    bundle = build_bundle()
    handler._test_fake_ecr.add_bundle(bundle)
    handler._test_fake_ecr.blobs[bundle.config_digest] = b"tampered-config-payload"

    from cairn_oci.digest import IntegrityError

    with pytest.raises(IntegrityError):
        handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    failed = handler._test_recorder.details("artifact.archive-failed")
    assert len(failed) == 1, "expected exactly one archive-failed event"
    return failed[0], bundle


def test_archive_failed_emit_is_a_real_archive_failed_event(reload_handler, lambda_context) -> None:
    """Sanity: the harness genuinely produces an archive-failed detail naming the
    offending artifact (so the seam assertions below run against a real emit)."""
    detail, bundle = _drive_real_archive_failed_detail(reload_handler, lambda_context)
    assert detail["manifestDigest"] == bundle.manifest_digest
    assert detail["artifact"] == "config"
    assert detail["expectedDigest"] == bundle.config_digest


def test_archive_failed_event_carries_consumer_resolvable_triple(
    reload_handler, lambda_context
) -> None:
    """The consumer addresses the tenant-PK row via ``identity_triple(detail)``
    (default). The real archive-failed detail must resolve a non-empty
    ``tenantId`` and ``projectId`` so ``archiveStatus="failed"`` can land."""
    detail, bundle = _drive_real_archive_failed_detail(reload_handler, lambda_context)

    # Mirror update_metadata._conditional_update's EXACT extractor call.
    _org_id, tenant_id, project_id = identity_triple(detail)

    assert tenant_id == bundle.tenant_id, (
        "archive-failed detail must carry the tenant id the Metadata_Index "
        "partition key is derived from"
    )
    assert project_id == bundle.project_id, (
        "archive-failed detail must carry the project id the sort-key sub-hierarchy is derived from"
    )

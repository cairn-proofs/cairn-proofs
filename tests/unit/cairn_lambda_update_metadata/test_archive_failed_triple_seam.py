"""Consumer-side seam guard for the failure-path identity triple (gap-fill area 1).

``update_metadata`` addresses the tenant-PK Metadata_Index row from the opaque
identity triple threaded on every post-validated event ([META-001]). The
producer side of this seam — that ``archive_to_s3`` actually
PUTS the triple on the ``artifact.archive-failed`` detail — is guarded in
``tests/unit/cairn_lambda_archive_to_s3/test_archive_failed_triple_seam.py``.

These tests pin the consumer half:

1. Fed a triple-bearing archive-failed event (what the fixed producer must
   emit), the row is marked ``archiveStatus="failed"`` — the desired end state.
2. Fed a triple-LESS archive-failed event (exactly what ``archive_to_s3``
   emits TODAY — manifestDigest / repositoryName / computedDigest /
   expectedDigest / artifact / configBlob / attestationDigest, NO
   ``attestation``), the consumer FAILS CLOSED with a ``ValueError`` and never
   silently writes a partial/mis-keyed row. The loud failure is the symptom of
   the producer-side defect; this guard ensures the consumer never degrades to
   a silent no-op or a wrong-row write.
"""

from __future__ import annotations

import pytest

from tests.unit.cairn_lambda_update_metadata.conftest import (
    archive_failed_event,
    manifest_digest,
    row_key,
    validated_event,
)


def _real_archive_to_s3_archive_failed_event(md: str) -> dict:
    """The ``artifact.archive-failed`` detail shape ``archive_to_s3`` emits today.

    Mirrors ``lambdas/archive_to_s3/cairn_lambda_archive_to_s3/handler.py`` ~line
    554 EXACTLY: no ``attestation`` and no top-level identity triple, so the
    consumer's ``identity_triple(detail)`` resolves nothing.
    """
    return {
        "detail-type": "artifact.archive-failed",
        "source": "dev.cairnproofs.artifact",
        "time": "2026-05-26T12:11:00Z",
        "account": "123456789012",
        "region": "us-east-2",
        "detail": {
            "manifestDigest": md,
            "repositoryName": "artifact-cmh-alpha/org-x/tnt-y/prj-z",
            "computedDigest": "sha256:" + "b" * 64,
            "expectedDigest": "sha256:" + "a" * 64,
            "artifact": "config",
            "configBlob": {},
            "attestationDigest": "sha256:" + "d" * 64,
        },
    }


def test_triple_bearing_archive_failed_marks_row_failed(reload_handler, lambda_context) -> None:
    """The desired end state: a triple-bearing archive-failed event advances the
    tenant-PK row to ``archiveStatus="failed"`` (this is what the fixed producer
    must enable)."""
    handler = reload_handler
    md = manifest_digest("a")
    handler.lambda_handler(validated_event(md=md), lambda_context)

    handler.lambda_handler(archive_failed_event(md=md), lambda_context)

    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert item["archiveStatus"] == "failed"


def test_triple_less_archive_failed_fails_closed_not_silent(reload_handler, lambda_context) -> None:
    """A triple-LESS archive-failed event (today's real archive_to_s3 emit) makes
    the consumer raise rather than silently no-op or mis-key — the row keeps its
    prior ``archiveStatus`` and is never written under a wrong/absent tenant key."""
    handler = reload_handler
    md = manifest_digest("a")
    handler.lambda_handler(validated_event(md=md), lambda_context)

    event = _real_archive_to_s3_archive_failed_event(md)
    with pytest.raises(ValueError, match="tenantId / projectId / manifestDigest"):
        handler.lambda_handler(event, lambda_context)

    # The row was NOT advanced to failed by the triple-less event (fail-closed,
    # not a silent partial write).
    item = handler._test_tables["metadata"].get_item(Key=row_key(md))["Item"]
    assert item["archiveStatus"] == "pending"

"""Authoritative byte-true per-artifact size enforcement at archive-to-s3.

The advisory descriptor-``size`` check at validate-bundle
is spoofable. The binding 10 GiB enforcement lands here, where the blob is already
fetched and digest-verified, so ``len(blob)`` is the TRUE byte count. A violation
must raise an ``IntegrityError`` (subclass) so it routes through the existing
archive-failed / DLQ chokepoint, naming the offending artifact.
"""

from __future__ import annotations

import json

import pytest
from cairn_oci import compute_sha256_digest
from cairn_oci.digest import IntegrityError

from tests.unit.cairn_lambda_archive_to_s3.conftest import (
    ARCHIVE_BUCKET,
    BundleFixture,
    build_bundle,
    signed_event,
)


def _seed(handler, bundle: BundleFixture) -> None:
    handler._test_fake_ecr.add_bundle(bundle)


def test_oversized_true_bytes_rejected_and_routes_to_archive_failed(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blob whose TRUE byte length exceeds the (monkeypatched-small) cap is
    rejected as a permanent integrity failure, emits archive-failed naming the
    offending layer, and re-raises so destination-on-failure DLQ-routes it."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)

    # Shrink the cap below EVERY honest blob's true length so the first
    # archived blob (the config) trips the 10 GiB ceiling without allocating
    # gigabytes. Declared sizes still match true bytes, so this isolates the
    # cap check from the declared-vs-true check.
    monkeypatch.setattr(handler, "MAX_ARTIFACT_SIZE_BYTES", 1)

    with pytest.raises(IntegrityError):
        handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    failed = handler._test_recorder.details("artifact.archive-failed")
    assert len(failed) == 1
    # Config is archived first, so it is the offending artifact named.
    assert failed[0]["artifact"] == "config"
    # computed = true byte count; expected = the breached cap.
    assert failed[0]["computedDigest"] == str(len(bundle.config_bytes))
    assert failed[0]["expectedDigest"] == "1"


def test_oversized_message_is_size_not_digest(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The raised error reads as a byte-size violation (not a digest mismatch)
    yet remains an IntegrityError so existing handling applies unchanged."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)
    monkeypatch.setattr(handler, "MAX_ARTIFACT_SIZE_BYTES", 1)

    with pytest.raises(IntegrityError, match="byte-size violation") as exc:
        handler.lambda_handler(signed_event(bundle=bundle), lambda_context)
    assert "10 GiB" in str(exc.value)
    assert "digest mismatch" not in str(exc.value)
    assert isinstance(exc.value, handler.SizeIntegrityError)


def test_declared_size_vs_true_bytes_mismatch_rejected(reload_handler, lambda_context) -> None:
    """When the descriptor's declared ``size`` disagrees with the blob's true
    byte length (digest still valid), the blob is rejected and archive-failed
    names the offending layer. Catches a producer understating ``size``."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)

    # Rewrite the layer descriptor's declared size to a lie while keeping the
    # digest (and therefore the true bytes) intact. The manifest digest must be
    # recomputed so the manifest itself still passes its own digest check.
    manifest = json.loads(bundle.manifest_raw)
    true_len = len(bundle.layer_bytes)
    manifest["layers"][0]["size"] = true_len + 100
    tampered_raw = json.dumps(manifest, separators=(",", ":"))
    tampered_digest = compute_sha256_digest(tampered_raw.encode("utf-8"))
    handler._test_fake_ecr.manifests[tampered_digest] = (
        manifest,
        tampered_raw,
        "application/vnd.oci.image.manifest.v1+json",
    )

    event = signed_event(bundle=bundle)
    event["detail"]["manifestDigest"] = tampered_digest

    with pytest.raises(IntegrityError, match="byte-size violation") as exc:
        handler.lambda_handler(event, lambda_context)
    assert "declared descriptor size" in str(exc.value)

    failed = handler._test_recorder.details("artifact.archive-failed")
    assert len(failed) == 1
    assert failed[0]["artifact"] == f"layer[{bundle.layer_digest}]"
    assert failed[0]["computedDigest"] == str(true_len)
    assert failed[0]["expectedDigest"] == str(true_len + 100)


def test_true_bytes_exactly_at_limit_accepted(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GAP: byte-true boundary exactness. The authoritative check is ``>`` not
    ``>=`` — a blob whose TRUE byte length equals MAX_ARTIFACT_SIZE_BYTES exactly
    is accepted. Pin the cap to the largest honest true byte length in the bundle
    so the boundary is exercised without allocating gigabytes."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)

    # Both config and layer have declared size == true length; set the cap to
    # the largest archived blob so EVERY blob is exactly at-or-below the limit,
    # with at least one sitting exactly on the boundary. The archive stage also
    # size-checks the Producer_Attestation_Referrer's claim layer + config, so the cap
    # must be the max over ALL archived blobs (the claim layer is the largest
    # here, since artifact-kind is not a content Config_Blob field).
    largest = max(
        len(bundle.config_bytes),
        len(bundle.layer_bytes),
        len(bundle.attestation_layer_bytes),
        len(bundle.attestation_config_bytes),
    )
    monkeypatch.setattr(handler, "MAX_ARTIFACT_SIZE_BYTES", largest)

    payload = handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    assert payload["idempotentShortCircuit"] is False
    assert handler._test_recorder.details("artifact.archive-failed") == []
    assert len(handler._test_recorder.details("artifact.archived")) == 1


def test_true_bytes_smaller_than_declared_size_rejected(reload_handler, lambda_context) -> None:
    """GAP: mismatch direction. The check is ``!=``, so a producer OVERstating the
    descriptor ``size`` (true bytes SMALLER than declared) is rejected too — not
    only the understating direction the existing test covers. ``computed`` is the
    true (smaller) length; ``expected`` is the (larger) declared size."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)

    manifest = json.loads(bundle.manifest_raw)
    true_len = len(bundle.layer_bytes)
    manifest["layers"][0]["size"] = true_len - 5  # producer overstates downward
    tampered_raw = json.dumps(manifest, separators=(",", ":"))
    tampered_digest = compute_sha256_digest(tampered_raw.encode("utf-8"))
    handler._test_fake_ecr.manifests[tampered_digest] = (
        manifest,
        tampered_raw,
        "application/vnd.oci.image.manifest.v1+json",
    )

    event = signed_event(bundle=bundle)
    event["detail"]["manifestDigest"] = tampered_digest

    with pytest.raises(IntegrityError, match="byte-size violation") as exc:
        handler.lambda_handler(event, lambda_context)
    assert "declared descriptor size" in str(exc.value)

    failed = handler._test_recorder.details("artifact.archive-failed")
    assert len(failed) == 1
    assert failed[0]["artifact"] == f"layer[{bundle.layer_digest}]"
    assert failed[0]["computedDigest"] == str(true_len)
    assert failed[0]["expectedDigest"] == str(true_len - 5)


def test_size_integrity_error_is_integrity_error_and_carries_chokepoint_fields() -> None:
    """REGRESSION: the archive-failed chokepoint reads ``.computed`` /
    ``.expected`` / ``.artifact`` off the caught exception (handler ~L537/543)
    and relies on ``except IntegrityError`` catching the size subclass. Pin both
    so a refactor of the exception hierarchy can't silently break emit."""
    import cairn_lambda_archive_to_s3.handler as handler_module

    err = handler_module.SizeIntegrityError(
        message="true byte length 5 exceeds the 1-byte (10 GiB) per-artifact limit",
        computed="5",
        expected="1",
        artifact="layer[sha256:abc]",
    )
    assert isinstance(err, IntegrityError)
    assert err.computed == "5"
    assert err.expected == "1"
    assert err.artifact == "layer[sha256:abc]"
    # Reads as a size violation, never a digest mismatch.
    assert "byte-size violation" in str(err)
    assert "digest mismatch" not in str(err)
    # The artifact label is folded into the message suffix.
    assert "artifact=layer[sha256:abc]" in str(err)


def test_size_integrity_error_without_artifact_has_no_suffix() -> None:
    """REGRESSION: ``artifact=None`` is a valid construction (the bound-breach
    path can omit it); no dangling ``(artifact=None)`` suffix leaks into the
    operator-facing message."""
    import cairn_lambda_archive_to_s3.handler as handler_module

    err = handler_module.SizeIntegrityError(message="m", computed="9", expected="8", artifact=None)
    assert isinstance(err, IntegrityError)
    assert err.artifact is None
    assert "artifact=" not in str(err)


def test_config_blob_declared_vs_true_mismatch_rejected(reload_handler, lambda_context) -> None:
    """GAP: the byte-true declared-vs-true check applies to the Config_Blob too
    (``_archive_blobs`` passes ``declared_size=manifest.config.size``), not only
    content layers. Config is archived first, so a lying config descriptor
    surfaces as the offending artifact='config'."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)

    manifest = json.loads(bundle.manifest_raw)
    true_len = len(bundle.config_bytes)
    manifest["config"]["size"] = true_len + 7  # config descriptor lies
    tampered_raw = json.dumps(manifest, separators=(",", ":"))
    tampered_digest = compute_sha256_digest(tampered_raw.encode("utf-8"))
    handler._test_fake_ecr.manifests[tampered_digest] = (
        manifest,
        tampered_raw,
        "application/vnd.oci.image.manifest.v1+json",
    )

    event = signed_event(bundle=bundle)
    event["detail"]["manifestDigest"] = tampered_digest

    with pytest.raises(IntegrityError, match="byte-size violation"):
        handler.lambda_handler(event, lambda_context)

    failed = handler._test_recorder.details("artifact.archive-failed")
    assert len(failed) == 1
    assert failed[0]["artifact"] == "config"
    assert failed[0]["computedDigest"] == str(true_len)
    assert failed[0]["expectedDigest"] == str(true_len + 7)


def test_honest_in_bounds_blob_still_archives(reload_handler, lambda_context) -> None:
    """An honest blob whose true bytes == declared size <= cap archives
    successfully: PutObject happens, no raise, archived event emitted."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)

    payload = handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    assert payload["idempotentShortCircuit"] is False
    archived = handler._test_recorder.details("artifact.archived")
    assert len(archived) == 1
    assert handler._test_recorder.details("artifact.archive-failed") == []

    expected_prefix = (
        f"{bundle.org_id}/{bundle.tenant_id}/{bundle.project_id}/{bundle.manifest_digest}/"
    )
    handler._test_s3.head_object(
        Bucket=ARCHIVE_BUCKET,
        Key=f"{expected_prefix}blobs/{bundle.layer_digest.replace(':', '/')}",
    )
    handler._test_s3.head_object(
        Bucket=ARCHIVE_BUCKET,
        Key=f"{expected_prefix}blobs/{bundle.config_digest.replace(':', '/')}",
    )

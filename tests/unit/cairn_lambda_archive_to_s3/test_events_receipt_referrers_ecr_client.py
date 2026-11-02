"""Tests for archive-to-s3 sidecar modules: events, receipt, referrers, ecr_client."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest
from cairn_observability.scope import extract_trace_context
from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider


@pytest.fixture(scope="session", autouse=True)
def _otel_tracer_provider() -> None:
    """Install an SDK TracerProvider so start_as_current_span yields valid
    SpanContexts (mirrors tests/unit/cairn_observability/test_scope.py)."""
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())


@pytest.fixture(autouse=True)
def _isolate_context() -> None:
    """Snapshot/restore OTEL context per test so an active span/baggage cannot
    leak across tests within a worker."""
    token = context.attach(context.Context())
    yield
    context.detach(token)


@pytest.fixture(autouse=True)
def reset_module_clients() -> None:
    import cairn_lambda_archive_to_s3.ecr_client as ecr_module
    import cairn_lambda_archive_to_s3.events as events_module
    import cairn_lambda_archive_to_s3.referrers as referrers_module

    ecr_module._ECR_CLIENT = None
    ecr_module._HTTP = None
    events_module._EVENTS_CLIENT = None
    referrers_module._ECR_CLIENT = None
    yield
    ecr_module._ECR_CLIENT = None
    ecr_module._HTTP = None
    events_module._EVENTS_CLIENT = None
    referrers_module._ECR_CLIENT = None


# ── events.py ───────────────────────────────────────────────────────────────


def test_emit_archived_uses_correct_detail_type(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_archive_to_s3.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "B")
    fake = MagicMock()
    monkeypatch.setattr(mod, "_events", lambda: fake)

    mod.emit_archived({"manifestDigest": "sha256:" + "0" * 64})
    entry = fake.put_events.call_args.kwargs["Entries"][0]
    assert entry["DetailType"] == "artifact.archived"


def test_emit_archive_failed_uses_correct_detail_type(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_archive_to_s3.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "B")
    fake = MagicMock()
    monkeypatch.setattr(mod, "_events", lambda: fake)

    mod.emit_archive_failed({"artifact": "config"})
    entry = fake.put_events.call_args.kwargs["Entries"][0]
    assert entry["DetailType"] == "artifact.archive-failed"


def test_emit_raises_when_event_bus_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_archive_to_s3.events as mod

    monkeypatch.delenv("EVENT_BUS_NAME", raising=False)
    monkeypatch.setattr(mod, "_events", lambda: MagicMock())
    with pytest.raises(RuntimeError, match="EVENT_BUS_NAME"):
        mod.emit_archived({})


def test_emit_archive_events_client_lazy_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_archive_to_s3.events as mod

    sentinel = object()
    monkeypatch.setattr("boto3.client", lambda *a, **k: sentinel)
    assert mod._events() is sentinel
    assert mod._events() is sentinel


def test_emit_injects_trace_context_resolvable_downstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """emit() seeds the serialized Detail with traceparent from the active span,
    so a downstream extract_trace_context resolves source="headers" / same id."""
    import cairn_lambda_archive_to_s3.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "B")
    fake = MagicMock()
    monkeypatch.setattr(mod, "_events", lambda: fake)

    detail = {"manifestDigest": "sha256:" + "0" * 64}
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("producer") as span:
        expected_trace_id = format(span.get_span_context().trace_id, "032x")
        mod.emit_archived(detail)

    entry = fake.put_events.call_args.kwargs["Entries"][0]
    parsed = json.loads(entry["Detail"])
    assert "traceparent" in parsed
    assert parsed["manifestDigest"] == "sha256:" + "0" * 64
    resolved = extract_trace_context({"detail": parsed})
    assert resolved.source == "headers"
    assert resolved.trace_id == expected_trace_id


def test_emit_does_not_mutate_caller_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller's detail dict is left untouched — trace keys live only in the
    serialized payload (shallow copy)."""
    import cairn_lambda_archive_to_s3.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "B")
    monkeypatch.setattr(mod, "_events", lambda: MagicMock())

    detail = {"manifestDigest": "sha256:" + "0" * 64}
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("producer"):
        mod.emit_archived(detail)

    assert "traceparent" not in detail
    assert detail == {"manifestDigest": "sha256:" + "0" * 64}


# ── receipt.py ──────────────────────────────────────────────────────────────


def test_build_receipt_returns_canonical_json() -> None:
    from cairn_lambda_archive_to_s3.receipt import build_receipt

    receipt_bytes = build_receipt(
        manifest_digest="sha256:" + "a" * 64,
        archived_at="2026-05-26T12:00:00Z",
        s3_bucket="b",
        s3_key_prefix="p/",
        archive_operator="archive-to-s3:alpha",
        worm_enabled=False,
        blob_count=3,
        integrity_verified=True,
        retain_until="2033-05-24T12:00:00Z",
    )
    parsed = json.loads(receipt_bytes)
    assert parsed == {
        "manifestDigest": "sha256:" + "a" * 64,
        "archivedAt": "2026-05-26T12:00:00Z",
        "s3Bucket": "b",
        "s3KeyPrefix": "p/",
        "archiveOperator": "archive-to-s3:alpha",
        "wormEnabled": False,
        "blobCount": 3,
        "integrityVerified": True,
        "retainUntil": "2033-05-24T12:00:00Z",
        "schemaVersion": "v1",
    }


def test_build_receipt_manifest_envelope_shape() -> None:
    from cairn_lambda_archive_to_s3.receipt import (
        RECEIPT_ARTIFACT_TYPE,
        RECEIPT_MEDIA_TYPE,
        build_receipt_manifest,
    )

    body = b'{"x":1}'
    subj_digest = "sha256:" + "1" * 64
    artifact = build_receipt_manifest(
        receipt_body=body,
        subject_digest=subj_digest,
        subject_size=512,
        subject_media_type="application/vnd.oci.image.manifest.v1+json",
        retain_until="2033-05-24T12:00:00Z",
    )
    assert artifact["schemaVersion"] == 2
    assert artifact["artifactType"] == RECEIPT_ARTIFACT_TYPE
    assert artifact["subject"]["digest"] == subj_digest
    assert artifact["subject"]["size"] == 512
    assert artifact["layers"][0]["mediaType"] == RECEIPT_MEDIA_TYPE
    assert artifact["layers"][0]["size"] == len(body)
    receipt_type_key = "dev.cairnproofs.artifact.receipt-type"
    assert artifact["annotations"][receipt_type_key] == RECEIPT_ARTIFACT_TYPE
    assert artifact["annotations"]["dev.cairnproofs.artifact.retain-until"] == (
        "2033-05-24T12:00:00Z"
    )


# ── referrers.py ────────────────────────────────────────────────────────────


_RECEIPT = "application/vnd.cairnproofs.artifact.worm-receipt.v1"


def test_existing_receipt_referrer_returns_first_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_archive_to_s3.referrers as mod

    fake = MagicMock()
    fake.list_image_referrers.return_value = {
        "referrers": [{"artifactType": _RECEIPT, "imageDigest": "sha256:" + "7" * 64}],
    }
    monkeypatch.setattr(mod, "_ecr", lambda: fake)

    assert (
        mod.existing_receipt_referrer(
            registry_id="111122223333",
            repository_name="r",
            image_digest="sha256:" + "0" * 64,
        )
        == "sha256:" + "7" * 64
    )
    kwargs = fake.list_image_referrers.call_args.kwargs
    assert kwargs["subjectId"] == {"imageDigest": "sha256:" + "0" * 64}
    assert kwargs["registryId"] == "111122223333"


def test_existing_receipt_referrer_returns_none_when_no_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cairn_lambda_archive_to_s3.referrers as mod

    fake = MagicMock()
    fake.list_image_referrers.return_value = {"referrers": []}
    monkeypatch.setattr(mod, "_ecr", lambda: fake)
    assert (
        mod.existing_receipt_referrer(
            registry_id="x", repository_name="r", image_digest="sha256:" + "0" * 64
        )
        is None
    )


def test_existing_receipt_referrer_filters_non_receipt_artifact_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cairn_lambda_archive_to_s3.referrers as mod

    fake = MagicMock()
    fake.list_image_referrers.return_value = {
        "referrers": [
            {"artifactType": "application/vnd.cncf.notary.signature", "imageDigest": "skip"},
            {"artifactType": _RECEIPT, "imageDigest": "sha256:" + "9" * 64},
        ],
    }
    monkeypatch.setattr(mod, "_ecr", lambda: fake)
    assert (
        mod.existing_receipt_referrer(
            registry_id="x", repository_name="r", image_digest="sha256:" + "0" * 64
        )
        == "sha256:" + "9" * 64
    )


def test_referrers_ecr_lazy_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_archive_to_s3.referrers as mod

    sentinel = object()
    monkeypatch.setattr("boto3.client", lambda *a, **k: sentinel)
    assert mod._ecr() is sentinel
    assert mod._ecr() is sentinel


# ── ecr_client.py ───────────────────────────────────────────────────────────


@dataclass
class _FakeStreamResponse:
    body: bytes
    status: int = 200

    def stream(self, chunk_size: int):
        view = memoryview(self.body)
        for i in range(0, len(view), chunk_size):
            yield bytes(view[i : i + chunk_size])

    def release_conn(self) -> None:
        return None


@dataclass
class _FakeHttp:
    response: _FakeStreamResponse
    requests: list[tuple[str, str]] = field(default_factory=list)

    def request(self, method: str, url: str, preload_content: bool = True):
        self.requests.append((method, url))
        return self.response


def test_archive_fetch_manifest_returns_media_type(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_archive_to_s3.ecr_client as mod

    fake = MagicMock()
    fake.batch_get_image.return_value = {
        "images": [
            {
                "imageManifest": json.dumps({"schemaVersion": 2}),
                "imageManifestMediaType": "application/vnd.oci.image.manifest.v1+json",
            }
        ]
    }
    monkeypatch.setattr(mod, "_ecr", lambda: fake)
    parsed, raw, mt = mod.fetch_manifest(
        registry_id="111122223333", repository_name="r1", image_digest="sha256:" + "0" * 64
    )
    assert parsed == {"schemaVersion": 2}
    assert mt == "application/vnd.oci.image.manifest.v1+json"
    assert raw == json.dumps({"schemaVersion": 2})


def test_archive_fetch_manifest_defaults_media_type(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_archive_to_s3.ecr_client as mod

    fake = MagicMock()
    fake.batch_get_image.return_value = {
        "images": [{"imageManifest": json.dumps({"schemaVersion": 2})}]
    }
    monkeypatch.setattr(mod, "_ecr", lambda: fake)
    _, _, mt = mod.fetch_manifest(
        registry_id="111122223333", repository_name="r1", image_digest="sha256:" + "0" * 64
    )
    assert mt == "application/vnd.oci.image.manifest.v1+json"


def test_archive_fetch_manifest_raises_when_image_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_archive_to_s3.ecr_client as mod

    fake = MagicMock()
    fake.batch_get_image.return_value = {
        "images": [],
        "failures": [{"failureCode": "ImageNotFound"}],
    }
    monkeypatch.setattr(mod, "_ecr", lambda: fake)
    with pytest.raises(mod.EcrFetchError):
        mod.fetch_manifest(
            registry_id="111122223333", repository_name="r1", image_digest="sha256:" + "0" * 64
        )


def test_archive_fetch_manifest_raises_when_manifest_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cairn_lambda_archive_to_s3.ecr_client as mod

    fake = MagicMock()
    fake.batch_get_image.return_value = {"images": [{}]}
    monkeypatch.setattr(mod, "_ecr", lambda: fake)
    with pytest.raises(mod.EcrFetchError, match="no imageManifest"):
        mod.fetch_manifest(
            registry_id="111122223333", repository_name="r1", image_digest="sha256:" + "0" * 64
        )


def test_archive_fetch_blob_streams_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_archive_to_s3.ecr_client as mod

    payload = b"y" * 4096
    fake_ecr = MagicMock()
    fake_ecr.get_download_url_for_layer.return_value = {"downloadUrl": "https://x"}
    fake_http = _FakeHttp(response=_FakeStreamResponse(body=payload))
    monkeypatch.setattr(mod, "_ecr", lambda: fake_ecr)
    monkeypatch.setattr(mod, "_http", lambda: fake_http)

    result = mod.fetch_blob(
        registry_id="111122223333",
        repository_name="r1",
        blob_digest="sha256:" + "0" * 64,
    )
    assert result == payload


def test_archive_fetch_blob_raises_on_missing_download_url(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_archive_to_s3.ecr_client as mod

    fake_ecr = MagicMock()
    fake_ecr.get_download_url_for_layer.return_value = {}
    monkeypatch.setattr(mod, "_ecr", lambda: fake_ecr)
    with pytest.raises(mod.EcrFetchError, match="no downloadUrl"):
        mod.fetch_blob(
            registry_id="111122223333", repository_name="r1", blob_digest="sha256:" + "0" * 64
        )


def test_archive_fetch_blob_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_archive_to_s3.ecr_client as mod

    fake_ecr = MagicMock()
    fake_ecr.get_download_url_for_layer.return_value = {"downloadUrl": "https://x"}
    fake_http = _FakeHttp(response=_FakeStreamResponse(body=b"", status=503))
    monkeypatch.setattr(mod, "_ecr", lambda: fake_ecr)
    monkeypatch.setattr(mod, "_http", lambda: fake_http)
    with pytest.raises(mod.EcrFetchError, match="HTTP 503"):
        mod.fetch_blob(
            registry_id="111122223333", repository_name="r1", blob_digest="sha256:" + "0" * 64
        )


def test_archive_fetch_blob_enforces_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Over-cap streamed bytes abort the fetch as a PERMANENT, named integrity
    failure — NOT a transient EcrFetchError. fetch_blob is
    the authoritative first-line over-size gate: it raises SizeIntegrityError
    (an IntegrityError) so the handler's archive-failed / DLQ chokepoint catches
    it with the offending blob named, before the blob is fully buffered."""
    import cairn_lambda_archive_to_s3.ecr_client as mod
    from cairn_oci.digest import IntegrityError, SizeIntegrityError

    monkeypatch.setattr(mod, "_MAX_BLOB_BYTES", 1024)
    fake_ecr = MagicMock()
    fake_ecr.get_download_url_for_layer.return_value = {"downloadUrl": "https://x"}
    fake_http = _FakeHttp(response=_FakeStreamResponse(body=b"z" * 4096))
    monkeypatch.setattr(mod, "_ecr", lambda: fake_ecr)
    monkeypatch.setattr(mod, "_http", lambda: fake_http)
    digest = "sha256:" + "0" * 64
    with pytest.raises(SizeIntegrityError, match="per-artifact size cap") as exc_info:
        mod.fetch_blob(registry_id="111122223333", repository_name="r1", blob_digest=digest)
    # Routes through the existing IntegrityError chokepoint and names the blob.
    assert isinstance(exc_info.value, IntegrityError)
    assert digest in (exc_info.value.artifact or "")
    assert exc_info.value.expected == "1024"


def test_put_referrer_artifact_returns_image_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_archive_to_s3.ecr_client as mod

    fake = MagicMock()
    digest = "sha256:" + "9" * 64
    fake.put_image.return_value = {"image": {"imageId": {"imageDigest": digest}}}
    monkeypatch.setattr(mod, "_ecr", lambda: fake)
    result = mod.put_referrer_artifact(
        registry_id="111122223333",
        repository_name="r1",
        artifact_manifest={"schemaVersion": 2},
    )
    assert result == digest


def test_put_referrer_artifact_raises_when_no_image_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_archive_to_s3.ecr_client as mod

    fake = MagicMock()
    fake.put_image.return_value = {"image": {"imageId": {}}}
    monkeypatch.setattr(mod, "_ecr", lambda: fake)
    with pytest.raises(mod.EcrFetchError, match="no imageDigest"):
        mod.put_referrer_artifact(
            registry_id="111122223333",
            repository_name="r1",
            artifact_manifest={"schemaVersion": 2},
        )


def test_put_blob_invokes_upload_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_archive_to_s3.ecr_client as mod

    fake = MagicMock()
    fake.initiate_layer_upload.return_value = {"uploadId": "u1"}
    monkeypatch.setattr(mod, "_ecr", lambda: fake)

    blob = b"hello"
    mod.put_blob(registry_id="111122223333", repository_name="r1", blob=blob)
    fake.initiate_layer_upload.assert_called_once()
    fake.upload_layer_part.assert_called_once()
    fake.complete_layer_upload.assert_called_once()
    complete_kwargs = fake.complete_layer_upload.call_args.kwargs
    import hashlib

    expected = "sha256:" + hashlib.sha256(blob).hexdigest()
    assert complete_kwargs["layerDigests"] == [expected]
    assert complete_kwargs["uploadId"] == "u1"

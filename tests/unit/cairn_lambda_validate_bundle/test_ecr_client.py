"""Tests for cairn_lambda_validate_bundle.ecr_client.

Stubs the boto3 ECR client and urllib3 PoolManager because moto does not
implement ``initiate_layer_upload``, ``get_download_url_for_layer``, or
the underlying presigned-URL S3 flow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest


@dataclass
class _FakeStreamResponse:
    body: bytes
    status: int = 200
    bytes_read: int = 0
    released: bool = False

    def read(self, amt: int) -> bytes:
        chunk = self.body[self.bytes_read : self.bytes_read + amt]
        self.bytes_read += len(chunk)
        return chunk

    def release_conn(self) -> None:
        self.released = True


@dataclass
class _FakeHttp:
    response: _FakeStreamResponse
    requests: list[tuple[str, str]] = field(default_factory=list)

    def request(self, method: str, url: str, preload_content: bool = True):
        self.requests.append((method, url))
        return self.response


@pytest.fixture(autouse=True)
def reset_module_clients() -> None:
    import cairn_lambda_validate_bundle.ecr_client as mod

    mod._ECR_CLIENT = None
    mod._HTTP = None
    yield
    mod._ECR_CLIENT = None
    mod._HTTP = None


def test_fetch_manifest_returns_parsed_dict_and_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_validate_bundle.ecr_client as mod

    raw = json.dumps({"schemaVersion": 2})
    fake = MagicMock()
    fake.batch_get_image.return_value = {"images": [{"imageManifest": raw}]}
    monkeypatch.setattr(mod, "_ecr", lambda: fake)

    parsed, returned_raw = mod.fetch_manifest(
        registry_id="111122223333",
        repository_name="artifact-cmh-alpha/p/assessment-results",
        image_digest="sha256:" + "a" * 64,
    )
    assert parsed == {"schemaVersion": 2}
    assert returned_raw == raw
    fake.batch_get_image.assert_called_once()


def test_fetch_manifest_raises_when_images_list_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_validate_bundle.ecr_client as mod

    fake = MagicMock()
    fake.batch_get_image.return_value = {
        "images": [],
        "failures": [{"failureCode": "ImageNotFound"}],
    }
    monkeypatch.setattr(mod, "_ecr", lambda: fake)

    with pytest.raises(mod.EcrFetchError, match="ImageNotFound"):
        mod.fetch_manifest(
            registry_id="111122223333",
            repository_name="artifact-cmh-alpha/p/assessment-results",
            image_digest="sha256:" + "a" * 64,
        )


def test_fetch_manifest_raises_when_no_image_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_validate_bundle.ecr_client as mod

    fake = MagicMock()
    fake.batch_get_image.return_value = {"images": [{}]}
    monkeypatch.setattr(mod, "_ecr", lambda: fake)

    with pytest.raises(mod.EcrFetchError, match="no imageManifest"):
        mod.fetch_manifest(
            registry_id="111122223333",
            repository_name="artifact-cmh-alpha/p/assessment-results",
            image_digest="sha256:" + "a" * 64,
        )


def test_fetch_blob_streams_and_returns_full_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_validate_bundle.ecr_client as mod

    payload = b"x" * 200_000
    fake_ecr = MagicMock()
    fake_ecr.get_download_url_for_layer.return_value = {"downloadUrl": "https://signed.example/x"}
    fake_http = _FakeHttp(response=_FakeStreamResponse(body=payload))
    monkeypatch.setattr(mod, "_ecr", lambda: fake_ecr)
    monkeypatch.setattr(mod, "_http", lambda: fake_http)

    result = mod.fetch_blob(
        registry_id="111122223333",
        repository_name="artifact-cmh-alpha/p/assessment-results",
        blob_digest="sha256:" + "b" * 64,
    )
    assert result == payload
    assert fake_http.requests == [("GET", "https://signed.example/x")]


def test_fetch_blob_raises_on_missing_download_url(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_validate_bundle.ecr_client as mod

    fake_ecr = MagicMock()
    fake_ecr.get_download_url_for_layer.return_value = {}
    monkeypatch.setattr(mod, "_ecr", lambda: fake_ecr)

    with pytest.raises(mod.EcrFetchError, match="no downloadUrl"):
        mod.fetch_blob(
            registry_id="111122223333",
            repository_name="artifact-cmh-alpha/p/assessment-results",
            blob_digest="sha256:" + "c" * 64,
        )


def test_fetch_blob_raises_on_non_200_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_validate_bundle.ecr_client as mod

    fake_ecr = MagicMock()
    fake_ecr.get_download_url_for_layer.return_value = {"downloadUrl": "https://signed.example/x"}
    fake_http = _FakeHttp(response=_FakeStreamResponse(body=b"", status=403))
    monkeypatch.setattr(mod, "_ecr", lambda: fake_ecr)
    monkeypatch.setattr(mod, "_http", lambda: fake_http)

    with pytest.raises(mod.EcrFetchError, match="HTTP 403"):
        mod.fetch_blob(
            registry_id="111122223333",
            repository_name="artifact-cmh-alpha/p/assessment-results",
            blob_digest="sha256:" + "d" * 64,
        )


def test_fetch_blob_enforces_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_validate_bundle.ecr_client as mod

    monkeypatch.setattr(mod, "_MAX_BLOB_BYTES", 1024)
    fake_ecr = MagicMock()
    fake_ecr.get_download_url_for_layer.return_value = {"downloadUrl": "https://signed.example/x"}
    fake_http = _FakeHttp(response=_FakeStreamResponse(body=b"x" * 4096))
    monkeypatch.setattr(mod, "_ecr", lambda: fake_ecr)
    monkeypatch.setattr(mod, "_http", lambda: fake_http)

    with pytest.raises(mod.EcrFetchError, match="exceeds per-artifact size cap"):
        mod.fetch_blob(
            registry_id="111122223333",
            repository_name="artifact-cmh-alpha/p/assessment-results",
            blob_digest="sha256:" + "e" * 64,
        )


def _stub_blob(monkeypatch: pytest.MonkeyPatch, body: bytes) -> _FakeStreamResponse:
    import cairn_lambda_validate_bundle.ecr_client as mod

    fake_ecr = MagicMock()
    fake_ecr.get_download_url_for_layer.return_value = {"downloadUrl": "https://signed.example/x"}
    response = _FakeStreamResponse(body=body)
    monkeypatch.setattr(mod, "_ecr", lambda: fake_ecr)
    monkeypatch.setattr(mod, "_http", lambda: _FakeHttp(response=response))
    return response


@pytest.mark.parametrize("max_bytes", [1, 1000, 64 * 1024, 200_000])
def test_fetch_blob_max_bytes_exceeded_is_permanent_and_bounded(
    monkeypatch: pytest.MonkeyPatch, max_bytes: int
) -> None:
    """A stream of ``max_bytes + 1`` bytes (or more) raises the permanent
    ``BlobSizeLimitExceeded`` and reads no more than ``max_bytes + 1`` bytes."""
    import cairn_lambda_validate_bundle.ecr_client as mod

    digest = "sha256:" + "f" * 64
    for body_len in (max_bytes + 1, max_bytes * 3 + 70_000):
        response = _stub_blob(monkeypatch, b"x" * body_len)
        with pytest.raises(mod.BlobSizeLimitExceeded) as exc_info:
            mod.fetch_blob(
                registry_id="111122223333",
                repository_name="artifact-cmh-alpha/p/catalog",
                blob_digest=digest,
                max_bytes=max_bytes,
            )
        err = exc_info.value
        assert not isinstance(err, mod.EcrFetchError)
        assert err.digest == digest
        assert err.limit == max_bytes
        assert digest in str(err)
        assert "longer than the limit" in str(err)
        assert f"{max_bytes:,} bytes" in str(err)
        assert response.bytes_read <= max_bytes + 1
        assert response.released


@pytest.mark.parametrize("max_bytes", [1, 1000, 64 * 1024, 200_000])
def test_fetch_blob_at_max_bytes_returns_bytes(
    monkeypatch: pytest.MonkeyPatch, max_bytes: int
) -> None:
    import cairn_lambda_validate_bundle.ecr_client as mod

    payload = bytes(i % 251 for i in range(max_bytes))
    _stub_blob(monkeypatch, payload)
    result = mod.fetch_blob(
        registry_id="111122223333",
        repository_name="artifact-cmh-alpha/p/catalog",
        blob_digest="sha256:" + "f" * 64,
        max_bytes=max_bytes,
    )
    assert result == payload


def test_fetch_blob_without_max_bytes_keeps_transient_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``max_bytes`` the over-ceiling error stays the transient
    ``EcrFetchError``, never ``BlobSizeLimitExceeded``."""
    import cairn_lambda_validate_bundle.ecr_client as mod

    monkeypatch.setattr(mod, "_MAX_BLOB_BYTES", 1024)
    response = _stub_blob(monkeypatch, b"x" * 4096)
    with pytest.raises(mod.EcrFetchError) as exc_info:
        mod.fetch_blob(
            registry_id="111122223333",
            repository_name="artifact-cmh-alpha/p/catalog",
            blob_digest="sha256:" + "f" * 64,
        )
    assert not isinstance(exc_info.value, mod.BlobSizeLimitExceeded)
    assert response.bytes_read <= 1025


def test_module_level_client_factories_cache_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_ecr`` and ``_http`` cache one client per process.

    Catch a regression that creates a fresh boto3 client per call, which
    would defeat the per-Lambda-execution-environment client reuse the
    pipeline relies on.
    """
    import cairn_lambda_validate_bundle.ecr_client as mod

    sentinel_ecr = object()
    sentinel_http = object()
    monkeypatch.setattr("boto3.client", lambda *a, **k: sentinel_ecr)
    monkeypatch.setattr("urllib3.PoolManager", lambda *a, **k: sentinel_http)

    assert mod._ecr() is sentinel_ecr
    assert mod._ecr() is sentinel_ecr
    assert mod._http() is sentinel_http
    assert mod._http() is sentinel_http


@dataclass
class _ShortReadResponse(_FakeStreamResponse):
    """A response whose ``read(amt)`` returns at most ``max_chunk`` bytes, as a
    raw socket read can before EOF."""

    max_chunk: int = 7
    requested: list[int] = field(default_factory=list)

    def read(self, amt: int) -> bytes:
        self.requested.append(amt)
        return super().read(min(amt, self.max_chunk))


def _stub_response(monkeypatch: pytest.MonkeyPatch, response: _FakeStreamResponse) -> None:
    import cairn_lambda_validate_bundle.ecr_client as mod

    fake_ecr = MagicMock()
    fake_ecr.get_download_url_for_layer.return_value = {"downloadUrl": "https://signed.example/x"}
    monkeypatch.setattr(mod, "_ecr", lambda: fake_ecr)
    monkeypatch.setattr(mod, "_http", lambda: _FakeHttp(response=response))


def test_fetch_blob_short_reads_reassemble_within_max_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cairn_lambda_validate_bundle.ecr_client as mod

    payload = bytes(range(100))
    response = _ShortReadResponse(body=payload)
    _stub_response(monkeypatch, response)

    result = mod.fetch_blob(
        registry_id="111122223333",
        repository_name="artifact-cmh-alpha/p/catalog",
        blob_digest="sha256:" + "f" * 64,
        max_bytes=100,
    )

    assert result == payload
    assert all(1 <= amt <= 101 for amt in response.requested)
    assert response.released


def test_fetch_blob_short_reads_still_detect_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_validate_bundle.ecr_client as mod

    response = _ShortReadResponse(body=b"y" * 1000)
    _stub_response(monkeypatch, response)

    with pytest.raises(mod.BlobSizeLimitExceeded):
        mod.fetch_blob(
            registry_id="111122223333",
            repository_name="artifact-cmh-alpha/p/catalog",
            blob_digest="sha256:" + "f" * 64,
            max_bytes=100,
        )
    assert response.bytes_read == 101
    assert response.released


def test_fetch_blob_non_200_with_max_bytes_stays_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTTP error on the bounded OSCAL fetch is the transient
    ``EcrFetchError``, never the permanent size classification."""
    import cairn_lambda_validate_bundle.ecr_client as mod

    response = _FakeStreamResponse(body=b"<Error><Code>AccessDenied</Code></Error>", status=403)
    _stub_response(monkeypatch, response)

    with pytest.raises(mod.EcrFetchError, match="HTTP 403") as exc_info:
        mod.fetch_blob(
            registry_id="111122223333",
            repository_name="artifact-cmh-alpha/p/catalog",
            blob_digest="sha256:" + "f" * 64,
            max_bytes=1,
        )
    assert not isinstance(exc_info.value, mod.BlobSizeLimitExceeded)
    assert response.bytes_read == 0
    assert response.released


def test_fetch_blob_empty_body_with_max_bytes_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cairn_lambda_validate_bundle.ecr_client as mod

    _stub_response(monkeypatch, _FakeStreamResponse(body=b""))
    assert (
        mod.fetch_blob(
            registry_id="111122223333",
            repository_name="artifact-cmh-alpha/p/catalog",
            blob_digest="sha256:" + "f" * 64,
            max_bytes=10,
        )
        == b""
    )

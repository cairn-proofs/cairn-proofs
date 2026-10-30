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

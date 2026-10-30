"""Unit tests for the mint-tag ECR client edge helpers."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import cairn_lambda_mint_tag.ecr_client as ecr_module
import pytest
from cairn_lambda_mint_tag.ecr_client import (
    EcrMintError,
    digest_for_tag,
    fetch_manifest,
    manifest_media_type,
    put_image_tag,
    tags_for_digest,
)

DIGEST = "sha256:" + "b" * 64


@pytest.fixture(autouse=True)
def reset_client() -> Any:
    ecr_module._ECR_CLIENT = None
    yield
    ecr_module._ECR_CLIENT = None


def _fake_ecr_with_image_not_found() -> Any:
    fake = MagicMock()

    class ImageNotFoundException(Exception):
        pass

    fake.exceptions.ImageNotFoundException = ImageNotFoundException
    fake.describe_images.side_effect = ImageNotFoundException()
    return fake


def test_tags_for_digest_image_not_found_returns_empty(monkeypatch: Any) -> None:
    fake = _fake_ecr_with_image_not_found()
    monkeypatch.setattr(ecr_module, "_ecr", lambda: fake)
    assert tags_for_digest(registry_id="1", repository_name="r", image_digest=DIGEST) == []


def test_tags_for_digest_collects_all_tags(monkeypatch: Any) -> None:
    fake = MagicMock()
    fake.describe_images.return_value = {
        "imageDetails": [{"imageTags": ["catalog--_ref--1", "extra"]}]
    }
    monkeypatch.setattr(ecr_module, "_ecr", lambda: fake)
    tags = tags_for_digest(registry_id="1", repository_name="r", image_digest=DIGEST)
    assert set(tags) == {"catalog--_ref--1", "extra"}


def test_digest_for_tag_image_not_found_returns_none(monkeypatch: Any) -> None:
    fake = _fake_ecr_with_image_not_found()
    monkeypatch.setattr(ecr_module, "_ecr", lambda: fake)
    assert digest_for_tag(registry_id="1", repository_name="r", image_tag="t") is None


def test_digest_for_tag_returns_digest(monkeypatch: Any) -> None:
    fake = MagicMock()
    fake.describe_images.return_value = {"imageDetails": [{"imageDigest": DIGEST}]}
    monkeypatch.setattr(ecr_module, "_ecr", lambda: fake)
    out = digest_for_tag(registry_id="1", repository_name="r", image_tag="t")
    assert out == DIGEST
    assert fake.describe_images.call_args.kwargs["imageIds"] == [{"imageTag": "t"}]


def test_fetch_manifest_no_images_raises(monkeypatch: Any) -> None:
    fake = MagicMock()
    fake.batch_get_image.return_value = {"images": [], "failures": [{"failureCode": "x"}]}
    monkeypatch.setattr(ecr_module, "_ecr", lambda: fake)
    with pytest.raises(EcrMintError):
        fetch_manifest(registry_id="1", repository_name="r", image_digest=DIGEST)


def test_put_image_tag_returns_digest(monkeypatch: Any) -> None:
    fake = MagicMock()
    fake.put_image.return_value = {"image": {"imageId": {"imageDigest": DIGEST}}}
    monkeypatch.setattr(ecr_module, "_ecr", lambda: fake)
    out = put_image_tag(
        registry_id="1",
        repository_name="r",
        manifest_raw="{}",
        media_type="application/vnd.oci.image.manifest.v1+json",
        image_tag="catalog--_ref--1",
    )
    assert out == DIGEST
    # imageTag was passed to put_image.
    assert fake.put_image.call_args.kwargs["imageTag"] == "catalog--_ref--1"


def test_put_image_tag_missing_digest_raises(monkeypatch: Any) -> None:
    fake = MagicMock()
    fake.put_image.return_value = {"image": {"imageId": {}}}
    monkeypatch.setattr(ecr_module, "_ecr", lambda: fake)
    with pytest.raises(EcrMintError):
        put_image_tag(
            registry_id="1",
            repository_name="r",
            manifest_raw="{}",
            media_type="m",
            image_tag="t",
        )


def test_manifest_media_type_reads_body() -> None:
    raw = '{"mediaType":"application/vnd.oci.image.manifest.v1+json"}'
    assert manifest_media_type(raw, "fallback") == "application/vnd.oci.image.manifest.v1+json"


def test_manifest_media_type_falls_back_on_garbage() -> None:
    assert manifest_media_type("not json", "fallback") == "fallback"

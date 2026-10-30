"""Tests for cairn_oci.manifest."""

from __future__ import annotations

import pytest
from cairn_oci.manifest import Descriptor, OciManifest
from pydantic import ValidationError


def test_descriptor_constructs_from_camel_case_aliases() -> None:
    d = Descriptor(
        mediaType="application/vnd.oci.image.config.v1+json", digest="sha256:abc", size=42
    )
    assert d.media_type == "application/vnd.oci.image.config.v1+json"
    assert d.digest == "sha256:abc"
    assert d.size == 42
    assert d.annotations == {}


def test_descriptor_accepts_snake_case_field_name_via_populate_by_name() -> None:
    d = Descriptor(media_type="application/x", digest="sha256:0", size=1)
    assert d.media_type == "application/x"


def test_descriptor_accepts_annotations() -> None:
    d = Descriptor(mediaType="application/x", digest="sha256:0", size=1, annotations={"a": "b"})
    assert d.annotations == {"a": "b"}


def test_descriptor_allows_extra_keys_per_spec() -> None:
    # OCI Descriptor spec is open-ended; extra='allow' must let unknown fields
    # through (e.g., 'urls', 'platform').
    d = Descriptor(mediaType="application/x", digest="sha256:0", size=1, urls=["https://example/x"])
    assert d.urls == ["https://example/x"]


def test_descriptor_requires_digest_and_size() -> None:
    with pytest.raises(ValidationError):
        Descriptor(mediaType="application/x")  # type: ignore[call-arg]


def test_oci_manifest_default_schema_version_and_media_type() -> None:
    config = Descriptor(mediaType="application/x", digest="sha256:0", size=1)
    m = OciManifest(config=config)
    assert m.schema_version == 2
    assert m.media_type == "application/vnd.oci.image.manifest.v1+json"
    assert m.layers == []
    assert m.annotations == {}


def test_oci_manifest_round_trips_layers() -> None:
    config = Descriptor(mediaType="application/x", digest="sha256:0", size=1)
    layers = [
        Descriptor(mediaType="application/y", digest="sha256:1", size=10),
        Descriptor(mediaType="application/y", digest="sha256:2", size=20),
    ]
    m = OciManifest(config=config, layers=layers)
    assert len(m.layers) == 2
    assert m.layers[0].digest == "sha256:1"


def test_oci_manifest_round_trips_annotations() -> None:
    config = Descriptor(mediaType="application/x", digest="sha256:0", size=1)
    m = OciManifest(config=config, annotations={"k": "v"})
    assert m.annotations == {"k": "v"}


def test_oci_manifest_missing_config_raises() -> None:
    with pytest.raises(ValidationError):
        OciManifest()  # type: ignore[call-arg]

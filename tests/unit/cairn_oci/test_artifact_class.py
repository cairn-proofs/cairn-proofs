"""Tests for the pipeline-internal artifact classifier (cairn_oci.config_blob).

Covers the fail-closed denylist: only manifests positively bearing
a known pipeline-internal media type are classified for skip; a Cairn artifact
bundle and any unrecognized type fall through to ``None`` ([INGEST-017.07] validation).
"""

from __future__ import annotations

from typing import Any

from cairn_oci import (
    CONFIG_BLOB_MEDIA_TYPE,
    NOTARY_SIGNATURE_MEDIA_TYPE,
    OCI_EMPTY_MEDIA_TYPE,
    OCI_IMAGE_INDEX_MEDIA_TYPE,
    SkipReason,
    classify_pipeline_internal,
)
from hypothesis import given
from hypothesis import strategies as st

OCI_IMAGE_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"


def _manifest(config_media_type: str, **extra: Any) -> dict[str, Any]:
    """A well-formed image manifest carrying the given config media type."""
    return {
        "schemaVersion": 2,
        "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
        "config": {
            "mediaType": config_media_type,
            "digest": "sha256:0",
            "size": 2,
        },
        "layers": [],
        **extra,
    }


# --- Each internal media type maps to its SkipReason ------------------------


def test_notary_signature_config_classified_notary_signature() -> None:
    manifest = _manifest(NOTARY_SIGNATURE_MEDIA_TYPE)
    assert classify_pipeline_internal(manifest) is SkipReason.NOTARY_SIGNATURE


def test_oci_empty_config_classified_oci_empty() -> None:
    manifest = _manifest(OCI_EMPTY_MEDIA_TYPE)
    assert classify_pipeline_internal(manifest) is SkipReason.OCI_EMPTY


def test_image_index_with_no_config_key_classified_image_index() -> None:
    # An OCI image index carries no `config` descriptor; the classifier must
    # tolerate the absent key and key off the top-level mediaType.
    manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE,
        "manifests": [{"digest": "sha256:1", "size": 3}],
    }
    assert "config" not in manifest
    assert classify_pipeline_internal(manifest) is SkipReason.IMAGE_INDEX


# --- Fail-closed: real bundle / unknown / config-less non-index → None ------


def test_artifact_config_blob_is_never_skipped() -> None:
    # The "a real bundle is never skipped" property: a producer
    # bundle carrying the artifact Config_Blob media type proceeds to [INGEST-017.07]
    # validation (not pipeline-internal). Reads the discriminator via the
    # CONSTANT, so the media-type string is exercised by construction.
    manifest = _manifest(CONFIG_BLOB_MEDIA_TYPE)
    assert classify_pipeline_internal(manifest) is None


@given(
    extra_fields=st.dictionaries(
        keys=st.text().filter(
            lambda k: k not in {"mediaType", "config", "schemaVersion", "layers"}
        ),
        values=st.recursive(
            st.none() | st.booleans() | st.integers() | st.text(),
            lambda children: st.lists(children) | st.dictionaries(st.text(), children),
            max_leaves=5,
        ),
        max_size=5,
    ),
    annotations=st.dictionaries(st.text(), st.text(), max_size=5),
    layers=st.lists(
        st.fixed_dictionaries(
            {
                "mediaType": st.text(),
                "digest": st.text(),
                "size": st.integers(min_value=0),
            }
        ),
        max_size=5,
    ),
)
def test_cairn_bundle_never_skipped_under_arbitrary_extra_fields(
    extra_fields: dict[str, Any],
    annotations: dict[str, str],
    layers: list[dict[str, Any]],
) -> None:
    # Robustness: a manifest bearing the Cairn config type is NEVER classified
    # internal, regardless of arbitrary additional manifest fields, annotations,
    # or layers around it.
    manifest = _manifest(CONFIG_BLOB_MEDIA_TYPE, **extra_fields)
    manifest["annotations"] = annotations
    manifest["layers"] = layers
    assert classify_pipeline_internal(manifest) is None


def test_unknown_config_type_fails_closed_to_none() -> None:
    # An unexpected/unknown config type is NOT skipped — it falls through to
    # [INGEST-017.07], which rejects it as a permanent failure (preserving the
    # diagnostic). Never silently swallowed.
    manifest = _manifest("application/vnd.some.unexpected.type.v1+json")
    assert classify_pipeline_internal(manifest) is None


def test_manifest_with_no_config_and_non_index_media_type_returns_none() -> None:
    # No config key AND a non-index top-level mediaType: must not raise, must
    # fall through to validation.
    manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
        "layers": [],
    }
    assert classify_pipeline_internal(manifest) is None


def test_empty_manifest_dict_returns_none_without_raising() -> None:
    assert classify_pipeline_internal({}) is None


def test_config_present_but_not_a_dict_returns_none_without_raising() -> None:
    # Malformed input where config is not a mapping must not raise.
    manifest = {"mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE, "config": "garbage"}
    assert classify_pipeline_internal(manifest) is None


def test_config_without_media_type_returns_none() -> None:
    manifest = {
        "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
        "config": {"digest": "sha256:0", "size": 2},
    }
    assert classify_pipeline_internal(manifest) is None


# --- Precedence: index top-level wins even with a config present ------------


def test_image_index_takes_precedence_over_config() -> None:
    # If an image-index top-level mediaType somehow also carried a config (even
    # a Cairn or notary config), the index classification wins.
    manifest = {
        "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE,
        "config": {
            "mediaType": NOTARY_SIGNATURE_MEDIA_TYPE,
            "digest": "sha256:0",
            "size": 2,
        },
    }
    assert classify_pipeline_internal(manifest) is SkipReason.IMAGE_INDEX


def test_skip_reason_string_values() -> None:
    # The SkipReason values are the metric-dimension strings.
    assert SkipReason.NOTARY_SIGNATURE == "notary_signature"
    assert SkipReason.OCI_EMPTY == "oci_empty"
    assert SkipReason.IMAGE_INDEX == "image_index"

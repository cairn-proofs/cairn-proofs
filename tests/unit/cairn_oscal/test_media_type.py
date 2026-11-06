"""Tests for cairn_oscal.media_type (OSCAL layer classification, [OSCAL-002])."""

from __future__ import annotations

import pytest
from cairn_oscal import OSCAL_JSON_MEDIA_TYPE, OscalExtractionError, is_oscal_layer

DIGEST = "sha256:" + "ab" * 32


def test_constant_is_the_nist_media_type() -> None:
    assert OSCAL_JSON_MEDIA_TYPE == "application/oscal+json"


@pytest.mark.parametrize(
    "media_type",
    [
        "application/oscal+json",
        "Application/OSCAL+JSON",
        "APPLICATION/OSCAL+JSON",
        "application/oscal+json; charset=utf-8",
        "Application/OSCAL+JSON; charset=utf-8",
        "  application/oscal+json  ",
        "application/oscal+json;profile=x",
    ],
)
def test_oscal_json_is_oscal_layer(media_type: str) -> None:
    """[OSCAL-002.01], [OSCAL-002.05]: case and parameters are ignored."""
    assert is_oscal_layer(media_type, layer_digest=DIGEST) is True


@pytest.mark.parametrize(
    "media_type",
    [
        "application/json",
        "application/pdf",
        "image/png",
        "application/oscal-catalog+json",
        "application/oscaljson",
        "application/vnd.oscal+json",
        "",
    ],
)
def test_outside_family_is_not_oscal_layer(media_type: str) -> None:
    """[OSCAL-002.02]: media types outside the family are opaque."""
    assert is_oscal_layer(media_type, layer_digest=DIGEST) is False


@pytest.mark.parametrize(
    "media_type",
    [
        "application/oscal.catalog+json",
        "application/oscal.ssp+json",
        "APPLICATION/OSCAL.CATALOG+JSON",
        "application/oscal+xml",
        "application/oscal+yaml",
        "application/oscal+json+extra",
        "application/oscal.",
    ],
)
def test_other_family_member_is_rejected(media_type: str) -> None:
    """[OSCAL-002.03], [OSCAL-002.04]: other family members raise and name
    the layer digest and the required media type."""
    with pytest.raises(OscalExtractionError) as exc_info:
        is_oscal_layer(media_type, layer_digest=DIGEST)
    err = exc_info.value
    assert err.error_kind == "unsupported_media_type"
    assert err.layer_digest == DIGEST
    msg = str(err)
    assert DIGEST in msg
    assert "the required OSCAL media type is application/oscal+json" in msg
    assert "producer-declared" in msg
    assert ascii(media_type) in msg


def test_layer_digest_is_required_keyword() -> None:
    with pytest.raises(TypeError):
        is_oscal_layer("application/oscal+json")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        is_oscal_layer("application/oscal+json", DIGEST)  # type: ignore[misc]


def test_long_declared_value_is_truncated_with_original_length() -> None:
    """[OSCAL-002.08]: a 10,000-character family member is bounded."""
    media_type = "application/oscal." + "a" * (10_000 - len("application/oscal."))
    assert len(media_type) == 10_000
    with pytest.raises(OscalExtractionError) as exc_info:
        is_oscal_layer(media_type, layer_digest=DIGEST)
    msg = str(exc_info.value)
    assert "10000" in msg
    assert ascii(media_type[:256]) in msg
    assert "a" * 257 not in msg
    assert len(msg) < 600
    assert "application/oscal+json" in msg
    assert DIGEST in msg


def test_control_and_non_ascii_characters_are_escaped() -> None:
    """[OSCAL-002.08]: control, format, and non-ASCII characters show as escapes."""
    media_type = "application/oscal.cat\x1b[31m\u202e\u00e9\n" + "z" * 300
    with pytest.raises(OscalExtractionError) as exc_info:
        is_oscal_layer(media_type, layer_digest=DIGEST)
    msg = str(exc_info.value)
    assert msg.isascii()
    assert "\x1b" not in msg
    assert "\n" not in msg
    assert "\\x1b" in msg
    assert "\\u202e" in msg
    assert "\\xe9" in msg
    assert str(len(media_type)) in msg
    assert "application/oscal+json" in msg
    assert DIGEST in msg


@pytest.mark.parametrize(
    "media_type",
    [
        "application/oscal",
        "application/oscalx+json",
        "text/oscal+json",
    ],
)
def test_near_family_values_are_not_oscal_layers(media_type: str) -> None:
    """Only `application/oscal.` and `application/oscal+` prefixes are family members."""
    assert is_oscal_layer(media_type, layer_digest=DIGEST) is False


def test_parameter_on_rejected_family_member_still_rejects() -> None:
    """Parameters never turn a rejected family member into an accepted one."""
    with pytest.raises(OscalExtractionError) as exc_info:
        is_oscal_layer("application/oscal.catalog+json; charset=utf-8", layer_digest=DIGEST)
    assert exc_info.value.error_kind == "unsupported_media_type"


def test_value_exactly_at_display_bound_is_not_marked_truncated() -> None:
    media_type = "application/oscal." + "b" * (256 - len("application/oscal."))
    assert len(media_type) == 256
    with pytest.raises(OscalExtractionError) as exc_info:
        is_oscal_layer(media_type, layer_digest=DIGEST)
    msg = str(exc_info.value)
    assert "truncated" not in msg
    assert ascii(media_type) in msg

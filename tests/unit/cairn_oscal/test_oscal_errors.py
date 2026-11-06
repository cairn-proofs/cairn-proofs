"""Tests for cairn_oscal.errors.OscalExtractionError."""

from __future__ import annotations

from typing import get_args

import pytest
from cairn_oscal.errors import OscalErrorKind, OscalExtractionError

ALL_ERROR_KINDS = [
    "missing_schema_version",
    "unsupported_version",
    "parse_failure",
    "unsupported_doc_type",
    "unsupported_media_type",
    "oscal_layer_too_large",
]


def test_all_error_kinds_matches_the_literal() -> None:
    """The test list covers exactly the `OscalErrorKind` literal."""
    assert set(ALL_ERROR_KINDS) == set(get_args(OscalErrorKind))


@pytest.mark.parametrize("kind", ALL_ERROR_KINDS)
def test_error_kind_roundtrip(kind: str) -> None:
    """Every `OscalErrorKind` literal round-trips onto the exception attribute."""
    err = OscalExtractionError("msg", error_kind=kind)
    assert err.error_kind == kind


def test_default_optional_fields_are_none() -> None:
    """`layer_digest` and `document_version` default to None."""
    err = OscalExtractionError("msg", error_kind="parse_failure")
    assert err.layer_digest is None
    assert err.document_version is None


def test_optional_fields_are_stored_when_supplied() -> None:
    """Constructor preserves the optional context fields."""
    err = OscalExtractionError(
        "boom",
        error_kind="unsupported_version",
        layer_digest="sha256:abc",
        document_version="2.0.0",
    )
    assert err.layer_digest == "sha256:abc"
    assert err.document_version == "2.0.0"


def test_message_is_propagated_via_str() -> None:
    """The first positional arg surfaces via `str(err)` (standard Exception contract)."""
    err = OscalExtractionError("the message", error_kind="parse_failure")
    assert str(err) == "the message"


def test_is_subclass_of_exception() -> None:
    """Subclass of `Exception` so callers can catch it via `except Exception`."""
    assert issubclass(OscalExtractionError, Exception)

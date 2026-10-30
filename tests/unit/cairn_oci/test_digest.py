"""Tests for cairn_oci.digest.

Covers: [INGEST-006.02] (digest determinism) and [INGEST-007.03], [WORM-003.02]
(verify_digest true-iff-bytes-match, raises with both values on mismatch).
"""

from __future__ import annotations

import hashlib
import io

import pytest
from cairn_oci.digest import (
    DIGEST_ALGO,
    IntegrityError,
    compute_sha256_digest,
    format_digest,
    parse_digest,
    verify_digest,
)
from hypothesis import given
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# format_digest
# ---------------------------------------------------------------------------


def test_format_digest_wraps_valid_64_char_hex() -> None:
    hex_hash = "a" * 64
    assert format_digest(hex_hash) == f"sha256:{hex_hash}"


def test_format_digest_rejects_wrong_length() -> None:
    with pytest.raises(ValueError):
        format_digest("a" * 63)


def test_format_digest_rejects_uppercase_hex() -> None:
    # Canonical form is lowercase; uppercase MUST be rejected.
    with pytest.raises(ValueError):
        format_digest("A" * 64)


def test_format_digest_rejects_non_hex_chars() -> None:
    with pytest.raises(ValueError):
        format_digest("z" * 64)


def test_digest_algo_constant_is_sha256() -> None:
    assert DIGEST_ALGO == "sha256"


# ---------------------------------------------------------------------------
# parse_digest
# ---------------------------------------------------------------------------


def test_parse_digest_strips_prefix() -> None:
    hex_hash = "0" * 64
    assert parse_digest(f"sha256:{hex_hash}") == hex_hash


def test_parse_digest_rejects_missing_prefix() -> None:
    with pytest.raises(ValueError):
        parse_digest("0" * 64)


def test_parse_digest_rejects_wrong_algo_prefix() -> None:
    with pytest.raises(ValueError):
        parse_digest("sha512:" + "0" * 64)


def test_parse_digest_rejects_uppercase_body() -> None:
    with pytest.raises(ValueError):
        parse_digest("sha256:" + "A" * 64)


def test_parse_digest_rejects_short_body() -> None:
    with pytest.raises(ValueError):
        parse_digest("sha256:abc")


# ---------------------------------------------------------------------------
# compute_sha256_digest
# ---------------------------------------------------------------------------


def test_compute_sha256_digest_known_vector_empty_bytes() -> None:
    # SHA-256 of the empty string is a well-known constant.
    expected = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert compute_sha256_digest(b"") == expected


def test_compute_sha256_digest_known_vector_abc() -> None:
    expected = "sha256:" + hashlib.sha256(b"abc").hexdigest()
    assert compute_sha256_digest(b"abc") == expected


def test_compute_sha256_digest_accepts_bytearray() -> None:
    payload = bytearray(b"hello world")
    expected = "sha256:" + hashlib.sha256(b"hello world").hexdigest()
    assert compute_sha256_digest(payload) == expected


def test_compute_sha256_digest_streams_file_like_object() -> None:
    # Use a payload bigger than the 64 KiB stream chunk to exercise the loop.
    payload = b"\x00\x01\x02\x03" * (64 * 1024)
    expected = "sha256:" + hashlib.sha256(payload).hexdigest()
    assert compute_sha256_digest(io.BytesIO(payload)) == expected


def test_compute_sha256_digest_file_like_object_short_payload() -> None:
    payload = b"short"
    expected = "sha256:" + hashlib.sha256(payload).hexdigest()
    assert compute_sha256_digest(io.BytesIO(payload)) == expected


# ---------------------------------------------------------------------------
# [INGEST-006.02]: digest determinism — same bytes => same digest, twice.
# ---------------------------------------------------------------------------


@given(st.binary(max_size=4096))
def test_property2_digest_determinism_bytes(payload: bytes) -> None:
    """[INGEST-006.02] — pure-bytes path."""
    first = compute_sha256_digest(payload)
    second = compute_sha256_digest(payload)
    assert first == second
    assert first.startswith("sha256:")
    assert len(first) == len("sha256:") + 64


@given(st.binary(max_size=4096))
def test_property2_digest_determinism_streams_match_bytes(payload: bytes) -> None:
    """[INGEST-006.02] — stream and bytes paths produce identical digests."""
    via_bytes = compute_sha256_digest(payload)
    via_stream = compute_sha256_digest(io.BytesIO(payload))
    assert via_bytes == via_stream


# ---------------------------------------------------------------------------
# [INGEST-007.03], [WORM-003.02]: verify_digest is true iff bytes match; raises with both values.
# ---------------------------------------------------------------------------


def test_verify_digest_true_on_match() -> None:
    payload = b"some bytes"
    assert verify_digest(payload, compute_sha256_digest(payload)) is True


def test_verify_digest_raises_integrity_error_with_both_values_on_mismatch() -> None:
    payload = b"some bytes"
    expected = "sha256:" + "0" * 64
    with pytest.raises(IntegrityError) as excinfo:
        verify_digest(payload, expected)
    err = excinfo.value
    assert err.expected == expected
    assert err.computed == compute_sha256_digest(payload)
    # [INGEST-007.03], [WORM-003.02]: both values must appear in the message so an operator
    # reading the log can diagnose without re-running the pipeline.
    assert err.expected in str(err)
    assert err.computed in str(err)


def test_verify_digest_includes_artifact_in_error_when_given() -> None:
    with pytest.raises(IntegrityError) as excinfo:
        verify_digest(b"x", "sha256:" + "0" * 64, artifact="layer-3")
    assert excinfo.value.artifact == "layer-3"
    assert "layer-3" in str(excinfo.value)


@given(payload=st.binary(max_size=2048), wrong=st.binary(max_size=2048))
def test_property3_verify_digest_truth_table(payload: bytes, wrong: bytes) -> None:
    """[INGEST-007.03], [WORM-003.02] — verify_digest(content, computed_digest(content)) == True;
    if expected != computed, raises IntegrityError with both values exposed."""
    correct = compute_sha256_digest(payload)
    assert verify_digest(payload, correct) is True

    wrong_digest = compute_sha256_digest(wrong)
    if wrong_digest == correct:
        # Same hash => verification still succeeds.
        assert verify_digest(payload, wrong_digest) is True
        return
    with pytest.raises(IntegrityError) as excinfo:
        verify_digest(payload, wrong_digest)
    assert excinfo.value.expected == wrong_digest
    assert excinfo.value.computed == correct

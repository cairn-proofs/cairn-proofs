"""SHA-256 digest computation and verification helpers
([INGEST-006], [INGEST-007], [WORM-003]).

Digest format follows the OCI image-spec convention: `sha256:` prefix followed
by 64 hex chars (lowercase). `compute_sha256_digest` is deterministic per the
standard SHA-256 algorithm ([INGEST-006.02]). `verify_digest` returns true iff the
computed digest matches the expected digest and raises with both values on
failure ([INGEST-007.03], [WORM-003.02]).
"""

from __future__ import annotations

import hashlib
import re
from typing import BinaryIO, Final

DIGEST_ALGO: Final[str] = "sha256"

_DIGEST_RE = re.compile(r"^sha256:([0-9a-f]{64})$")

# Reasonable chunk size for streaming digest computation of large blobs (10GB ceiling).
_STREAM_CHUNK: Final[int] = 64 * 1024


class IntegrityError(ValueError):
    """Raised when a computed digest does not match the expected digest."""

    def __init__(self, *, computed: str, expected: str, artifact: str | None = None) -> None:
        suffix = f" (artifact={artifact})" if artifact else ""
        super().__init__(f"digest mismatch: expected {expected}, computed {computed}{suffix}")
        self.computed = computed
        self.expected = expected
        self.artifact = artifact


class SizeIntegrityError(IntegrityError):
    """Authoritative byte-true per-artifact size violation.

    The advisory descriptor-``size`` check at validate-bundle is spoofable (a
    producer can understate ``size``). The binding 10 GiB enforcement lands at
    the point the pipeline already reads the blob's TRUE bytes — the archive
    Lambda's streaming fetch (which aborts once the cap is exceeded) and the
    post-``verify_digest`` ``len(blob)`` checks in ``_archive_blob``. Lives here
    (next to :class:`IntegrityError`) so the archive ECR client and the handler
    raise the SAME type. Subclassing :class:`IntegrityError` keeps
    ``isinstance(e, IntegrityError)`` true so the existing archive-failed / DLQ
    chokepoint and the IntegrityMismatch metric path catch it unchanged; the
    message reads as a size violation rather than a digest mismatch, while still
    populating ``.computed`` / ``.expected`` / ``.artifact``. ``computed`` carries
    the true byte count (or bytes-seen-before-abort); ``expected`` carries the
    bound that was breached (the declared descriptor size, or the 10 GiB limit).
    """

    def __init__(self, *, message: str, computed: str, expected: str, artifact: str | None) -> None:
        suffix = f" (artifact={artifact})" if artifact else ""
        ValueError.__init__(self, f"byte-size violation: {message}{suffix}")
        self.computed = computed
        self.expected = expected
        self.artifact = artifact


def format_digest(hex_hash: str) -> str:
    """Wrap a 64-char hex sha256 in the canonical `sha256:` prefix."""
    if len(hex_hash) != 64 or not all(c in "0123456789abcdef" for c in hex_hash):
        raise ValueError(f"not a 64-char lowercase-hex sha256: {hex_hash!r}")
    return f"sha256:{hex_hash}"


def parse_digest(digest: str) -> str:
    """Strip the `sha256:` prefix and return the 64-char hex body.

    Raises ValueError if the digest is malformed.
    """
    m = _DIGEST_RE.match(digest)
    if not m:
        raise ValueError(f"not a valid sha256 digest string: {digest!r}")
    return m.group(1)


def compute_sha256_digest(content: bytes | BinaryIO) -> str:
    """Compute the canonical sha256 digest of bytes or a binary file-like object.

    Streams large inputs in 64 KiB chunks. Returns the digest in the
    `sha256:{hex}` form. Deterministic per [INGEST-006.02].
    """
    h = hashlib.sha256()
    if isinstance(content, (bytes, bytearray, memoryview)):
        h.update(content)
    else:
        while chunk := content.read(_STREAM_CHUNK):
            h.update(chunk)
    return format_digest(h.hexdigest())


def verify_digest(
    content: bytes | BinaryIO,
    expected: str,
    *,
    artifact: str | None = None,
) -> bool:
    """Verify that `content` hashes to `expected` ([INGEST-007], [WORM-003]).

    Returns True on match. Raises IntegrityError on mismatch with both the
    computed and expected digests included in the error.

    Note: the digest comparison here is content-address integrity, not MAC
    authenticity. The `expected` digest is public (it's the content-address
    itself), so constant-time comparison is not required and `hmac.compare_digest`
    would only add a false sense of security.
    """
    computed = compute_sha256_digest(content)
    if computed != expected:
        raise IntegrityError(computed=computed, expected=expected, artifact=artifact)
    return True

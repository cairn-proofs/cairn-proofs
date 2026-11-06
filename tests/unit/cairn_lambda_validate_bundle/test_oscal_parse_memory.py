"""Parse-memory bound for OSCAL layers at the default size limit ([OSCAL-001]).

Marked ``slow``: excluded from the default run. Run it on demand with
``uv run pytest -m slow tests/unit/cairn_lambda_validate_bundle/test_oscal_parse_memory.py``.

A subprocess builds two dense catalogs of minimal controls, each exactly
``DEFAULT_MAX_OSCAL_LAYER_BYTES`` long, and passes them in sequence through the
handler's per-layer helper (``_extract_oscal_layer``) with ``fetch_blob``
stubbed. The test then reads the subprocess peak RSS from
``resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`` and asserts that it is within
the budget. The budget leaves room in the 1,769 MB validate Lambda for the
runtime and the ADOT collector extension. ``RLIMIT_AS`` is not used: macOS does
not enforce it, and on Linux it limits address space, not resident memory.
"""

from __future__ import annotations

import os
import resource
import subprocess
import sys
import textwrap

import pytest

# The validate Lambda has 1,769 MB (1,687 MiB). The budget leaves about
# 287 MiB of it for the runtime and the ADOT collector extension.
_BUDGET_BYTES = 1400 * 1024 * 1024

_CHILD_SCRIPT = textwrap.dedent(
    """
    import io
    import sys
    from types import SimpleNamespace

    from cairn_oci import Descriptor, compute_sha256_digest
    from cairn_oscal import DEFAULT_MAX_OSCAL_LAYER_BYTES

    import cairn_lambda_validate_bundle.handler as handler

    LIMIT = DEFAULT_MAX_OSCAL_LAYER_BYTES
    assert handler.MAX_OSCAL_LAYER_BYTES == LIMIT


    def dense_catalog(uuid: str) -> bytes:
        head = (
            '{"catalog":{"uuid":"' + uuid + '","metadata":{"title":"Dense",'
            '"last-modified":"2026-01-01T00:00:00Z","version":"1.0",'
            '"oscal-version":"1.1.2"},"controls":['
        ).encode()
        tail = b"]}}"
        out = io.BytesIO()
        out.write(head)
        i = 0
        # Stop when one more control would not fit; pad the rest with JSON
        # whitespace so the document is exactly LIMIT bytes.
        while True:
            control = (b"," if i else b"") + b'{"id":"c-%d","title":"t"}' % i
            if out.tell() + len(control) + len(tail) > LIMIT:
                break
            out.write(control)
            i += 1
        out.write(tail)
        out.write(b" " * (LIMIT - out.tell()))
        blob = out.getvalue()
        assert len(blob) == LIMIT, len(blob)
        return blob


    blobs = {}
    descriptors = []
    for uuid in (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    ):
        blob = dense_catalog(uuid)
        digest = compute_sha256_digest(blob)
        blobs[digest] = blob
        descriptors.append(
            Descriptor.model_validate(
                {"mediaType": "application/oscal+json", "digest": digest, "size": len(blob)}
            )
        )
        del blob


    def fake_fetch_blob(*, registry_id, repository_name, blob_digest, max_bytes=None):
        # Hand the bytes over and drop the stub's reference, as a real fetch
        # leaves no copy behind.
        return blobs.pop(blob_digest)


    handler.fetch_blob = fake_fetch_blob
    push = SimpleNamespace(registry_id="123456789012", repository_name="artifact-cmh-alpha/o/t/p")
    for descriptor in descriptors:
        version, meta = handler._extract_oscal_layer(descriptor, push=push)
        assert version == "1.1.2"
        assert meta.doc_type == "catalog"
    sys.stdout.write("ok")
    """
)


def _maxrss_bytes(raw: int) -> int:
    """``ru_maxrss`` is bytes on macOS and KiB on Linux."""
    return raw if sys.platform == "darwin" else raw * 1024


@pytest.mark.slow
def test_two_limit_sized_dense_layers_stay_within_memory_budget() -> None:
    env = {
        **os.environ,
        "STAGE": "alpha",
        "REGION_CODE": "cmh",
        "OTEL_SDK_DISABLED": "true",
    }
    env.pop("MAX_OSCAL_LAYER_BYTES", None)
    before = _maxrss_bytes(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)

    result = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-4000:]
    assert result.stdout.endswith("ok")
    peak = _maxrss_bytes(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    # RUSAGE_CHILDREN is a maximum over every waited-for child of this process.
    # A larger earlier child would hide this child's peak, so require that this
    # child set the maximum.
    assert peak > before, "an earlier child process set a higher peak; run this test alone"
    print(f"OSCAL parse-memory peak RSS: {peak / (1024 * 1024):.0f} MiB")
    assert peak <= _BUDGET_BYTES, (
        f"peak RSS {peak / (1024 * 1024):.0f} MiB exceeds the "
        f"{_BUDGET_BYTES // (1024 * 1024)} MiB budget"
    )

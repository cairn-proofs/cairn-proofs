"""Characterization tests for the Byte-Exact Verification Requirement.

Pins the *already-true* guarantees behind `artifact-verifiability`'s
"Byte-Exact Verification" Requirement (verification is byte-exact: it depends
on no canonicalization scheme) (three scenarios):

  (a) Two byte-distinct but semantically-equivalent OSCAL/JSON documents (e.g.
      differing only by key order or whitespace) produce DISTINCT content
      digests, hence distinct layer descriptor digests, hence distinct
      Manifest_Digests — they are distinct artifacts.
  (b) The manifest Merkle-commits to its layers: a Manifest_Digest computed
      over manifest bytes that embed the layer descriptor digests CHANGES if
      any layer's bytes (hence its descriptor digest) change. Altering a
      layer's bytes after the fact breaks the manifest-digest commitment.
  (c) Digest computation is content-only and independent of any
      canonicalization: equal bytes => equal digest, unequal bytes =>
      unequal digest (no normalization is applied to the input).

These use the real cairn_oci digest/manifest APIs with no mocking of the thing
under test. The signature-level half of scenario (a) — "a signature over one
never verifies the other" — is NOT unit-testable here: it requires Notation +
AWS Signer integration. The digest-distinctness assertions below are the
unit-testable core of that claim; Notation verifies by digest, so distinct
Manifest_Digests are precisely what makes one bundle's signature reject the
other. The end-to-end signature claim is covered by Notation's
verify-by-digest behavior (signing capability + integration).

The store treats the *stored bytes* as identity: the Manifest_Digest is
`compute_sha256_digest` over the manifest's stored JSON bytes (the bytes ECR
holds), which embed each layer's content-address. There is deliberately no
canonicalization/re-serialization on the verification trust path, so these
tests model identity as "digest of bytes" throughout, mirroring how the
pipeline parses manifests via `OciManifest.model_validate(manifest_dict)` from
the original stored bytes rather than re-serializing a logical document.
"""

from __future__ import annotations

import json

from cairn_oci.digest import compute_sha256_digest
from cairn_oci.manifest import Descriptor, OciManifest

# ---------------------------------------------------------------------------
# Two semantically-equivalent OSCAL documents that differ only in byte
# serialization. Same logical content (a tiny OSCAL system-security-plan
# fragment), different key order and whitespace.
# ---------------------------------------------------------------------------

# Variant A: keys in one order, compact separators.
_OSCAL_DOC_A = (
    b'{"system-security-plan":{"uuid":"11111111-1111-1111-1111-111111111111",'
    b'"metadata":{"title":"Example SSP","version":"1.0"}}}'
)

# Variant B: SAME logical document — keys reordered, extra whitespace/newlines.
# json.loads(A) == json.loads(B) holds (asserted below), yet the bytes differ.
_OSCAL_DOC_B = b"""{
    "system-security-plan": {
        "metadata": {
            "version": "1.0",
            "title": "Example SSP"
        },
        "uuid": "11111111-1111-1111-1111-111111111111"
    }
}"""


def _manifest_bytes_over_layers(layers: list[Descriptor]) -> bytes:
    """Serialize an Oci manifest to the JSON bytes ECR would store.

    The Manifest_Digest is the sha256 of these bytes; because each layer
    Descriptor carries its content-address `digest`, the manifest bytes
    embed (Merkle-commit to) every layer's digest.
    """
    config = Descriptor(
        mediaType="application/vnd.cairnproofs.artifact.config.v1+json",
        digest=compute_sha256_digest(b"{}"),
        size=2,
    )
    manifest = OciManifest(config=config, layers=layers)
    # by_alias mirrors the camelCase mediaType/schemaVersion that the stored
    # OCI manifest bytes carry.
    return manifest.model_dump_json(by_alias=True).encode("utf-8")


def _layer_for(
    content: bytes, *, media_type: str = "application/vnd.cairnproofs.artifact.layer.v1"
) -> Descriptor:
    return Descriptor(
        mediaType=media_type,
        digest=compute_sha256_digest(content),
        size=len(content),
    )


# ---------------------------------------------------------------------------
# (a) Byte-distinct but semantically-equivalent documents are distinct artifacts.
# ---------------------------------------------------------------------------


def test_equivalent_oscal_docs_are_byte_distinct_but_semantically_equal() -> None:
    # Precondition for the whole scenario: the two documents are the SAME
    # logical OSCAL document (parse-equal) but DIFFERENT bytes.
    assert _OSCAL_DOC_A != _OSCAL_DOC_B
    assert json.loads(_OSCAL_DOC_A) == json.loads(_OSCAL_DOC_B)


def test_equivalent_oscal_docs_yield_distinct_content_digests() -> None:
    # No canonicalization is applied: distinct bytes => distinct content digest,
    # even though the documents are semantically identical.
    assert compute_sha256_digest(_OSCAL_DOC_A) != compute_sha256_digest(_OSCAL_DOC_B)


def test_equivalent_oscal_docs_yield_distinct_layer_and_manifest_digests() -> None:
    # The distinct content digests propagate to distinct layer descriptors and
    # therefore distinct Manifest_Digests: each is its own artifact, and (since
    # Notation verifies by digest) a signature over one never verifies the
    # other. Signature-level rejection is covered by Notation integration; the
    # digest distinctness here is its unit-testable core.
    layer_a = _layer_for(_OSCAL_DOC_A)
    layer_b = _layer_for(_OSCAL_DOC_B)
    assert layer_a.digest != layer_b.digest

    manifest_digest_a = compute_sha256_digest(_manifest_bytes_over_layers([layer_a]))
    manifest_digest_b = compute_sha256_digest(_manifest_bytes_over_layers([layer_b]))
    assert manifest_digest_a != manifest_digest_b


# ---------------------------------------------------------------------------
# (b) The manifest commits to all its layers (scenario: "Manifest commits to
#     all layers"). Altering any layer's bytes changes that layer's descriptor
#     digest, which changes the Manifest_Digest — breaking the commitment.
# ---------------------------------------------------------------------------


def test_manifest_digest_changes_when_a_layer_byte_is_altered() -> None:
    original_layer = _layer_for(b"layer-payload-v1")
    other_layer = _layer_for(b"second-layer-unchanged")
    original_manifest_digest = compute_sha256_digest(
        _manifest_bytes_over_layers([original_layer, other_layer])
    )

    # Flip one byte of the first layer's content after the fact.
    tampered_layer = _layer_for(b"layer-payload-vX")
    assert tampered_layer.digest != original_layer.digest

    tampered_manifest_digest = compute_sha256_digest(
        _manifest_bytes_over_layers([tampered_layer, other_layer])
    )
    # The manifest's layer descriptor no longer matches the original layer
    # digest, so the Manifest_Digest moves: verification against the original
    # signature (which is bound to original_manifest_digest) would fail.
    assert tampered_manifest_digest != original_manifest_digest


def test_manifest_digest_is_stable_when_layer_bytes_are_unchanged() -> None:
    # Same layer bytes => same layer digests => same manifest bytes => same
    # Manifest_Digest. The commitment is deterministic, not merely sensitive.
    layers = [_layer_for(b"alpha"), _layer_for(b"beta")]
    first = compute_sha256_digest(_manifest_bytes_over_layers(layers))
    # Rebuild fresh descriptors from the identical bytes.
    layers_again = [_layer_for(b"alpha"), _layer_for(b"beta")]
    second = compute_sha256_digest(_manifest_bytes_over_layers(layers_again))
    assert first == second


def test_manifest_digest_distinguishes_layer_order() -> None:
    # Layer ordering is part of the committed manifest bytes: reordering layers
    # is a distinct artifact under the byte-identity model (no canonicalization
    # collapses the two).
    layer_a = _layer_for(b"alpha")
    layer_b = _layer_for(b"beta")
    forward = compute_sha256_digest(_manifest_bytes_over_layers([layer_a, layer_b]))
    reversed_ = compute_sha256_digest(_manifest_bytes_over_layers([layer_b, layer_a]))
    assert forward != reversed_


# ---------------------------------------------------------------------------
# (c) Digest is content-only and canonicalization-independent.
# ---------------------------------------------------------------------------


def test_digest_is_content_only_equal_bytes_equal_digest() -> None:
    payload = b'{"a":1,"b":2}'
    assert compute_sha256_digest(payload) == compute_sha256_digest(bytes(payload))


def test_digest_applies_no_json_canonicalization() -> None:
    # If any JSON canonicalization were applied before hashing, these two
    # parse-equal documents would collide. They MUST NOT: the digest is over
    # raw bytes.
    assert json.loads(_OSCAL_DOC_A) == json.loads(_OSCAL_DOC_B)
    assert compute_sha256_digest(_OSCAL_DOC_A) != compute_sha256_digest(_OSCAL_DOC_B)


def test_digest_distinguishes_whitespace_only_difference() -> None:
    # The minimal canonicalization-independence case: a single added space.
    compact = b'{"k":"v"}'
    spaced = b'{"k": "v"}'
    assert json.loads(compact) == json.loads(spaced)
    assert compute_sha256_digest(compact) != compute_sha256_digest(spaced)


# ---------------------------------------------------------------------------
# Content-type agnosticism (scenario: "Verification is content-type agnostic").
# The Manifest_Digest commitment is computed identically whether layers are
# OSCAL, non-OSCAL, or a mix — the cryptographic guarantee is independent of
# payload semantics, because it is over bytes, not parsed content.
# ---------------------------------------------------------------------------


def test_manifest_digest_is_computed_identically_for_non_oscal_layers() -> None:
    # A bundle with ONLY a non-OSCAL (binary) layer commits exactly the same way
    # as one with an OSCAL layer: the manifest digest is over the layer's
    # content-address, with no payload-semantics branch.
    binary_layer = _layer_for(
        b"\x00\x01\x02PDF-ish-bytes\xff", media_type="application/octet-stream"
    )
    first = compute_sha256_digest(_manifest_bytes_over_layers([binary_layer]))
    again = compute_sha256_digest(
        _manifest_bytes_over_layers(
            [_layer_for(b"\x00\x01\x02PDF-ish-bytes\xff", media_type="application/octet-stream")]
        )
    )
    # Same bytes → same digest, regardless of (non-OSCAL) media type.
    assert first == again


def test_mixed_oscal_and_non_oscal_layers_commit_to_each_layer() -> None:
    # A mixed bundle (OSCAL layer + non-OSCAL layer): altering EITHER layer's
    # bytes moves the Manifest_Digest. The commitment is not OSCAL-specific.
    oscal_layer = _layer_for(_OSCAL_DOC_A)
    blob_layer = _layer_for(b"attachment-bytes", media_type="application/octet-stream")
    baseline = compute_sha256_digest(_manifest_bytes_over_layers([oscal_layer, blob_layer]))

    tampered_oscal = compute_sha256_digest(
        _manifest_bytes_over_layers([_layer_for(_OSCAL_DOC_B), blob_layer])
    )
    tampered_blob = compute_sha256_digest(
        _manifest_bytes_over_layers(
            [oscal_layer, _layer_for(b"attachment-bytez", media_type="application/octet-stream")]
        )
    )
    assert tampered_oscal != baseline
    assert tampered_blob != baseline

"""RED reproduction tests for the deterministic-resource-uuid bugfix.

These tests encode the *Expected Behavior* of the bugfix change
`deterministic-resource-uuid`: `make_artifact_reference` must derive
`resource.uuid` deterministically as `uuid5(CAIRN_NAMESPACE, manifest_digest)`
(byte-identical across independent calls), and must record an OSCAL
`document-ids[]` entry carrying either the supplied OSCAL root `uuid` or — when
absent — the same `uuid5(...)` document identity, distinct from `resource.uuid`.

They are written to FAIL against unfixed `main`, where:
  - `resource.uuid` is a fresh `uuid4()` (so two calls mismatch and never equal
    the `uuid5` derivation), and
  - `make_artifact_reference` has no `oscal_root_uuid` parameter and emits no
    `document_ids`.

The `CAIRN_NAMESPACE` is re-derived INLINE here (`uuid5(NAMESPACE_DNS,
"cairnproofs.dev")`) rather than imported, so the derivation is pinned
independently of the production constant.

These tests pin the POST-FLIP contract: `make_artifact_reference` accepts the
canonical PREFIXED `sha256:<hex>` Manifest_Digest and derives the default
`resource.uuid` as `str(uuid5(CAIRN_NAMESPACE, "sha256:<hex>"))` over the
prefixed form (metadata-index *Deterministic derived identity*).
"""

from __future__ import annotations

import uuid

from cairn_oscal.reference import make_artifact_reference
from hypothesis import given
from hypothesis import strategies as st

# Re-derived inline (do NOT import CAIRN_NAMESPACE) so the derivation is pinned
# independently of the production constant.
_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "cairnproofs.dev")

ECR_URI_TMPL = "123456789012.dkr.ecr.us-east-2.amazonaws.com/cairn/alpha-bank@{d}"
S3_URI_TMPL = "s3://cairn-alpha-archive-us-east-2/alpha-bank/abc/{h}.tar"

# 64 lowercase hex characters; fed to the builder as the prefixed sha256:<hex>.
_hex64 = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)


@given(hex_body=_hex64)
def test_default_uuid_is_deterministic_across_calls(hex_body: str) -> None:
    """Expected #1/#2: two independent calls with no resource_uuid yield a
    byte-identical resource.uuid for the same manifest_digest."""
    digest = f"sha256:{hex_body}"
    ecr = ECR_URI_TMPL.format(d=digest)
    s3 = S3_URI_TMPL.format(h=hex_body)
    r1 = make_artifact_reference(manifest_digest=digest, ecr_uri=ecr, s3_uri=s3)
    r2 = make_artifact_reference(manifest_digest=digest, ecr_uri=ecr, s3_uri=s3)
    assert r1.uuid == r2.uuid


@given(hex_body=_hex64)
def test_default_uuid_equals_uuid5_of_prefixed_digest(hex_body: str) -> None:
    """Expected #1 (exact derivation): the default resource.uuid equals
    str(uuid5(CAIRN_NAMESPACE, "sha256:<hex>")) — derived over the PREFIXED form."""
    digest = f"sha256:{hex_body}"
    ecr = ECR_URI_TMPL.format(d=digest)
    s3 = S3_URI_TMPL.format(h=hex_body)
    resource = make_artifact_reference(manifest_digest=digest, ecr_uri=ecr, s3_uri=s3)
    assert resource.uuid == str(uuid.uuid5(_NS, digest))


def test_oscal_root_uuid_recorded_as_distinct_document_id() -> None:
    """Expected #4: a supplied oscal_root_uuid is recorded in document_ids[0]
    and is a distinct identity from resource.uuid."""
    hex_body = "a" * 64
    digest = f"sha256:{hex_body}"
    root_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    resource = make_artifact_reference(
        manifest_digest=digest,
        ecr_uri=ECR_URI_TMPL.format(d=digest),
        s3_uri=S3_URI_TMPL.format(h=hex_body),
        oscal_root_uuid=root_uuid,
    )
    assert resource.document_ids is not None
    assert resource.document_ids[0].identifier == root_uuid
    assert resource.document_ids[0].identifier != resource.uuid


def test_omitted_root_uuid_records_uuid5_document_id() -> None:
    """Expected #5: when oscal_root_uuid is omitted, document_ids[0] carries
    str(uuid5(CAIRN_NAMESPACE, "sha256:<hex>")) so the citation still has a
    stable document identity."""
    hex_body = "b" * 64
    digest = f"sha256:{hex_body}"
    resource = make_artifact_reference(
        manifest_digest=digest,
        ecr_uri=ECR_URI_TMPL.format(d=digest),
        s3_uri=S3_URI_TMPL.format(h=hex_body),
    )
    assert resource.document_ids is not None
    assert resource.document_ids[0].identifier == str(uuid.uuid5(_NS, digest))

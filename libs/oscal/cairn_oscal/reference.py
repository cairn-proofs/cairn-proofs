"""OSCAL artifact reference generation ([OSCAL-007.01]).

Produces an OSCAL `BackMatterResource` element (suitable for inclusion in a
back-matter `resources[]` list) with two `Rlink` entries — one for the
ECR URI, one for the S3 URI — each carrying a SHA-256 `Hash` equal to
the bundle's Manifest_Digest. Both rlinks are generated regardless of
`WORM_Enabled`: the archive workflow always writes to S3, so the S3
URI is meaningful even when Object Lock is not applied.

The validate-bundle Lambda includes the returned `BackMatterResource` in the
`artifact.validated` event payload so downstream consumers don't have
to re-derive it.
"""

from __future__ import annotations

from cairn_oci import CAIRN_NAMESPACE as CAIRN_NAMESPACE  # re-export for callers/tests
from cairn_oci import resource_uuid_for
from cairn_oci.digest import parse_digest
from oscal_bindings import (
    BackMatterResource,
    DocumentId,
    make_hash,
    make_resource,
    make_rlink,
)

OCI_IMAGE_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"


def make_artifact_reference(
    *,
    manifest_digest: str,
    ecr_uri: str,
    s3_uri: str,
    title: str | None = None,
    description: str | None = None,
    resource_uuid: str | None = None,
    oscal_root_uuid: str | None = None,
) -> BackMatterResource:
    """Build the OSCAL artifact reference for one Artifact_Bundle.

    Args:
        manifest_digest: The **canonical prefixed** `sha256:<hex>`
            Manifest_Digest (64 lowercase hex chars after the prefix).
            The derived identity is computed over this exact prefixed form
            so every independent re-derivation byte-matches (metadata-index
            *Deterministic derived identity*). [OSCAL-007.01] requires the rlink
            hashes to match the Manifest_Digest, so a malformed digest here
            would silently produce an artifact reference that no consumer can
            verify against the bundle.
        ecr_uri: Canonical ECR URI for the bundle (e.g.,
            `<acct>.dkr.ecr.us-east-2.amazonaws.com/<repo>@sha256:<digest>`).
        s3_uri: S3 URI for the archived bundle (e.g.,
            `s3://<bucket>/<key>`).
        title: Optional display title for the resource.
        description: Optional summary.
        resource_uuid: Optional explicit UUID. When omitted, the resource
            UUID is derived deterministically as
            ``uuid5(CAIRN_NAMESPACE, "sha256:<hex>")`` over the canonical
            prefixed digest (metadata-index *Deterministic derived identity
            over the canonical Manifest_Digest*) so the same bundle always
            yields the same identity and the Metadata_Index stays rebuildable
            from ECR + S3 alone (which that derivation underpins — [META-012]).
        oscal_root_uuid: Optional OSCAL document root ``uuid`` to record as a
            distinct identity in the resource's ``document-ids[]``. When
            omitted, the deterministic digest-derived UUID is recorded there
            instead so the citation still carries a stable document identity.

    Raises:
        ValueError: If `manifest_digest` is not the canonical prefixed
            `sha256:<hex>` form (a bare 64-hex digest or any malformed value
            is rejected — see `cairn_oci.identity.resource_uuid_for` and
            `cairn_oci.digest.parse_digest`).
    """
    # Both calls validate the prefixed contract: resource_uuid_for rejects a
    # missing sha256: prefix, parse_digest rejects any non-sha256:<64-hex> form.
    derived_uuid = resource_uuid_for(manifest_digest)
    digest_hex = parse_digest(manifest_digest)

    digest_hash = make_hash(digest_hex)
    rlinks = [
        make_rlink(
            ecr_uri,
            media_type=OCI_IMAGE_MANIFEST_MEDIA_TYPE,
            hashes=[digest_hash],
        ),
        make_rlink(
            s3_uri,
            media_type=OCI_IMAGE_MANIFEST_MEDIA_TYPE,
            hashes=[digest_hash],
        ),
    ]
    document_id = DocumentId(identifier=oscal_root_uuid or derived_uuid)
    return make_resource(
        title=title,
        description=description,
        rlinks=rlinks,
        uuid=resource_uuid if resource_uuid is not None else derived_uuid,
        document_ids=[document_id],
    )

"""OCI manifest pydantic models ([INGEST-010], [INGEST-011]).

A minimal subset of the OCI image-spec manifest sufficient for the
Provenance_System's validate/sign/archive pipeline: schemaVersion, config
Descriptor, layers[], annotations.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# [INGEST-003.04] bundle bounds (shared single source of truth so client/server/archive
# cannot drift). See docs/adr/0014-metadata-index-item-budget-governs-layer-cap.md.
#
# Content-layer count: 1..MAX_CONTENT_LAYERS. The cap is governed by the
# single-row Metadata_Index item budget — 300 is the worst-case-safe max under
# the current per-layer projection (~352 KB < 400 KB DynamoDB item limit).
MAX_CONTENT_LAYERS = 300

# Per-artifact (per-blob) size ceiling. The byte basis is 10 GiB (binary),
# matching the existing ``_MAX_BLOB_BYTES = 10 * 1024 * 1024 * 1024`` in both
# ``lambdas/validate_bundle/.../ecr_client.py`` and
# ``lambdas/archive_to_s3/.../ecr_client.py`` — kept as a single shared constant
# here so the client/server/archive bounds cannot drift apart.
MAX_ARTIFACT_SIZE_BYTES = 10 * 1024**3


class Descriptor(BaseModel):
    """OCI Descriptor — mediaType + digest + size, optional annotations."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    media_type: str = Field(alias="mediaType")
    digest: str
    size: int
    annotations: dict[str, str] = Field(default_factory=dict)


class OciManifest(BaseModel):
    """OCI manifest — references one Config_Blob descriptor and 1..300 layers ([INGEST-010])."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_version: int = Field(alias="schemaVersion", default=2)
    media_type: str = Field(
        alias="mediaType",
        default="application/vnd.oci.image.manifest.v1+json",
    )
    config: Descriptor
    layers: list[Descriptor] = Field(default_factory=list, max_length=MAX_CONTENT_LAYERS)
    annotations: dict[str, str] = Field(default_factory=dict)

"""Config_Blob pydantic model ([INGEST-016]).

Carries ONLY **content-descriptive** bundle-level metadata: project-id (a
content-descriptive, IAM-grounded identifier, Req 318) and content-descriptive
OSCAL references. The Config_Blob is part of the content Manifest_Digest, so it
MUST NOT carry the producer's compliance claim — which lives in the
separately-signed :class:`cairn_oci.producer_attestation.ProducerAttestationReferrer` (see
artifact-ingestion "Config_Blob Media Type and OSCAL Schema Version" and
"Producer Claim Carried as Signed Attestation Referrer").

artifact-kind is likewise NOT a Config_Blob field and is not a
content-manifest annotation, so kind — a producer label, not intrinsic to the
content bytes — does not perturb the content Manifest_Digest. Kind is carried
solely as the Producer_Attestation_Referrer's
``artifactKind`` claim (see "Bundle Identity Is Pure Content Addressing").
``projectId`` is deliberately retained: it stays content-descriptive and IS part
of the digest (Req 318).

The media type is v1.

For bundles containing any `application/oscal.*` layer, the OSCAL schema version
is content-derived from the document's own `metadata.oscal-version`; a declared
`oscalSchemaVersion` field is not required and not read. If a
producer supplies it, the value is tolerated and ignored.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

CONFIG_BLOB_MEDIA_TYPE: Final[str] = "application/vnd.cairnproofs.artifact.config.v1+json"

# Pipeline-internal / non-artifact-bundle media types. These artifacts are
# produced by the Provenance_System's own ingestion and signing flow (notably the
# Notation signature attached as an OCI Referrer, which itself generates an ECR
# PutImage event) and are NOT producer-submitted Artifact_Bundles. The validate
# stage skips them before [INGEST-017.07] (see artifact-ingestion spec).
NOTARY_SIGNATURE_MEDIA_TYPE: Final[str] = "application/vnd.cncf.notary.signature"
OCI_EMPTY_MEDIA_TYPE: Final[str] = "application/vnd.oci.empty.v1+json"
OCI_IMAGE_INDEX_MEDIA_TYPE: Final[str] = "application/vnd.oci.image.index.v1+json"

# Resource-as-referrer discovery labels. A resource artifact is attached to its
# subject manifest as an OCI Referrer carrying this `artifactType`, with an
# empty (`OCI_EMPTY_MEDIA_TYPE`) config. The referrer is skipped before
# [INGEST-017.07] purely on its empty config mediaType — these labels are
# discovery-only and MUST NOT participate in `classify_pipeline_internal`'s skip
# decision (keeping `artifactType` out of the skip path keeps the evasion
# surface narrow).
RESOURCE_ARTIFACT_TYPE: Final[str] = "application/vnd.cairnproofs.artifact.resource.v1"
RESOURCE_MEDIA_TYPE: Final[str] = "application/vnd.cairnproofs.artifact.resource.v1+json"

# Pipeline-minted OCI referrer artifactType single source of truth. The three
# referrers the pipeline attaches to a subject manifest are the Notation
# signature, the OSCAL-resource artifact, and the WORM_Receipt. Their canonical
# artifactType values live here so the TUI classifiers (and any other consumer)
# import them rather than re-declaring literals that can drift. The signature's
# artifactType doubles as its config mediaType, so it is the same value as
# NOTARY_SIGNATURE_MEDIA_TYPE above; the alias names that role explicitly.
NOTARY_SIGNATURE_ARTIFACT_TYPE: Final[str] = NOTARY_SIGNATURE_MEDIA_TYPE
WORM_RECEIPT_ARTIFACT_TYPE: Final[str] = "application/vnd.cairnproofs.artifact.worm-receipt.v1"
# Annotation on the WORM_Receipt referrer manifest carrying the bundle's
# Retain_Until in RFC 3339 UTC ([WORM-005]). Producer-writable like any ECR
# annotation, so readers treat it as display / pre-filter only ([RETAIN-012]).
RETAIN_UNTIL_ANNOTATION: Final[str] = "dev.cairnproofs.artifact.retain-until"


class SkipReason(StrEnum):
    """Why a pushed OCI artifact was classified pipeline-internal and skipped.

    Used as the ``SkipReason`` dimension on the ``ValidationSkipped`` metric.
    """

    NOTARY_SIGNATURE = "notary_signature"
    OCI_EMPTY = "oci_empty"
    IMAGE_INDEX = "image_index"


def classify_pipeline_internal(manifest_dict: dict[str, Any]) -> SkipReason | None:
    """Classify a fetched OCI manifest as pipeline-internal, or not.

    Pure, fail-closed predicate: returns a :class:`SkipReason` ONLY
    when the manifest positively matches a known pipeline-internal media type,
    and ``None`` for everything else — including the Cairn artifact Config_Blob
    type and any unrecognized/absent type. A ``None`` result means the artifact
    proceeds to [INGEST-017.07] bundle validation.

    Operates on the raw manifest dict (the handler has this before/independent
    of :class:`OciManifest` parsing) and tolerates a missing ``config`` key
    without raising — that is the OCI image-index case.

    Precedence: a top-level image-index ``mediaType`` is classified
    ``IMAGE_INDEX`` regardless of any ``config`` it might carry.
    """
    if manifest_dict.get("mediaType") == OCI_IMAGE_INDEX_MEDIA_TYPE:
        return SkipReason.IMAGE_INDEX

    config = manifest_dict.get("config")
    config_media_type = config.get("mediaType") if isinstance(config, dict) else None

    if config_media_type == NOTARY_SIGNATURE_MEDIA_TYPE:
        return SkipReason.NOTARY_SIGNATURE
    if config_media_type == OCI_EMPTY_MEDIA_TYPE:
        return SkipReason.OCI_EMPTY

    return None


class OscalReferences(BaseModel):
    """OSCAL UUID references stored in the Config_Blob ([OSCAL-006])."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    ar_uuid: str | None = Field(default=None, alias="arUuid")
    ssp_uuid: str | None = Field(default=None, alias="sspUuid")
    findings: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)


class ConfigBlob(BaseModel):
    """Content-descriptive bundle metadata.

    Media type ``application/vnd.cairnproofs.artifact.config.v1+json``.
    Reduced to content-descriptive fields only — the producer's compliance claim
    lives in :class:`cairn_oci.producer_attestation.ProducerAttestationReferrer` and is not
    part of the content Manifest_Digest.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # Project ID as a content-descriptive identifier (spec: REQUIRED). It stays
    # in the Config_Blob and IS part of the content Manifest_Digest (Req 318);
    # the claim carries a copy too, but this content-descriptive retention is
    # deliberate (see "Bundle Identity Is Pure Content Addressing").
    project_id: str = Field(alias="projectId")

    # Tolerated-and-ignored field. The OSCAL schema version is derived
    # from each OSCAL layer's own `metadata.oscal-version`, never from
    # this declaration. Present on the model so producers that emit it
    # still validate; the value is not read.
    oscal_schema_version: str | None = Field(default=None, alias="oscalSchemaVersion")
    oscal_references: OscalReferences | None = Field(default=None, alias="oscalReferences")

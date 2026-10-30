"""Annotation key constants for OCI manifests and content layers ([INGEST-013], [INGEST-014]).

Manifest-level required annotations ([INGEST-013]) are the irreducible identity set
only: project-id. There is NO creation-timestamp requirement — a *wall-clock*
`org.opencontainers.image.created` MUST NOT be folded into the manifest, because
per-push `now()` pollutes content identity (see artifact-ingestion "Bundle
Identity Is Pure Content Addressing").

artifact-kind is NOT a manifest annotation: kind — a producer label, not
intrinsic to the content bytes — is not carried on the content manifest, so it
does not perturb the content Manifest_Digest. Kind is carried solely as the
Producer_Attestation_Referrer's `artifactKind` claim; no artifact-kind
annotation constant is defined.

Subtlety: `oras push` (the producer's packaging tool) AUTO-INJECTS
`org.opencontainers.image.created` with the wall-clock push time unless the
producer supplies the key itself, and oras 1.3.2 ignores `SOURCE_DATE_EPOCH`.
"Just don't set it" is therefore NOT enough — leaving it unset lets oras stamp
`now()`, which is exactly the ambient state the spec forbids and makes the
content digest non-reproducible (identical content re-pushed mints a new
bundle). So the producer pins the key to :data:`PINNED_CREATED` — a fixed
epoch sentinel carrying no ambient information — which neutralizes oras's
injection and keeps the content Manifest_Digest a pure function of content
bytes. The annotation is present but deterministic; temporal provenance lives in
`ecrPushedAt` (EventBridge eventTime) and per-OSCAL-layer `metadata.last-modified`.

The producer's compliance claim (controls, assessment-period, assessor,
assessment-id, supersedes) is NOT a manifest annotation at all — it lives in the
separately-signed Producer_Attestation_Referrer (see
:class:`cairn_oci.producer_attestation.ProducerAttestationReferrer`), not on the content manifest.
`control-family` is likewise NOT a manifest annotation — it is a derived
projection of the control identifiers, computed on demand by downstream consumers
and never carried in the bundle. The claim-field constant keys below are defined
on :class:`CairnAnnotations` for importers,
but they are intentionally absent from both the REQUIRED and OPTIONAL manifest
annotation sets.

Layer-level required annotation: `org.opencontainers.image.title` ([INGEST-014]);
optional: source-tool, generation-timestamp.
"""

from typing import Final


class CairnAnnotations:
    """Cairn-namespaced manifest annotation keys."""

    PROJECT_ID: Final[str] = "dev.cairnproofs.artifact.project-id"
    CONTROLS: Final[str] = "dev.cairnproofs.artifact.controls"
    # NOTE: there is intentionally no control-family annotation — control-family is
    # a derived projection of the control ids (see cairn_oci.controls.derive_families),
    # never bundle data. Do not re-introduce one.
    ASSESSMENT_PERIOD: Final[str] = "dev.cairnproofs.artifact.assessment-period"
    CREATED: Final[str] = "org.opencontainers.image.created"

    ASSESSOR: Final[str] = "dev.cairnproofs.artifact.assessor"
    ASSESSMENT_ID: Final[str] = "dev.cairnproofs.artifact.assessment-id"
    SUPERSEDES: Final[str] = "dev.cairnproofs.artifact.supersedes"

    SOURCE_TOOL: Final[str] = "dev.cairnproofs.artifact.source-tool"
    GENERATION_TIMESTAMP: Final[str] = "dev.cairnproofs.artifact.generation-timestamp"


# The fixed value the producer pins `org.opencontainers.image.created` to, so
# `oras push` does not auto-inject a wall-clock timestamp (oras 1.3.2 ignores
# SOURCE_DATE_EPOCH; supplying the key ourselves overrides its auto value). The
# Unix epoch is a deterministic sentinel carrying no ambient state, keeping the
# content Manifest_Digest reproducible (artifact-ingestion "Manifest Digest
# Computation": no wall-clock now() in the digest input).
PINNED_CREATED: Final[str] = "1970-01-01T00:00:00Z"


# Layer-level required annotation ([INGEST-014]) — the OCI image-spec standard key,
# not Cairn-namespaced. Producers MUST set this on every content layer.
LAYER_REQUIRED_TITLE: Final[str] = "org.opencontainers.image.title"


# The irreducible identity set: project-id. There is NO creation-timestamp
# requirement — `org.opencontainers.image.created` is pinned to PINNED_CREATED (a
# fixed epoch) rather than carrying wall-clock `now()`, so it is not part of the
# meaningful identity set and never required/read as a real time. artifact-kind is
# NOT in this set: kind is not on the content manifest; it is carried only by
# the Producer_Attestation_Referrer claim. The producer's compliance claim is likewise not a
# manifest annotation at all; it lives in the Producer_Attestation_Referrer.
MANIFEST_REQUIRED: Final[frozenset[str]] = frozenset(
    {
        CairnAnnotations.PROJECT_ID,
    }
)

# No manifest-level optional annotations remain. The producer claim (controls,
# assessment-period, assessor, assessment-id, supersedes) lives in the
# Producer_Attestation_Referrer, not on the content manifest. The constant keys
# remain defined on `CairnAnnotations` but are intentionally absent from this set.
MANIFEST_OPTIONAL: Final[frozenset[str]] = frozenset()

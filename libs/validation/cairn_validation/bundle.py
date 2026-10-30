"""Bundle validation completeness per [INGEST-017.07]
([INGEST-013], [INGEST-014], [INGEST-016], [INGEST-017], [INGEST-029]).

Returns a structured ValidationResult enumerating ALL failing criteria when
invalid, so callers can surface every problem at once rather than
fail-on-first.

Split by trust origin:
- :func:`validate_bundle` covers the **content manifest** structure — the
  required identity annotations, layer titles, and the content-descriptive
  Config_Blob (media type + parse). These are pure functions of the content
  bytes.
- :func:`validate_claim` covers the producer's **compliance claim**, carried
  in the signed :class:`~cairn_oci.producer_attestation.ProducerAttestationReferrer` rather than
  the Config_Blob: the artifact-kind controlled-vocabulary membership, the
  well-formed ``catalogOrProfileUuid``, and the well-formed ``controls``
  identifiers. A malformed claim is a permanent validation failure.

artifact-kind is not part of the content manifest (no ``bundleType``
Config_Blob field, no ``dev.cairnproofs.artifact.artifact-kind`` annotation), so
``MANIFEST_REQUIRED`` does not require it. Kind correctness rests solely on the claim
vocabulary check (criterion (d)) in :func:`validate_claim`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from cairn_oci.annotations import LAYER_REQUIRED_TITLE, MANIFEST_REQUIRED
from cairn_oci.categories import (
    ARTIFACT_KINDS,
    InvalidArtifactKindError,
    normalize_artifact_kind,
)
from cairn_oci.config_blob import CONFIG_BLOB_MEDIA_TYPE, ConfigBlob
from cairn_oci.controls import is_valid_control_id
from cairn_oci.manifest import (
    MAX_ARTIFACT_SIZE_BYTES,
    MAX_CONTENT_LAYERS,
    Descriptor,
    OciManifest,
)
from cairn_oci.producer_attestation import ProducerAttestationReferrer
from pydantic import ValidationError as PydanticValidationError

# OSCAL UUIDDatatype: type-4 or type-5 UUID, per the OSCAL 1.x schema's
# UUIDDatatype regex. Used to validate the Config_Blob `catalogOrProfileUuid`
# field ([INGEST-016]).
_OSCAL_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[45][0-9A-Fa-f]{3}-[89ABab][0-9A-Fa-f]{3}-[0-9A-Fa-f]{12}$"
)


@dataclass
class BundleValidationError:
    """One failing criterion from [INGEST-017.07].

    `criterion` is a short stable code (e.g., "a", "b1", "e"); `field` names
    the offending field path; `message` is human-readable.
    """

    criterion: str
    field: str
    message: str


class BundleValidationFailure(ValueError):
    """Raised by validate_bundle when one or more criteria fail.

    The full error list is on `.errors`; the str() form joins them for log
    output.
    """

    def __init__(self, errors: list[BundleValidationError]) -> None:
        self.errors = errors
        msg = "; ".join(f"[{e.criterion}] {e.field}: {e.message}" for e in errors)
        super().__init__(msg)


@dataclass
class ValidationResult:
    manifest: OciManifest
    config_blob: ConfigBlob | None = None
    errors: list[BundleValidationError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        if self.errors:
            raise BundleValidationFailure(self.errors)


def _check_manifest_annotations(
    manifest: OciManifest,
    errors: list[BundleValidationError],
) -> None:
    """[INGEST-017.07] criterion (a): all required manifest-level annotations present."""
    missing = MANIFEST_REQUIRED - set(manifest.annotations)
    for key in sorted(missing):
        errors.append(
            BundleValidationError(
                criterion="a",
                field=f"manifest.annotations[{key}]",
                message="required manifest annotation missing",
            )
        )


def _check_layer_titles(
    manifest: OciManifest,
    errors: list[BundleValidationError],
) -> None:
    """[INGEST-017.07] criterion (b): every layer has the required title annotation.

    The required annotation is ``org.opencontainers.image.title`` ([INGEST-014]).
    """
    for idx, layer in enumerate(manifest.layers):
        if not layer.annotations.get(LAYER_REQUIRED_TITLE):
            errors.append(
                BundleValidationError(
                    criterion="b",
                    field=f"manifest.layers[{idx}].annotations[{LAYER_REQUIRED_TITLE}]",
                    message="required layer annotation missing",
                )
            )


def _check_layer_count(
    manifest: OciManifest,
    errors: list[BundleValidationError],
) -> None:
    """[INGEST-003.04] which-constraint (i1/i2): content-layer count is within bounds.

    A bundle MUST carry at least one content layer (i1) and at most
    ``MAX_CONTENT_LAYERS`` (i2). The count is taken from the descriptor list,
    which is un-spoofable at the trust boundary — no blob fetch involved.
    """
    count = len(manifest.layers)
    if count < 1:
        errors.append(
            BundleValidationError(
                criterion="i1",
                field="manifest.layers",
                message="content-layer count must be at least 1; got 0",
            )
        )
    elif count > MAX_CONTENT_LAYERS:
        errors.append(
            BundleValidationError(
                criterion="i2",
                field="manifest.layers",
                message=(f"content-layer count must be at most {MAX_CONTENT_LAYERS}; got {count}"),
            )
        )


def _check_layer_sizes(
    manifest: OciManifest,
    errors: list[BundleValidationError],
) -> None:
    """[INGEST-003.04] which-constraint (j): ADVISORY per-layer descriptor-size bound.

    Reads the producer-asserted OCI descriptor ``size`` (NO blob fetch), so this
    is the ADVISORY check — the authoritative byte-true size enforcement happens
    at archive when the blob is streamed. Each layer whose declared ``size``
    exceeds ``MAX_ARTIFACT_SIZE_BYTES`` (10 GiB) is named with its index and
    digest. A size exactly at the limit is accepted.
    """
    for idx, layer in enumerate(manifest.layers):
        if layer.size > MAX_ARTIFACT_SIZE_BYTES:
            errors.append(
                BundleValidationError(
                    criterion="j",
                    field=f"manifest.layers[{idx}].size",
                    message=(
                        f"layer[{idx}] ({layer.digest}) declared size {layer.size} "
                        f"exceeds the {MAX_ARTIFACT_SIZE_BYTES} byte (10 GiB) limit "
                        "(advisory descriptor-size check; authoritative byte-true "
                        "check is at archive)"
                    ),
                )
            )


def _check_config_blob_media_type(
    config_descriptor: Descriptor,
    errors: list[BundleValidationError],
) -> None:
    if config_descriptor.media_type != CONFIG_BLOB_MEDIA_TYPE:
        errors.append(
            BundleValidationError(
                criterion="c1",
                field="manifest.config.mediaType",
                message=(
                    f"Config_Blob media type must be {CONFIG_BLOB_MEDIA_TYPE!r}; "
                    f"got {config_descriptor.media_type!r}"
                ),
            )
        )


def _parse_config_blob(
    config_bytes: bytes | None,
    errors: list[BundleValidationError],
) -> ConfigBlob | None:
    if config_bytes is None:
        errors.append(
            BundleValidationError(
                criterion="c2",
                field="config_blob",
                message="Config_Blob body not provided to validator",
            )
        )
        return None
    try:
        raw = json.loads(config_bytes)
    except json.JSONDecodeError as e:
        errors.append(
            BundleValidationError(
                criterion="c3",
                field="config_blob.body",
                message=f"Config_Blob is not valid JSON: {e}",
            )
        )
        return None
    try:
        return ConfigBlob.model_validate(raw)
    except PydanticValidationError as e:
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"])
            errors.append(
                BundleValidationError(
                    criterion="c4",
                    field=f"config_blob.{loc}",
                    message=err["msg"],
                )
            )
        return None


def _check_artifact_kind_vocabulary(
    claim: ProducerAttestationReferrer,
    errors: list[BundleValidationError],
) -> None:
    """[INGEST-029.06] criterion (d): claim artifact-kind resolves to an Artifact_Kind.

    The artifact-kind is sourced from the Producer_Attestation_Referrer claim
    (``artifactKind``), not a content-manifest annotation. It is routed
    through the single fail-closed resolver (:func:`normalize_artifact_kind`)
    BEFORE the membership test, so a value that resolves — canonical
    OR a known alias — passes. Only a token that resolves to no canonical value
    is a criterion-(d) failure, and the error lists the allowed CANONICAL values.
    """
    try:
        normalize_artifact_kind(claim.artifact_kind)
    except InvalidArtifactKindError:
        allowed = ", ".join(sorted(ARTIFACT_KINDS))
        errors.append(
            BundleValidationError(
                criterion="d",
                field="attestation.artifactKind",
                message=(
                    f"artifact-kind {claim.artifact_kind!r} does not resolve to an "
                    f"Artifact_Kind value; allowed canonical values: {allowed}"
                ),
            )
        )


def _check_catalog_or_profile_uuid(
    claim: ProducerAttestationReferrer,
    errors: list[BundleValidationError],
) -> None:
    """[INGEST-017.07] criterion (c5): claim catalogOrProfileUuid is a well-formed OSCAL UUID.

    Control linkage is OPTIONAL: the claim `catalogOrProfileUuid` field is
    validated only when PRESENT; its absence is never an error. The
    Producer_Attestation_Referrer is the sole carrier — the Config_Blob does not
    hold it.

    UUIDDatatype per the OSCAL 1.x JSON schema: type-4 or type-5.
    """
    if claim.catalog_or_profile_uuid is not None and not _OSCAL_UUID_RE.match(
        claim.catalog_or_profile_uuid
    ):
        errors.append(
            BundleValidationError(
                criterion="c5",
                field="attestation.catalogOrProfileUuid",
                message=(
                    f"catalogOrProfileUuid {claim.catalog_or_profile_uuid!r} is not a "
                    "valid OSCAL UUID (type-4 or type-5 per OSCAL 1.x UUIDDatatype)"
                ),
            )
        )


def _check_control_ids(
    claim: ProducerAttestationReferrer,
    errors: list[BundleValidationError],
) -> None:
    """[INGEST-017.07] criterion (h): well-formed control identifiers.

    Control linkage is OPTIONAL — this check applies ONLY when `controls` are
    present in the claim. When they are, every identifier MUST be well-formed
    per the shared grammar (`is_valid_control_id` from cairn_oci.controls — do
    NOT inline the regex; the shared helper uses `fullmatch` to close the
    trailing-newline bypass). A malformed id is a permanent validation failure,
    named in the error, evaluated BEFORE any downstream family derivation or
    Controls-table PK `{catalogOrProfileUuid}#{controlId}` construction.
    """
    for control_id in claim.controls:
        if not is_valid_control_id(control_id):
            errors.append(
                BundleValidationError(
                    criterion="h",
                    field="attestation.controls",
                    message=(
                        f"control identifier {control_id!r} is malformed; must match "
                        r"^[a-z0-9]+([.-][a-z0-9]+)*$ (ASCII lowercase-alphanumeric "
                        "segments joined by single '.'/'-')"
                    ),
                )
            )


def validate_bundle(
    manifest: OciManifest,
    config_blob_bytes: bytes | None,
    *,
    raise_on_error: bool = False,
) -> ValidationResult:
    """Run the content-manifest [INGEST-017.07] criteria (a)-(c) on a manifest + config-blob pair.

    Covers the structure that is a pure function of the **content** bytes:
    criterion (a) required identity manifest annotations, (b) layer titles,
    (i1/i2) content-layer count bounds (1..``MAX_CONTENT_LAYERS``), (j) the
    ADVISORY per-layer descriptor-size bound against ``MAX_ARTIFACT_SIZE_BYTES``
    (producer-asserted descriptor ``size``, NO blob fetch — the authoritative
    byte-true check is at archive), (c1) Config_Blob media type, and (c2-c4)
    Config_Blob parse. The claim-derived
    criteria — (d) artifact-kind vocabulary, (c5) catalogOrProfileUuid, and (h)
    control-id grammar — are in :func:`validate_claim` because the producer
    claim lives in the signed Producer_Attestation_Referrer, not the Config_Blob.

    Criterion (f) — the uniform org + tenant + project cross-check against the
    ECR repository path — lives in `cairn_validation.crosscheck` since it
    requires external context (the resolved repo path) that this function does
    not have.
    There is no bundle-type-triple criterion (artifact-kind is not on the
    content manifest); kind correctness is criterion (d) in
    :func:`validate_claim`.
    """
    errors: list[BundleValidationError] = []

    _check_manifest_annotations(manifest, errors)
    _check_layer_titles(manifest, errors)
    _check_layer_count(manifest, errors)
    _check_layer_sizes(manifest, errors)
    _check_config_blob_media_type(manifest.config, errors)
    config_blob = _parse_config_blob(config_blob_bytes, errors)

    result = ValidationResult(manifest=manifest, config_blob=config_blob, errors=errors)
    if raise_on_error:
        result.raise_for_errors()
    return result


def validate_claim(
    claim: ProducerAttestationReferrer,
    *,
    raise_on_error: bool = False,
) -> list[BundleValidationError]:
    """Run the claim-derived [INGEST-017.07] / [INGEST-029.06] criteria (d),(c5),(h).

    The producer's compliance claim is carried in the signed Producer_Attestation_Referrer,
    so its well-formedness checks are sourced
    from the claim, not the content Config_Blob:

    - (d)  ``artifactKind`` resolves to an Artifact_Kind controlled-vocabulary value;
    - (c5) ``catalogOrProfileUuid``, when present, is a well-formed OSCAL UUID;
    - (h)  every ``controls`` identifier, when present, matches the control-id grammar.

    Returns ALL failing criteria so callers can surface every problem at once.
    A malformed claim is a permanent validation failure.
    """
    errors: list[BundleValidationError] = []
    _check_artifact_kind_vocabulary(claim, errors)
    _check_catalog_or_profile_uuid(claim, errors)
    _check_control_ids(claim, errors)
    if raise_on_error and errors:
        raise BundleValidationFailure(errors)
    return errors

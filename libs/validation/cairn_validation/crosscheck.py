"""Cross-check the producer-asserted identity tuple against the ECR path.

[PIPELINE-013]: the Producer_Attestation_Referrer-claimed identity triple
``orgId`` / ``tenantId`` / ``projectId`` must EACH exactly equal the
corresponding ``{org-id}`` / ``{tenant-id}`` / ``{project-id}`` segment of the
IAM-enforced ECR repository path. The path is the single uniform three-segment
shape::

    {appName}-artifact-{regionCode}-{stage}/{org-id}/{tenant-id}/{project-id}

— every segment present, none optional, none gated on a deployment mode (design
D.1, D.5). The producer's claim lives in the signed
Producer_Attestation_Referrer, not the content Config_Blob; the IAM-enforced ECR
path is the authoritative anchor each producer-asserted coordinate is checked
against. This is the same three-segment binding that scopes the [INGEST-005]
anti-hijack subject check (a referrer and its subject content manifest must
share one ``{org-id}/{tenant-id}/{project-id}`` repository path).

There is exactly one path shape, so the uniform org/tenant/project cross-check
has no third-leg or path branch to condition on.
"""

from __future__ import annotations

from cairn_oci.producer_attestation import ProducerAttestationReferrer

from cairn_validation.bundle import BundleValidationError

# The ECR namespace (first '/'-delimited path segment) is
# ``{appName}-artifact-{qualifier}`` or the bare ``artifact-{qualifier}``. The
# appName prefix is variable per deployment, so the namespace is recognized by
# its ``artifact-`` marker rather than a fixed prefix.
_ARTIFACT_PREFIX = "artifact-"
_ARTIFACT_INFIX = "-artifact-"

#: The number of logical-hierarchy segments that follow the namespace:
#: ``{org-id}/{tenant-id}/{project-id}`` — always three, never optional, never
#: branching per segment.
_IDENTITY_SEGMENT_COUNT = 3

#: The three identity coordinates, each as
#: ``(claim-model attribute, ECR-path segment label, claim-field/error label)``.
#: Iterated in path order so a mismatch error names the producer-facing claim
#: field (the camelCase wire spelling) and the path segment it failed against.
_IDENTITY_COORDINATES: tuple[tuple[str, str, str], ...] = (
    ("org_id", "org-id", "orgId"),
    ("tenant_id", "tenant-id", "tenantId"),
    ("project_id", "project-id", "projectId"),
)


def _identity_segments(repo_path: str) -> tuple[str, str, str]:
    """Return the ``(org-id, tenant-id, project-id)`` triple after the namespace.

    The ECR namespace is the FIRST ``/``-delimited segment —
    ``{appName}-artifact-{qualifier}`` (the bare ``artifact-{qualifier}``
    is also accepted). Exactly three logical-hierarchy segments follow it: there
    is one uniform path shape and nothing branches per segment. The
    namespace is IAM-enforced (a producer can only push under its own
    deployment's exact ``repository/{appName}-artifact-{qualifier}/*`` namespace),
    so this is a structural anchor used to LOCATE the trusted segments — not the
    authorization.

    Fails CLOSED (``ValueError``) when the first segment is not a recognizable
    ``artifact-`` namespace, or when the
    count of segments after it is not exactly three (a malformed path — too few,
    too many, or a stray trailing slash that appends an empty terminal segment).
    """
    parts = repo_path.split("/")
    namespace = parts[0]
    if not (namespace.startswith(_ARTIFACT_PREFIX) or _ARTIFACT_INFIX in namespace):
        raise ValueError(
            f"repo path namespace {namespace!r} is not an 'artifact-' namespace "
            f"(expected '(<appName>-)?artifact-<qualifier>'): {repo_path!r}"
        )
    rest = parts[1:]
    if len(rest) != _IDENTITY_SEGMENT_COUNT:
        raise ValueError(
            f"ECR repo path {repo_path!r} does not have exactly "
            f"{_IDENTITY_SEGMENT_COUNT} logical-hierarchy segments after the namespace "
            "(expected {appName}-artifact-{qualifier}/{org-id}/{tenant-id}/{project-id})"
        )
    return rest[0], rest[1], rest[2]


def crosscheck_identity(
    claim: ProducerAttestationReferrer,
    repo_path: str,
) -> list[BundleValidationError]:
    """[PIPELINE-013]: claim org/tenant/project exact-match the ECR path segments.

    Extracts the ``{org-id}``/``{tenant-id}``/``{project-id}`` triple from the
    IAM-enforced ECR repository path and cross-checks each, exact-match
    (case-sensitive, byte-exact — the S3/ECR address is byte-exact), against the
    corresponding producer-asserted coordinate on the
    Producer_Attestation_Referrer. The check is uniform across all three
    segments: the path is the single three-segment shape, so every coordinate is
    present and none is gated on a deployment mode.

    Returns ALL failures so the caller can surface every mismatched segment at
    once (and emit an ``artifact.validation-failed`` event naming each), rather
    than fail-on-first. Each failure is a criterion-(f) error.

    Fails CLOSED:

    - a malformed/unparseable repo path (bad namespace or wrong segment count)
      is ONE criterion-(f) ``ecr.repository`` error and short-circuits the
      per-coordinate comparison (there are no trustworthy segments to compare);
    - a missing/empty/whitespace claim coordinate is itself a criterion-(f)
      error (it can never legitimately match a non-empty path segment);
    - a mismatch names the producer-claimed value AND the path segment value.
    """
    errors: list[BundleValidationError] = []
    try:
        org_seg, tenant_seg, project_seg = _identity_segments(repo_path)
    except ValueError as e:
        errors.append(
            BundleValidationError(
                criterion="f",
                field="ecr.repository",
                message=str(e),
            )
        )
        return errors

    repo_values = {"org_id": org_seg, "tenant_id": tenant_seg, "project_id": project_seg}
    for attr, path_label, claim_field in _IDENTITY_COORDINATES:
        claim_value = getattr(claim, attr)
        repo_value = repo_values[attr]
        if not claim_value or not claim_value.strip():
            errors.append(
                BundleValidationError(
                    criterion="f",
                    field=f"attestation.{claim_field}",
                    message=f"{claim_field} is required and must be non-empty",
                )
            )
            continue
        if repo_value != claim_value:
            errors.append(
                BundleValidationError(
                    criterion="f",
                    field=f"attestation.{claim_field}",
                    message=(
                        f"Attestation {claim_field}={claim_value!r} does not match ECR "
                        f"repository path {path_label} segment={repo_value!r}"
                    ),
                )
            )
    return errors

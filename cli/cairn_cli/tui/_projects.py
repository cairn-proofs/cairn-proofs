"""Derive validated, distinct logical-hierarchy coordinates from ECR repo names.

The read-only TUI scopes its "all bundles" list without an unscoped DynamoDB
Scan: enumerate ECR repos -> derive the opaque ``{org-id}``/``{tenant-id}``/
``{project-id}`` coordinates from their names -> run the existing per-project
``query_by_project`` (tenant-PK partition + project sort-key prefix) for each
distinct coordinate.

Threat T2 mitigation: a crafted ECR repo name MUST NOT mis-derive a coordinate
and mis-scope a query. Every candidate name is validated against the canonical
repo-path grammar owned by ``cairn_oci.categories`` via *round-trip* validation
(parse -> rebuild with the canonical builder -> require byte-for-byte equality),
so the TUI cannot drift from ``cairn_oci``'s rules. Names that do not match the
grammar or the expected deployment qualifier are skipped and never used to scope
a query.

There is exactly ONE repository path shape — the uniform three-segment logical
hierarchy ``{appName}-artifact-{qualifier}/{org-id}/{tenant-id}/{project-id}``.
No mode-conditioned path handling: the third segment is
the logical ``{tenant-id}`` whose display name lives in the control-plane
Identity_Registry, never in the path.

Pure functions only: no AWS, no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from cairn_oci.categories import ArtifactLocator, InvalidIdError

# Uniform repo name: {appName}-artifact-{qualifier}/{org-id}/{tenant-id}/{project-id}
# -- exactly 4 slash-separated segments (one repo per project, every
# segment mandatory, nothing branching per segment). The leading segment is the
# appName-prefixed ECR namespace; the trailing three are the opaque logical
# coordinates.
_EXPECTED_SEGMENTS = 4
_ARTIFACT_INFIX = "artifact-"


@dataclass(frozen=True)
class RepoCoordinates:
    """The opaque logical-hierarchy coordinates encoded in an ECR repo name.

    Each is a stable, opaque, immutable, allocate-once identifier validated
    against the canonical ``cairn_oci`` segment grammar; the human display name
    for each lives only in the control-plane Identity_Registry (resolved by
    opaque id at read time), never in the path. Frozen + hashable so a derived
    coordinate can key the all-bundles fan-out's per-coordinate digest set.
    """

    org_id: str
    tenant_id: str
    project_id: str


def coordinates_from_repo_name(
    name: str,
    *,
    deployment_qualifier: str,
    app_name: str | None = None,
) -> RepoCoordinates | None:
    """Return the :class:`RepoCoordinates` encoded in an ECR repo name, else None.

    Returns ``None`` (rather than raising) for any name that does not match the
    canonical three-segment grammar or whose qualifier differs from
    ``deployment_qualifier`` -- the caller skips such names.

    The deployed namespace segment is ``{appName}-artifact-{qualifier}``. When
    ``app_name`` is supplied the namespace MUST equal it exactly; when ``None``
    (the verify-command-composition path, which lacks the resolved appName) the
    segment is accepted if it ENDS WITH ``artifact-{qualifier}`` — tolerant of
    any appName prefix, which is safe because the repositoryName came from the
    operator's own authoritative metadata record.

    Validation is *round-trip*: the namespace prefix is checked, then the bare
    ``artifact-{qualifier}/{org-id}/{tenant-id}/{project-id}`` is rebuilt with
    ``ArtifactLocator().repo_name(...)`` and required to equal the stored name's
    coordinate suffix exactly — reusing the canonical grammar (org/tenant/project
    id and qualifier rules) verbatim instead of re-implementing it here. An
    invalid segment makes ``repo_name`` raise :class:`InvalidIdError`, which
    fails closed to ``None``.
    """
    if not isinstance(name, str):
        return None

    segments = name.split("/")
    if len(segments) != _EXPECTED_SEGMENTS:
        return None

    namespace_segment, org_id, tenant_id, project_id = segments

    bare_namespace = f"{_ARTIFACT_INFIX}{deployment_qualifier}"
    if app_name is not None:
        # Exact match against the resolved deployment namespace.
        if namespace_segment != f"{app_name}-{bare_namespace}":
            return None
    elif namespace_segment != bare_namespace and not namespace_segment.endswith(
        f"-{bare_namespace}"
    ):
        # No resolved appName: accept the bare ``artifact-{qualifier}`` namespace
        # OR any ``{appName}-artifact-{qualifier}`` (the hyphen before the infix
        # is REQUIRED so ``notartifact-{qualifier}`` does NOT slip through).
        return None

    try:
        bare = ArtifactLocator().repo_name(
            deployment_qualifier=deployment_qualifier,
            org_id=org_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
    except InvalidIdError:
        return None

    # Round-trip the coordinate segments against the canonical builder: ``bare``
    # is ``artifact-{qualifier}/{org}/{tenant}/{project}``, so the stored name
    # must end with that exact coordinate suffix (org/tenant/project + qualifier
    # grammar all enforced by ``repo_name`` above).
    coordinate_suffix = bare.split("/", 1)[1]
    if coordinate_suffix != f"{org_id}/{tenant_id}/{project_id}" or not name.endswith(
        f"/{coordinate_suffix}"
    ):
        return None

    return RepoCoordinates(org_id=org_id, tenant_id=tenant_id, project_id=project_id)


def project_id_from_repo_name(
    name: str,
    *,
    deployment_qualifier: str,
    app_name: str | None = None,
) -> str | None:
    """Return just the ``{project-id}`` coordinate of an ECR repo name, else None.

    Thin wrapper over :func:`coordinates_from_repo_name` for callers (the
    surfaced ``cairn verify`` command composition) that need only the project
    coordinate. Inherits the full round-trip validation.
    """
    coords = coordinates_from_repo_name(
        name, deployment_qualifier=deployment_qualifier, app_name=app_name
    )
    return coords.project_id if coords is not None else None


def derive_coordinates(
    repo_names: Iterable[str],
    *,
    deployment_qualifier: str,
    app_name: str | None = None,
) -> list[RepoCoordinates]:
    """Return the distinct, validated :class:`RepoCoordinates` from ECR repo names.

    Every name is validated against the canonical repo-path grammar (see
    :func:`coordinates_from_repo_name`); names that fail validation or that
    belong to a different deployment qualifier are skipped and contribute
    nothing. The result is de-duplicated and returned in a stable (sorted by the
    org/tenant/project tuple) order so the TUI's project list is deterministic.
    """
    derived: set[RepoCoordinates] = set()
    for name in repo_names:
        coords = coordinates_from_repo_name(
            name, deployment_qualifier=deployment_qualifier, app_name=app_name
        )
        if coords is not None:
            derived.add(coords)
    return sorted(derived, key=lambda c: (c.org_id, c.tenant_id, c.project_id))

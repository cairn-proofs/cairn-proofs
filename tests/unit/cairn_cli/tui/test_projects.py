"""Tests for cairn_cli.tui._projects -- coordinate derivation from ECR repo names.

Malformed / unexpected / cross-qualifier repo names are
skipped and never used to scope a query. The repo path is the uniform
three-segment logical hierarchy ``{appName}-artifact-{qualifier}/{org-id}/
{tenant-id}/{project-id}`` — no mode-conditioned handling.
"""

from __future__ import annotations

import pytest
from cairn_cli.tui._projects import (
    RepoCoordinates,
    coordinates_from_repo_name,
    derive_coordinates,
    project_id_from_repo_name,
)
from cairn_oci.categories import ArtifactLocator

QUALIFIER = "use2-alpha"
APP = "cairnproofs4af"
ORG = "org-a"
TENANT = "tnt-a"


def _repo(
    project_id: str,
    *,
    org_id: str = ORG,
    tenant_id: str = TENANT,
    qualifier: str = QUALIFIER,
) -> str:
    # The locator emits the BARE ``artifact-{qualifier}`` namespace (no appName).
    return ArtifactLocator().repo_name(
        deployment_qualifier=qualifier,
        org_id=org_id,
        tenant_id=tenant_id,
        project_id=project_id,
    )


def _deployed_repo(
    project_id: str,
    *,
    app_name: str = APP,
    org_id: str = ORG,
    tenant_id: str = TENANT,
    qualifier: str = QUALIFIER,
) -> str:
    # The appName-prefixed namespace as DescribeRepositories actually returns it.
    return (
        f"{app_name}-{_repo(project_id, org_id=org_id, tenant_id=tenant_id, qualifier=qualifier)}"
    )


def _coord(project_id: str, *, org_id: str = ORG, tenant_id: str = TENANT) -> RepoCoordinates:
    return RepoCoordinates(org_id=org_id, tenant_id=tenant_id, project_id=project_id)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_single_well_formed_name_yields_coordinates() -> None:
    name = _repo("acme-app")
    assert coordinates_from_repo_name(name, deployment_qualifier=QUALIFIER) == _coord("acme-app")


def test_project_id_wrapper_returns_project_segment() -> None:
    name = _repo("acme-app")
    assert project_id_from_repo_name(name, deployment_qualifier=QUALIFIER) == "acme-app"


def test_happy_path_multiple_distinct_projects_sorted() -> None:
    names = [
        _repo("zeta-svc"),
        _repo("acme-app"),
        _repo("middle-1"),
    ]
    assert derive_coordinates(names, deployment_qualifier=QUALIFIER) == [
        _coord("acme-app"),
        _coord("middle-1"),
        _coord("zeta-svc"),
    ]


def test_sort_is_by_org_then_tenant_then_project() -> None:
    names = [
        _repo("p", org_id="org-b", tenant_id="tnt-z"),
        _repo("p", org_id="org-a", tenant_id="tnt-z"),
        _repo("p", org_id="org-a", tenant_id="tnt-a"),
    ]
    assert derive_coordinates(names, deployment_qualifier=QUALIFIER) == [
        _coord("p", org_id="org-a", tenant_id="tnt-a"),
        _coord("p", org_id="org-a", tenant_id="tnt-z"),
        _coord("p", org_id="org-b", tenant_id="tnt-z"),
    ]


def test_distinct_tenants_same_project_id_are_distinct_coordinates() -> None:
    # A project id is opaque/unique, but two tenants are two distinct coordinates
    # even were a project id to repeat — the tenant is the partition boundary.
    names = [_repo("p", tenant_id="tnt-a"), _repo("p", tenant_id="tnt-b")]
    assert derive_coordinates(names, deployment_qualifier=QUALIFIER) == [
        _coord("p", tenant_id="tnt-a"),
        _coord("p", tenant_id="tnt-b"),
    ]


# --------------------------------------------------------------------------- #
# Dedupe
# --------------------------------------------------------------------------- #


def test_exact_duplicate_repo_names_dedupe() -> None:
    name = _repo("acme-app")
    assert derive_coordinates([name, name, name], deployment_qualifier=QUALIFIER) == [
        _coord("acme-app")
    ]


# --------------------------------------------------------------------------- #
# Malformed / unexpected names are skipped
# --------------------------------------------------------------------------- #

MALFORMED_NAMES = [
    pytest.param("", id="empty-string"),
    pytest.param("artifact-use2-alpha", id="single-segment"),
    pytest.param("artifact-use2-alpha/org-a", id="two-segments"),
    pytest.param("artifact-use2-alpha/org-a/tnt-a", id="three-segments-missing-project"),
    # A trailing artifact-kind segment is now an extra (5th) segment.
    pytest.param(
        "artifact-use2-alpha/org-a/tnt-a/acme-app/other", id="category-in-path-5-segments"
    ),
    pytest.param("notartifact-use2-alpha/org-a/tnt-a/acme-app", id="wrong-prefix"),
    pytest.param("use2-alpha/org-a/tnt-a/acme-app", id="missing-artifact-prefix"),
    pytest.param("artifact-other-stage/org-a/tnt-a/acme-app", id="mismatched-qualifier"),
    pytest.param("artifact-use2-alpha/org-a/tnt-a/ACME-APP", id="uppercase-project-id"),
    pytest.param("artifact-use2-alpha/ORG/tnt-a/acme", id="uppercase-org-id"),
    pytest.param("artifact-use2-alpha/org-a/TNT/acme", id="uppercase-tenant-id"),
    pytest.param("artifact-use2-alpha/org-a/tnt-a/-acme", id="leading-hyphen-project-id"),
    pytest.param("artifact-use2-alpha/org-a/tnt-a/acme-", id="trailing-hyphen-project-id"),
    pytest.param("artifact-use2-alpha/org-a/tnt-a/acme_app", id="underscore-project-id"),
    pytest.param("artifact-use2-alpha/org-a/tnt-a/", id="empty-project-id"),
    pytest.param("artifact-use2-alpha//tnt-a/acme", id="empty-org-id"),
    pytest.param("artifact-use2-alpha/org-a//acme", id="empty-tenant-id"),
    pytest.param("artifact-use2-alpha/org-a/tnt-a/..", id="path-traversal-dotdot"),
    pytest.param("artifact-use2-alpha/org-a/tnt-a/acme app", id="embedded-space"),
    pytest.param("artifact-use2-alpha/org-a/tnt-a/acme\napp", id="control-char-newline"),
    pytest.param("artifact-use2-alpha/org-a/tnt-a/acme\x00app", id="control-char-null"),
    pytest.param(
        "artifact-use2-alpha/org-a/tnt-a/acme-app/../etc/passwd",
        id="traversal-extra-segments",
    ),
    pytest.param("ARTIFACT-use2-alpha/org-a/tnt-a/acme-app", id="uppercase-prefix"),
    pytest.param(
        "artifact-use2-alpha/org-a/tnt-a/" + "a" * 65,
        id="project-id-too-long",
    ),
    # Untrusted-input hardening: registry would never emit these, but the
    # repo name is attacker-influenceable, so the grammar must reject them.
    pytest.param(" artifact-use2-alpha/org-a/tnt-a/acme-app", id="leading-whitespace"),
    pytest.param("artifact-use2-alpha/org-a/tnt-a/acme-app ", id="trailing-whitespace"),
    pytest.param("artifact-use2-alpha/org-a/tnt-a/acme-app/", id="trailing-slash"),
    pytest.param("artifact-use2-alpha\\org-a\\tnt-a\\acme-app", id="backslash-separators"),
    # \N{...} = CYRILLIC SMALL LETTER A, a homoglyph of ASCII "a".
    pytest.param(
        "artifact-use2-alpha/org-a/tnt-a/\N{CYRILLIC SMALL LETTER A}cme",
        id="cyrillic-homoglyph-project-id",
    ),
]


@pytest.mark.parametrize("name", MALFORMED_NAMES)
def test_malformed_name_returns_none(name: str) -> None:
    assert coordinates_from_repo_name(name, deployment_qualifier=QUALIFIER) is None
    assert project_id_from_repo_name(name, deployment_qualifier=QUALIFIER) is None


@pytest.mark.parametrize("name", MALFORMED_NAMES)
def test_malformed_name_contributes_nothing(name: str) -> None:
    # A valid name alongside a malformed one: only the valid coordinate survives.
    good = _repo("acme-app")
    assert derive_coordinates([name, good], deployment_qualifier=QUALIFIER) == [_coord("acme-app")]


def test_all_malformed_yields_empty_list() -> None:
    names = [param.values[0] for param in MALFORMED_NAMES]
    assert derive_coordinates(names, deployment_qualifier=QUALIFIER) == []


def test_mismatched_qualifier_is_skipped_even_when_otherwise_valid() -> None:
    # Well-formed name, but for a *different* deployment.
    other = _repo("acme-app", qualifier="usw2-prod")
    assert coordinates_from_repo_name(other, deployment_qualifier=QUALIFIER) is None
    assert derive_coordinates([other], deployment_qualifier=QUALIFIER) == []


def test_non_string_input_returns_none() -> None:
    assert coordinates_from_repo_name(None, deployment_qualifier=QUALIFIER) is None  # type: ignore[arg-type]
    assert project_id_from_repo_name(12345, deployment_qualifier=QUALIFIER) is None  # type: ignore[arg-type]


def test_empty_input_iterable() -> None:
    assert derive_coordinates([], deployment_qualifier=QUALIFIER) == []


def test_accepts_single_pass_generator_input() -> None:
    # derive_coordinates takes an Iterable, not just a list; a one-shot generator
    # must be consumed correctly.
    names = (_repo(p) for p in ("acme-app", "acme-app"))
    assert derive_coordinates(names, deployment_qualifier=QUALIFIER) == [_coord("acme-app")]


def test_single_char_ids_are_valid() -> None:
    name = _repo("a", org_id="o", tenant_id="t")
    assert coordinates_from_repo_name(name, deployment_qualifier=QUALIFIER) == _coord(
        "a", org_id="o", tenant_id="t"
    )


def test_max_length_project_id_is_valid() -> None:
    pid = "a" * 64
    name = _repo(pid)
    assert project_id_from_repo_name(name, deployment_qualifier=QUALIFIER) == pid


# --------------------------------------------------------------------------- #
# appName-prefixed namespace (the deployed name shape; T-13)
# --------------------------------------------------------------------------- #


def test_deployed_appname_namespace_parses_with_app_name() -> None:
    # DescribeRepositories returns ``{appName}-artifact-{qualifier}/{org}/{tenant}/{project}``.
    name = _deployed_repo("acme-app")
    assert coordinates_from_repo_name(name, deployment_qualifier=QUALIFIER, app_name=APP) == _coord(
        "acme-app"
    )


def test_deployed_appname_namespace_parses_without_app_name_tolerantly() -> None:
    # The verify-command path lacks the resolved appName and accepts any
    # ``{appName}-artifact-{qualifier}`` namespace by hyphen-anchored suffix.
    name = _deployed_repo("acme-app")
    assert project_id_from_repo_name(name, deployment_qualifier=QUALIFIER) == "acme-app"


def test_wrong_app_name_is_rejected_when_app_name_pinned() -> None:
    name = _deployed_repo("acme-app", app_name="otherapp9")
    assert coordinates_from_repo_name(name, deployment_qualifier=QUALIFIER, app_name=APP) is None


def test_derive_coordinates_threads_app_name() -> None:
    names = [_deployed_repo("zeta"), _deployed_repo("acme")]
    assert derive_coordinates(names, deployment_qualifier=QUALIFIER, app_name=APP) == [
        _coord("acme"),
        _coord("zeta"),
    ]


def test_notartifact_prefix_does_not_slip_through_tolerant_branch() -> None:
    # The hyphen before the ``artifact-`` infix is required, so a namespace that
    # merely ENDS WITH ``artifact-{qualifier}`` (no separator) is rejected even on
    # the tolerant (no app_name) path.
    assert (
        coordinates_from_repo_name(
            f"notartifact-{QUALIFIER}/org-a/tnt-a/acme-app", deployment_qualifier=QUALIFIER
        )
        is None
    )

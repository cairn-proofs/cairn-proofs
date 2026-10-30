"""Tests for cairn_cli._aws — ECR auth decoding + SSM lookups."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest
from cairn_cli._aws import (
    AppNameUnresolvedError,
    DeploymentTarget,
    EcrAuthError,
    get_ecr_basic_auth,
    get_ecr_registry_host,
    resolve_app_name,
    resolve_table_names,
)

APP = "cairnproofs4af"


def test_deployment_target_derives_qualifier_and_appname_prefixed_names() -> None:
    t = DeploymentTarget(stage="alpha", region_code="cmh", app_name=APP)
    assert t.qualifier == "cmh-alpha"
    assert t.metadata_table == f"{APP}-ArtifactMetadata-cmh-alpha"
    assert t.control_claims_table == f"{APP}-ControlClaims-cmh-alpha"
    assert t.identity_registry_table == f"{APP}-IdentityRegistry-cmh-alpha"
    assert t.artifact_namespace == f"{APP}-artifact-cmh-alpha"
    assert t.ssm_ecr_host_param == f"/{APP}/alpha/registry/ecr-host"
    assert t.ssm_trust_policy_param == f"/{APP}/alpha/notation/trust-policy"
    assert t.ssm_reviewer_profile_arn_param == f"/{APP}/alpha/signing/reviewer-profile-version-arn"
    assert t.ssm_app_name_param == "/cairnproofs/cmh-alpha/app-name"


def test_name_for_and_dlq_name_mirror_cdk_scheme() -> None:
    t = DeploymentTarget(stage="alpha", region_code="cmh", app_name=APP)
    assert t.name_for("alias-mover") == f"{APP}-alias-mover-cmh-alpha"
    assert t.dlq_name("validate-bundle-dlq") == f"{APP}-validate-bundle-dlq-cmh-alpha"


def test_repo_path_namespaces_locator_output() -> None:
    t = DeploymentTarget(stage="alpha", region_code="cmh", app_name=APP)
    # The locator emits the bare ``artifact-{qualifier}/{project}`` form; repo_path
    # swaps the bare prefix for the appName-namespaced one.
    assert t.repo_path("artifact-cmh-alpha/acme") == f"{APP}-artifact-cmh-alpha/acme"
    assert (
        t.repo_path("artifact-cmh-alpha/acme/catalog") == f"{APP}-artifact-cmh-alpha/acme/catalog"
    )


def test_repo_path_threads_uniform_three_segment_locator_output() -> None:
    """repo_path namespaces the uniform {org-id}/{tenant-id}/{project-id} path.

    Wave-0's ``ArtifactLocator.repo_name`` emits the single three-segment shape
    ``artifact-{qualifier}/{org-id}/{tenant-id}/{project-id}`` ([INGEST-018]);
    ``repo_path`` must swap the bare ``artifact-{qualifier}`` prefix for the
    appName-namespaced one across all three segments ([INGEST-032]).
    """
    from cairn_oci import ArtifactLocator

    t = DeploymentTarget(stage="alpha", region_code="cmh", app_name=APP)
    repo_name = ArtifactLocator().repo_name(
        deployment_qualifier=t.qualifier,
        org_id="acme-co",
        tenant_id="acme",
        project_id="payments",
    )
    assert repo_name == "artifact-cmh-alpha/acme-co/acme/payments"
    assert t.repo_path(repo_name) == f"{APP}-artifact-cmh-alpha/acme-co/acme/payments"


def test_name_property_raises_when_app_name_unresolved() -> None:
    t = DeploymentTarget(stage="alpha", region_code="cmh")  # app_name=None
    with pytest.raises(AppNameUnresolvedError):
        _ = t.metadata_table
    with pytest.raises(AppNameUnresolvedError):
        _ = t.artifact_namespace


def test_resolve_app_name_explicit_override_wins_no_ssm_call() -> None:
    ssm = MagicMock()
    t = DeploymentTarget(stage="alpha", region_code="cmh")
    resolved = resolve_app_name(ssm, t, override="overrideapp")
    assert resolved.app_name == "overrideapp"
    ssm.get_parameter.assert_not_called()


def test_resolve_app_name_reads_ssm_discovery_parameter() -> None:
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": f"  {APP}\n"}}
    t = DeploymentTarget(stage="alpha", region_code="cmh")
    resolved = resolve_app_name(ssm, t)
    assert resolved.app_name == APP
    ssm.get_parameter.assert_called_once_with(Name="/cairnproofs/cmh-alpha/app-name")


def test_resolve_app_name_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRN_APP_NAME", "envapp42")
    ssm = MagicMock()
    t = DeploymentTarget(stage="alpha", region_code="cmh")
    resolved = resolve_app_name(ssm, t)
    assert resolved.app_name == "envapp42"
    ssm.get_parameter.assert_not_called()


def test_resolve_app_name_unresolved_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAIRN_APP_NAME", raising=False)
    ssm = MagicMock()
    ssm.get_parameter.side_effect = RuntimeError("ParameterNotFound")
    t = DeploymentTarget(stage="alpha", region_code="cmh")
    with pytest.raises(AppNameUnresolvedError):
        resolve_app_name(ssm, t)


def test_resolve_app_name_already_set_is_idempotent_noop() -> None:
    # Resolution order step 2: a DeploymentTarget that already carries app_name
    # re-resolves to itself with NO SSM round-trip (idempotent).
    ssm = MagicMock()
    t = DeploymentTarget(stage="alpha", region_code="cmh", app_name=APP)
    resolved = resolve_app_name(ssm, t)
    assert resolved.app_name == APP
    ssm.get_parameter.assert_not_called()


def test_resolve_app_name_override_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Resolution order: explicit override outranks the CAIRN_APP_NAME env var.
    monkeypatch.setenv("CAIRN_APP_NAME", "envapp42")
    ssm = MagicMock()
    t = DeploymentTarget(stage="alpha", region_code="cmh")
    resolved = resolve_app_name(ssm, t, override="overrideapp")
    assert resolved.app_name == "overrideapp"
    ssm.get_parameter.assert_not_called()


def test_resolve_app_name_set_app_name_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Resolution order: an already-set target.app_name outranks the env var
    # (override absent) — the env var is only consulted when the target is unset.
    monkeypatch.setenv("CAIRN_APP_NAME", "envapp42")
    ssm = MagicMock()
    t = DeploymentTarget(stage="alpha", region_code="cmh", app_name=APP)
    resolved = resolve_app_name(ssm, t)
    assert resolved.app_name == APP
    ssm.get_parameter.assert_not_called()


def test_resolve_app_name_ssm_present_but_empty_value_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SSM-miss variant: the parameter EXISTS but holds an empty/whitespace value
    # (e.g. a half-provisioned deployment). get_parameter does not raise, so the
    # second guard must fail closed rather than return a half-formed appName.
    monkeypatch.delenv("CAIRN_APP_NAME", raising=False)
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": "   \n"}}
    t = DeploymentTarget(stage="alpha", region_code="cmh")
    with pytest.raises(AppNameUnresolvedError, match="empty/unresolved"):
        resolve_app_name(ssm, t)


def test_resolve_app_name_ssm_missing_parameter_field_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SSM returns a response shape with no Parameter/Value at all — the
    # ``(response.get("Parameter") or {}).get("Value", "")`` path must coalesce
    # to empty and fail closed, never raise an unrelated KeyError/TypeError.
    monkeypatch.delenv("CAIRN_APP_NAME", raising=False)
    ssm = MagicMock()
    ssm.get_parameter.return_value = {}
    t = DeploymentTarget(stage="alpha", region_code="cmh")
    with pytest.raises(AppNameUnresolvedError):
        resolve_app_name(ssm, t)


def test_repo_path_fallback_when_no_bare_prefix() -> None:
    # repo_path's else-branch: a repo_name that does NOT start with the bare
    # ``artifact-{qualifier}`` prefix is appName-prefixed wholesale.
    t = DeploymentTarget(stage="alpha", region_code="cmh", app_name=APP)
    assert t.repo_path("some-other-repo") == f"{APP}-some-other-repo"
    # A near-miss prefix (wrong qualifier) takes the fallback, not the swap.
    assert t.repo_path("artifact-iad-prod/acme") == f"{APP}-artifact-iad-prod/acme"


def test_all_name_deriving_members_raise_when_app_name_unresolved() -> None:
    # Every member that composes a physical name MUST refuse to produce a
    # half-formed string while appName is unresolved (so a caller cannot read a
    # deployment under the wrong name). Covers the SSM-path + DLQ + repo members
    # not exercised by test_name_property_raises_when_app_name_unresolved.
    t = DeploymentTarget(stage="alpha", region_code="cmh")  # app_name=None
    with pytest.raises(AppNameUnresolvedError):
        _ = t.control_claims_table
    with pytest.raises(AppNameUnresolvedError):
        _ = t.identity_registry_table
    with pytest.raises(AppNameUnresolvedError):
        _ = t.ssm_ecr_host_param
    with pytest.raises(AppNameUnresolvedError):
        _ = t.ssm_trust_policy_param
    with pytest.raises(AppNameUnresolvedError):
        _ = t.ssm_reviewer_profile_arn_param
    with pytest.raises(AppNameUnresolvedError):
        t.name_for("alias-mover")
    with pytest.raises(AppNameUnresolvedError):
        t.dlq_name("validate-bundle-dlq")
    with pytest.raises(AppNameUnresolvedError):
        t.repo_path("artifact-cmh-alpha/acme")
    # ...but the token-INDEPENDENT discovery path is readable while unresolved
    # (it is the bootstrap lookup that resolves appName in the first place).
    assert t.ssm_app_name_param == "/cairnproofs/cmh-alpha/app-name"
    assert t.qualifier == "cmh-alpha"


def test_get_ecr_basic_auth_decodes_token() -> None:
    token = base64.b64encode(b"AWS:secret-password").decode("ascii")
    ecr = MagicMock()
    ecr.get_authorization_token.return_value = {
        "authorizationData": [{"authorizationToken": token}]
    }
    user, password = get_ecr_basic_auth(ecr)
    assert user == "AWS"
    assert password == "secret-password"


def test_get_ecr_basic_auth_empty_auth_data() -> None:
    ecr = MagicMock()
    ecr.get_authorization_token.return_value = {"authorizationData": []}
    with pytest.raises(EcrAuthError, match="no authorizationData"):
        get_ecr_basic_auth(ecr)


def test_get_ecr_basic_auth_missing_separator() -> None:
    token = base64.b64encode(b"nodelimiter").decode("ascii")
    ecr = MagicMock()
    ecr.get_authorization_token.return_value = {
        "authorizationData": [{"authorizationToken": token}]
    }
    with pytest.raises(EcrAuthError, match="separator"):
        get_ecr_basic_auth(ecr)


def test_get_ecr_basic_auth_not_base64() -> None:
    ecr = MagicMock()
    ecr.get_authorization_token.return_value = {
        "authorizationData": [{"authorizationToken": "!!not-base64!!"}]
    }
    with pytest.raises(EcrAuthError, match="base64"):
        get_ecr_basic_auth(ecr)


def test_get_ecr_registry_host_strips_whitespace() -> None:
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": "  registry.example.com\n"}}
    target = DeploymentTarget(stage="alpha", region_code="cmh", app_name=APP)
    assert get_ecr_registry_host(ssm, target) == "registry.example.com"


def _table_ssm(values: dict[str, str], *, missing_exc: Exception | None = None) -> MagicMock:
    """An ssm stub whose get_parameter returns ``values[Name]`` or raises.

    A name absent from ``values`` raises ``missing_exc`` (default: a
    ParameterNotFound-shaped RuntimeError), mirroring boto3's behavior for an
    unpublished parameter.
    """
    ssm = MagicMock()

    def _get(*, Name: str) -> dict:
        if Name in values:
            return {"Parameter": {"Value": values[Name]}}
        raise missing_exc or RuntimeError("ParameterNotFound")

    ssm.get_parameter.side_effect = _get
    return ssm


def test_resolve_table_names_reads_auto_names_from_ssm() -> None:
    # The tables are auto-named; their physical names are discovered from SSM
    # and the table-name properties then return the SSM value (not name_for).
    target = DeploymentTarget(stage="alpha", region_code="cmh", app_name=APP)
    ssm = _table_ssm(
        {
            f"/{APP}/alpha/tables/metadata": "Stack-MetaABC",
            f"/{APP}/alpha/tables/control-claims": "Stack-CcDEF",
            f"/{APP}/alpha/tables/identity-registry": "Stack-IdGHI",
        }
    )
    resolved = resolve_table_names(ssm, target)

    assert resolved.metadata_table == "Stack-MetaABC"
    assert resolved.control_claims_table == "Stack-CcDEF"
    assert resolved.identity_registry_table == "Stack-IdGHI"
    # Pure return of a new target; the input is unchanged (frozen dataclass).
    assert target.metadata_table_name is None


def test_resolve_table_names_falls_back_to_name_for_when_absent() -> None:
    # An older deployment that still pins explicit table names (no discovery
    # params) leaves the fields unresolved, so the properties fall back to the
    # legacy name_for reconstruction — discovery is best-effort, never fatal.
    target = DeploymentTarget(stage="alpha", region_code="cmh", app_name=APP)
    ssm = _table_ssm({})  # every get_parameter raises ParameterNotFound

    resolved = resolve_table_names(ssm, target)

    assert resolved.metadata_table == f"{APP}-ArtifactMetadata-cmh-alpha"
    assert resolved.control_claims_table == f"{APP}-ControlClaims-cmh-alpha"
    assert resolved.identity_registry_table == f"{APP}-IdentityRegistry-cmh-alpha"


def test_resolve_table_names_partial_resolution_is_per_table() -> None:
    # A param published for one table but not the others resolves only that one;
    # the rest fall back independently.
    target = DeploymentTarget(stage="alpha", region_code="cmh", app_name=APP)
    ssm = _table_ssm({f"/{APP}/alpha/tables/metadata": "Stack-MetaABC"})

    resolved = resolve_table_names(ssm, target)

    assert resolved.metadata_table == "Stack-MetaABC"  # from SSM
    assert resolved.control_claims_table == f"{APP}-ControlClaims-cmh-alpha"  # fallback
    assert resolved.identity_registry_table == f"{APP}-IdentityRegistry-cmh-alpha"  # fallback

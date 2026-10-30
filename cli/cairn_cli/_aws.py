"""AWS helpers — SSM lookups, ECR auth, DynamoDB table-name construction.

All boto3 clients are passed explicitly into the helpers rather than cached
at module level, so per-invocation state cannot leak between concurrent
test runs (see AGENTS.md "Concurrency-safe ambient state").

The only module-level state here is read-only configuration constants.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, replace
from typing import Any

# The dead-letter queues the pipeline drains to, in pipeline order. Each entry
# is the ``{resource}`` middle segment of the CDK ``nameFor(resource)`` scheme
# (cdk/lib/constructs/dead-letter-queues.ts) — the full physical name is
# composed once by :meth:`DeploymentTarget.dlq_name`, never hand-interpolated.
PIPELINE_DLQ_RESOURCES: tuple[tuple[str, str], ...] = (
    ("validate", "validate-bundle-dlq"),
    ("sign", "sign-bundle-dlq"),
    ("archive", "archive-to-s3-dlq"),
    ("metadata", "update-metadata-dlq"),
)

# Environment override for the resolved appName (mirrors the CDK ``appName``
# stack prop). Takes precedence over the SSM discovery lookup so an operator can
# target a deployment without an extra round-trip.
APP_NAME_ENV_VAR = "CAIRN_APP_NAME"


class AppNameUnresolvedError(RuntimeError):
    """Raised when the deployment ``appName`` cannot be resolved.

    Operations that compose a physical resource name (ECR namespace, DynamoDB
    table, ``/{appName}/{stage}/...`` SSM path, DLQ name) require ``appName``;
    it is resolved at runtime from an explicit override, the
    ``CAIRN_APP_NAME`` env var, or the SSM discovery parameter
    ``/cairnproofs/{qualifier}/app-name`` written by the CDK stack.
    """


@dataclass(frozen=True)
class DeploymentTarget:
    """Identifies a deployment by (stage, region-code) — drives SSM + table names.

    ``app_name`` is the per-deployment uniquifier (e.g. ``cairnproofs4af``) that
    the CDK ``nameFor()`` scheme prefixes onto every physical resource name. It
    is ``None`` until resolved at runtime (see :func:`resolve_app_name`); the
    name-deriving properties raise :class:`AppNameUnresolvedError` while unset so
    a caller cannot accidentally read a deployment under a half-formed name.
    """

    stage: str
    region_code: str
    app_name: str | None = None
    # Physical DynamoDB table names, resolved from SSM by
    # :func:`resolve_table_names`. ``None`` until resolved; the table-name
    # properties fall back to the legacy ``name_for`` reconstruction while
    # unset, so a deployment that still pins explicit table names (or a test
    # that never resolves) keeps working.
    metadata_table_name: str | None = None
    control_claims_table_name: str | None = None
    identity_registry_table_name: str | None = None

    @property
    def qualifier(self) -> str:
        return f"{self.region_code}-{self.stage}"

    @property
    def _app_name(self) -> str:
        if self.app_name is None:
            raise AppNameUnresolvedError(
                "appName is unresolved; call resolve_app_name(ssm_client, target) "
                "first (or set the CAIRN_APP_NAME env var / pass --app-name)."
            )
        return self.app_name

    @property
    def ssm_app_name_param(self) -> str:
        """Token-independent SSM discovery path for this deployment's appName.

        Keyed on the ``{qualifier}`` (region+stage), NOT the appName itself —
        this is the bootstrap lookup the CLI reads BEFORE it knows the appName.
        Mirrors the CDK write at cdk/lib/artifact-store-stack.ts.
        """
        return f"/cairnproofs/{self.qualifier}/app-name"

    @property
    def artifact_namespace(self) -> str:
        """The ECR repository namespace ``{appName}-artifact-{qualifier}``.

        The SINGLE composition point for the producer-facing ECR namespace —
        mirrors the CDK ``Naming.artifactNamespace`` (cdk/lib/naming.ts). The
        per-project repo path is ``{artifact_namespace}/{projectId}``; compose
        it via :meth:`repo_path` rather than re-interpolating the prefix.
        """
        return self.name_for("artifact")

    def repo_path(self, repo_name: str) -> str:
        """Prefix the appName-bearing ECR namespace onto a project repo name.

        ``repo_name`` is the qualifier-scoped path the ``cairn_oci`` locator
        emits (``artifact-{qualifier}/{projectId}[/...]``); this swaps its bare
        ``artifact-`` prefix for the namespaced ``{appName}-artifact-`` one so
        every push/read target threads through one helper.
        """
        bare_prefix = f"artifact-{self.qualifier}"
        if repo_name.startswith(bare_prefix):
            return f"{self.artifact_namespace}{repo_name[len(bare_prefix) :]}"
        return f"{self._app_name}-{repo_name}"

    @property
    def metadata_table(self) -> str:
        """Physical name of the Metadata_Index table ([META-001]).

        Prefers the SSM-resolved name (the table is auto-named; its physical
        name is published to ``/{appName}/{stage}/tables/metadata`` — see
        :func:`resolve_table_names`); falls back to the legacy ``name_for``
        reconstruction for a deployment that still pins an explicit table name.
        """
        if self.metadata_table_name is not None:
            return self.metadata_table_name
        return self.name_for("ArtifactMetadata")

    @property
    def control_claims_table(self) -> str:
        """Physical name of the Control Claims table (``ControlClaims``; [PCLAIM-001]).

        Prefers the SSM-resolved name (published to
        ``/{appName}/{stage}/tables/control-claims``); falls back to the legacy
        ``name_for("ControlClaims")`` reconstruction, which the TUI mirrors, so
        the CLI and TUI target the same deployed table either way.
        """
        if self.control_claims_table_name is not None:
            return self.control_claims_table_name
        return self.name_for("ControlClaims")

    @property
    def identity_registry_table(self) -> str:
        """Physical name of the control-plane Identity_Registry table ([IDREG-001]).

        Prefers the SSM-resolved name (published to
        ``/{appName}/{stage}/tables/identity-registry``); falls back to the
        legacy ``name_for("IdentityRegistry")`` reconstruction, matching the
        TUI's read-time display-name join target.
        """
        if self.identity_registry_table_name is not None:
            return self.identity_registry_table_name
        return self.name_for("IdentityRegistry")

    def name_for(self, resource: str) -> str:
        """Compose a physical resource name ``{appName}-{resource}-{qualifier}``.

        The CLI-side mirror of the CDK ``Naming.nameFor`` (cdk/lib/naming.ts) —
        the SINGLE place a hyphenated AWS resource physical name is composed, so
        the appName/qualifier prefix-suffix pattern lives in exactly one helper
        (DLQ queue names, the alias-mover managed-policy name, …).
        """
        return f"{self._app_name}-{resource}-{self.qualifier}"

    def dlq_name(self, resource: str) -> str:
        """Compose a pipeline DLQ physical name (``resource`` ⇒ ``{...}-dlq``).

        ``resource`` is a value from :data:`PIPELINE_DLQ_RESOURCES` (e.g.
        ``"validate-bundle-dlq"``). Thin wrapper over :meth:`name_for` so the
        TUI's DLQ-health reads track the deployed queue names.
        """
        return self.name_for(resource)

    @property
    def ssm_ecr_host_param(self) -> str:
        return f"/{self._app_name}/{self.stage}/registry/ecr-host"

    @property
    def ssm_trust_policy_param(self) -> str:
        return f"/{self._app_name}/{self.stage}/notation/trust-policy"

    @property
    def ssm_reviewer_profile_arn_param(self) -> str:
        """SSM param holding the artifact-reviewer-signoff Signing_Profile version ARN.

        Written by the CDK stack alongside the pipeline profile ARN. ``cairn
        sign`` reads it to target the reviewer profile, mirroring how the
        sign-bundle Lambda receives the pipeline profile ARN — neither path
        consults the Metadata_Index.
        """
        return f"/{self._app_name}/{self.stage}/signing/reviewer-profile-version-arn"

    @property
    def ssm_metadata_table_param(self) -> str:
        """SSM path holding the Metadata_Index table's physical name.

        Mirrors the CDK write ``naming.ssmPath(stage, 'tables/metadata')``
        (cdk/lib/artifact-store-stack.ts) ⇒ ``/{appName}/{stage}/tables/metadata``.
        """
        return f"/{self._app_name}/{self.stage}/tables/metadata"

    @property
    def ssm_control_claims_table_param(self) -> str:
        """SSM path holding the ControlClaims table's physical name."""
        return f"/{self._app_name}/{self.stage}/tables/control-claims"

    @property
    def ssm_identity_registry_table_param(self) -> str:
        """SSM path holding the Identity_Registry table's physical name."""
        return f"/{self._app_name}/{self.stage}/tables/identity-registry"


def resolve_app_name(
    ssm_client: Any,
    target: DeploymentTarget,
    *,
    override: str | None = None,
) -> DeploymentTarget:
    """Return ``target`` with ``app_name`` resolved (explicit → env → SSM).

    Resolution order:

    1. ``override`` (an explicit ``--app-name`` value), if given.
    2. ``target.app_name`` if already set (idempotent — a re-resolve is a no-op).
    3. the ``CAIRN_APP_NAME`` env var.
    4. the SSM discovery parameter ``/cairnproofs/{qualifier}/app-name`` written
       by the CDK stack.

    Raises :class:`AppNameUnresolvedError` if none of these yield a value.
    """
    chosen = override or target.app_name or os.environ.get(APP_NAME_ENV_VAR)
    if not chosen:
        try:
            response = ssm_client.get_parameter(Name=target.ssm_app_name_param)
        except Exception as e:
            raise AppNameUnresolvedError(
                f"could not resolve appName from SSM {target.ssm_app_name_param!r}: {e}. "
                f"Set {APP_NAME_ENV_VAR} / pass --app-name, or check the deployment "
                "wrote the discovery parameter."
            ) from e
        chosen = (response.get("Parameter") or {}).get("Value", "").strip()
    if not chosen:
        raise AppNameUnresolvedError(
            f"appName for deployment {target.qualifier!r} is empty/unresolved; "
            f"set {APP_NAME_ENV_VAR}, pass --app-name, or redeploy the stack."
        )
    return replace(target, app_name=chosen)


def resolve_table_names(ssm_client: Any, target: DeploymentTarget) -> DeploymentTarget:
    """Return ``target`` with DynamoDB table names resolved from SSM.

    The tables are auto-named by CloudFormation (no explicit ``tableName``), so
    their physical names cannot be reconstructed client-side; the CDK stack
    publishes each to a deterministic SSM path
    (``/{appName}/{stage}/tables/{metadata,control-claims,identity-registry}``).
    This stores them on the target so the table-name properties return the live
    physical name.

    ``appName`` MUST already be resolved (call :func:`resolve_app_name` first) —
    the parameter paths are appName-prefixed.

    Reads each parameter with the singular ``get_parameter`` (the only SSM read
    the read-only TUI client exposes — see ``tui/_readonly_client.py``). A
    parameter that is **absent** (or any transport error) leaves the
    corresponding field unresolved, so the property falls back to the legacy
    ``name_for`` reconstruction — safe against an older deployment that still
    pins explicit table names. Discovery is best-effort, never a hard
    dependency.
    """

    def _read(param_name: str) -> str | None:
        try:
            response = ssm_client.get_parameter(Name=param_name)
        except Exception:
            return None  # absent / unreadable → property falls back to name_for
        return ((response.get("Parameter") or {}).get("Value") or "").strip() or None

    wanted = (
        ("metadata_table_name", target.ssm_metadata_table_param),
        ("control_claims_table_name", target.ssm_control_claims_table_param),
        ("identity_registry_table_name", target.ssm_identity_registry_table_param),
    )
    resolved: dict[str, str] = {}
    for field, param_name in wanted:
        value = _read(param_name)
        if value:
            resolved[field] = value
    if not resolved:
        return target
    return replace(target, **resolved)


class EcrAuthError(RuntimeError):
    """Raised when ECR `get_authorization_token` returns an unusable payload."""


def get_ecr_registry_host(ssm_client: Any, target: DeploymentTarget) -> str:
    """Read the registry hostname from SSM (written by the CDK stack).

    Raises ClientError if the SSM parameter is missing — caller surfaces a
    friendly install hint pointing at the stack output.
    """
    response = ssm_client.get_parameter(Name=target.ssm_ecr_host_param)
    return response["Parameter"]["Value"].strip()


# Media types accepted when re-fetching a stored manifest to repoint a tag.
_ACCEPTED_MANIFEST_MEDIA_TYPES: tuple[str, ...] = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)


def tags_for_digest(
    ecr_client: Any,
    *,
    repository_name: str,
    image_digest: str,
) -> list[str]:
    """Return the tags currently applied to ``image_digest`` (empty if absent).

    A missing image (``ImageNotFoundException``) is reported as no tags — this
    is how the filed-digest precondition distinguishes "never pushed" (the
    digest is absent → see :func:`image_exists`) from "pushed but not yet
    canonically tagged" (the digest exists but carries no ``_ref`` tag).
    """
    try:
        resp = ecr_client.describe_images(
            repositoryName=repository_name,
            imageIds=[{"imageDigest": image_digest}],
        )
    except ecr_client.exceptions.ImageNotFoundException:
        return []
    tags: list[str] = []
    for detail in resp.get("imageDetails") or []:
        tags.extend(detail.get("imageTags") or [])
    return tags


def image_exists(
    ecr_client: Any,
    *,
    repository_name: str,
    image_digest: str,
) -> bool:
    """True iff a manifest with ``image_digest`` is present in the repository."""
    try:
        resp = ecr_client.describe_images(
            repositoryName=repository_name,
            imageIds=[{"imageDigest": image_digest}],
        )
    except ecr_client.exceptions.ImageNotFoundException:
        return False
    return bool(resp.get("imageDetails"))


def digest_for_tag(
    ecr_client: Any,
    *,
    repository_name: str,
    image_tag: str,
) -> str | None:
    """Return the digest ``image_tag`` currently points at, or None if absent.

    Used to decide ``set`` (create-or-fail) vs ``move`` (rebind): a present tag
    blocks ``set``; an absent tag blocks ``move``. The returned prior digest
    also feeds the move audit event (prior → new).
    """
    try:
        resp = ecr_client.describe_images(
            repositoryName=repository_name,
            imageIds=[{"imageTag": image_tag}],
        )
    except ecr_client.exceptions.ImageNotFoundException:
        return None
    for detail in resp.get("imageDetails") or []:
        digest = detail.get("imageDigest")
        if digest:
            return str(digest)
    return None


def put_alias_tag(
    ecr_client: Any,
    *,
    repository_name: str,
    image_digest: str,
    alias_tag: str,
) -> None:
    """Repoint ``alias_tag`` at ``image_digest`` in a single atomic ECR call.

    Re-fetches the stored manifest bytes for the (already-filed) digest and
    re-supplies them to ``put_image`` with the alias ``imageTag`` — ECR records
    the tag against the same content (the digest is unchanged). This is the
    fallback for ``oras tag`` and is itself the atomic registry rebind a
    ``move`` needs; a ``set`` onto the same digest is an idempotent no-op.

    The alias-namespace tag is mutable only because the repository carries an
    ``IMMUTABLE_WITH_EXCLUSION`` filter scoped to the ``_als``/``_chn`` reserved
    forms (the load-bearing alias-movement control is the dedicated IAM role,
    not this client).
    """
    fetched = ecr_client.batch_get_image(
        repositoryName=repository_name,
        imageIds=[{"imageDigest": image_digest}],
        acceptedMediaTypes=list(_ACCEPTED_MANIFEST_MEDIA_TYPES),
    )
    images = fetched.get("images") or []
    if not images:
        failures = fetched.get("failures") or []
        raise EcrAuthError(
            f"batch_get_image returned no manifest for digest {image_digest!r}; failures={failures}"
        )
    image = images[0]
    manifest_raw = image.get("imageManifest")
    media_type = image.get("imageManifestMediaType") or _ACCEPTED_MANIFEST_MEDIA_TYPES[0]
    if not manifest_raw:
        raise EcrAuthError(f"batch_get_image entry has no imageManifest for {image_digest!r}")
    ecr_client.put_image(
        repositoryName=repository_name,
        imageManifest=manifest_raw,
        imageManifestMediaType=media_type,
        imageTag=alias_tag,
    )


def ecr_login(ecr_client: Any, oras_module: Any, *, registry_host: str) -> None:
    """Log the local docker/oras credential store in to ``registry_host``.

    Obtains a fresh ECR authorization token and hands it to ``oras login``.
    ``oras login`` writes the shared docker credential store
    (``~/.docker/config.json``) that BOTH the `oras` CLI (manifest fetch, layer
    pull) AND the `notation` CLI (signature verification) consult — so a single
    login authenticates every registry round-trip a `push`, `pull`, or `verify`
    makes, with no per-tool credential plumbing. This is the read-path mirror of
    the `push` flow; both sides obtain credentials the same way, so the operator
    needs no ambient `docker login` precondition.

    Raises ``oras_module.OrasCommandError`` if the login itself fails and
    :class:`EcrAuthError` if the token payload is unusable; the caller surfaces
    either as a friendly CLI error.
    """
    username, password = get_ecr_basic_auth(ecr_client)
    oras_module.login(registry=registry_host, username=username, password=password)


def get_ecr_basic_auth(ecr_client: Any) -> tuple[str, str]:
    """Resolve a (username, password) pair for ECR via `get_authorization_token`.

    Returns ("AWS", "<password>") on success. The CLI passes this to
    `oras login` via stdin so the password never appears in the process
    table.
    """
    response = ecr_client.get_authorization_token()
    auth_data = response.get("authorizationData") or []
    if not auth_data:
        raise EcrAuthError("ECR get_authorization_token returned no authorizationData")
    token = auth_data[0].get("authorizationToken")
    if not token:
        raise EcrAuthError("ECR authorizationData[0].authorizationToken is empty")
    try:
        decoded = base64.b64decode(token).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as e:
        raise EcrAuthError(f"ECR authorizationToken is not base64-encoded UTF-8: {e}") from e
    if ":" not in decoded:
        raise EcrAuthError("ECR authorizationToken decoded payload is missing ':' separator")
    user, _, password = decoded.partition(":")
    return user, password

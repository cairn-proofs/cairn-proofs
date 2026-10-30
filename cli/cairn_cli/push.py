"""`cairn push` — emit a pure-content manifest + a signed Producer_Attestation_Referrer.

``cairn push`` emits **two** OCI objects, splitting content identity from the
producer claim:

  1. A **content manifest** — a content-descriptive Config_Blob (project-id,
     OSCAL references) plus the content layers, with only the identity-floor
     manifest annotation (project-id). It carries NO producer claim and NO
     artifact-kind. ``org.opencontainers.image.created`` is PINNED to a fixed
     epoch (``PINNED_CREATED``) rather than left unset — ``oras push`` would
     otherwise auto-inject wall-clock ``now()`` (oras 1.3.2 ignores
     SOURCE_DATE_EPOCH), folding a per-push value into the manifest and polluting
     content identity. With the timestamp pinned, the content Manifest_Digest is a
     pure, reproducible function of the content bytes — byte-identical evidence
     pushed twice yields the identical digest (see "Bundle Identity Is Pure
     Content Addressing").

  2. A **Producer_Attestation_Referrer** — an OCI referrer whose ``subject`` is the
     content Manifest_Digest, with ``artifactType``
     :data:`~cairn_oci.PRODUCER_ATTESTATION_ARTIFACT_TYPE`, carrying the producer's
     compliance claim (the opaque org-id/tenant-id/project-id identity tuple,
     artifact-kind, controls, catalog/profile UUID, assessment-period, assessor,
     assessment-id, supersedes). It has its own digest and is independently
     signed by the pipeline (see "Producer Claim Carried as Signed Attestation
     Referrer").

Flow:
  1. Sniff layer media types (or accept --media-type overrides).
  2. Validate the producer-supplied claim fields client-side (control-id
     grammar, catalog/profile UUID) as a UX pre-flight before the network
     round-trip — the validate-bundle Lambda re-validates server-side.
  3. Build the content-descriptive Config_Blob and serialize it.
  4. Resolve the ECR registry hostname from SSM, get an ECR auth token, and
     `oras login`.
  5. Compose the collapsed Customer_Hosted repo name (one repo per project) via
     the single `ArtifactLocator` authority. The client does NOT mint or supply
     a canonical ECR tag: both objects are admitted
     UNTAGGED, referenced by `sha256:` content digest. The canonical tag is
     derived by the pipeline from the validated artifact's intrinsic identity.
  6. Push the content manifest BY DIGEST (a TAGLESS reference) and capture its
     content Manifest_Digest.
  7. Attach the Producer_Attestation_Referrer against that content digest (subject +
     artifactType). The referrer's `PutImage` is the submission-complete
     validation trigger.
  8. Print the content Manifest_Digest only AFTER the attestation push
     succeeds — a content-only push that never receives its attestation is an
     incomplete submission and must not report a digest as if the submission
     were complete (design risk mitigation).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

import boto3
from cairn_oci import (
    ARTIFACT_KINDS,
    CONFIG_BLOB_MEDIA_TYPE,
    INTOTO_STATEMENT_MEDIA_TYPE,
    PINNED_CREATED,
    PRODUCER_ATTESTATION_ARTIFACT_TYPE,
    ArtifactLocator,
    CairnAnnotations,
    ConfigBlob,
    Descriptor,
    InvalidArtifactKindError,
    InvalidIdError,
    OciManifest,
    ProducerAttestationReferrer,
    build_producer_statement,
    compute_sha256_digest,
    normalize_artifact_kind,
)
from cairn_oci.controls import is_valid_control_id
from cairn_oci.manifest import MAX_CONTENT_LAYERS
from cairn_oci.periods import resolve_assessment_period
from cairn_validation import validate_bundle, validate_claim
from pydantic import ValidationError as PydanticValidationError

from cairn_cli import _oras
from cairn_cli._aws import (
    AppNameUnresolvedError,
    DeploymentTarget,
    EcrAuthError,
    ecr_login,
    get_ecr_registry_host,
    resolve_app_name,
)
from cairn_cli._errors import fail
from cairn_cli._help import CairnHelpFormatter, build_epilog
from cairn_cli._media_type import sniff_media_type
from cairn_cli._pending import TAG_PENDING_LABEL
from cairn_cli.config import TargetResolutionError, resolve_target

# The irreducible operator-supplied identity is ``project_id`` + ``files``; the
# CLI defaults ``--artifact-kind`` to ``other``, the catch-all canonical kind.
_DEFAULT_ARTIFACT_KIND = "other"

#: Surfaced to the user after a successful push. The canonical ECR tag is
#: derived by the pipeline from the validated OSCAL identity and is eventually
#: consistent — it does not exist at push time. The manifest stays resolvable
#: by its returned digest for the entire pushed-but-not-yet-tagged window.
#: The leading label is the shared
#: :data:`~cairn_cli._pending.TAG_PENDING_LABEL` so producer and consumers
#: speak the same phrase from one source.
TAG_PENDING_MESSAGE = (
    f"{TAG_PENDING_LABEL}: the canonical tag is assigned by the pipeline and is "
    "eventually consistent; resolve this bundle by its manifest digest "
    "(above) until the tag appears."
)


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "push",
        help="push an Artifact_Bundle to the Cairn Proofs store",
        formatter_class=CairnHelpFormatter,
        epilog=build_epilog(
            [
                "  cairn push --org-id acme-co --tenant-id acme --project-id payments \\\n"
                "      --assessment-period 2026-q2 assessment-results.json evidence.pdf",
                "  cairn push --target acme-co/acme/payments report.json   # fully-qualified",
                "  cairn push report.json   # identity from CAIRN_* env or saved config",
            ],
            notes=(
                "Identity coordinates: the opaque {org-id}/{tenant-id}/{project-id} "
                "triple is resolved from (1) explicit args (--org-id/--tenant-id/"
                "--project-id, or --target {org-id}/{tenant-id}/{project-id}), then "
                "(2) the CAIRN_ORG_ID/CAIRN_TENANT_ID/CAIRN_PROJECT_ID env vars, then "
                "(3) a persisted config (project-local .cairn.toml over the per-user "
                "config written by 'cairn config set-target'). An unresolved "
                "coordinate fails loud; a file token is never consumed as a "
                "coordinate.\n"
                "Every trailing token is a file (one content layer per file)."
            ),
        ),
    )
    # Identity is supplied via NAMED switches (or resolved from env/config), and
    # ``files`` is the SOLE positional, so argparse can never mis-allocate a
    # leading file token to an identity coordinate or metadata field. Each
    # identity coordinate is OPTIONAL on the command line because it may be
    # resolved from the environment or a persisted config; an unresolved
    # coordinate fails loud at run time (see ``_run`` / [INGEST-032]).
    p.add_argument("--org-id", help="opaque org-id identity coordinate (else env/config)")
    p.add_argument("--tenant-id", help="opaque tenant-id identity coordinate (else env/config)")
    p.add_argument("--project-id", help="opaque project-id identity coordinate (else env/config)")
    p.add_argument(
        "--target",
        help="fully-qualified {org-id}/{tenant-id}/{project-id} (populates all three coordinates)",
    )
    p.add_argument(
        "--assessment-period",
        default=None,
        help="YYYY-MM-DD | YYYY-qN (lowercase) | YYYY (optional; defaults to the ingestion date)",
    )
    p.add_argument("files", nargs="+", type=Path)
    # NOT ``choices=ARTIFACT_KINDS``: argparse ``choices`` would reject an alias
    # (e.g. ``attestations``, ``baseline``, ``poam``) the store itself accepts and
    # normalizes on input. Instead the supplied token is validated AFTER parse via
    # the single ``normalize_artifact_kind`` resolver (see ``_run``), so an alias is
    # accepted client-side and only a genuinely-unknown kind is rejected — with an
    # error listing the allowed canonical values.
    p.add_argument(
        "--artifact-kind",
        default=_DEFAULT_ARTIFACT_KIND,
        metavar="ARTIFACT_KIND",
        help=(
            "artifact-kind: a canonical value or a known alias "
            f"(optional; defaults to {_DEFAULT_ARTIFACT_KIND!r}). Canonical values: "
            + ", ".join(sorted(ARTIFACT_KINDS))
        ),
    )
    p.add_argument(
        "--catalog-or-profile-uuid",
        help="OSCAL Catalog or Profile UUID (UUIDDatatype, lowercase 8-4-4-4-12) (optional)",
    )
    p.add_argument("--control", action="append", help="control id (repeatable; optional)")
    p.add_argument(
        "--media-type",
        action="append",
        default=[],
        metavar="FILENAME=MEDIATYPE",
        help="override sniffed media type for FILENAME (repeatable)",
    )
    p.add_argument("--source-tool")
    p.add_argument("--assessor")
    p.add_argument("--assessment-id")
    p.add_argument(
        "--supersedes",
        help="comma-separated tags within the same repository that this bundle supersedes",
    )
    p.add_argument("--stage", default="alpha")
    p.add_argument("--region-code", default="cmh")
    p.add_argument(
        "--app-name",
        default=None,
        help="deployment appName override (else CAIRN_APP_NAME or SSM discovery)",
    )
    p.set_defaults(handler=run)


def _parse_media_type_overrides(raw: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for entry in raw:
        if "=" not in entry:
            raise ValueError(f"--media-type override {entry!r} must be FILENAME=MEDIATYPE")
        name, _, media_type = entry.partition("=")
        overrides[name] = media_type
    return overrides


def _build_descriptor_for(path: Path, media_type: str, layer_title: str) -> Descriptor:
    body = path.read_bytes()
    digest = compute_sha256_digest(body)
    return Descriptor(
        mediaType=media_type,  # type: ignore[call-arg]
        digest=digest,
        size=len(body),
        annotations={"org.opencontainers.image.title": layer_title},
    )


def run(args: argparse.Namespace) -> int:
    return _run(args, ssm_client=None, ecr_client=None, oras_module=_oras)


def _run(
    args: argparse.Namespace,
    *,
    ssm_client,
    ecr_client,
    oras_module,
) -> int:
    """Inner entry point: clients are injected so tests can pass mocks.

    Tests call ``_run(...)`` directly with pre-configured boto3 stubs and a
    fake ``oras_module``. Production callers go through ``run()`` which
    builds real boto3 clients lazily — keeping the dependency chain
    explicit and per-invocation rather than module-level.
    """
    target = DeploymentTarget(stage=args.stage, region_code=args.region_code)

    # Validate --artifact-kind via the single fail-closed resolver (the store's own
    # rule): a canonical value or a known alias is accepted; anything else fails
    # fast with the allowed CANONICAL values. The producer-supplied spelling is
    # preserved verbatim downstream, so we only validate here — we do NOT
    # substitute the canonical.
    try:
        normalize_artifact_kind(args.artifact_kind)
    except InvalidArtifactKindError:
        allowed = ", ".join(sorted(ARTIFACT_KINDS))
        return fail(
            f"invalid --artifact-kind {args.artifact_kind!r}; allowed canonical values: {allowed}",
            exit_code=2,
        )

    # Resolve the three identity coordinates via the [INGEST-032] precedence
    # chain — explicit args (--org-id/--tenant-id/--project-id or --target) >
    # CAIRN_* env vars > persisted config (project-local .cairn.toml over the
    # per-user config). An unresolved coordinate fails loud; a file token is
    # never consumed as a coordinate (identity is switches/env/config only).
    try:
        coordinates = resolve_target(
            org_id=args.org_id,
            tenant_id=args.tenant_id,
            project_id=args.project_id,
            target=args.target,
        )
    except TargetResolutionError as e:
        return fail(str(e), exit_code=2)
    org_id = coordinates["org-id"].value
    tenant_id = coordinates["tenant-id"].value
    project_id = coordinates["project-id"].value

    # 1. Resolve media types.
    try:
        overrides = _parse_media_type_overrides(args.media_type)
    except ValueError as e:
        return fail(str(e), exit_code=2)

    files_with_types: list[tuple[Path, str, str]] = []
    for path in args.files:
        if not path.exists():
            return fail(f"file not found: {path}", exit_code=2)
        media_type = overrides.get(path.name) or sniff_media_type(path)
        layer_title = path.name
        files_with_types.append((path, media_type, layer_title))

    # Control linkage is optional and is a CLAIM field carried in
    # the Producer_Attestation_Referrer, not the content manifest. When supplied,
    # fast-fail any malformed control id client-side before the
    # network round-trip — the validate gate enforces the same grammar server-side
    # against the referrer.
    controls = list(args.control) if args.control else []
    for control_id in controls:
        if not is_valid_control_id(control_id):
            return fail(
                f"malformed control id {control_id!r}: expected lowercase-alphanumeric "
                "segments joined by single '.'/'-' (e.g. 'ac-2', 'ac-2.1', '5.1')",
                exit_code=2,
            )

    # Resolve the assessment-period claim coordinate. assessmentPeriod is
    # producer-supplied in the Producer_Attestation_Referrer (Decision D1) and is NEVER
    # part of the content digest. We retain the ingestion-date default for the
    # evidence-form claim so an omitted period still records a usable window; the
    # value rides in the referrer, not in the content manifest or any tag segment.
    ingestion_date = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    resolved_period = resolve_assessment_period(args.assessment_period, ingestion_date)

    # 2. Build the content-descriptive Config_Blob (Req 318). It carries ONLY
    # the content-descriptive, IAM-grounded identity field (project-id) — NO
    # producer claim and NO artifact-kind. The claim fields (including
    # artifact-kind) live in the Producer_Attestation_Referrer below, so they are not
    # part of the content Manifest_Digest.
    config_blob_model = ConfigBlob(
        projectId=project_id,  # type: ignore[call-arg]
    )
    config_blob_bytes = config_blob_model.model_dump_json(by_alias=True, exclude_none=True).encode(
        "utf-8"
    )
    config_descriptor = Descriptor(
        mediaType=CONFIG_BLOB_MEDIA_TYPE,  # type: ignore[call-arg]
        digest=compute_sha256_digest(config_blob_bytes),
        size=len(config_blob_bytes),
    )

    # 3. Build the content manifest. Identity floor ONLY (project-id). NO
    # artifact-kind annotation (kind is claim-only) and NO claim annotations
    # (controls, period, assessor, assessment-id, supersedes) — those are
    # referrer fields. The one subtlety: ``oras push`` AUTO-INJECTS
    # ``org.opencontainers.image.created`` with the wall-clock push time unless we
    # supply the key ourselves (and oras 1.3.2 ignores SOURCE_DATE_EPOCH), so
    # simply omitting it lets oras fold per-push ``now()`` into the manifest and
    # breaks content addressing. We therefore PIN it to a fixed epoch sentinel
    # (PINNED_CREATED): present but deterministic, carrying no ambient state. With
    # no per-push value folded in, the content manifest bytes — and therefore the
    # content Manifest_Digest — are a pure function of the content member set (see
    # "Bundle Identity Is Pure Content Addressing" / "Manifest Digest
    # Computation").
    layer_descriptors = [
        _build_descriptor_for(path, media_type, title)
        for path, media_type, title in files_with_types
    ]
    manifest_annotations: dict[str, str] = {
        CairnAnnotations.PROJECT_ID: project_id,
        CairnAnnotations.CREATED: PINNED_CREATED,
    }
    # The source-tool is content-descriptive provenance (not a producer claim), so
    # it remains a manifest annotation when supplied.
    if args.source_tool:
        manifest_annotations[CairnAnnotations.SOURCE_TOOL] = args.source_tool

    # Assemble the in-memory content manifest for the client-side structural
    # pre-flight; oras assembles the wire manifest from the same inputs.
    # ``OciManifest`` enforces the content-layer cap (``MAX_CONTENT_LAYERS``) at
    # the Pydantic layer, so a too-many-layers bundle raises here BEFORE
    # ``validate_bundle`` can surface criterion ``i2``. Catch that construction
    # error and route it through the SAME exit-3 pre-flight rejection so the user
    # gets a clean which-constraint message locally rather than an uncaught
    # traceback — and, like every other pre-flight failure, before any blob
    # upload / registry login.
    try:
        content_manifest = OciManifest(
            config=config_descriptor,
            layers=layer_descriptors,
            annotations=manifest_annotations,
        )
    except PydanticValidationError as e:
        print("error: client-side bundle validation failed:", file=sys.stderr)
        print(
            f"  [i2] manifest.layers: content-layer count must be at most "
            f"{MAX_CONTENT_LAYERS}; got {len(layer_descriptors)} ({e.error_count()} "
            "manifest validation error(s))",
            file=sys.stderr,
        )
        return 3

    # 4. Build the producer's compliance claim (the Producer_Attestation_Referrer payload).
    # Every CLI arg that supplies a claim value feeds this referrer (not the
    # Config_Blob or a manifest annotation). ``assessmentPeriod`` is the
    # producer-supplied (or ingestion-date-defaulted) evidence-form coordinate.
    # The claim is wrapped into an in-toto Statement at attach time (below), once
    # the content Manifest_Digest — the Statement's subject — is known.
    attestation = ProducerAttestationReferrer(
        orgId=org_id,
        tenantId=tenant_id,
        projectId=project_id,
        artifactKind=args.artifact_kind,
        controls=controls,
        catalogOrProfileUuid=args.catalog_or_profile_uuid,
        assessmentPeriod=resolved_period,
        assessor=args.assessor,
        assessmentId=args.assessment_id,
        supersedes=args.supersedes,
    )

    # Client-side pre-flight (purely for UX — the validate-bundle Lambda runs
    # the SAME checks server-side post-push). The content manifest's structure is
    # checked by `validate_bundle`; the producer's claim grammar (artifact-kind
    # vocabulary, catalogOrProfileUuid, control ids) is checked by `validate_claim`
    # against the Producer_Attestation_Referrer, mirroring the split-by-trust-origin in the
    # validator (the claim lives in the Producer_Attestation_Referrer, not the Config_Blob).
    errors = validate_bundle(content_manifest, config_blob_bytes).errors
    errors += validate_claim(attestation)
    if errors:
        print("error: client-side bundle validation failed:", file=sys.stderr)
        for err in errors:
            print(f"  [{err.criterion}] {err.field}: {err.message}", file=sys.stderr)
        return 3

    # 5. Resolve registry, log in.
    if ssm_client is None:
        ssm_client = boto3.client("ssm")
    if ecr_client is None:
        ecr_client = boto3.client("ecr")

    # Resolve the deployment appName first — every SSM read path and the ECR
    # namespace are derived from it.
    try:
        target = resolve_app_name(ssm_client, target, override=args.app_name)
    except AppNameUnresolvedError as e:
        return fail(str(e), exit_code=4)

    registry_host = get_ecr_registry_host(ssm_client, target)
    # Uniform three-segment path: one repo per
    # {org-id}/{tenant-id}/{project-id}, no category segment. We derive ONLY the
    # repo name here — NOT a tag. The client never composes a canonical tag;
    # the pipeline mints it server-side. ``repo_path``
    # namespaces the locator's bare ``artifact-{qualifier}/...`` prefix with the
    # resolved appName so the push target matches the deployed ECR repo. A
    # malformed coordinate fails loud here rather than at the registry.
    try:
        repo_path = target.repo_path(
            ArtifactLocator().repo_name(
                deployment_qualifier=target.qualifier,
                org_id=org_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )
        )
    except InvalidIdError as e:
        return fail(str(e), exit_code=2)
    # Push BY DIGEST: a TAGLESS reference. oras admits both objects under their
    # `sha256:` content digests with no tag.
    reference = f"{registry_host}/{repo_path}"

    try:
        ecr_login(ecr_client, oras_module, registry_host=registry_host)
    except (EcrAuthError, oras_module.OrasCommandError) as e:
        return fail(f"ECR login failed: {e}", exit_code=4)

    # 6 + 7. Push the content manifest first (capturing its content digest), then
    # attach the Producer_Attestation_Referrer against that digest. Order matters: a
    # referrer cannot precede its subject (it references the content digest), and
    # the referrer's push is the submission-complete validation trigger.
    with tempfile.TemporaryDirectory(prefix="cairn-push-") as tmpdir:
        tmp_path = Path(tmpdir)
        config_path = tmp_path / "config.json"
        config_path.write_bytes(config_blob_bytes)
        manifest_export = tmp_path / "manifest.json"
        try:
            content_result = oras_module.push(
                reference=reference,
                config_file=config_path,
                config_media_type=CONFIG_BLOB_MEDIA_TYPE,
                files=files_with_types,
                annotations=manifest_annotations,
                export_manifest_to=manifest_export,
            )
        except oras_module.OrasCommandError as e:
            return fail(f"oras push failed: {(e.stderr or '').strip()}", exit_code=5)

        content_digest = content_result.manifest_digest
        subject_reference = f"{reference}@{content_digest}"

        # The claim layer is a standard in-toto Statement whose single-element
        # ``subject`` binds the content Manifest_Digest (only known now, after the
        # content push) — and ``build_producer_statement``. The
        # claim layer ``mediaType`` is ``application/vnd.in-toto+json`` to announce
        # the format to external tooling; the OCI envelope ``artifactType`` is
        # ``PRODUCER_ATTESTATION_ARTIFACT_TYPE`` — the role label and validate-trigger
        # discriminator. The Statement's subject digest equals
        # the value the envelope subject points at (``subject_reference`` above).
        statement = build_producer_statement(content_digest, attestation)
        statement_bytes = json.dumps(statement).encode("utf-8")

        attestation_path = tmp_path / "attestation.json"
        attestation_path.write_bytes(statement_bytes)
        attestation_export = tmp_path / "attestation-manifest.json"
        try:
            oras_module.attach(
                subject_reference=subject_reference,
                artifact_type=PRODUCER_ATTESTATION_ARTIFACT_TYPE,
                file=attestation_path,
                file_media_type=INTOTO_STATEMENT_MEDIA_TYPE,
                # Do NOT pin the referrer manifest's `created`. The content
                # manifest's `created` is pinned for content addressing, but the
                # Producer_Attestation_Referrer is NOT content-addressed identity, and
                # pinning it makes an identical content+claim re-push produce an
                # identical referrer digest — which (a) is an ECR no-op that emits
                # no PutImage event, so validate never re-triggers, and (b) leaves
                # both subjects already-signed, so sign-bundle short-circuits.
                # Letting oras stamp a wall-clock `created` keeps each referrer
                # unique per push, preserving re-trigger + non-idempotent signing.
                # The claim layer title is still set to the basename by the
                # wrapper, so the ephemeral temp path stays out of the referrer.
                annotations={},
                export_manifest_to=attestation_export,
            )
        except oras_module.OrasCommandError as e:
            # The content manifest landed but the claim did not. The content-only
            # manifest is an incomplete submission (D5) — never promoted — so we
            # do NOT print the content digest as if the submission completed.
            return fail(
                f"attestation referrer push failed (content manifest was pushed but is an "
                f"incomplete submission until its attestation lands): {(e.stderr or '').strip()}",
                exit_code=5,
            )

    # 8. Report the content Manifest_Digest synchronously — ONLY now that the
    # attestation push has succeeded ([INGEST-026], R4). The canonical tag is
    # eventually consistent — minted later by the pipeline — so surface a
    # "tag pending" notice pointing the user at the digest meanwhile.
    print(content_digest)
    print(TAG_PENDING_MESSAGE, file=sys.stderr)
    return 0

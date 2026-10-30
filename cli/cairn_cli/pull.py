"""`cairn pull` — fetch an Artifact_Bundle from ECR, verify, write layers.

Order is deliberate (strict-by-default per signing spec):
  1. Resolve registry from SSM and parse the reference.
  2. Fetch the manifest via `oras manifest fetch`.
  3. Verify the signature via Notation BEFORE writing any layer bytes.
     Failure short-circuits with no files written.
  4. Pull the layers via `oras pull`.
  5. Per-layer digest verification ([INGEST-007.03]) as
     defense-in-depth integrity check on top of oras' own verification.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import boto3
from cairn_oci import alias_tag_of as _grammar_alias_tag_of
from cairn_oci import compute_sha256_digest, verify_digest
from cairn_oci.digest import IntegrityError
from cairn_signing import NotationCommandError, verify_artifact

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
from cairn_cli._pending import (
    TAG_PENDING_EXIT_CODE,
    is_by_tag,
    is_tag_miss,
    surface_tag_pending,
)
from cairn_cli._refs import InvalidReferenceError, ResolvedReference, resolve_reference

# Re-exported from cairn_cli._pending (shared with `cairn verify`) so scripts
# importing it off this module keep working.
__all__ = ["TAG_PENDING_EXIT_CODE", "alias_tag_of", "mutable_alias_label"]


def alias_tag_of(ref: ResolvedReference) -> str | None:
    """Return the alias tag string if ``ref`` was resolved BY an alias tag.

    Aliases never carry the verification guarantee: the
    digest — anchored by signature + WORM_Receipt — is always the identity, and
    an alias is only ever a *mutable label* over it. This is the ``pull``/``verify``
    adapter over the single pure-grammar classifier (``cairn_oci.alias_tag_of``):
    a by-DIGEST reference (selector not ``:``-prefixed) is never an alias; for a
    by-tag reference the tag is classified by the grammar authority, so a
    canonical `_ref` tag or any tag the parser rejects returns ``None``. Resolving
    the alias to its underlying digest stays the registry's job — done natively by
    the existing tag→digest machinery (`oras`/`notation`), not a new client path.
    """
    if not ref.selector.startswith(":"):
        return None
    return _grammar_alias_tag_of(ref.selector[1:])


def mutable_alias_label(alias_tag: str, digest: str) -> str:
    """The one canonical "reached via mutable alias" line.

    Surfaces an alias strictly as a mutable label pointing AT the resolved
    immutable digest — never as a verified identity. Shared by `pull` / `verify`
    (and mirrored by the TUI) so every read consumer speaks identically.
    """
    return f"reached via mutable alias {alias_tag} -> {digest}"


def _is_safe_layer_title(title: str) -> bool:
    """Reject layer titles that could escape the output directory.

    The OCI `org.opencontainers.image.title` annotation is producer-supplied;
    a successfully signature-verified bundle from a compromised signing key
    (or a future signing-profile expansion) could still ship `../../etc/...`
    as a layer title. We refuse to materialize anything that isn't a bare
    filename — no separators, no `..`, no absolute paths, no NUL bytes.
    """
    if not title or title in (".", ".."):
        return False
    if "\x00" in title or "/" in title or "\\" in title:
        return False
    # PurePath.name strips any directory component; if the result differs from
    # the input, the title contained traversal syntax that pathlib normalized.
    return Path(title).name == title


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "pull",
        help="pull, verify, and extract an Artifact_Bundle to the local filesystem",
        description=(
            "Fetch an Artifact_Bundle from ECR, verify its Notation signature "
            "before writing any bytes, then extract each layer to the output "
            "directory. The reference may be a long-form ref or the short "
            "<project-id>/<artifact-kind>:tag form."
        ),
        formatter_class=CairnHelpFormatter,
        epilog=build_epilog(
            ["  cairn pull alpha-bank/assessment-results:2026-q1 --output-dir ./out"]
        ),
    )
    p.add_argument("reference", help="long-form ref OR <project-id>/<artifact-kind>:tag")
    p.add_argument("--output-dir", type=Path, default=Path.cwd())
    p.add_argument("--stage", default="alpha")
    p.add_argument("--region-code", default="cmh")
    p.add_argument(
        "--app-name",
        default=None,
        help="deployment appName override (else CAIRN_APP_NAME or SSM discovery)",
    )
    p.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    return _run(
        args,
        ssm_client=None,
        ecr_client=None,
        oras_module=_oras,
        verify_fn=verify_artifact,
    )


def _run(
    args: argparse.Namespace,
    *,
    ssm_client,
    oras_module,
    verify_fn,
    ecr_client=None,
) -> int:
    target = DeploymentTarget(stage=args.stage, region_code=args.region_code)
    if ssm_client is None:
        ssm_client = boto3.client("ssm")
    if ecr_client is None:
        ecr_client = boto3.client("ecr")

    try:
        target = resolve_app_name(ssm_client, target, override=args.app_name)
    except AppNameUnresolvedError as e:
        return fail(str(e), exit_code=4)

    try:
        registry_host = get_ecr_registry_host(ssm_client, target)
    except Exception as e:
        return fail(f"failed to resolve ECR registry host: {e}", exit_code=4)

    try:
        ref = resolve_reference(args.reference, registry_host=registry_host, target=target)
    except InvalidReferenceError as e:
        return fail(str(e), exit_code=2)

    # Authenticate the local oras/docker credential store to ECR before any
    # registry round-trip (verify, manifest fetch, layer pull) — the read paths
    # obtain credentials exactly as `push` does, so no ambient `docker login` is
    # required of the operator.
    try:
        ecr_login(ecr_client, oras_module, registry_host=registry_host)
    except (EcrAuthError, oras_module.OrasCommandError) as e:
        return fail(f"ECR login failed: {e}", exit_code=4)

    # 1. Verify signature BEFORE writing anything.
    try:
        verify_fn(ref.full)
    except NotationCommandError as e:
        # A by-tag lookup that misses because the canonical tag has not been
        # minted yet is the eventual-consistency window, NOT a
        # signature rejection — surface "tag pending" and point at the digest.
        if is_by_tag(ref) and is_tag_miss(e.stderr):
            return surface_tag_pending(ref)
        return fail(f"signature verification failed: {e.stderr.strip()}", exit_code=4)
    except RuntimeError as e:
        return fail(f"notation not available: {e}", exit_code=4)

    # 2. Fetch the manifest so we know each layer's expected digest +
    #    layer title before pulling.
    try:
        manifest_bytes = oras_module.fetch_manifest(reference=ref.full)
    except oras_module.OrasCommandError as e:
        if is_by_tag(ref) and is_tag_miss(e.stderr):
            return surface_tag_pending(ref)
        return fail(f"failed to fetch manifest: {e.stderr.strip()}", exit_code=4)
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as e:
        return fail(f"registry returned non-JSON manifest payload: {e}", exit_code=4)

    # The fetched manifest's own sha256 IS the resolved Manifest_Digest — the
    # registry resolved the (possibly-alias) tag to it natively, so this is the
    # identity we anchor on and present, never the tag string.
    manifest_digest = compute_sha256_digest(manifest_bytes)

    layers = manifest.get("layers") or []
    expected: dict[str, str] = {}  # filename → expected sha256
    for layer in layers:
        annotations = layer.get("annotations") or {}
        title = annotations.get("org.opencontainers.image.title")
        digest = layer.get("digest")
        if not title:
            return fail(
                "manifest layer has no org.opencontainers.image.title annotation; "
                "cannot determine output filename",
                exit_code=4,
            )
        if not _is_safe_layer_title(title):
            return fail(
                f"layer title {title!r} is not a bare filename; refusing to "
                "extract — path traversal or absolute paths in layer titles are rejected",
                exit_code=4,
            )
        if title in expected:
            return fail(
                f"manifest declares two layers with identical title {title!r}; "
                "refusing to overwrite",
                exit_code=4,
            )
        expected[title] = digest

    output_dir = args.output_dir.resolve()
    # Pre-check: refuse to overwrite ANY existing file with a layer's title.
    for title in expected:
        target_path = output_dir / title
        if target_path.exists():
            return fail(
                f"refusing to overwrite existing file {target_path}; "
                "specify a different --output-dir",
                exit_code=4,
            )

    # 3. Pull the layers — oras pull writes one file per layer using the
    #    layer's title annotation.
    try:
        oras_module.pull(reference=ref.full, output_dir=output_dir)
    except oras_module.OrasCommandError as e:
        return fail(f"oras pull failed: {e.stderr.strip()}", exit_code=4)

    # 4. Defense-in-depth: verify each written file matches the manifest digest.
    return _verify_and_report(ref, expected, output_dir, manifest_digest=manifest_digest)


def _verify_and_report(
    ref: ResolvedReference,
    expected: dict[str, str],
    output_dir: Path,
    *,
    manifest_digest: str,
) -> int:
    for title, digest in expected.items():
        path = output_dir / title
        if not path.exists():
            return fail(f"expected layer file not written by oras: {path}", exit_code=4)
        try:
            with path.open("rb") as fh:
                verify_digest(fh, digest, artifact=str(path))
        except IntegrityError as e:
            # Pulled bytes don't match the manifest digest — possible tampering
            # in transit or a registry-side corruption. Leave the file in place
            # for forensic inspection; operator can re-pull after diagnosing.
            return fail(f"layer integrity check failed: {e}", exit_code=4)
    for title in expected:
        print(f"wrote: {output_dir / title}")
    # When the artifact was reached via a mutable alias, surface the alias as a
    # LABEL pointing at the resolved immutable digest, and key the verdict on
    # the DIGEST — never on the alias string. A canonical / by-
    # digest pull keeps presenting its resolved reference unchanged.
    alias_tag = alias_tag_of(ref)
    if alias_tag is not None:
        print(mutable_alias_label(alias_tag, manifest_digest))
        print(f"verified: {manifest_digest}")
    else:
        print(f"verified: {ref.full}")
    return 0

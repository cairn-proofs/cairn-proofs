"""Thin subprocess wrapper around the `oras` CLI binary.

Why not oras-py:
    The Python `oras` library exists but its API for pushing a manifest
    with both a custom Config_Blob (custom media type + body) and
    arbitrary manifest annotations is awkward at best — it requires
    constructing the manifest dict by hand and bypassing the
    OrasClient.push convenience method, at which point the library
    offers little over plain `oras push --config foo.json:type ... -a`.
    The `oras` binary, by contrast, exposes both as first-class flags
    and is already pinned by mise.toml as a project tool.

The wrapper exists to:
    1. Keep all subprocess invocation in one place (single test surface).
    2. Materialize a structured error type that callers can catch.
    3. Pass ECR credentials via stdin to `oras login` so the password
       never appears in the process table.

All credentials and paths are taken as explicit arguments — there is no
module-level configuration state.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

ORAS_BIN: Final[str] = "oras"


class OrasCommandError(RuntimeError):
    """Raised when an `oras` invocation exits non-zero.

    Carries the full stderr so callers can surface immutability-rejected
    push attempts ([INGEST-028] → "tag already exists, bump --tag-sequence")
    and other ECR-side errors.
    """

    def __init__(self, *, returncode: int, stderr: str, command: list[str]) -> None:
        super().__init__(f"oras exited {returncode}: {stderr.strip()}")
        self.returncode = returncode
        self.stderr = stderr
        self.command = command


@dataclass(frozen=True)
class PushResult:
    """Result of a successful `oras push`."""

    manifest_digest: str
    reference: str  # registry/repo:tag form passed to oras


def _ensure_oras_installed() -> None:
    if shutil.which(ORAS_BIN) is None:
        raise RuntimeError(
            f"{ORAS_BIN!r} not found on PATH; install via mise (`mise install oras`) "
            "or add it to mise.toml"
        )


def _run(
    command: list[str],
    *,
    timeout_seconds: float,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(  # noqa: S603 - argv list built internally from the fixed ORAS_BIN, never a shell string or untrusted input
        command,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        input=stdin,
        check=False,
    )
    if proc.returncode != 0:
        raise OrasCommandError(
            returncode=proc.returncode,
            stderr=proc.stderr,
            command=command,
        )
    return proc


def login(
    *,
    registry: str,
    username: str,
    password: str,
    timeout_seconds: float = 30.0,
) -> None:
    """`oras login` — credentials passed via stdin to keep them off argv."""
    _ensure_oras_installed()
    command = [ORAS_BIN, "login", "--username", username, "--password-stdin", registry]
    _run(command, timeout_seconds=timeout_seconds, stdin=password)


_TITLE_ANNOTATION: Final[str] = "org.opencontainers.image.title"


def _write_annotation_file(
    export_manifest_to: Path,
    *,
    manifest_annotations: dict[str, str],
    layer_titles: list[tuple[Path, str]],  # (path, chosen layer title)
) -> Path:
    """Write the ``oras --annotation-file`` JSON and return its path.

    Shared by :func:`push` and :func:`attach`. The payload's top-level keys are
    file paths; ``"$manifest"`` is the special key for manifest-level
    annotations. Two load-bearing jobs:

    1. Each layer's ``org.opencontainers.image.title`` is overridden to a chosen
       title (the file's basename), so the absolute on-disk path — an ephemeral
       ``cairn-push-*`` temp path for the attach claim — never lands in the
       published, signed, WORM-archived manifest as a layer title.
    2. ``$manifest`` carries the caller's annotations verbatim, including the
       pinned ``org.opencontainers.image.created`` that overrides oras's
       auto-injected wall-clock value, so the manifest is deterministic.
    """
    payload: dict[str, dict[str, str]] = {"$manifest": dict(manifest_annotations)}
    for path, title in layer_titles:
        payload[str(path)] = {_TITLE_ANNOTATION: title}
    annotations_path = export_manifest_to.with_suffix(".annotations.json")
    annotations_path.write_text(json.dumps(payload), encoding="utf-8")
    return annotations_path


def _digest_from_exported_manifest(export_manifest_to: Path) -> str:
    """Canonical Manifest_Digest = sha256 over the oras-exported manifest bytes."""
    from cairn_oci.digest import compute_sha256_digest

    return compute_sha256_digest(export_manifest_to.read_bytes())


def push(
    *,
    reference: str,
    config_file: Path,
    config_media_type: str,
    files: list[tuple[Path, str, str]],  # (path, media_type, layer_title)
    annotations: dict[str, str],
    export_manifest_to: Path,
    timeout_seconds: float = 600.0,
) -> PushResult:
    """`oras push --config ... --annotation ... <reference> <file>:<type> ...`.

    Layer titles are passed via per-file ``--annotation`` flags scoped to
    the file (oras supports `--annotation <file>=<key>=<value>` for that
    via the `-a` short flag in newer releases; here we use the
    annotation-file mechanism so per-layer titles land correctly).

    Returns a PushResult with the exported manifest's digest.
    """
    _ensure_oras_installed()

    if not files:
        raise ValueError("push() requires at least one file")

    annotations_path = _write_annotation_file(
        export_manifest_to,
        manifest_annotations=annotations,
        layer_titles=[(path, layer_title) for path, _media_type, layer_title in files],
    )

    command: list[str] = [
        ORAS_BIN,
        "push",
        "--config",
        f"{config_file}:{config_media_type}",
        "--annotation-file",
        str(annotations_path),
        "--export-manifest",
        str(export_manifest_to),
        # `cairn push` always overrides the layer title via the annotation
        # file (set to `path.name` upstream), so the absolute path is used
        # only to read file bytes for upload — it never lands in the
        # manifest. oras's path-validation guard exists to catch the
        # opposite case (absolute path leaking into the layer filename),
        # which can't happen here.
        "--disable-path-validation",
        reference,
    ]
    for path, media_type, _ in files:
        command.append(f"{path}:{media_type}")

    _run(command, timeout_seconds=timeout_seconds)

    return PushResult(
        manifest_digest=_digest_from_exported_manifest(export_manifest_to),
        reference=reference,
    )


def attach(
    *,
    subject_reference: str,
    artifact_type: str,
    file: Path,
    file_media_type: str,
    annotations: dict[str, str],
    export_manifest_to: Path,
    timeout_seconds: float = 600.0,
) -> PushResult:
    """`oras attach --artifact-type <type> --annotation-file ... <subject_ref> <file>:<type>`.

    Pushes an OCI **referrer** whose ``subject`` is ``subject_reference`` (a
    digest-pinned ``registry/repo@sha256:...`` reference) and whose
    ``artifactType`` is ``artifact_type``. ``cairn push`` uses this to attach
    the Producer_Attestation_Referrer (claim) against an already-pushed content manifest:
    the referrer carries the producer's compliance claim as its single layer and
    is admitted untagged (referenced by its own ``sha256:`` digest).

    Goes through the same ``--annotation-file`` mechanism as :func:`push` (via
    :func:`_write_annotation_file`) so the referrer manifest is clean and
    deterministic: the claim layer's title is the file basename (never the
    ephemeral ``cairn-push-*`` temp path), and ``annotations`` pins the
    referrer's ``org.opencontainers.image.created`` rather than letting oras
    fold in wall-clock ``now()``.

    Returns a PushResult with the exported referrer manifest's digest.
    """
    _ensure_oras_installed()

    annotations_path = _write_annotation_file(
        export_manifest_to,
        manifest_annotations=annotations,
        layer_titles=[(file, file.name)],
    )

    command: list[str] = [
        ORAS_BIN,
        "attach",
        "--artifact-type",
        artifact_type,
        "--annotation-file",
        str(annotations_path),
        "--export-manifest",
        str(export_manifest_to),
        # The layer title is overridden to ``file.name`` via the annotation file
        # above, so the absolute path is used only to read bytes for upload and
        # never lands in the referrer manifest. Path validation can stay off, as
        # in push().
        "--disable-path-validation",
        subject_reference,
        f"{file}:{file_media_type}",
    ]

    _run(command, timeout_seconds=timeout_seconds)

    return PushResult(
        manifest_digest=_digest_from_exported_manifest(export_manifest_to),
        reference=subject_reference,
    )


def fetch_manifest(*, reference: str, timeout_seconds: float = 60.0) -> bytes:
    """`oras manifest fetch <ref>` — returns raw manifest bytes."""
    _ensure_oras_installed()
    command = [ORAS_BIN, "manifest", "fetch", reference]
    proc = _run(command, timeout_seconds=timeout_seconds)
    return proc.stdout.encode("utf-8")


def pull(
    *,
    reference: str,
    output_dir: Path,
    timeout_seconds: float = 600.0,
) -> None:
    """`oras pull -o <dir> <ref>` — extracts layers using layer titles as filenames.

    Uses ``--allow-path-traversal=false`` (default) so a malicious layer
    title can't write outside ``output_dir``.
    """
    _ensure_oras_installed()
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [ORAS_BIN, "pull", "--output", str(output_dir), reference]
    _run(command, timeout_seconds=timeout_seconds)

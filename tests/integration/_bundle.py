"""Low-level Evidence_Bundle assembly helpers for integration tests.

The happy-path test uses the public ``cairn push`` CLI. Failure-path tests
need to push deliberately broken bundles — manifests with missing
annotations, malformed Config_Blob fields, accountId mismatches — which the
CLI's client-side validation refuses to assemble. This module bypasses the
CLI and drives ``oras push`` directly against a hand-crafted Config_Blob +
manifest pair.

Helpers exposed:

- ``build_config_blob_bytes``        serialize a ConfigBlob (with overrides
                                     that bypass model validation)
- ``ecr_login``                      acquire a basic-auth token and
                                     ``oras login`` against ECR
- ``oras_push_raw``                  push a directory tree of layer files
                                     plus a hand-built Config_Blob to a
                                     registry reference, returning the
                                     resulting manifest digest
- ``oras_attach_raw``                attach a Producer_Attestation_Referrer against an
                                     already-pushed content manifest digest —
                                     the submission-complete validate trigger,
                                     so a hand-pushed bundle is
                                     actually evaluated by validate-bundle
"""

from __future__ import annotations

import base64
import json
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def build_config_blob_bytes(payload: dict[str, Any]) -> bytes:
    """Serialize an arbitrary Config_Blob payload to bytes.

    Failure-path tests need to omit required fields or insert mismatched
    values, which ``cairn_oci.ConfigBlob`` rejects at construction. This
    helper trusts the caller to assemble the dict however they need and
    just JSON-encodes it.
    """
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def ecr_login(ecr_client: Any, registry_host: str) -> None:
    response = ecr_client.get_authorization_token()
    token = response["authorizationData"][0]["authorizationToken"]
    decoded = base64.b64decode(token).decode("ascii")
    username, _, password = decoded.partition(":")
    subprocess.run(
        [
            "oras",
            "login",
            registry_host,
            "--username",
            username,
            "--password-stdin",
        ],
        input=password,
        text=True,
        check=True,
        capture_output=True,
    )


def oras_push_raw(
    *,
    reference: str,
    config_path: Path,
    config_media_type: str,
    layers: Iterable[tuple[Path, str]],
    annotations: dict[str, str],
    manifest_export_path: Path,
) -> str:
    """Invoke ``oras push`` and return the exported manifest digest.

    ``layers`` is an iterable of ``(path, media_type)`` tuples. Each layer
    is passed as ``<path>:<media-type>`` to oras. Annotations are emitted
    as ``--annotation key=value`` flags on the manifest.
    """
    cmd = [
        "oras",
        "push",
        reference,
        "--config",
        f"{config_path}:{config_media_type}",
        "--export-manifest",
        str(manifest_export_path),
        # Failure-path tests pass absolute paths to layer files. The
        # `cairn push` CLI overrides layer titles via annotation-file so the
        # absolute path never lands in the manifest; here the manifest's
        # `org.opencontainers.image.title` comes from the file's basename
        # via oras's default behavior, which is the same effective outcome.
        "--disable-path-validation",
    ]
    for key, value in annotations.items():
        cmd.extend(["--annotation", f"{key}={value}"])
    for path, media_type in layers:
        cmd.append(f"{path}:{media_type}")

    subprocess.run(cmd, check=True, capture_output=True, text=True)
    manifest_bytes = manifest_export_path.read_bytes()
    import hashlib

    return "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()


def oras_attach_raw(
    *,
    subject_reference: str,
    artifact_type: str,
    file_path: Path,
    file_media_type: str,
    manifest_export_path: Path,
) -> str:
    """Invoke ``oras attach`` and return the exported referrer manifest digest.

    Mirrors ``cli/cairn_cli/_oras.attach`` (and what ``cairn push`` does after
    the content push): pushes an OCI **referrer** whose ``subject`` is
    ``subject_reference`` (a digest-pinned ``registry/repo@sha256:...``) and
    whose ``artifactType`` is ``artifact_type``. The Producer_Attestation_Referrer push
    is validate-bundle's single positive trigger, so a
    hand-crafted bundle is only evaluated once its referrer lands — which is why
    the (referrer-less) ``oras_push_raw``-only path never reaches validation.
    """
    cmd = [
        "oras",
        "attach",
        "--artifact-type",
        artifact_type,
        "--export-manifest",
        str(manifest_export_path),
        "--disable-path-validation",
        subject_reference,
        f"{file_path}:{file_media_type}",
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    manifest_bytes = manifest_export_path.read_bytes()
    import hashlib

    return "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()

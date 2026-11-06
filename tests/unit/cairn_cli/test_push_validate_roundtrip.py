"""CLI -> validator round-trip regression guard.

The ``cairn`` CLI's default ``push`` output (controls-less, no ``catalogOrProfileUuid``)
must round-trip through ``validate_bundle`` with ZERO errors. The push tests assert
push *shape* (`tests/unit/cairn_cli/test_push.py`) and the validator tests use
hand-built fixtures (`tests/unit/cairn_validation/test_bundle.py`), so neither crosses
the CLI<->validator seam.

This test closes that seam. It drives the CLI's DEFAULT artifact construction for the
minimal default bundle — a project-id plus one OSCAL layer, NO controls, NO
``catalogOrProfileUuid`` — captures the produced manifest + config-blob bytes, and
asserts they round-trip through ``validate_bundle`` with ZERO errors.

This is a regression guard against CLI/validator drift.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest
from cairn_cli import push as push_module
from cairn_oci import (
    CairnAnnotations,
    Descriptor,
    OciManifest,
    compute_sha256_digest,
)
from cairn_validation import validate_bundle


class _CapturingOras:
    """Fake oras that captures the inputs ``push.py`` hands to ``oras push``.

    ``push.py`` builds the ``OciManifest`` in-memory then passes its constituent
    parts to ``oras_module.push`` (config blob bytes via ``config_file``, the
    ``(path, media_type, title)`` layer triples via ``files``, and the manifest
    ``annotations`` dict). We capture exactly those so the test can reconstruct
    the produced manifest the SAME way push.py builds it (see ``_build_manifest``),
    rather than re-deriving CLI behaviour by hand.
    """

    class OrasCommandError(RuntimeError):
        def __init__(self, *, returncode: int, stderr: str, command: list[str]) -> None:
            super().__init__(f"oras exit {returncode}: {stderr}")
            self.returncode = returncode
            self.stderr = stderr
            self.command = command

    def __init__(self) -> None:
        self.push_calls: list[dict[str, Any]] = []

    def login(self, *, registry: str, username: str, password: str) -> None:
        pass

    def push(
        self,
        *,
        reference: str,
        config_file: Path,
        config_media_type: str,
        files: list[tuple[Path, str, str]],
        annotations: dict[str, str],
        export_manifest_to: Path,
    ):
        export_manifest_to.write_text(json.dumps({"layers": []}))
        self.push_calls.append(
            {
                "config_bytes": config_file.read_bytes(),
                "config_media_type": config_media_type,
                "files": files,
                "annotations": annotations,
            }
        )

        class _R:
            manifest_digest = "sha256:" + "a" * 64

        return _R()

    def attach(
        self,
        *,
        subject_reference: str,
        artifact_type: str,
        file: Path,
        file_media_type: str,
        annotations: dict[str, str],
        export_manifest_to: Path,
    ):
        export_manifest_to.write_text(json.dumps({"layers": []}))

        class _R:
            manifest_digest = "sha256:" + "b" * 64

        return _R()


def _build_manifest(call: dict[str, Any]) -> OciManifest:
    """Reconstruct the OciManifest exactly as ``push.py`` assembled it.

    Mirrors ``push.py``: the config Descriptor is the Config_Blob media type +
    digest/size of the serialized bytes, each layer Descriptor is its file's
    media type + digest/size with the ``org.opencontainers.image.title`` set to
    the layer title, and the manifest annotations are passed through verbatim.
    """
    config_bytes = call["config_bytes"]
    config_descriptor = Descriptor(
        mediaType=call["config_media_type"],
        digest=compute_sha256_digest(config_bytes),
        size=len(config_bytes),
    )
    layer_descriptors = [
        Descriptor(
            mediaType=media_type,
            digest=compute_sha256_digest(path.read_bytes()),
            size=len(path.read_bytes()),
            annotations={"org.opencontainers.image.title": title},
        )
        for path, media_type, title in call["files"]
    ]
    return OciManifest(
        config=config_descriptor,
        layers=layer_descriptors,
        annotations=call["annotations"],
    )


@pytest.fixture
def minimal_oscal_file(tmp_path: Path) -> Path:
    """A minimal OSCAL catalog JSON layer.

    The root key ``catalog`` makes ``sniff_media_type`` resolve the layer to
    ``application/oscal+json`` — the real CLI path for an OSCAL bundle. The
    validator only inspects the layer's media type here, so a minimal-but-real
    OSCAL root is sufficient; no full document is needed.
    """
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps({"catalog": {"uuid": "11111111-1111-4111-8111-111111111111"}}))
    return p


def _default_push_args(files: list[Path]) -> argparse.Namespace:
    """The argparse Namespace for ``cairn push --project-id <id> <oscal-file>`` with
    all optional control linkage / period / artifact-kind left at their defaults.

    This is the controls-less, no-``catalogOrProfileUuid`` shape.
    """
    return argparse.Namespace(
        org_id="mvp-org",
        tenant_id="mvp-tenant",
        project_id="mvp-project",
        target=None,
        assessment_period=None,
        files=files,
        artifact_kind=push_module._DEFAULT_ARTIFACT_KIND,  # "other"
        catalog_or_profile_uuid=None,
        control=None,
        media_type=[],
        source_tool=None,
        assessor=None,
        assessment_id=None,
        supersedes=None,
        stage="alpha",
        region_code="cmh",
        app_name=None,
    )


def test_default_push_artifacts_roundtrip_through_validator_with_zero_errors(
    aws_stack, minimal_oscal_file: Path
) -> None:
    """The CLI's default controls-less push output validates with ZERO errors.

    Drives ``push._run`` for the minimal bundle (project-id + one OSCAL layer, no
    controls, no catalogOrProfileUuid), captures the produced manifest + config-blob
    bytes via the fake oras, and asserts ``validate_bundle(...).errors == []``.
    """
    fake_oras = _CapturingOras()
    args = _default_push_args([minimal_oscal_file])

    rc = push_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        oras_module=fake_oras,
    )

    # The CLI pushed (its own client-side validate gate already passed).
    assert rc == 0
    assert len(fake_oras.push_calls) == 1
    call = fake_oras.push_calls[0]

    # Sanity: this is the controls-less / no-catalog shape. The producer's claim is
    # not in the content manifest — neither as annotations nor as Config_Blob fields.
    annotations = call["annotations"]
    assert CairnAnnotations.CONTROLS not in annotations
    config_blob_json = json.loads(call["config_bytes"])
    assert "controls" not in config_blob_json
    assert "catalogOrProfileUuid" not in config_blob_json
    # artifact-kind is not in the content manifest — no `artifact-kind` manifest
    # annotation and no Config_Blob `bundleType`. Kind is carried ONLY by the
    # Producer_Attestation_Referrer `artifactKind` claim, so it never perturbs the content
    # Manifest_Digest.
    assert "dev.cairnproofs.artifact.artifact-kind" not in annotations
    assert "bundleType" not in config_blob_json
    assert call["files"][0][1] == "application/oscal+json"

    manifest = _build_manifest(call)
    config_blob_bytes: bytes = call["config_bytes"]

    # The durable guard: the CLI's default output round-trips through the
    # committed validator with no errors.
    result = validate_bundle(OciManifest.model_validate(manifest), config_blob_bytes)
    assert result.errors == [], (
        "cairn CLI default push output drifted from validate_bundle: "
        + "; ".join(f"[{e.criterion}] {e.field}: {e.message}" for e in result.errors)
    )
    assert result.ok

"""Tests for the `cli/cairn_cli/_oras.py` subprocess wrapper.

These tests drive the REAL ``_oras.push`` (not the ``_FakeOras`` injected at
the ``push`` command layer), with the ``oras`` subprocess itself mocked at the
``_run`` boundary. The load-bearing seam under test is the ``--annotation-file``
payload: whatever the caller puts in ``annotations`` MUST land verbatim in the
``"$manifest"`` block of the annotation file handed to ``oras``. That block is
how the producer's pinned ``org.opencontainers.image.created`` overrides oras's
auto-injected wall-clock value — if a caller's
manifest annotation silently failed to reach ``$manifest``, the pin would be a
no-op and content identity would again be non-reproducible.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cairn_cli import _oras
from cairn_oci import PINNED_CREATED


@pytest.fixture
def captured_oras_run(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Stub out the oras binary check and the subprocess invocation.

    ``push`` re-reads the exported manifest to compute the digest, so the stub
    also writes a placeholder manifest file to ``--export-manifest`` so the read
    does not blow up. Returns the list of captured argv lists.
    """
    commands: list[list[str]] = []

    monkeypatch.setattr(_oras, "_ensure_oras_installed", lambda: None)

    def _fake_run(command: list[str], **_kwargs: Any) -> None:
        commands.append(command)
        # Mirror oras's --export-manifest side effect so push() can read it back.
        if "--export-manifest" in command:
            export_path = Path(command[command.index("--export-manifest") + 1])
            export_path.write_bytes(b'{"schemaVersion":2}')

    monkeypatch.setattr(_oras, "_run", _fake_run)
    return commands


def _annotation_file_payload(commands: list[list[str]]) -> dict[str, Any]:
    """Read back the JSON annotation file that push() wrote for oras."""
    command = commands[0]
    annotations_path = Path(command[command.index("--annotation-file") + 1])
    return json.loads(annotations_path.read_text(encoding="utf-8"))


def _do_push(tmp_path: Path, annotations: dict[str, str]) -> Path:
    config_file = tmp_path / "config.json"
    config_file.write_bytes(b"{}")
    layer = tmp_path / "catalog.json"
    layer.write_bytes(b'{"x":1}')
    export_to = tmp_path / "manifest.json"
    _oras.push(
        reference="registry.example/repo",
        config_file=config_file,
        config_media_type="application/vnd.cairn.config+json",
        files=[(layer, "application/oscal.catalog+json", "catalog.json")],
        annotations=annotations,
        export_manifest_to=export_to,
    )
    return export_to


def test_manifest_block_carries_caller_pinned_created(
    tmp_path: Path, captured_oras_run: list[list[str]]
) -> None:
    """The caller's pinned ``created`` reaches oras's ``$manifest`` block verbatim.

    This is the override path that neutralizes oras's auto-injected wall-clock
    ``created`` — the actual mechanism of the dedup fix.
    """
    _do_push(
        tmp_path,
        {
            "dev.cairnproofs.artifact.project-id": "alpha-bank",
            "org.opencontainers.image.created": PINNED_CREATED,
        },
    )

    payload = _annotation_file_payload(captured_oras_run)
    assert payload["$manifest"]["org.opencontainers.image.created"] == PINNED_CREATED


def test_manifest_block_is_exactly_the_caller_annotations(
    tmp_path: Path, captured_oras_run: list[list[str]]
) -> None:
    """``$manifest`` is exactly the caller's annotation dict — the wrapper adds
    nothing of its own (no ambient state, no extra keys) to the manifest level."""
    annotations = {
        "dev.cairnproofs.artifact.project-id": "alpha-bank",
        "org.opencontainers.image.created": PINNED_CREATED,
    }
    _do_push(tmp_path, annotations)

    payload = _annotation_file_payload(captured_oras_run)
    assert payload["$manifest"] == annotations
    # artifact-kind is claim-only — the producer does not put it on the content
    # manifest, so it must not appear in the passed-through $manifest block.
    assert "dev.cairnproofs.artifact.artifact-kind" not in payload["$manifest"]


def test_annotation_file_flag_is_passed_to_oras(
    tmp_path: Path, captured_oras_run: list[list[str]]
) -> None:
    """Guard that the override mechanism is actually wired into the argv — if
    ``--annotation-file`` were dropped, oras would fall back to auto-injecting
    ``created`` and the pin would be silently ineffective."""
    _do_push(
        tmp_path,
        {
            "dev.cairnproofs.artifact.project-id": "alpha-bank",
            "org.opencontainers.image.created": PINNED_CREATED,
        },
    )

    command = captured_oras_run[0]
    assert "--annotation-file" in command


def _do_attach(tmp_path: Path, annotations: dict[str, str]) -> Path:
    # The claim is written to an ephemeral temp dir whose absolute path must NOT
    # leak into the referrer manifest — exactly the attach-side leak this guards.
    tmp = tmp_path / "cairn-push-xxxx"
    tmp.mkdir()
    claim = tmp / "attestation.json"
    claim.write_bytes(b'{"_type":"https://in-toto.io/Statement/v1"}')
    export_to = tmp_path / "attestation-manifest.json"
    _oras.attach(
        subject_reference="registry.example/repo@sha256:" + "a" * 64,
        artifact_type="application/vnd.cairnproofs.producer-attestation.v1+json",
        file=claim,
        file_media_type="application/vnd.in-toto+json",
        annotations=annotations,
        export_manifest_to=export_to,
    )
    return claim


def test_attach_overrides_layer_title_to_basename_not_temp_path(
    tmp_path: Path, captured_oras_run: list[list[str]]
) -> None:
    """The claim layer title is the file BASENAME, never the absolute ephemeral
    temp path — so the local filesystem layout / random temp-dir name never lands
    in the published, signed, WORM-archived referrer manifest."""
    claim = _do_attach(tmp_path, {"org.opencontainers.image.created": PINNED_CREATED})

    payload = _annotation_file_payload(captured_oras_run)
    title = payload[str(claim)]["org.opencontainers.image.title"]
    assert title == "attestation.json"
    assert "/" not in title  # not an absolute path


def test_attach_forwards_manifest_annotations_verbatim(
    tmp_path: Path, captured_oras_run: list[list[str]]
) -> None:
    """``attach`` forwards whatever manifest annotations the caller passes into
    oras's ``$manifest`` block (the same shared mechanism ``push`` uses to pin the
    *content* manifest's ``created``). Note ``cairn push`` itself passes ``{}`` for
    the referrer — it deliberately does NOT pin the referrer's ``created`` (a
    deterministic referrer collides across re-pushes) — so this exercises the
    pass-through with an explicit value rather than the production call."""
    _do_attach(tmp_path, {"org.opencontainers.image.created": PINNED_CREATED})

    payload = _annotation_file_payload(captured_oras_run)
    assert payload["$manifest"]["org.opencontainers.image.created"] == PINNED_CREATED
    assert "--annotation-file" in captured_oras_run[0]

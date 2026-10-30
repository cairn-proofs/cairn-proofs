"""TUI mutable-alias labeling.

The read-only TUI's per-bundle detail panel must render an alias-tagged handle
(`_als`/`_chn`) as a MUTABLE LABEL, visually distinct from a canonical `_ref`
immutable identity, and must keep the digest as the presented identity. The
producer-influenceable tag still passes through the render-safety choke point.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from cairn_cli._aws import DeploymentTarget
from cairn_cli.tui._render_safety import render_safe
from cairn_cli.tui._sources import alias_tag_of
from cairn_cli.tui.app import ArtifactStoreApp
from cairn_oci import ArtifactLocator

_TARGET = DeploymentTarget(stage="alpha", region_code="cmh", app_name="cairnproofs4af")
_LOC = ArtifactLocator()
_NAMED_ALIAS = _LOC.build_named_alias(artifact_kind="catalog", name="nist-800-53-latest")
_CHANNEL = _LOC.build_channel_pointer(artifact_kind="catalog", channel="latest")
_REF_TAG = _LOC.build_ref_tag(artifact_kind="catalog", digest="a" * 12)


def _app() -> ArtifactStoreApp:
    return ArtifactStoreApp(MagicMock(), deployment_target=_TARGET)


# --- classifier ------------------------------------------------------------


def test_alias_tag_of_named_and_channel():
    assert alias_tag_of(_NAMED_ALIAS) == _NAMED_ALIAS
    assert alias_tag_of(_CHANNEL) == _CHANNEL


def test_alias_tag_of_rejects_ref_and_nonstring():
    assert alias_tag_of(_REF_TAG) is None
    assert alias_tag_of(None) is None
    assert alias_tag_of("") is None
    assert alias_tag_of("not-a-cairn-tag") is None


# --- detail rendering ------------------------------------------------------


def _tag_line(record: dict) -> str:
    lines = _app().render_detail_lines(record)
    return next(line for line in lines if line.startswith("ECR tag:"))


def test_alias_tag_rendered_as_mutable_label_distinct_from_ref():
    digest = "sha256:" + "a" * 64
    record = {
        "projectId": "acme",
        "artifactKind": "catalog",
        "manifestDigest": digest,
        "imageTag": _NAMED_ALIAS,
    }
    lines = _app().render_detail_lines(record)
    joined = "\n".join(lines)
    tag_line = next(line for line in lines if line.startswith("ECR tag:"))
    # Visually distinct: the alias carries an explicit MUTABLE ALIAS marker.
    assert "MUTABLE ALIAS" in tag_line
    assert _NAMED_ALIAS in tag_line
    # The digest stays the presented immutable identity.
    digest_line = next(line for line in lines if line.startswith("ECR digest:"))
    assert digest in digest_line
    assert "immutable identity" in digest_line
    assert digest in joined


def test_channel_pointer_rendered_as_mutable_label():
    digest = "sha256:" + "b" * 64
    record = {
        "projectId": "acme",
        "artifactKind": "catalog",
        "manifestDigest": digest,
        "imageTag": _CHANNEL,
    }
    tag_line = _tag_line(record)
    assert "MUTABLE ALIAS" in tag_line
    assert _CHANNEL in tag_line


def test_canonical_ref_tag_not_marked_mutable():
    digest = "sha256:" + "c" * 64
    record = {
        "projectId": "acme",
        "artifactKind": "catalog",
        "manifestDigest": digest,
        "imageTag": _REF_TAG,
    }
    tag_line = _tag_line(record)
    assert "MUTABLE ALIAS" not in tag_line
    assert _REF_TAG in tag_line


def test_alias_tag_line_passes_through_render_safety():
    """A hostile alias-shaped tag carrying control bytes is sanitized.

    The alias tag is producer-influenceable; even though its grammar forbids
    control bytes, the detail line MUST still be render-safe (the single TUI
    choke point), so the rendered line equals its own render_safe form.
    """
    digest = "sha256:" + "d" * 64
    # A control byte injected into the tag value: classification fails closed
    # (so it is NOT marked an alias), and the value is still sanitized on render.
    hostile = f"{_NAMED_ALIAS}\x1b]0;pwn\x07"
    record = {
        "projectId": "acme",
        "artifactKind": "catalog",
        "manifestDigest": digest,
        "imageTag": hostile,
    }
    tag_line = _tag_line(record)
    assert tag_line == render_safe(tag_line)
    assert "\x1b" not in tag_line
    assert "\x07" not in tag_line

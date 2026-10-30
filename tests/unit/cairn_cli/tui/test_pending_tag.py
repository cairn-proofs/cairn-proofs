"""TUI tolerance for the pushed-but-not-yet-tagged window (task 6.1/6.2, T-08).

The read-only TUI's per-bundle detail panel resolves a bundle by its digest
(always present) and labels the absent canonical tag as "tag pending" rather
than an empty placeholder, so an operator viewing an artifact during the
eventual-consistency window reads a transient state, not a fault.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from cairn_cli._aws import DeploymentTarget
from cairn_cli._pending import TAG_PENDING_LABEL
from cairn_cli.tui.app import ArtifactStoreApp

_TARGET = DeploymentTarget(stage="alpha", region_code="cmh", app_name="cairnproofs4af")


def _app() -> ArtifactStoreApp:
    return ArtifactStoreApp(MagicMock(), deployment_target=_TARGET)


def test_detail_lines_show_tag_pending_for_untagged_digest():
    """Digest present, no tag yet → ECR tag line reads 'tag pending'."""
    digest = "sha256:" + "a" * 64
    record = {"projectId": "acme", "artifactKind": "assessment-results", "manifestDigest": digest}
    lines = _app().render_detail_lines(record)
    joined = "\n".join(lines)
    # Digest is the resolvable anchor and stays visible.
    assert digest in joined
    # The absent tag reads as the pending state, not an empty placeholder.
    tag_line = next(line for line in lines if line.startswith("ECR tag:"))
    assert TAG_PENDING_LABEL in tag_line
    assert "—" not in tag_line


def test_detail_lines_show_real_tag_when_present():
    """A minted tag is shown verbatim — pending label only for the gap."""
    digest = "sha256:" + "a" * 64
    record = {
        "projectId": "acme",
        "artifactKind": "assessment-results",
        "manifestDigest": digest,
        "imageTag": "assessment-results--_ref--0123456789ab",
    }
    lines = _app().render_detail_lines(record)
    tag_line = next(line for line in lines if line.startswith("ECR tag:"))
    assert "assessment-results--_ref--0123456789ab" in tag_line
    assert TAG_PENDING_LABEL not in tag_line

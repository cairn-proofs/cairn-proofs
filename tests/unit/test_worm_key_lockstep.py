"""Lockstep guard: the validate-predicted and archive-produced S3 key prefixes
MUST stay character-for-character identical.

The OSCAL evidence rlink that validate-bundle stamps onto the manifest predicts
where archive-to-s3 will write the bundle. If the two builders ever diverge — a
segment added on one side, a separator changed — the rlink would point at a key
that does not exist. This test pins both builders to the same uniform three-segment
shape ``{org-id}/{tenant-id}/{project-id}/{manifest-digest}/`` ([WORM-002])
across a representative set of inputs so the divergence is caught at
test time rather than as a dangling reference in production (worm-archive
"Predicted S3 URI matches the archived key shape" [WORM-002.04]).

Both builders are 4-arg ``(org_id, tenant_id, project_id, manifest_digest)``
constructors emitting the single uniform path shape — nothing branches per
segment and no segment is optional, omitted, defaulted, or fabricated.
"""

from __future__ import annotations

import pytest
from cairn_lambda_archive_to_s3.handler import _s3_key_prefix as archive_prefix
from cairn_lambda_validate_bundle.handler import (
    _predicted_s3_key_prefix as validate_prefix,
)

_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "1" * 64


_BARE_DIGEST = "f" * 64  # a digest passed without the `sha256:` algorithm prefix


@pytest.mark.parametrize(
    ("org_id", "tenant_id", "project_id", "manifest_digest"),
    [
        ("org-acme", "tnt-foo", "alpha-bank", _DIGEST_A),
        ("org-1", "tnt-2", "other-tenant", _DIGEST_B),
        ("o", "t", "p", _DIGEST_A),
        ("org-with-dashes-1", "tnt-with-dashes-2", "project-with-dashes-123", _DIGEST_B),
        ("OrgMixedCase", "TntMixedCase", "AlphaBankMixedCase", _DIGEST_A),
        # Dotted segments: a '.' in any segment must not perturb either builder
        # (the key is a flat string join, not a path-normalized one).
        ("org.with.dots", "tnt.with.dots", "app.with.dots", _DIGEST_A),
        # Bare digest (no `sha256:` prefix): both builders interpolate the digest
        # verbatim, so they stay in lockstep regardless of algorithm prefix.
        ("org-acme", "tnt-foo", "alpha-bank", _BARE_DIGEST),
    ],
)
def test_validate_and_archive_prefixes_are_identical(
    org_id: str, tenant_id: str, project_id: str, manifest_digest: str
) -> None:
    """The archive prefix and the validate predicted prefix are byte-for-byte
    equal for the same (org, tenant, project, digest) coordinates."""
    archived = archive_prefix(org_id, tenant_id, project_id, manifest_digest)
    predicted = validate_prefix(org_id, tenant_id, project_id, manifest_digest)

    assert predicted == archived
    # And both are the expected uniform three-segment, digest-terminal shape —
    # no fabricated segment.
    assert predicted == f"{org_id}/{tenant_id}/{project_id}/{manifest_digest}/"


def test_lockstep_prefix_has_no_period_or_bundle_type_segment() -> None:
    """Neither builder fabricates an assessment-period or bundle-type segment;
    the path is exactly the three identity segments plus the digest as the sole
    identity anchor between project and the terminal slash."""
    org_id = "org-acme"
    tenant_id = "tnt-foo"
    project_id = "alpha-bank"
    digest = _DIGEST_A

    predicted = validate_prefix(org_id, tenant_id, project_id, digest)
    archived = archive_prefix(org_id, tenant_id, project_id, digest)

    # Exactly five components: org, tenant, project, digest, trailing empty
    # (terminal slash) — never a period or bundle-type segment.
    assert predicted.split("/") == [org_id, tenant_id, project_id, digest, ""]
    assert archived.split("/") == [org_id, tenant_id, project_id, digest, ""]

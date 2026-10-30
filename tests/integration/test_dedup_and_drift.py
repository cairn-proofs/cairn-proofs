"""Content-addressed dedup + behavioral-drift integration tests (tasks 2.1-2.5).

These exercise, end-to-end against a real cmh-alpha deployment, the
content-addressed idempotency guarantee (`artifact-ingestion`
*Content-Addressable Deduplication* / *Bundle Identity Is Pure Content
Addressing*) and the stability of the pipeline's observable outputs:

- **Identical content pushed twice yields exactly one bundle** — push the
  stable fixture, wait for the pipeline, then push byte-identical content
  again into the same shared repo; ECR holds exactly one manifest with the
  same content `Manifest_Digest`.
- **Idempotent index rows under re-push** — the Metadata_Index and
  ControlsIndex carry no duplicate/conflicting rows keyed on the shared
  digest after the double push.
- **Trivially-distinct content is not over-collapsed** — content whose real
  bytes differ (an extra marker layer) yields a NEW distinct
  content `Manifest_Digest` and a distinct bundle.
- **Observable outputs are pinned against drift** — the stable fixture yields
  the *exact* pinned content `Manifest_Digest` (the drift tripwire), while
  project-scoped outputs (archive S3 key, projection fields) are pinned by
  SHAPE only (regex/structural).

The content digest is a pure
function of content bytes plus the content-descriptive Config_Blob (projectId
ONLY — `bundleType`/`artifact-kind` are not in the content manifest, so kind is no
longer hashed into the digest) — no wall-clock, randomness, or producer-CLAIM
fields (artifactKind, controls, assessor, …, which live in the
Producer_Attestation_Referrer) — so the
exact-digest pin is possible and "one content, many attestations" is a
legitimate feature — the dedup assertions therefore target the CONTENT
manifest and the index rows keyed on it, NOT `Producer_Attestation_Referrer`
cardinality.

The whole suite reuses the stack's pre-provisioned per-project repo:
the project comes from the environment (`CAIRN_INTEGRATION_PROJECT_ID`,
default `mvp-tenant`) and the repo is resolved + verified describe-not-create
by the ``shared_repo_name`` fixture. No test here creates or deletes an ECR
repository; cleanup is targeted (manifests + rows + S3 keys only), via
``bundle_cleanup``. The suite is gated by ``CAIRN_INTEGRATION_ENV``
(inherited from ``tests/integration/conftest.py``) and lives in the separate
``mise run integration`` gate, never ``mise run check``.

Parallel execution
------------------
These tests are latency-bound (minutes of pipeline-completion polling), so the
suite runs under ``pytest -n auto`` to collapse wall-clock from the sum of the
waits to roughly the slowest single test. The production pipeline is idempotent
under concurrent content-addressed pushes, so concurrent *pushes* never race;
the only test-level hazard is teardown — one test deleting a digest another is
asserting. Content distinctness: every test pushes a UNIQUE,
fixed-bytes content set (a per-test ``_marker`` layer), so each owns a distinct
content digest and its targeted cleanup can never touch another test's bundle.
A test that re-pushes "identical content" re-pushes *its own* marked content
(intra-test, still byte-identical → same digest). The drift test keeps
the bare fixture, so it uniquely owns that digest and the pin stays "the stable
fixture's digest".

Capturing the drift pin
-----------------------
The exact-digest pin reads its expected value from the sibling file
``fixtures/dedup_drift_fixture.expected-digest.txt``. While that value is the
``SENTINEL`` placeholder the drift test SKIPS with a message telling the
operator to capture the real digest from the first ``mise run integration``
run and commit it (tied to the fixed-bytes fixture). This keeps the pin honest
(no false pass, no collection break) until a real deployment yields the value.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from tests.integration._waiters import wait_for_pipeline_complete
from tests.integration.conftest import BundleCleanup, CairnCli, StackOutputs

pytestmark = pytest.mark.integration

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_FIXTURE_PATH = _FIXTURES_DIR / "dedup_drift_fixture.json"
_EXPECTED_DIGEST_PATH = _FIXTURES_DIR / "dedup_drift_fixture.expected-digest.txt"

# The fixture declares one OSCAL control selection (`ac-1`); these match what
# the bundle claims so the ControlsIndex row lands deterministically.
_CATALOG_UUID = "d3d1f00d-0000-4000-8000-000000000003"
_CONTROL_ID = "ac-1"
_ASSESSMENT_PERIOD = "2025-q1"
_BUNDLE_TYPE = "assessment-results"

_PIPELINE_TIMEOUT_SECONDS = 360.0

# The pinned-digest file uses this token until an operator captures the real
# value from a live deployment (see the module docstring).
_DIGEST_SENTINEL = "SENTINEL"

# Archive S3 key shape: `{project-id}/{manifest-digest}/...`
# ([WORM-002] digest-terminal prefix). Pinned by SHAPE — the project segment and
# trailing object name vary per run, the digest segment is the content digest.
_ARCHIVE_KEY_SHAPE = re.compile(r"^[^/]+/[^/]+/[^/]+/sha256:[0-9a-f]{64}/.+")


_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _read_pinned_digest() -> str | None:
    """Return the committed exact content-digest pin, or ``None`` if unset.

    The file's first non-comment, non-blank line is the pinned digest; the
    ``SENTINEL`` placeholder (or a missing file) means "not yet captured". A
    present-but-malformed pin raises ``ValueError`` naming the file, so a bad
    committed value fails as "malformed pin" rather than a confusing "DRIFT".
    """
    if not _EXPECTED_DIGEST_PATH.exists():
        return None
    for raw in _EXPECTED_DIGEST_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == _DIGEST_SENTINEL:
            return None
        if not _DIGEST_RE.match(line):
            raise ValueError(
                f"malformed content-digest pin {line!r} in {_EXPECTED_DIGEST_PATH}: "
                "expected the SENTINEL or a `sha256:<64 lowercase hex>` value"
            )
        return line
    return None


def _push_fixture(
    cairn_cli: CairnCli,
    org_id: str,
    tenant_id: str,
    project_id: str,
    *extra_layers: Path,
) -> str:
    """Push ``dedup_drift_fixture.json`` (plus any ``extra_layers``) and return
    the emitted content ``Manifest_Digest``.

    With NO extra layers the push is byte-identical content every time (the
    fixture is fixed bytes and the identity triple (org/tenant/project) is held
    constant — projectId IS in the content digest, so the pin is project-scoped;
    ``bundleType`` is now a claim field on the Producer_Attestation_Referrer and
    is NOT in the content digest), so the digest is reproducible — that is what
    makes the dedup and exact-pin assertions meaningful. An
    ``extra_layers`` marker file changes the real content bytes and therefore the
    digest.

    The three identity coordinates are passed EXPLICITLY (never left to ambient
    ``CAIRN_ORG_ID``/``CAIRN_TENANT_ID`` env resolution) so the push targets the
    deployed ``{org-id}/{tenant-id}/{project-id}`` repo deterministically
    ([INGEST-032]).
    """
    args = [
        "push",
        "--org-id",
        org_id,
        "--tenant-id",
        tenant_id,
        "--project-id",
        project_id,
        "--assessment-period",
        _ASSESSMENT_PERIOD,
        str(_FIXTURE_PATH),
        *[str(layer) for layer in extra_layers],
        "--artifact-kind",
        _BUNDLE_TYPE,
        "--catalog-or-profile-uuid",
        _CATALOG_UUID,
        "--control",
        _CONTROL_ID,
        "--source-tool",
        "cairn-integration-suite",
        "--stage",
        "alpha",
        "--region-code",
        "cmh",
    ]
    result = cairn_cli(*args, check=True)
    manifest_digest = result.stdout.strip().splitlines()[-1].strip()
    assert manifest_digest.startswith("sha256:"), (
        f"cairn push did not emit a manifest digest; stdout was:\n{result.stdout}"
    )
    return manifest_digest


def _marker(tmp_path: Path, filename: str, tag: str) -> Path:
    """A fixed-bytes marker layer that makes a test's content unique across the
    parallel suite.

    The bytes are constant per ``tag`` (no randomness), so the test's content
    digest stays deterministic and reproducible across runs — a dedup re-push
    within a test still pushes byte-identical content. Distinct ``tag`` values
    across tests give distinct content digests, so each test owns its own bundle
    and its targeted teardown can never race another test's cleanup.
    """
    marker = tmp_path / filename
    marker.write_text(f"cairn-dedup-suite-marker:{tag}\n")
    return marker


def test_identical_content_pushed_twice_yields_one_bundle(
    aws_session: Any,
    stack_outputs: StackOutputs,
    cairn_cli: CairnCli,
    bundle_cleanup: BundleCleanup,
    shared_repo_name: str,
    target_org_id: str,
    target_tenant_id: str,
    target_project_id: str,
    tmp_path: Path,
) -> None:
    """Identical content pushed twice mints no new content manifest.

    Push this test's content (the fixture + a per-test marker, fixed bytes),
    wait for the pipeline, record content digest D; then push byte-identical
    content again into the same repo. The re-push reproduces D
    (``digest_again == digest``) — under content addressing that equality IS the
    no-duplicate guarantee: identical bytes hash to one content-addressed
    manifest, so a true duplicate could only ever surface as a *different*
    digest. ECR is confirmed to resolve a single content manifest at D
    (toolchain Scenario: *Identical content pushed twice yields exactly one
    bundle*). The marker only makes this test's content unique within the
    parallel suite; both pushes use it, so they stay byte-identical.
    """
    project_id = target_project_id
    repo_path = shared_repo_name
    ddb = aws_session.client("dynamodb")
    ecr = aws_session.client("ecr")
    marker = _marker(tmp_path, "marker.txt", "identical-twice")

    # First push — this test's (marked) content.
    digest = _push_fixture(cairn_cli, target_org_id, target_tenant_id, project_id, marker)
    bundle_cleanup.track_ecr_manifest(repo_path, digest)
    bundle_cleanup.track_metadata_row(target_tenant_id, project_id, digest)

    item = wait_for_pipeline_complete(
        ddb,
        stack_outputs.metadata_table,
        target_tenant_id,
        project_id,
        digest,
        timeout_seconds=_PIPELINE_TIMEOUT_SECONDS,
    )
    assert item.get("signingStatus", {}).get("S") == "signed"
    assert item.get("archiveStatus", {}).get("S") == "archived"

    bundle_cleanup.track_archived_objects(target_org_id, target_tenant_id, project_id, digest)

    # Second push — byte-identical content (same marker) into the SAME repo.
    digest_again = _push_fixture(cairn_cli, target_org_id, target_tenant_id, project_id, marker)

    # The no-duplicate guarantee: under content addressing, byte-identical
    # content reproduces the identical Manifest_Digest, so ECR holds exactly ONE
    # content-addressed manifest for it. A regression that minted a second
    # distinct bundle could only do so by producing a DIFFERENT digest — which
    # this equality rejects.
    assert digest_again == digest, (
        "byte-identical content produced a different content Manifest_Digest "
        f"({digest_again} != {digest}); content addressing is not reproducible"
    )

    # Confirm ECR resolves a single content manifest at D (keyed by digest, so it
    # is present-or-absent, never duplicated). Read-only — threat-T1 safe.
    resolved = ecr.batch_get_image(
        repositoryName=repo_path,
        imageIds=[{"imageDigest": digest}],
    )
    images = resolved.get("images", [])
    assert len(images) == 1, (
        f"expected ECR to resolve exactly one content manifest at {digest} in "
        f"{repo_path} after the byte-identical re-push, got {len(images)}"
    )


def test_idempotent_index_rows_under_re_push(
    aws_session: Any,
    stack_outputs: StackOutputs,
    cairn_cli: CairnCli,
    bundle_cleanup: BundleCleanup,
    shared_repo_name: str,
    target_org_id: str,
    target_tenant_id: str,
    target_project_id: str,
    tmp_path: Path,
) -> None:
    """Re-pushing identical content leaves index rows idempotent (no dupes).

    After two byte-identical pushes, the Metadata_Index holds a single row
    keyed on (projectId, D) and the ControlsIndex holds a single row per cited
    control keyed on the shared digest D — no duplicate or conflicting rows
    (toolchain Scenario: *Idempotent index rows under re-push*). The per-test
    marker only makes this test's content unique within the parallel suite;
    both pushes use it, so they stay byte-identical.
    """
    project_id = target_project_id
    repo_path = shared_repo_name
    ddb = aws_session.client("dynamodb")
    marker = _marker(tmp_path, "marker.txt", "idempotent-rows")

    digest = _push_fixture(cairn_cli, target_org_id, target_tenant_id, project_id, marker)
    bundle_cleanup.track_ecr_manifest(repo_path, digest)
    bundle_cleanup.track_metadata_row(target_tenant_id, project_id, digest)

    wait_for_pipeline_complete(
        ddb,
        stack_outputs.metadata_table,
        target_tenant_id,
        project_id,
        digest,
        timeout_seconds=_PIPELINE_TIMEOUT_SECONDS,
    )
    bundle_cleanup.track_archived_objects(target_org_id, target_tenant_id, project_id, digest)

    # Re-push identical content (same marker) and let it complete again.
    digest_again = _push_fixture(cairn_cli, target_org_id, target_tenant_id, project_id, marker)
    assert digest_again == digest
    wait_for_pipeline_complete(
        ddb,
        stack_outputs.metadata_table,
        target_tenant_id,
        project_id,
        digest,
        timeout_seconds=_PIPELINE_TIMEOUT_SECONDS,
    )

    # Metadata_Index: exactly one row keyed on (projectId, D). The composite
    # key (pk=projectId, sk=manifestDigest) is unique by construction, so a
    # `query` on that exact pair must return exactly one item — a second,
    # conflicting row would be impossible under the key schema, but querying
    # confirms re-push neither duplicated nor dropped it.
    metadata_rows = ddb.query(
        TableName=stack_outputs.metadata_table,
        KeyConditionExpression="pk = :pk AND sk = :sk",
        ExpressionAttributeValues={
            ":pk": {"S": target_tenant_id},
            ":sk": {"S": f"{project_id}#{digest}"},
        },
    ).get("Items", [])
    assert len(metadata_rows) == 1, (
        f"expected exactly one Metadata_Index row for {project_id}/{digest} "
        f"after a re-push, found {len(metadata_rows)}"
    )

    # ControlsIndex tenant-PK plane: pk=`{tenant-id}`,
    # sk=`{catalogOrProfileUuid}#{controlId}#{manifestDigest}`. Querying the
    # exact (pk, sk) keyed on the shared digest D must return exactly one row —
    # re-push does not duplicate the control linkage.
    controls_sk = f"{_CATALOG_UUID}#{_CONTROL_ID}#{digest}"
    controls_rows = ddb.query(
        TableName=stack_outputs.control_claims_table,
        KeyConditionExpression="pk = :pk AND sk = :sk",
        ExpressionAttributeValues={
            ":pk": {"S": target_tenant_id},
            ":sk": {"S": controls_sk},
        },
    ).get("Items", [])
    assert len(controls_rows) == 1, (
        f"expected exactly one ControlsIndex row for tenant {target_tenant_id} / "
        f"{controls_sk} after a re-push, found {len(controls_rows)}"
    )


def test_trivially_distinct_content_is_not_over_collapsed(
    aws_session: Any,
    stack_outputs: StackOutputs,
    cairn_cli: CairnCli,
    bundle_cleanup: BundleCleanup,
    shared_repo_name: str,
    target_org_id: str,
    target_tenant_id: str,
    target_project_id: str,
    tmp_path: Path,
) -> None:
    """Content whose true bytes differ yields a NEW distinct content digest.

    Push the stable fixture (digest D), then push the same fixture plus an
    extra marker layer whose bytes differ (distinctness derived
    from real content, not from `projectId`). The second push MUST yield a
    distinct content `Manifest_Digest` D' != D and a distinct bundle, proving
    dedup keys on real content identity rather than over-collapsing. Asserts
    against the CONTENT manifest digest, NOT `Producer_Attestation_Referrer`
    cardinality ("one content, many attestations" is legitimate)
    (toolchain Scenario: *Trivially-distinct content is not over-collapsed*).
    """
    project_id = target_project_id
    repo_path = shared_repo_name
    ddb = aws_session.client("dynamodb")

    # Baseline content: fixture + this test's base marker → digest D. The marker
    # makes the baseline unique within the parallel suite; it does
    # not collide with the bare-fixture drift test.
    base_marker = _marker(tmp_path, "oc-base.txt", "over-collapse-base")
    digest = _push_fixture(cairn_cli, target_org_id, target_tenant_id, project_id, base_marker)
    bundle_cleanup.track_ecr_manifest(repo_path, digest)
    bundle_cleanup.track_metadata_row(target_tenant_id, project_id, digest)
    wait_for_pipeline_complete(
        ddb,
        stack_outputs.metadata_table,
        target_tenant_id,
        project_id,
        digest,
        timeout_seconds=_PIPELINE_TIMEOUT_SECONDS,
    )
    bundle_cleanup.track_archived_objects(target_org_id, target_tenant_id, project_id, digest)

    # Distinct content: the SAME baseline plus an extra layer with fixed-but-
    # different bytes. D' is reproducible; distinctness from D comes purely from
    # the added layer's bytes.
    extra_marker = _marker(tmp_path, "oc-extra.txt", "over-collapse-extra")

    distinct_digest = _push_fixture(
        cairn_cli, target_org_id, target_tenant_id, project_id, base_marker, extra_marker
    )
    bundle_cleanup.track_ecr_manifest(repo_path, distinct_digest)
    bundle_cleanup.track_metadata_row(target_tenant_id, project_id, distinct_digest)

    assert distinct_digest != digest, (
        "content with a distinct extra layer collapsed onto the baseline "
        f"content digest ({distinct_digest} == {digest}); dedup over-collapsed "
        "trivially-distinct content"
    )

    distinct_item = wait_for_pipeline_complete(
        ddb,
        stack_outputs.metadata_table,
        target_tenant_id,
        project_id,
        distinct_digest,
        timeout_seconds=_PIPELINE_TIMEOUT_SECONDS,
    )
    bundle_cleanup.track_archived_objects(
        target_org_id, target_tenant_id, project_id, distinct_digest
    )

    # A genuinely distinct bundle exists: its own Metadata_Index row keyed on D'
    # reached the terminal pipeline state.
    assert distinct_item.get("signingStatus", {}).get("S") == "signed"
    assert distinct_item.get("archiveStatus", {}).get("S") == "archived"


def test_observable_outputs_pinned_against_drift(
    aws_session: Any,
    stack_outputs: StackOutputs,
    cairn_cli: CairnCli,
    bundle_cleanup: BundleCleanup,
    shared_repo_name: str,
    target_org_id: str,
    target_tenant_id: str,
    target_project_id: str,
) -> None:
    """The stable fixture's observable outputs match pinned expectations.

    The content `Manifest_Digest` is pinned EXACTLY against the committed
    sibling file (the drift tripwire — fully deterministic for the fixed-bytes
    fixture, independent of `projectId`).
    Project-scoped outputs (archive S3 key, projectId/timestamp projection
    fields) are pinned by SHAPE only (regex/structural), since they embed
    per-run identifiers.

    Until an operator captures the real expected digest and commits it tied to
    the fixture, this test SKIPS with a clear capture instruction rather than
    falsely passing or breaking collection (toolchain Scenario: *Observable
    outputs are pinned against drift*).
    """
    project_id = target_project_id
    repo_path = shared_repo_name
    ddb = aws_session.client("dynamodb")

    digest = _push_fixture(cairn_cli, target_org_id, target_tenant_id, project_id)
    bundle_cleanup.track_ecr_manifest(repo_path, digest)
    bundle_cleanup.track_metadata_row(target_tenant_id, project_id, digest)

    item = wait_for_pipeline_complete(
        ddb,
        stack_outputs.metadata_table,
        target_tenant_id,
        project_id,
        digest,
        timeout_seconds=_PIPELINE_TIMEOUT_SECONDS,
    )
    archive_keys = bundle_cleanup.track_archived_objects(
        target_org_id, target_tenant_id, project_id, digest
    )

    # --- SHAPE pins (project-scoped; never literal) ---
    assert archive_keys, (
        f"archive bucket has no objects under "
        f"{target_org_id}/{target_tenant_id}/{project_id}/{digest}/ for the "
        "stable fixture after the pipeline completed"
    )
    for key in archive_keys:
        assert _ARCHIVE_KEY_SHAPE.match(key), (
            f"archive S3 key {key!r} does not match the pinned digest-terminal "
            f"shape {_ARCHIVE_KEY_SHAPE.pattern!r} ([WORM-002])"
        )
        # The digest segment of the key is exactly this bundle's content digest.
        # Key shape is {org}/{tenant}/{project}/{manifest-digest}/… so the digest
        # is the 4th segment (index 3) ([WORM-002]).
        assert key.split("/")[3] == digest, (
            f"archive S3 key {key!r} digest segment != content digest {digest}"
        )

    # Projection-field SHAPE: the row key is the tenant-PK schema (pk={tenant-id},
    # sk={project-id}#{manifest-digest}); the timestamp projections are present
    # and ISO-8601-shaped.
    assert item.get("pk", {}).get("S") == target_tenant_id
    assert item.get("sk", {}).get("S") == f"{project_id}#{digest}"
    ecr_pushed_at = item.get("ecrPushedAt", {}).get("S", "")
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", ecr_pushed_at), (
        f"ecrPushedAt projection {ecr_pushed_at!r} is not ISO-8601 date-time shaped"
    )

    # --- EXACT pin (content digest; the drift tripwire) ---
    # On DRIFT failure here, do NOT just bump the pin. See
    # runbooks/drift-digest-pin.md: re-pin only if you intended to change bundle
    # identity; otherwise it is a regression. (projectId IS in this digest;
    # artifactKind and the rest of the producer claim are NOT — they ride the
    # Producer_Attestation_Referrer.)
    pinned = _read_pinned_digest()
    if pinned is None:
        pytest.skip(
            "exact content-digest pin not yet captured: "
            f"{_EXPECTED_DIGEST_PATH.name} still holds the SENTINEL. Capture the "
            f"observed digest {digest!r} from this run (it is deterministic for "
            "the fixed-bytes fixture), replace the SENTINEL with that "
            "`sha256:...` value, and commit it tied to dedup_drift_fixture.json "
            "(see the module docstring)."
        )
    assert digest == pinned, (
        f"DRIFT: stable fixture produced content Manifest_Digest {digest}, "
        f"pinned expectation is {pinned}. If the bundle identity change is "
        f"intended, update {_EXPECTED_DIGEST_PATH.name} in the same change with "
        "the new digest justified; otherwise this is unintended behavioral drift."
    )


# Task 2.5 — gate + no-repo-mutation guard.
#
# This module respects the suite gate purely structurally: it carries
# ``pytestmark = pytest.mark.integration`` (above) and relies on
# ``tests/integration/conftest.py``'s ``pytest_collection_modifyitems`` to skip
# every item here unless ``CAIRN_INTEGRATION_ENV`` is set, so it is excluded
# from the fast ``mise run check`` path and lives only in the separate
# ``mise run integration`` gate (toolchain Scenarios: *Suite pushes into the
# pre-provisioned repository*, *Dedup gate excluded from the fast pre-merge
# check*). It NEVER calls ``CreateRepository`` or ``DeleteRepository``: pushes
# go through ``cairn push`` into the describe-not-create ``shared_repo_name``
# repo (skips if absent — *Missing pre-provisioned repository skips, not
# creates*), and cleanup is targeted manifest/row/S3-key deletion via
# ``bundle_cleanup`` that leaves the shared repo intact (*Teardown leaves the
# shared repository intact*). The structural guarantee (this module
# references no ``create_repository``/``delete_repository`` path) is the
# guard — verifiable by grep.

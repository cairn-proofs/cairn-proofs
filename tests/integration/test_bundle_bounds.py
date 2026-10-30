"""Active integration coverage for the [INGEST-003.04] bundle-bounds enforcement.

The unit suite proves the bounds logic with mocked ECR/fetch; this exercises the
AUTHORITATIVE trust boundary end-to-end: a real out-of-bounds bundle pushed to
the deployed ECR repo, plus its Producer_Attestation_Referrer (the submission-complete
validate trigger), must be rejected by the live validate-bundle
Lambda to the validate DLQ — never silently accepted.

Scope note. The content-layer cap (1..300) is enforced server-side in two
layers: the ``OciManifest.layers`` Pydantic ``max_length`` (defense-in-depth,
which fires first at ``model_validate`` for an over-cap manifest) and the
``validate_bundle`` ``i1``/``i2`` which-constraint criteria behind it. A
301-layer bundle therefore routes to the validate DLQ as a permanent failure
via the manifest-schema gate; this test asserts that authoritative rejection
reaches the DLQ keyed on the offending manifest digest. The 0-layer (``i1``) and
oversize-descriptor (``j``) cases need a hand-crafted-manifest push (a 0-layer or
size-lying manifest cannot be expressed through ``oras push``, which builds
descriptors from real files) and are left to a follow-on once that harness
helper exists.

Like the rest of the suite this reuses the stack's pre-provisioned per-project
repo (describe-not-create) and leaves zero residue via
``bundle_cleanup``. Requires a deployed stack + ``CAIRN_INTEGRATION_ENV``; run
with ``mise run integration``.
"""

from __future__ import annotations

import json
import secrets
import tempfile
from pathlib import Path
from typing import Any

import pytest
from cairn_oci import MAX_CONTENT_LAYERS, CairnAnnotations
from cairn_oci.config_blob import CONFIG_BLOB_MEDIA_TYPE
from cairn_oci.producer_attestation import (
    INTOTO_STATEMENT_MEDIA_TYPE,
    PRODUCER_ATTESTATION_ARTIFACT_TYPE,
    ProducerAttestationReferrer,
    build_producer_statement,
)

from tests.integration._bundle import (
    build_config_blob_bytes,
    ecr_login,
    oras_attach_raw,
    oras_push_raw,
)
from tests.integration._waiters import (
    drain_dlq_messages,
    fetch_metadata_row,
    wait_for,
)
from tests.integration.conftest import BundleCleanup, StackOutputs

pytestmark = pytest.mark.integration

_LAYER_MEDIA_TYPE = "application/octet-stream"
_CATALOG_UUID = "22222222-2222-4222-8222-222222222222"
_ASSESSMENT_PERIOD = "2025-q1"
_BUNDLE_TYPE = "assessment-results"
_DLQ_TIMEOUT_SECONDS = 240.0


def _rejection_reason_text(message: dict[str, Any]) -> str:
    """Lower-cased reason text of an in-band rejection body for keyword asserts.

    Reads the structured rejection fields the handler emits — ``errorKind`` plus
    every ``failedCriteria`` entry's ``criterion``/``field``/``message`` — and
    falls back to the whole serialized body if those are absent.
    """
    parts: list[str] = []
    error_kind = message.get("errorKind")
    if isinstance(error_kind, str):
        parts.append(error_kind)
    failed_criteria = message.get("failedCriteria")
    if isinstance(failed_criteria, list):
        for entry in failed_criteria:
            if isinstance(entry, dict):
                for key in ("criterion", "field", "message"):
                    value = entry.get(key)
                    if isinstance(value, str):
                        parts.append(value)
    if not parts:
        parts.append(json.dumps(message, default=str))
    return " ".join(parts).lower()


def _wait_for_dlq_rejection(
    sqs_client: Any,
    queue_url: str,
    *,
    manifest_digest: str,
    description: str,
) -> dict[str, Any]:
    def _probe() -> dict[str, Any] | None:
        for message in drain_dlq_messages(sqs_client, queue_url):
            if message.get("manifestDigest") == manifest_digest:
                return message
            if manifest_digest in json.dumps(message, default=str):
                return message
        return None

    return wait_for(_probe, timeout_seconds=_DLQ_TIMEOUT_SECONDS, description=description)


def test_over_max_layers_bundle_routes_to_validate_dlq(
    aws_session: Any,
    stack_outputs: StackOutputs,
    bundle_cleanup: BundleCleanup,
    shared_repo_name: str,
    target_org_id: str,
    target_tenant_id: str,
    target_project_id: str,
) -> None:
    """A bundle with > MAX_CONTENT_LAYERS (301) content layers is rejected.

    Given an Evidence_Bundle whose content manifest carries 301 layers (one over
    the 300 cap) pushed to the shared repo, and its Producer_Attestation_Referrer attached
    against that content digest (the validate trigger),
    When the referrer push reaches validate-bundle,
    Then validate-bundle rejects it as a permanent failure and deposits an
    in-band rejection on the validate DLQ keyed on the content manifest digest,
    naming the layer-count / manifest-schema constraint, and no Metadata_Index
    row for the bundle reaches ``signingStatus=signed``.
    """
    project_id = target_project_id
    repo_path = shared_repo_name
    ecr = aws_session.client("ecr")
    reference = f"{stack_outputs.ecr_registry_host}/{repo_path}"

    over_cap = MAX_CONTENT_LAYERS + 1  # 301
    # A per-run marker in layer 0 keeps the manifest digest unique across runs
    # (mirrors the e2e suite's unique_marker_layer), so re-pushes don't collide
    # with a prior run's content-addressed manifest.
    marker = secrets.token_hex(8)

    ecr_login(ecr, stack_outputs.ecr_registry_host)
    with tempfile.TemporaryDirectory(prefix="cairn-bounds-") as raw_tmpdir:
        tmpdir = Path(raw_tmpdir)

        # 301 distinct tiny layer files (distinct content => distinct digests =>
        # genuinely 301 layer descriptors, not deduped).
        layers: list[tuple[Path, str]] = []
        for i in range(over_cap):
            layer_path = tmpdir / f"layer-{i:04d}.bin"
            body = f"{marker}-layer-{i}\n" if i == 0 else f"layer-{i}\n"
            layer_path.write_text(body)
            layers.append((layer_path, _LAYER_MEDIA_TYPE))

        config_path = tmpdir / "config.json"
        config_path.write_bytes(
            build_config_blob_bytes(
                {
                    "projectId": project_id,
                    "controls": ["ac-1"],
                    "catalogOrProfileUuid": _CATALOG_UUID,
                    "assessmentPeriod": _ASSESSMENT_PERIOD,
                }
            )
        )
        # The content manifest does not carry artifact-kind — no Config_Blob
        # `bundleType` and no `artifact-kind` annotation. Kind rides only the
        # Producer_Attestation_Referrer `artifactKind` claim (set on the
        # ProducerAttestationReferrer below).
        annotations = {
            CairnAnnotations.PROJECT_ID: project_id,
            CairnAnnotations.CREATED: "2026-05-26T12:00:00Z",
            CairnAnnotations.ASSESSMENT_PERIOD: _ASSESSMENT_PERIOD,
            CairnAnnotations.CONTROLS: "ac-1",
        }

        manifest_export = tmpdir / "manifest.json"
        content_digest = oras_push_raw(
            reference=reference,
            config_path=config_path,
            config_media_type=CONFIG_BLOB_MEDIA_TYPE,
            layers=layers,
            annotations=annotations,
            manifest_export_path=manifest_export,
        )
        bundle_cleanup.track_ecr_manifest(repo_path, content_digest)

        # Attach the Producer_Attestation_Referrer — the submission-complete validate
        # trigger. The claim layer is a WELL-FORMED in-toto Statement (the
        # production write-path format) so the
        # ONLY defect validate sees is the over-cap content manifest, not a
        # malformed claim. The OCI envelope artifactType stays the unchanged
        # trigger discriminator; the layer mediaType is the in-toto type.
        attestation = ProducerAttestationReferrer(
            projectId=project_id,
            orgId=target_org_id,
            tenantId=target_tenant_id,
            artifactKind=_BUNDLE_TYPE,
            controls=["ac-1"],
            catalogOrProfileUuid=_CATALOG_UUID,
            assessmentPeriod=_ASSESSMENT_PERIOD,
        )
        statement = build_producer_statement(content_digest, attestation)
        attestation_path = tmpdir / "attestation.json"
        attestation_path.write_bytes(json.dumps(statement).encode("utf-8"))
        referrer_export = tmpdir / "attestation-manifest.json"
        referrer_digest = oras_attach_raw(
            subject_reference=f"{reference}@{content_digest}",
            artifact_type=PRODUCER_ATTESTATION_ARTIFACT_TYPE,
            file_path=attestation_path,
            file_media_type=INTOTO_STATEMENT_MEDIA_TYPE,
            manifest_export_path=referrer_export,
        )
        bundle_cleanup.track_ecr_manifest(repo_path, referrer_digest)

    bundle_cleanup.track_metadata_row(target_tenant_id, project_id, content_digest)

    sqs = aws_session.client("sqs")
    dlq_message = _wait_for_dlq_rejection(
        sqs,
        stack_outputs.validate_bundle_dlq_url,
        manifest_digest=content_digest,
        description=(
            f"validate-bundle DLQ to surface a rejection referencing "
            f"{content_digest} (>{MAX_CONTENT_LAYERS}-layer bundle)"
        ),
    )

    reason = _rejection_reason_text(dlq_message)
    assert any(token in reason for token in ("layer", "manifest", "validation", "300")), (
        f"validate-DLQ rejection did not name the layer-count/manifest constraint: {dlq_message!r}"
    )

    ddb = aws_session.client("dynamodb")
    item = fetch_metadata_row(
        ddb, stack_outputs.metadata_table, target_tenant_id, project_id, content_digest
    )
    assert item is None or item.get("signingStatus", {}).get("S") != "signed", (
        "Metadata_Index row reached signingStatus=signed for an over-cap bundle "
        "that should have failed validation"
    )

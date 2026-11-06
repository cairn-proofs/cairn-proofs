"""Coordinate recording + re-tag idempotency.

The validate stage persists the canonical-tag coordinates onto the durable
digest-keyed Metadata_Index row so the downstream canonical-tag-minting
stage consumes intent WITHOUT re-deriving it, and short-circuits a re-seen manifest
digest (the re-entrant ``PutImage`` mint emits) so validate is idempotent against
its own re-tag re-trigger.

The canonical tag is the nameless
``{artifact-kind}--_ref--{digest}`` form: validate records ``artifactKind`` +
``validateStatus`` (plus the attestation-referrer-digest accumulation) and NO
``name`` — no producer free text (OSCAL ``metadata.title`` / pushed filename) ever
reaches the canonical-tag path. These tests pin that the row carries no ``name``
for OSCAL AND non-OSCAL bundles (the anti-favoritism guard), for multi-file
bundles, and that the per-layer OSCAL projection is nevertheless preserved.

These tests exercise the handler against an in-memory fake that mirrors the REAL
``cairn_metadata.MetadataIndex`` facade — same ``(pk, sk)`` keying, same
``get_record`` / ``create_or_merge`` semantics. The handler's
``_metadata_index()`` factory is monkeypatched to return the fake, matching the
seam the handler is designed around.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.unit.cairn_lambda_validate_bundle.conftest import (
    TENANT_ID,
    attestation_claim,
    build_bundle,
    config_blob_body,
    submit,
)


class FakeMetadataIndex:
    """In-memory stand-in for ``cairn_metadata.MetadataIndex``.

    Faithful to the real facade's tenant-PK keying and write semantics (design
    D.6 / [META-001]):

    - the row is addressed by ``(tenant_id, project_id, manifest_digest)`` — the
      same identity the real facade derives, with ``tenant_id`` keying the
      partition and ``{project_id}#{manifest_digest}`` keying the sort;
    - ``create_or_merge`` creates the row if absent, else merges only the
      still-absent attributes (per-attribute ``if_not_exists``), exactly like
      the real primitive — order-independent, clobber-safe, and a true no-op on
      re-application. This is the path validate's marker write takes.
    """

    def __init__(self) -> None:
        # key: (tenant_id, project_id, manifest_digest) -> record dict
        self.rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.merge_calls: list[tuple[str, str, str]] = []

    @staticmethod
    def _key(*, manifest_digest: str, tenant_id: str, project_id: str) -> tuple[str, str, str]:
        return (tenant_id, project_id, manifest_digest)

    def get_record(
        self, *, manifest_digest: str, tenant_id: str, project_id: str
    ) -> dict[str, Any] | None:
        return self.rows.get(
            self._key(manifest_digest=manifest_digest, tenant_id=tenant_id, project_id=project_id)
        )

    def create_or_merge(
        self,
        *,
        manifest_digest: str,
        tenant_id: str,
        project_id: str,
        record: dict[str, Any],
    ) -> None:
        key = self._key(manifest_digest=manifest_digest, tenant_id=tenant_id, project_id=project_id)
        self.merge_calls.append(key)
        row = self.rows.setdefault(key, {})
        # if_not_exists per attribute: fill only the still-absent keys; never
        # clobber a value already present (peer field or later transition).
        for attr, value in record.items():
            row.setdefault(attr, value)

    def add_validated_attestation(
        self,
        *,
        manifest_digest: str,
        tenant_id: str,
        project_id: str,
        attestation_digest: str,
    ) -> None:
        """Mirror the real facade's idempotent String-Set ``ADD``.

        Accumulates ``attestation_digest`` into the SAME ``(tenant_id,
        project_id, manifest_digest)`` row's ``validatedAttestationDigests`` set
        that ``create_or_merge``/``get_record`` use — a re-recorded member is a
        no-op, a distinct one accumulates, and no peer/transition field is
        touched.
        """
        key = self._key(manifest_digest=manifest_digest, tenant_id=tenant_id, project_id=project_id)
        row = self.rows.setdefault(key, {})
        row.setdefault("validatedAttestationDigests", set()).add(attestation_digest)


@pytest.fixture
def fake_index(reload_handler, monkeypatch):
    """Wire the handler's ``_metadata_index()`` factory to a fresh fake index."""
    index = FakeMetadataIndex()
    monkeypatch.setattr(reload_handler, "_metadata_index", lambda: index)
    return index


def _emf_metrics(out: str) -> list[dict[str, Any]]:
    docs = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "_aws" in doc:
            docs.append(doc)
    return docs


def _metric_value(doc: dict[str, Any], name: str) -> float | None:
    for block in doc["_aws"]["CloudWatchMetrics"]:
        for m in block["Metrics"]:
            if m["Name"] == name:
                v = doc.get(name)
                return v[0] if isinstance(v, list) else v
    return None


def _validate(handler: Any, bundle: Any, lambda_context: Any, *, seed_bundle: bool = True) -> Any:
    """Seed the bundle + its Producer_Attestation_Referrer and invoke the handler on the
    attestation push (the single positive trigger)."""
    _attestation, event = submit(handler, bundle=bundle, seed_bundle=seed_bundle)
    return handler.lambda_handler(event, lambda_context)


# --- canonical-tag coordinate recording: NO name ----
# The canonical tag is the nameless {artifact-kind}--_ref--{digest} form, so the
# validate stage records JUST artifactKind + the validateStatus marker — never a
# name slug derived from producer free text. version/uuid/period/scope/tagForm are
# likewise not tag fields and are not recorded.

CATALOG_UUID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"


def test_validated_oscal_bundle_records_no_name(reload_handler, lambda_context, fake_index) -> None:
    """A validated OSCAL bundle records artifactKind + the validateStatus marker
    and NO ``name`` — even though it carries an OSCAL ``metadata.title``, the
    canonical tag grants OSCAL no naming privilege. No
    version/uuid/period/scope/tagForm coordinates are recorded."""
    handler = reload_handler
    bundle = build_bundle()  # OSCAL root title = "Test Catalog"
    _attestation, event = submit(handler, bundle=bundle)

    handler.lambda_handler(event, lambda_context)

    record = fake_index.get_record(
        manifest_digest=bundle.manifest_digest, project_id="alpha-bank", tenant_id=TENANT_ID
    )
    assert record is not None
    assert record["validateStatus"] == "validated"
    assert record["artifactKind"] == "assessment-results"
    # The canonical tag carries no name: nothing producer-derived is recorded, even
    # though the OSCAL metadata.title ("Test Catalog") was present.
    assert "name" not in record
    # version/uuid are not tag fields and are not recorded.
    assert "version" not in record
    assert "uuid" not in record
    # No tag-form coordinates are recorded.
    assert "tagForm" not in record
    assert "assessmentPeriod" not in record
    assert "controlScope" not in record
    # Written via the create_or_merge path (the create-or-merge primitive owns
    # both the create and the collision-merge — no separate upsert fallback).
    assert fake_index.merge_calls == [(TENANT_ID, "alpha-bank", bundle.manifest_digest)]


def test_validated_non_oscal_bundle_records_no_name(
    reload_handler, lambda_context, fake_index
) -> None:
    """A validated NON-OSCAL bundle (no OSCAL layer) records NO ``name`` either —
    the content layer's ``org.opencontainers.image.title`` (pushed filename) is
    NOT reached into for a name. Parity with the OSCAL case is the anti-favoritism
    guard: neither content type gets a content-derived canonical-tag name."""
    handler = reload_handler
    # A non-OSCAL content layer: arbitrary media type outside the OSCAL
    # media-type family, carrying the pushed-filename annotation.
    bundle = build_bundle(
        layer_media_type="application/pdf",
        layer_bytes=b"%PDF-1.7 not-oscal",
        layer_title="Quarterly Pentest Report.pdf",
    )
    _validate(handler, bundle, lambda_context)

    record = fake_index.get_record(
        manifest_digest=bundle.manifest_digest, project_id="alpha-bank", tenant_id=TENANT_ID
    )
    assert record is not None
    assert record["validateStatus"] == "validated"
    assert record["artifactKind"] == "assessment-results"
    # No filename slug — the pushed filename never enters the canonical-tag path.
    assert "name" not in record
    assert "version" not in record
    assert "uuid" not in record


def test_oscal_and_non_oscal_bundles_record_identical_coordinate_shape(
    reload_handler, lambda_context, fake_index
) -> None:
    """Anti-favoritism parity: an OSCAL-bearing
    bundle and a non-OSCAL bundle of the SAME artifact-kind record the identical
    set of canonical-tag coordinate keys — neither carries a ``name``, so OSCAL is
    granted no content-derived naming a non-OSCAL bundle is denied."""
    handler = reload_handler

    oscal_bundle = build_bundle()  # application/oscal+json catalog layer
    _validate(handler, oscal_bundle, lambda_context)
    oscal_record = fake_index.get_record(
        manifest_digest=oscal_bundle.manifest_digest, project_id="alpha-bank", tenant_id=TENANT_ID
    )

    non_oscal_bundle = build_bundle(
        layer_media_type="application/pdf",
        layer_bytes=b"%PDF-1.7 not-oscal",
        layer_title="Quarterly Pentest Report.pdf",
    )
    _validate(handler, non_oscal_bundle, lambda_context)
    non_oscal_record = fake_index.get_record(
        manifest_digest=non_oscal_bundle.manifest_digest,
        project_id="alpha-bank",
        tenant_id=TENANT_ID,
    )

    assert oscal_record is not None and non_oscal_record is not None
    # Identical coordinate-key set — and neither set contains "name".
    assert set(oscal_record) == set(non_oscal_record)
    assert "name" not in oscal_record
    assert "name" not in non_oscal_record


def test_multi_file_bundle_records_no_name(reload_handler, lambda_context, fake_index) -> None:
    """A MULTI-file bundle (content manifest with >1 layer, >1 pushed file) records
    NO ``name`` — the canonical tag cannot misrepresent the bundle as any one of
    its members because no member's filename/title enters the row at all
    (spec "Multi-file bundle is addressed without
    naming any single member")."""
    handler = reload_handler
    bundle = _multi_layer_bundle(
        handler,
        layers=[
            {"media_type": "application/pdf", "blob": b"%PDF-1.7 first", "title": "first.pdf"},
            {"media_type": "application/pdf", "blob": b"%PDF-1.7 second", "title": "second.pdf"},
        ],
        config_bytes=config_blob_body(),
    )
    # The builder already seeded the bundle on the fake ECR, so seed_bundle=False.
    _validate(handler, bundle, lambda_context, seed_bundle=False)

    record = fake_index.get_record(
        manifest_digest=bundle.manifest_digest, project_id="alpha-bank", tenant_id=TENANT_ID
    )
    assert record is not None
    assert record["validateStatus"] == "validated"
    assert "name" not in record


def test_oscal_per_layer_projection_is_still_produced(
    reload_handler, lambda_context, fake_index
) -> None:
    """Regression guard against over-deletion: removing the canonical-tag NAME
    derivation must NOT remove the per-layer OSCAL projection. A validated
    OSCAL bundle's emitted ``layers`` still carry the ``oscal`` sub-object
    (``oscalSchemaVersion`` + ``metadata``) re-derived from the parsed blob."""
    handler = reload_handler
    bundle = build_bundle()  # application/oscal+json catalog layer, oscal-version 1.2.1
    _attestation, event = submit(handler, bundle=bundle)

    handler.lambda_handler(event, lambda_context)

    validated = handler._test_recorder.details("artifact.validated")
    assert len(validated) == 1
    layers = validated[0]["layers"]
    assert len(layers) == 1
    oscal = layers[0]["oscal"]
    # The OSCAL sub-object survived: schema version + projected metadata. The
    # projected metadata exposes the OSCAL title as ``project_name`` (the same
    # field the deleted name-slug derivation read), proving the OSCAL projection
    # is intact even though the name derivation that consumed it is gone.
    assert oscal["oscalSchemaVersion"] == "1.2.1"
    assert oscal["metadata"]["project_name"] == "Test Catalog"
    assert oscal["metadata"]["oscal_version"] == "1.2.1"


def test_record_coordinates_unit_records_no_name(
    reload_handler, lambda_context, fake_index
) -> None:
    """Unit-level contract: ``_record_coordinates`` (which takes no
    ``name`` parameter) records artifactKind + the validateStatus marker and NO
    name key, accumulating the triggering attestation digest into the set."""
    handler = reload_handler
    referrer = "sha256:" + "b" * 64

    handler._record_coordinates(
        image_digest="sha256:" + "a" * 64,
        tenant_id=TENANT_ID,
        project_id="alpha-bank",
        artifact_kind="assessment-results",
        attestation_referrer_digest=referrer,
    )

    record = fake_index.get_record(
        manifest_digest="sha256:" + "a" * 64, project_id="alpha-bank", tenant_id=TENANT_ID
    )
    assert record is not None
    assert record["validateStatus"] == "validated"
    assert record["artifactKind"] == "assessment-results"
    assert "name" not in record
    assert "version" not in record
    assert "uuid" not in record
    assert record["validatedAttestationDigests"] == {referrer}


def test_record_coordinates_merges_marker_onto_preexisting_row(
    reload_handler, lambda_context, fake_index
) -> None:
    """If a peer event pre-created the row, create_or_merge merges the marker +
    coordinates onto it (filling only still-absent attrs) rather than failing the
    validate. The pre-existing field is preserved."""
    handler = reload_handler
    bundle = build_bundle()
    _attestation, event = submit(handler, bundle=bundle)
    # Pre-seed the row as if signing-failed landed first (no validateStatus yet).
    fake_index.rows[(TENANT_ID, "alpha-bank", bundle.manifest_digest)] = {"signStatus": "failed"}

    handler.lambda_handler(event, lambda_context)

    record = fake_index.get_record(
        manifest_digest=bundle.manifest_digest, project_id="alpha-bank", tenant_id=TENANT_ID
    )
    assert record is not None
    # Pre-existing field preserved, coordinates merged on (and still no name).
    assert record["signStatus"] == "failed"
    assert record["validateStatus"] == "validated"
    assert record["artifactKind"] == "assessment-results"
    assert "name" not in record
    assert fake_index.merge_calls == [(TENANT_ID, "alpha-bank", bundle.manifest_digest)]


def test_record_coordinates_repush_of_advanced_digest_is_clean_noop(
    reload_handler, lambda_context, fake_index
) -> None:
    """Regression guard: a re-push of a digest whose row has already
    advanced through later pipeline stages (signingStatus/archiveStatus present)
    completes WITHOUT error and WITHOUT regressing any later field — if_not_exists
    leaves the existing marker + advanced fields untouched."""
    handler = reload_handler
    bundle = build_bundle()
    _attestation, event = submit(handler, bundle=bundle)
    # Row already validated AND advanced through sign + archive.
    fake_index.rows[(TENANT_ID, "alpha-bank", bundle.manifest_digest)] = {
        "validateStatus": "validated",
        "artifactKind": "assessment-results",
        "signingStatus": "signed",
        "archiveStatus": "archived",
    }

    # Re-push: must not raise (no uncaught ConditionalUpdateFailed) ...
    handler.lambda_handler(event, lambda_context)

    record = fake_index.get_record(
        manifest_digest=bundle.manifest_digest, project_id="alpha-bank", tenant_id=TENANT_ID
    )
    assert record is not None
    # ... and the advanced mutable fields did NOT regress.
    assert record["signingStatus"] == "signed"
    assert record["archiveStatus"] == "archived"
    assert record["validateStatus"] == "validated"


# --- re-tag idempotency short-circuit ----------------------------------------


def test_repush_of_same_attestation_on_terminal_row_is_skipped(
    reload_handler, lambda_context, fake_index, capsys
) -> None:
    """Non-negotiable T2 regression (pair-wise + terminal-gated): a re-push of the
    SAME Producer_Attestation_Referrer whose CONTENT digest row is terminal-success (signed
    AND archived) and whose validatedAttestationDigests set already contains the
    triggering referrer is skipped — NO re-mint, NO downstream event, and the
    re-seen-digest skip metric is emitted. Idempotency keys on the (CONTENT
    manifest DIGEST, Producer_Attestation_Referrer digest) pair, never the tag.

    The first push records the referrer into the set and stamps validateStatus; we
    then advance the row to terminal-success (signed+archived) — the transition
    update-metadata would write — so the SAME-referrer re-push short-circuits."""
    handler = reload_handler
    bundle = build_bundle()

    # First attestation push: validates, emits artifact.validated, records coords
    # AND accumulates the referrer digest into validatedAttestationDigests.
    a1, event1 = submit(handler, bundle=bundle)
    handler.lambda_handler(event1, lambda_context)
    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    capsys.readouterr()  # drain the first invocation's EMF

    # Advance the content row to terminal-success, as update-metadata would once
    # the bundle is signed + archived. (The set already holds a1's digest from the
    # first push's _record_coordinates.)
    row = fake_index.rows[(TENANT_ID, "alpha-bank", bundle.manifest_digest)]
    row["signingStatus"] = "signed"
    row["archiveStatus"] = "archived"
    assert a1.attestation_digest in row["validatedAttestationDigests"]

    # Re-push the SAME attestation over the same content; it short-circuits.
    a2, event2 = submit(handler, bundle=bundle, seed_bundle=False)
    assert a2.attestation_digest == a1.attestation_digest  # identical referrer
    result = handler.lambda_handler(event2, lambda_context)

    # Skipped: no NEW downstream event beyond the first push's single validated.
    assert result is handler._SKIPPED
    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    assert handler._test_recorder.details("artifact.validation-failed") == []

    out = capsys.readouterr().out
    docs = _emf_metrics(out)
    assert any(_metric_value(d, "ValidationSkipped") == 1.0 for d in docs)
    assert any(d.get("SkipReason") == "re-seen-digest" for d in docs)
    # A skip is neither Success nor Failure.
    assert not any(_metric_value(d, "Success") == 1.0 for d in docs)
    assert not any(_metric_value(d, "Failure") == 1.0 for d in docs)
    # No second row write — the short-circuit precedes coordinate recording.
    assert fake_index.merge_calls == [(TENANT_ID, "alpha-bank", bundle.manifest_digest)]


def test_repush_of_same_attestation_on_non_terminal_row_is_processed(
    reload_handler, lambda_context, fake_index
) -> None:
    """A re-push of the same attestation whose content row is still NON-terminal
    (validated but not yet signed+archived, the parked-pending production incident)
    is PROCESSED, not skipped, so the parked row can progress."""
    handler = reload_handler
    bundle = build_bundle()

    # First push: validates + records (row left non-terminal — no sign/archive).
    _a1, event1 = submit(handler, bundle=bundle)
    handler.lambda_handler(event1, lambda_context)
    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    row = fake_index.rows[(TENANT_ID, "alpha-bank", bundle.manifest_digest)]
    assert "signingStatus" not in row  # parked non-terminal

    # Re-push the SAME attestation: must PROCESS (re-emit validated), not skip.
    _a2, event2 = submit(handler, bundle=bundle, seed_bundle=False)
    result = handler.lambda_handler(event2, lambda_context)

    assert result is not handler._SKIPPED
    assert handler._test_recorder.detail_types() == ["artifact.validated", "artifact.validated"]


def test_distinct_attestation_on_terminal_row_is_processed(
    reload_handler, lambda_context, fake_index
) -> None:
    """A DISTINCT Producer_Attestation_Referrer over an already terminal-success content
    subject is PROCESSED ("one content, many attestations"), not skipped. The
    terminal row's recorded set holds only the FIRST referrer; the second, distinct
    referrer is not a member, so it proceeds and is itself added to the set."""
    handler = reload_handler
    bundle = build_bundle()

    # Seed a terminal-success row whose set holds a DIFFERENT referrer digest.
    other_referrer = "sha256:" + "0" * 64
    fake_index.rows[(TENANT_ID, "alpha-bank", bundle.manifest_digest)] = {
        "validateStatus": "validated",
        "artifactKind": "assessment-results",
        "signingStatus": "signed",
        "archiveStatus": "archived",
        "validatedAttestationDigests": {other_referrer},
    }

    a2, event = submit(handler, bundle=bundle)
    assert a2.attestation_digest != other_referrer  # genuinely new referrer
    result = handler.lambda_handler(event, lambda_context)

    # Processed (one content, many attestations), and the new referrer accumulates.
    assert result is not handler._SKIPPED
    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    row = fake_index.rows[(TENANT_ID, "alpha-bank", bundle.manifest_digest)]
    assert row["validatedAttestationDigests"] == {other_referrer, a2.attestation_digest}


def test_successful_validation_accumulates_attestation_digests(
    reload_handler, lambda_context, fake_index
) -> None:
    """design Test Properties (b) — after a successful validation the content row's
    validatedAttestationDigests contains push.image_digest; a second successful
    validation with a DISTINCT attestation over the same content subject ADDs its
    digest without removing the first (set accumulation, write-once-per-member)."""
    handler = reload_handler
    bundle = build_bundle()

    # First successful validation records its referrer into the set.
    a1, event1 = submit(handler, bundle=bundle)
    handler.lambda_handler(event1, lambda_context)
    row = fake_index.rows[(TENANT_ID, "alpha-bank", bundle.manifest_digest)]
    assert row["validatedAttestationDigests"] == {a1.attestation_digest}

    # A second successful validation with a DISTINCT attestation (a different,
    # still-valid claim body → different referrer digest) over the SAME content
    # subject accumulates.
    a2, event2 = submit(
        handler,
        bundle=bundle,
        seed_bundle=False,
        claim_bytes=attestation_claim(assessment_id="distinct-second-claim"),
    )
    assert a2.attestation_digest != a1.attestation_digest
    handler.lambda_handler(event2, lambda_context)

    row = fake_index.rows[(TENANT_ID, "alpha-bank", bundle.manifest_digest)]
    # BOTH digests present — the first was not removed (accumulation).
    assert row["validatedAttestationDigests"] == {a1.attestation_digest, a2.attestation_digest}


def test_first_validation_is_not_skipped(reload_handler, lambda_context, fake_index) -> None:
    """A digest with NO prior durable record is validated normally (the
    short-circuit must not fire on the first sighting)."""
    handler = reload_handler
    bundle = build_bundle()
    result = _validate(handler, bundle, lambda_context)

    assert result is not handler._SKIPPED
    assert handler._test_recorder.detail_types() == ["artifact.validated"]


def test_no_metadata_index_degrades_to_no_short_circuit(
    reload_handler, lambda_context, monkeypatch
) -> None:
    """When no Metadata_Index is wired (factory returns None), recording and the
    re-tag short-circuit both degrade to no-ops — validate still runs end-to-end."""
    handler = reload_handler
    monkeypatch.setattr(handler, "_metadata_index", lambda: None)
    bundle = build_bundle()
    result = _validate(handler, bundle, lambda_context)

    assert result is not handler._SKIPPED
    assert handler._test_recorder.detail_types() == ["artifact.validated"]


# --- multi-layer content-manifest builder ------------------------------------
# A local builder for a CONTENT manifest carrying N arbitrary layers, so the
# multi-file no-name test can seed a real >1-layer bundle on the fake ECR. The
# stock conftest ``build_bundle`` only emits a single-layer manifest.


def _multi_layer_bundle(
    handler: Any,
    *,
    layers: list[dict[str, Any]],
    config_bytes: bytes,
    repo_name: str | None = None,
) -> Any:
    """Build a CONTENT bundle with arbitrary layers, seed it on the fake ECR, and
    return a ``BundleFixture``-shaped object exposing ``manifest_digest`` /
    ``repo_name`` (enough for ``submit(..., seed_bundle=False)``).

    Each ``layers`` entry is ``{"media_type", "blob", "title"}`` (title optional).
    The config blob + every layer blob are registered on the fake ECR so the
    handler resolves them.
    """
    from dataclasses import dataclass

    from cairn_oci import CONFIG_BLOB_MEDIA_TYPE, compute_sha256_digest
    from cairn_oci.annotations import CairnAnnotations

    from tests.unit.cairn_lambda_validate_bundle.conftest import ACCOUNT_ID, REGION, REPO_NAME

    repo = repo_name or REPO_NAME
    config_digest = compute_sha256_digest(config_bytes)
    layer_descs: list[dict[str, Any]] = []
    blobs: dict[str, bytes] = {config_digest: config_bytes}
    for spec in layers:
        blob = spec["blob"]
        digest = compute_sha256_digest(blob)
        blobs[digest] = blob
        desc: dict[str, Any] = {
            "mediaType": spec["media_type"],
            "digest": digest,
            "size": len(blob),
        }
        if spec.get("title") is not None:
            desc["annotations"] = {"org.opencontainers.image.title": spec["title"]}
        layer_descs.append(desc)

    manifest: dict[str, Any] = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": CONFIG_BLOB_MEDIA_TYPE,
            "digest": config_digest,
            "size": len(config_bytes),
        },
        "layers": layer_descs,
        "annotations": {
            CairnAnnotations.PROJECT_ID: "alpha-bank",
        },
    }
    manifest_raw = json.dumps(manifest, separators=(",", ":"))
    manifest_digest = compute_sha256_digest(manifest_raw.encode("utf-8"))

    # Seed manifest + all blobs + residency on the fake ECR (mirrors add_bundle).
    fake = handler._test_fake_ecr
    fake.manifests[manifest_digest] = (manifest, manifest_raw)
    fake.blobs.update(blobs)
    fake.residency.add((repo, manifest_digest))

    @dataclass(frozen=True)
    class _MultiLayerBundle:
        manifest_digest: str
        repo_name: str
        registry_id: str = ACCOUNT_ID
        region: str = REGION

    return _MultiLayerBundle(manifest_digest=manifest_digest, repo_name=repo)

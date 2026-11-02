"""Prune_Candidate eligibility ([RETAIN-004], [RETAIN-008], [RETAIN-009], [RETAIN-012]).

Every AWS call is served by the in-memory fakes in ``prune_fakes`` (the boto3
boundary); no real AWS.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cairn_cli._prune_eligibility import (
    ItemClass,
    RunCache,
    classify,
    parse_rfc3339_utc,
    project_digests,
    prove_coordinates,
)
from cairn_oci import NOTARY_SIGNATURE_ARTIFACT_TYPE, OCI_IMAGE_INDEX_MEDIA_TYPE

from tests.unit.cairn_cli.prune_fakes import (
    METADATA_TABLE,
    NOW,
    ORG,
    PERIOD,
    PROJECT,
    TENANT,
    WINDOW,
    World,
    digest_of,
    rfc,
)


def _classify(w: World, digest: str, *, unproven: str | None = None):
    return classify(w.clients(), w.deployment(), digest, now=NOW, coordinates_unproven=unproven)


# ── [RETAIN-004] expired bundle ──────────────────────────────────────────────


@pytest.mark.parametrize("worm", [False, True])
def test_expired_bundle_without_legal_hold_is_a_candidate(worm: bool) -> None:
    """[RETAIN-004.01]"""
    w = World(worm=worm)
    d = w.add_bundle()
    c = _classify(w, d)
    assert c.eligible, c.reason
    assert c.item_class is ItemClass.EXPIRED_BUNDLE
    assert c.ecr_present
    assert len(c.s3_versions) == 4
    # Tag-schema index first, then referrers children-before-parent.
    images = w.images
    first = images[c.referrer_digests[0]]
    assert first.manifest["mediaType"] == OCI_IMAGE_INDEX_MEDIA_TYPE
    positions = {dig: i for i, dig in enumerate(c.referrer_digests)}
    for dig in c.referrer_digests[1:]:
        parent = (images[dig].manifest.get("subject") or {}).get("digest")
        if parent in positions:
            assert positions[dig] < positions[parent]
    assert d not in c.referrer_digests
    assert len(c.referrer_digests) == 5  # index, att, att-sig, sig, receipt


def test_legal_hold_vetoes_eligibility() -> None:
    """[RETAIN-004.02]"""
    w = World(worm=True)
    d = w.add_bundle(legal_hold=True)
    c = _classify(w, d)
    assert not c.eligible
    assert "legal hold ON" in c.reason


def test_bundle_under_retention_is_not_a_candidate_whatever_the_index_says() -> None:
    """[RETAIN-004.03] / [RETAIN-012.03]"""
    w = World()
    d = w.add_bundle(archived_at=NOW - timedelta(days=10))
    w.items[(METADATA_TABLE, TENANT, f"{PROJECT}#{d}")]["retainUntil"] = {
        "S": "2000-01-01T00:00:00Z"
    }
    c = _classify(w, d)
    assert not c.eligible
    assert c.reason.startswith("retention in force until")


def test_empty_prefix_is_never_vacuously_expired() -> None:
    """[RETAIN-004.04] No S3 version: never an expired bundle."""
    w = World()
    d = w.add_bundle()
    w.versions = [v for v in w.versions if d not in v.key]
    c = _classify(w, d)
    assert not c.eligible
    assert c.item_class is None


def test_one_object_version_still_locked_refuses_under_worm() -> None:
    w = World(worm=True)
    d = w.add_bundle()
    w.prefix_versions(d)[0].retain_until = NOW + timedelta(days=1)
    c = _classify(w, d)
    assert not c.eligible
    assert "retention in force" in c.reason


def test_object_version_without_lock_refuses_under_worm() -> None:
    w = World(worm=True)
    d = w.add_bundle()
    w.prefix_versions(d)[1].retain_until = None
    c = _classify(w, d)
    assert not c.eligible
    assert "no Object Lock retention" in c.reason


def test_delete_markers_are_counted_for_deletion() -> None:
    """M32: delete markers are part of the bundle's versions."""
    w = World()
    d = w.add_bundle()
    w.put_version(f"{ORG}/{TENANT}/{PROJECT}/{d}/old.json", delete_marker=True)
    c = _classify(w, d)
    assert c.eligible
    assert sum(v.is_delete_marker for v in c.s3_versions) == 1


def test_missing_s3_receipt_copy_refuses() -> None:
    w = World()
    d = w.add_bundle()
    w.versions = [v for v in w.versions if not v.key.endswith("worm-receipt.json")]
    c = _classify(w, d)
    assert not c.eligible
    assert "no S3 receipt copy" in c.reason


def test_receipt_copy_for_a_different_bundle_refuses() -> None:
    w = World()
    d = w.add_bundle()
    other = w.add_bundle("other")
    mine = next(v for v in w.prefix_versions(d) if v.key.endswith("worm-receipt.json"))
    theirs = next(v for v in w.prefix_versions(other) if v.key.endswith("worm-receipt.json"))
    mine.body = theirs.body
    c = _classify(w, d)
    assert not c.eligible
    assert "different bundle" in c.reason


# ── [RETAIN-012] authoritative inputs ────────────────────────────────────────


def test_forged_ecr_receipt_with_past_date_cannot_redirect_eligibility() -> None:
    """[RETAIN-012.01] The S3 copy shows retention in force; a forged ECR receipt says past."""
    w = World()
    d = w.add_bundle(archived_at=NOW - timedelta(days=10), annotation="2000-01-01T00:00:00Z")
    c = _classify(w, d)
    assert not c.eligible


def test_forged_receipt_naming_an_empty_prefix_is_ignored() -> None:
    """[RETAIN-012.01] The prefix is derived, so a receipt body cannot point elsewhere."""
    w = World()
    d = w.add_bundle(archived_at=NOW - timedelta(days=10))
    c = _classify(w, d)
    assert not c.eligible
    assert c.s3_versions  # read from the derived prefix
    assert all(v.key.startswith(f"{ORG}/{TENANT}/{PROJECT}/{d}/") for v in c.s3_versions)


def test_multiple_receipt_referrers_refuse_and_name_every_one() -> None:
    """[RETAIN-012.02] / M33"""
    from cairn_oci import RETAIN_UNTIL_ANNOTATION, WORM_RECEIPT_ARTIFACT_TYPE

    w = World()
    d = w.add_bundle()
    decoy = w.add_image(
        "decoy",
        subject=d,
        artifact_type=WORM_RECEIPT_ARTIFACT_TYPE,
        annotations={RETAIN_UNTIL_ANNOTATION: "2000-01-01T00:00:00Z"},
    )
    receipts = [
        img.digest
        for img in w.images.values()
        if img.manifest.get("artifactType") == WORM_RECEIPT_ARTIFACT_TYPE
    ]
    c = _classify(w, d)
    assert not c.eligible
    assert c.reason.startswith("multiple WORM_Receipt referrers")
    for r in receipts:
        assert r in c.reason
    assert decoy in c.reason


@pytest.mark.parametrize("annotation", ["2000-01-01T00:00:00Z", "garbage", None])
def test_ecr_and_s3_receipt_disagreement_refuses(annotation: str | None) -> None:
    """[RETAIN-012.02]"""
    w = World()
    d = w.add_bundle(annotation=annotation)
    c = _classify(w, d)
    assert not c.eligible
    assert "disagrees with the S3 receipt copy" in c.reason


def test_bundle_with_ecr_parts_gone_classifies_from_s3_copy() -> None:
    """[RETAIN-010] resume: ECR deleted, S3 remains."""
    w = World()
    d = w.add_bundle()
    w.images.clear()
    c = _classify(w, d)
    assert c.eligible
    assert c.item_class is ItemClass.EXPIRED_BUNDLE
    assert not c.ecr_present
    assert c.referrer_digests == ()


def test_index_records_only_remnant_resumes() -> None:
    w = World()
    archived = NOW - timedelta(days=PERIOD + 10)
    d = w.add_bundle(archived_at=archived)
    w.images.clear()
    w.versions.clear()
    # Covers: RETAIN-010.04
    c = _classify(w, d)
    assert c.eligible
    assert "only index records" in c.reason
    assert c.retain_until == archived + timedelta(days=PERIOD)


def test_index_records_only_remnant_under_retention_is_refused() -> None:
    """Retain_Until evidence is required: the index archivedAt plus the period."""
    w = World()
    d = w.add_bundle(archived_at=NOW - timedelta(days=PERIOD - 1))
    w.images.clear()
    w.versions.clear()
    c = _classify(w, d)
    assert not c.eligible
    assert "retention in force" in c.reason


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("archiveStatus", "failed"),
        ("s3Bucket", "some-other-bucket"),
        ("s3KeyPrefix", f"{ORG}/{TENANT}/other-project/sha256:{'a' * 64}/"),
        ("archivedAt", "2020-01-01"),
        ("archivedAt", None),
    ],
)
def test_index_records_only_remnant_needs_matching_archive_coordinates(
    attribute: str, value: str | None
) -> None:
    """The record must name the derived bucket and prefix, and a parseable archivedAt."""
    w = World()
    d = w.add_bundle()
    w.images.clear()
    w.versions.clear()
    row = w.items[(METADATA_TABLE, TENANT, f"{PROJECT}#{d}")]
    if value is None:
        row.pop(attribute)
    else:
        row[attribute] = {"S": value}
    c = _classify(w, d)
    assert not c.eligible
    assert not c.check_failed


def test_index_records_only_remnant_without_the_repository_is_a_failed_check() -> None:
    w = World()
    d = w.add_bundle()
    w.images.clear()
    w.versions.clear()
    w.fail("describe_repositories", w.deployment().repository_name, "RepositoryNotFoundException")
    c = _classify(w, d)
    assert not c.eligible
    assert c.check_failed
    assert "ECR DescribeRepositories" in c.reason


def test_bare_index_row_without_archive_fields_is_not_a_candidate() -> None:
    w = World()
    d = digest_of("row-only")
    w.add_row(d)
    assert not _classify(w, d).eligible


def test_nothing_anywhere_is_not_a_candidate() -> None:
    w = World()
    c = _classify(w, digest_of("never"))
    assert not c.eligible
    assert c.reason.startswith("nothing to prune")


# ── [RETAIN-009] receipt without retainUntil ─────────────────────────────────


def test_worm_receipt_without_retain_until_uses_latest_object_lock_date() -> None:
    """[RETAIN-009.01]"""
    w = World(worm=True)
    d = w.add_bundle(retain_until=None, lock_until=NOW - timedelta(days=5))
    latest = NOW - timedelta(days=1)
    w.prefix_versions(d)[2].retain_until = latest
    c = _classify(w, d)
    assert c.eligible, c.reason
    assert c.retain_until == latest

    w.prefix_versions(d)[2].retain_until = NOW + timedelta(days=1)
    assert not _classify(w, d).eligible


def test_non_worm_receipt_without_retain_until_uses_archive_time_plus_period() -> None:
    """[RETAIN-009.02]"""
    w = World()
    archived = NOW - timedelta(days=PERIOD + 1)
    d = w.add_bundle(archived_at=archived, retain_until=None)
    c = _classify(w, d)
    assert c.eligible
    assert c.retain_until == archived + timedelta(days=PERIOD)

    w2 = World()
    d2 = w2.add_bundle(archived_at=NOW - timedelta(days=PERIOD - 1), retain_until=None)
    assert not _classify(w2, d2).eligible


# ── [RETAIN-008] unarchived manifest ─────────────────────────────────────────


def test_aged_manifest_with_no_referrer_record_or_archive_is_a_candidate() -> None:
    """[RETAIN-008.01]"""
    w = World()
    w.add_other_project_row()
    d = w.add_image("junk")
    c = _classify(w, d)
    assert c.eligible, c.reason
    assert c.item_class is ItemClass.UNARCHIVED_MANIFEST


@pytest.mark.parametrize("evidence", ["index-row", "attestation", "signature", "s3-object"])
def test_bundle_in_or_failed_in_the_pipeline_is_retained(evidence: str) -> None:
    """[RETAIN-008.02]"""
    w = World()
    w.add_other_project_row()
    d = w.add_image("in-pipeline")
    if evidence == "index-row":
        w.add_row(d)
    elif evidence == "attestation":
        w.add_image("att", subject=d, artifact_type="application/vnd.cairnproofs.x")
    elif evidence == "signature":
        w.add_image("sig", subject=d, artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE)
    else:
        w.put_version(f"{ORG}/{TENANT}/{PROJECT}/{d}/manifest.json")
    assert not _classify(w, d).eligible


def test_referrer_whose_subject_is_gone_is_a_candidate() -> None:
    """[RETAIN-008.03] signature referrer and its tag-schema index."""
    w = World()
    gone = digest_of("deleted-subject")
    sig = w.add_image("orphan-sig", subject=gone, artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE)
    index = w.add_image(
        "orphan-index",
        media_type=OCI_IMAGE_INDEX_MEDIA_TYPE,
        tags=[f"sha256-{gone.removeprefix('sha256:')}"],
    )
    for d in (sig, index):
        c = _classify(w, d)
        assert c.eligible, c.reason
        assert c.item_class is ItemClass.UNARCHIVED_MANIFEST
        assert gone in c.reason


def _subject_prefix(subject: str) -> str:
    return f"{ORG}/{TENANT}/{PROJECT}/{subject}/"


@pytest.mark.parametrize("kind", ["signature", "tag-schema-index"])
def test_referrer_of_an_absent_but_archived_subject_is_retained(kind: str) -> None:
    """[RETAIN-008] item 2: the subject's own prefix must also be empty."""
    # Covers: RETAIN-008.07
    w = World()
    gone = digest_of("archived-subject")
    w.put_version(f"{_subject_prefix(gone)}manifest.json")
    if kind == "signature":
        d = w.add_image("orphan-sig", subject=gone, artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE)
    else:
        d = w.add_image(
            "orphan-index",
            media_type=OCI_IMAGE_INDEX_MEDIA_TYPE,
            tags=[f"sha256-{gone.removeprefix('sha256:')}"],
        )
    c = _classify(w, d)
    assert not c.eligible
    assert not c.check_failed
    assert gone in c.reason
    assert _subject_prefix(gone) in c.reason


def _index_row(w: World, digest: str, **attrs: Any) -> None:
    """A Metadata_Index row for ``digest`` carrying the given digest attributes."""
    row: dict[str, Any] = {
        "pk": {"S": TENANT},
        "sk": {"S": f"{PROJECT}#{digest}"},
        "manifestDigest": {"S": digest},
    }
    for name, value in attrs.items():
        row[name] = {"SS": value} if isinstance(value, list) else {"S": value}
    w.items[(METADATA_TABLE, TENANT, f"{PROJECT}#{digest}")] = row


def _tag_index(w: World, label: str, subject: str) -> str:
    return w.add_image(
        label,
        media_type=OCI_IMAGE_INDEX_MEDIA_TYPE,
        tags=[f"sha256-{subject.removeprefix('sha256:')}"],
    )


def test_attestation_chain_of_an_absent_archived_bundle_is_retained() -> None:
    """SigA -> A -> S with S and A gone from ECR and S still archived."""
    # Covers: RETAIN-008.08
    w = World()
    s = digest_of("content-manifest-S")
    a = digest_of("attestation-A")
    w.put_version(f"{_subject_prefix(s)}manifest.json")
    sig_a = w.add_image("sig-A", subject=a, artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE)
    index_a = _tag_index(w, "index-A", a)
    _index_row(w, s, attestationDigest=a, attestationSignatureDigest=sig_a)
    assert not w.prefix_versions(a)
    assert not w.prefix_versions(sig_a)
    expected = {
        sig_a: f"{sig_a} is named by the index record of {s}",
        index_a: f"subject {a} is named by the index record of {s}",
    }
    for d, reason in expected.items():
        c = _classify(w, d)
        assert not c.eligible, c.reason
        assert not c.check_failed
        assert c.reason == reason


def test_orphan_whose_own_digest_is_named_by_a_record_is_retained() -> None:
    """[RETAIN-008.08] the orphan's own digest, not only its subject's."""
    w = World()
    s = digest_of("content-manifest-S")
    gone = digest_of("gone")
    sig = w.add_image("sig", subject=gone, artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE)
    _index_row(w, s, signatureDigest=sig)
    c = _classify(w, sig)
    assert not c.eligible and not c.check_failed
    assert c.reason == f"{sig} is named by the index record of {s}"


def test_subject_named_only_in_validated_attestation_digests_is_retained() -> None:
    """[RETAIN-008.08] a member of the String Set counts as naming the digest."""
    w = World()
    s = digest_of("content-manifest-S")
    a = digest_of("attestation-A")
    sig_a = w.add_image("sig-A", subject=a, artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE)
    _index_row(w, s, validatedAttestationDigests=[digest_of("other"), a])
    c = _classify(w, sig_a)
    assert not c.eligible and not c.check_failed
    assert s in c.reason and a in c.reason


def test_record_without_manifest_digest_attribute_is_named_by_its_key() -> None:
    w = World()
    s = digest_of("content-manifest-S")
    a = digest_of("attestation-A")
    sig_a = w.add_image("sig-A", subject=a, artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE)
    _index_row(w, s, attestationDigest=a)
    del w.items[(METADATA_TABLE, TENANT, f"{PROJECT}#{s}")]["manifestDigest"]
    c = _classify(w, sig_a)
    assert not c.eligible
    assert c.reason == f"subject {a} is named by the index record of {s}"


def test_index_query_error_refuses_the_orphan_as_a_failed_check() -> None:
    """[RETAIN-008.05] the naming-record Query error is not proof of absence."""
    w = World()
    gone = digest_of("gone")
    sig = w.add_image("sig", subject=gone, artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE)
    w.fail("query", METADATA_TABLE, "ThrottlingException")
    c = _classify(w, sig)
    assert not c.eligible
    assert c.check_failed
    assert "Metadata_Index Query" in c.reason and "ThrottlingException" in c.reason


def test_orphan_named_by_no_record_is_still_a_candidate() -> None:
    """[RETAIN-008.03] unrelated records do not block a true orphan."""
    w = World()
    w.add_other_project_row()
    other = digest_of("unrelated-S")
    _index_row(w, other, attestationDigest=digest_of("unrelated-A"))
    gone = digest_of("deleted-subject")
    sig = w.add_image("orphan-sig", subject=gone, artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE)
    index = _tag_index(w, "orphan-index", gone)
    for d in (sig, index):
        c = _classify(w, d)
        assert c.eligible, c.reason
        assert c.item_class is ItemClass.UNARCHIVED_MANIFEST


def _list_in_index(w: World, index: str, *children: str) -> None:
    """Set the ``manifests[]`` of a tag-schema index (the digest key is not recomputed)."""
    w.images[index].manifest["manifests"] = [
        {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": d, "size": 1}
        for d in children
    ]


def test_index_with_two_referrers_tag_schema_tags_is_refused() -> None:
    """An added ``sha256-<Z>`` tag cannot redirect the subject of a real fallback index."""
    w = World()
    a = w.add_image("retained-bundle-A")
    gone = digest_of("absent-Z")
    tags = [f"sha256-{a.removeprefix('sha256:')}", f"sha256-{gone.removeprefix('sha256:')}"]
    index = w.add_image("fallback-index", media_type=OCI_IMAGE_INDEX_MEDIA_TYPE, tags=tags)
    c = _classify(w, index)
    assert not c.eligible
    assert not c.check_failed
    assert c.item_class is None
    for tag in tags:
        assert tag in c.reason


def test_tag_schema_index_listing_a_present_manifest_is_refused() -> None:
    w = World()
    gone = digest_of("absent-subject")
    sig = w.add_image(
        "live-sig", subject=digest_of("live"), artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE
    )
    index = _tag_index(w, "orphan-index", gone)
    _list_in_index(w, index, sig)
    c = _classify(w, index)
    assert not c.eligible and not c.check_failed
    assert c.reason == f"index lists {sig}, which is present in ECR"


def test_tag_schema_index_listing_a_digest_named_by_a_record_is_refused() -> None:
    w = World()
    s = digest_of("content-manifest-S")
    sig_s = digest_of("signature-of-S")
    _index_row(w, s, signatureDigest=sig_s)
    index = _tag_index(w, "orphan-index", digest_of("absent-subject"))
    _list_in_index(w, index, sig_s)
    c = _classify(w, index)
    assert not c.eligible and not c.check_failed
    assert c.reason == f"index lists {sig_s}, which is named by the index record of {s}"


def test_tag_schema_index_child_describe_error_is_a_failed_check() -> None:
    """[RETAIN-008.05] a DescribeImages error on a listed digest is not proof of absence."""
    w = World()
    index = _tag_index(w, "orphan-index", digest_of("absent-subject"))
    child = digest_of("unreadable-child")
    _list_in_index(w, index, child)
    w.fail("describe_images", child, "ThrottlingException")
    c = _classify(w, index)
    assert not c.eligible and c.check_failed
    assert "ECR DescribeImages" in c.reason and child in c.reason


def test_tag_schema_index_with_a_malformed_manifests_entry_is_refused() -> None:
    w = World()
    index = _tag_index(w, "orphan-index", digest_of("absent-subject"))
    w.images[index].manifest["manifests"] = [{"digest": "not-a-digest"}]
    c = _classify(w, index)
    assert not c.eligible
    assert "manifests[]" in c.reason


def _malformed_row(w: World, s: str, name: str, value: dict[str, Any]) -> None:
    _index_row(w, s)
    w.items[(METADATA_TABLE, TENANT, f"{PROJECT}#{s}")][name] = value


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("attestationDigest", {"SS": [digest_of("attestation-A")]}),
        ("attestationDigest", {"S": "not-a-digest"}),
        ("validatedAttestationDigests", {"S": digest_of("attestation-A")}),
        ("validatedAttestationDigests", {"SS": [digest_of("ok"), "not-a-digest"]}),
        ("validatedAttestationDigests", {"L": [{"S": digest_of("attestation-A")}]}),
    ],
)
def test_malformed_naming_attribute_refuses_the_orphan_as_a_failed_check(
    name: str, value: dict[str, Any]
) -> None:
    """[RETAIN-008.05] a malformed naming attribute is not skipped."""
    w = World()
    s = digest_of("content-manifest-S")
    _malformed_row(w, s, name, value)
    sig = w.add_image(
        "orphan-sig", subject=digest_of("gone"), artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE
    )
    c = _classify(w, sig)
    assert not c.eligible
    assert c.check_failed
    assert f"Metadata_Index Query failed: malformed {name} in {PROJECT}#{s}" in c.reason


def test_run_cache_reads_the_naming_records_once_per_run() -> None:
    w = World()
    gone = digest_of("deleted-subject")
    orphans = [
        w.add_image(f"orphan-{i}", subject=gone, artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE)
        for i in range(3)
    ]
    clients = w.clients()
    calls: list[dict[str, Any]] = []
    real_query = clients.ddb.query

    def counting_query(**kw: Any) -> dict[str, Any]:
        calls.append(kw)
        return real_query(**kw)

    clients.ddb.query = counting_query
    cache = RunCache()
    for d in orphans:
        c = classify(clients, w.deployment(), d, now=NOW, coordinates_unproven=None, cache=cache)
        assert c.eligible, c.reason
    assert len(calls) == 1
    assert calls[0]["ConsistentRead"] is True
    assert calls[0]["ExpressionAttributeValues"][":sk"] == {"S": f"{PROJECT}#"}


def test_naming_records_are_read_across_every_page() -> None:
    w = World()
    s = digest_of("content-manifest-S")
    a = digest_of("attestation-A")
    sig_a = w.add_image("sig-A", subject=a, artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE)
    clients = w.clients()
    pages = [
        {"Items": [{"sk": {"S": f"{PROJECT}#{digest_of('x')}"}}], "LastEvaluatedKey": {"k": 1}},
        {
            "Items": [
                {"sk": {"S": f"{PROJECT}#{s}"}, "attestationDigest": {"S": a}},
            ]
        },
    ]
    clients.ddb.query = lambda **kw: pages[1 if "ExclusiveStartKey" in kw else 0]
    c = classify(clients, w.deployment(), sig_a, now=NOW, coordinates_unproven=None)
    assert not c.eligible
    assert c.reason == f"subject {a} is named by the index record of {s}"


def test_subject_prefix_read_error_is_a_failed_check() -> None:
    """[RETAIN-008.05] an error listing the subject's prefix is not proof of absence."""
    w = World()
    w.add_other_project_row()
    gone = digest_of("unreadable-subject")
    sig = w.add_image("orphan-sig", subject=gone, artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE)
    w.fail("list_object_versions", _subject_prefix(gone), "ThrottlingException")
    c = _classify(w, sig)
    assert not c.eligible
    assert c.check_failed
    assert "S3 ListObjectVersions" in c.reason
    assert _subject_prefix(gone) in c.reason


def test_orphan_with_every_absence_proven_past_the_window_is_a_candidate() -> None:
    """[RETAIN-008.03] absent subject, empty subject and own prefixes, no index record."""
    w = World()
    gone = digest_of("deleted-subject")
    sig = w.add_image("orphan-sig", subject=gone, artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE)
    assert not w.prefix_versions(gone)
    assert not w.prefix_versions(sig)
    assert (METADATA_TABLE, TENANT, f"{PROJECT}#{sig}") not in w.items
    c = _classify(w, sig)
    assert c.eligible, c.reason
    assert c.item_class is ItemClass.UNARCHIVED_MANIFEST
    assert gone in c.reason


def _forge_dangling_subject(w: World, digest: str) -> str:
    """Give an archived content manifest a producer-written subject that is absent."""
    gone = digest_of("never-pushed-subject")
    w.images[digest].manifest["subject"] = {
        "digest": gone,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "size": 1,
    }
    return gone


def test_archived_bundle_with_a_dangling_subject_is_not_an_orphan() -> None:
    """[RETAIN-008.06] a forged subject cannot route an archived bundle past retention."""
    w = World()
    w.add_other_project_row()
    d = w.add_bundle(archived_at=NOW - timedelta(days=WINDOW + 1))  # retention in force
    _forge_dangling_subject(w, d)
    c = _classify(w, d)
    assert not c.eligible
    assert c.reason.startswith("retention in force until")
    assert w.prefix_versions(d)


def test_expired_bundle_with_a_dangling_subject_is_only_an_expired_bundle() -> None:
    """[RETAIN-008.06] classified under [RETAIN-004], never as an unarchived manifest."""
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    _forge_dangling_subject(w, d)
    c = _classify(w, d)
    assert c.eligible
    assert c.item_class is ItemClass.EXPIRED_BUNDLE
    assert c.s3_versions


def _forge_archived_referrer(w: World, subject: str, *, archived_at: datetime) -> str:
    """An archived bundle B whose producer-written ``subject`` names ``subject``."""
    b = w.add_bundle("bundle-b", archived_at=archived_at)
    w.images[b].manifest["subject"] = {
        "digest": subject,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "size": 1,
    }
    return b


@pytest.mark.parametrize("b_evidence", ["s3-and-index", "s3-only", "index-only"])
def test_expired_bundle_with_an_archived_referrer_is_refused(b_evidence: str) -> None:
    """[RETAIN-012] B (subject=A) under retention keeps A's tree, so B stays intact."""
    # Covers: RETAIN-010.05
    w = World()
    w.add_other_project_row()
    a = w.add_bundle()  # expired
    b = _forge_archived_referrer(w, a, archived_at=NOW - timedelta(days=WINDOW + 1))
    if b_evidence == "s3-only":
        w.items.pop((METADATA_TABLE, TENANT, f"{PROJECT}#{b}"))
    elif b_evidence == "index-only":
        w.versions = [v for v in w.versions if v not in w.prefix_versions(b)]
    c = _classify(w, a)
    assert not c.eligible
    assert b in c.reason
    assert b in c.referrer_digests
    if b_evidence == "index-only":
        assert "Metadata_Index record" in c.reason
    else:
        assert "S3 object versions" in c.reason


def test_archived_referrer_read_error_is_a_failed_check() -> None:
    """[RETAIN-008.05] an error on the referrer's own prefix is not proof of absence."""
    w = World()
    w.add_other_project_row()
    a = w.add_bundle()
    sig = next(
        img.digest
        for img in w.images.values()
        if (img.manifest.get("subject") or {}).get("digest") == a
        and img.manifest.get("artifactType") == NOTARY_SIGNATURE_ARTIFACT_TYPE
    )
    w.fail("list_object_versions", f"{ORG}/{TENANT}/{PROJECT}/{sig}/", "ThrottlingException")
    c = _classify(w, a)
    assert not c.eligible and c.check_failed
    assert "S3 ListObjectVersions" in c.reason


def test_network_error_is_a_failed_check_not_a_crash() -> None:
    """[RETAIN-008.05] a BotoCoreError (no HTTP response) refuses the item."""
    from tests.unit.cairn_cli.prune_fakes import NETWORK_ERROR

    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    w.fail("get_object", f"{ORG}/{TENANT}/{PROJECT}/{d}/worm-receipt.json", NETWORK_ERROR)
    c = _classify(w, d)
    assert not c.eligible and c.check_failed
    assert "EndpointConnectionError" in c.reason


def test_network_error_while_proving_coordinates_is_reported() -> None:
    from tests.unit.cairn_cli.prune_fakes import NETWORK_ERROR

    w = World()
    w.add_other_project_row()
    w.fail("list_objects_v2", "*", NETWORK_ERROR)
    reason = prove_coordinates(w.clients(), w.deployment())
    assert reason and "EndpointConnectionError" in reason


def test_dangling_subject_with_an_index_record_is_not_an_orphan() -> None:
    w = World()
    w.add_other_project_row()
    d = w.add_image("content")
    _forge_dangling_subject(w, d)
    w.add_row(d)
    c = _classify(w, d)
    assert not c.eligible
    assert "Metadata_Index record" in c.reason


def test_orphan_referrer_under_unproven_coordinates_is_refused() -> None:
    w = World()
    sig = w.add_image("orphan-sig", subject=digest_of("gone"), artifact_type="x")
    c = _classify(w, sig, unproven="no S3 object under project prefix")
    assert not c.eligible
    assert c.check_failed
    assert "coordinates unproven" in c.reason


@pytest.mark.parametrize(
    ("method", "check"),
    [("list_object_versions", "S3 ListObjectVersions"), ("get_item", "Metadata_Index GetItem")],
)
def test_orphan_absence_read_error_is_a_failed_check(method: str, check: str) -> None:
    w = World()
    w.add_other_project_row()
    sig = w.add_image("orphan-sig", subject=digest_of("gone"), artifact_type="x")
    key = (
        f"{ORG}/{TENANT}/{PROJECT}/{sig}/"
        if method == "list_object_versions"
        else f"{PROJECT}#{sig}"
    )
    w.fail(method, key, "InternalError")
    c = _classify(w, sig)
    assert not c.eligible
    assert c.check_failed
    assert check in c.reason


def test_referrer_whose_subject_is_present_is_retained() -> None:
    w = World()
    d = w.add_bundle()
    sig = next(
        img.digest
        for img in w.images.values()
        if (img.manifest.get("subject") or {}).get("digest") == d
    )
    assert not _classify(w, sig).eligible


def test_image_index_without_referrers_tag_is_not_a_candidate() -> None:
    w = World()
    d = w.add_image("plain-index", media_type=OCI_IMAGE_INDEX_MEDIA_TYPE, tags=["latest"])
    assert not _classify(w, d).eligible


@pytest.mark.parametrize("kind", ["content", "orphan"])
def test_manifest_inside_the_window_is_retained(kind: str) -> None:
    """[RETAIN-008.04]"""
    w = World()
    w.add_other_project_row()
    recent = NOW - timedelta(days=WINDOW - 1)
    if kind == "content":
        d = w.add_image("recent", pushed_at=recent)
    else:
        d = w.add_image(
            "recent-sig", subject=digest_of("gone"), artifact_type="x", pushed_at=recent
        )
    c = _classify(w, d)
    assert not c.eligible
    assert "window" in c.reason


@pytest.mark.parametrize(
    ("method", "key", "code", "check"),
    [
        ("describe_images", "SUBJECT", "ThrottlingException", "ECR DescribeImages"),
        ("get_item", "ROW", "AccessDeniedException", "Metadata_Index GetItem"),
        ("list_object_versions", "PREFIX", "InternalError", "S3 ListObjectVersions"),
        ("list_image_referrers", "DIGEST", "ThrottlingException", "ECR ListImageReferrers"),
    ],
)
def test_transient_error_is_not_proof_of_absence(method, key, code, check) -> None:
    """[RETAIN-008.05]"""
    w = World()
    w.add_other_project_row()
    gone = digest_of("subject")
    if key == "SUBJECT":
        d = w.add_image("sig", subject=gone, artifact_type="x")
        w.fail(method, gone, code)
    else:
        d = w.add_image("junk")
        target = {
            "ROW": f"{PROJECT}#{d}",
            "PREFIX": f"{ORG}/{TENANT}/{PROJECT}/{d}/",
            "DIGEST": d,
        }[key]
        w.fail(method, target, code)
    c = _classify(w, d)
    assert not c.eligible
    assert c.check_failed
    assert check in c.reason and code in c.reason


def test_unproven_coordinates_refuse_the_unarchived_class() -> None:
    """M11 / M32: absence under wrong coordinates would be vacuous."""
    w = World()
    d = w.add_image("junk")
    c = _classify(w, d, unproven="no S3 object under project prefix")
    assert not c.eligible
    assert "coordinates unproven" in c.reason


def test_prove_coordinates_requires_positive_finds() -> None:
    w = World()
    assert "no S3 object" in (prove_coordinates(w.clients(), w.deployment()) or "")
    w.put_version(f"{ORG}/{TENANT}/{PROJECT}/sha256:{'f' * 64}/manifest.json")
    assert "no Metadata_Index record" in (prove_coordinates(w.clients(), w.deployment()) or "")
    w.add_other_project_row()
    assert prove_coordinates(w.clients(), w.deployment()) is None
    w.fail("describe_repositories", w.deployment().repository_name, "RepositoryNotFoundException")
    assert "not found" in (prove_coordinates(w.clients(), w.deployment()) or "")


def test_project_digests_unions_ecr_images_and_s3_prefixes() -> None:
    w = World()
    d = w.add_bundle()
    w.images.pop(d)
    junk = w.add_image("junk")
    found = project_digests(w.clients(), w.deployment())
    assert d in found and junk in found
    assert set(found) >= set(w.images)


def test_project_digests_finds_delete_marker_only_prefixes_and_index_only_remnants() -> None:
    """[RETAIN-005] item 1: remnants of an interrupted prune are reported too."""
    w = World()
    markers_only = digest_of("markers-only")
    w.put_version(f"{ORG}/{TENANT}/{PROJECT}/{markers_only}/manifest.json", delete_marker=True)
    index_only = digest_of("index-only")
    w.add_row(index_only, archived_at=NOW - timedelta(days=PERIOD + 1))
    w.add_row(digest_of("other-project-row"))
    w.items[(METADATA_TABLE, TENANT, "other-project#" + digest_of("x"))] = {
        "pk": {"S": TENANT},
        "sk": {"S": "other-project#" + digest_of("x")},
    }
    found = project_digests(w.clients(), w.deployment())
    assert markers_only in found
    assert index_only in found
    assert digest_of("x") not in found


def test_project_digests_surfaces_an_index_query_error() -> None:
    from cairn_cli._prune_eligibility import CheckError

    w = World()
    w.fail("query", METADATA_TABLE, "ProvisionedThroughputExceededException")
    with pytest.raises(CheckError, match="Metadata_Index Query"):
        project_digests(w.clients(), w.deployment())


def test_project_digests_surfaces_an_enumeration_error() -> None:
    from cairn_cli._prune_eligibility import CheckError

    w = World()
    w.add_bundle()
    clients = w.clients()

    def boom(name: str):
        from tests.unit.cairn_cli.prune_fakes import client_error

        raise client_error("AccessDeniedException", "ListImages")

    clients.ecr.get_paginator = boom  # type: ignore[method-assign]
    with pytest.raises(CheckError, match="ECR ListImages"):
        project_digests(clients, w.deployment())


# ── RFC 3339 parsing (trust boundary) ────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2033-05-24T12:00:00Z", datetime(2033, 5, 24, 12, tzinfo=UTC)),
        ("2033-05-24T12:00:00.5+00:00", datetime(2033, 5, 24, 12, 0, 0, 500000, tzinfo=UTC)),
        ("2033-05-24T12:00:00+01:00", None),
        ("2033-05-24 12:00:00Z", None),
        ("2033-02-30T00:00:00Z", None),
        ("\x1b[31m2033-05-24T12:00:00Z", None),
        (12345, None),
        (None, None),
    ],
)
def test_parse_rfc3339_utc_is_strict(value, expected) -> None:
    assert parse_rfc3339_utc(value) == expected


def test_rfc_helper_round_trip() -> None:
    assert parse_rfc3339_utc(rfc(NOW)) == NOW


# ── pagination, failed reads, and boundaries (tester gap fill) ────────────────


def _paged_referrers(w: World):
    """Clients whose ListImageReferrers returns one referrer per page (nextToken)."""
    clients = w.clients()
    real = clients.ecr.list_image_referrers

    def paged(*, repositoryName: str, subjectId: dict, nextToken: str | None = None, **_):
        refs = real(repositoryName=repositoryName, subjectId=subjectId)["referrers"]
        i = int(nextToken or 0)
        page: dict = {"referrers": refs[i : i + 1]}
        if i + 1 < len(refs):
            page["nextToken"] = str(i + 1)
        return page

    clients.ecr.list_image_referrers = paged  # type: ignore[method-assign]
    return clients


def _paged_versions(w: World):
    """Clients whose ListObjectVersions returns one entry per page (IsTruncated)."""
    clients = w.clients()
    real = clients.s3.list_object_versions

    def paged(*, Bucket: str, Prefix: str, KeyMarker: str | None = None, **kw):
        full = real(Bucket=Bucket, Prefix=Prefix)
        entries = [("Versions", v) for v in full["Versions"]] + [
            ("DeleteMarkers", m) for m in full["DeleteMarkers"]
        ]
        i = int(KeyMarker or 0)
        page: dict = {"Versions": [], "DeleteMarkers": [], "IsTruncated": i + 1 < len(entries)}
        if entries[i : i + 1]:
            kind, entry = entries[i]
            page[kind].append(entry)
        if page["IsTruncated"]:
            page["NextKeyMarker"] = str(i + 1)
            page["NextVersionIdMarker"] = "x"
        return page

    clients.s3.list_object_versions = paged  # type: ignore[method-assign]
    return clients


def test_receipt_referrer_on_a_later_referrers_page_is_counted() -> None:
    """[RETAIN-012.02] / M33: a decoy receipt on page 2 still refuses the item."""
    from cairn_oci import RETAIN_UNTIL_ANNOTATION, WORM_RECEIPT_ARTIFACT_TYPE

    w = World()
    d = w.add_bundle()
    decoy = w.add_image(
        "decoy",
        subject=d,
        artifact_type=WORM_RECEIPT_ARTIFACT_TYPE,
        annotations={RETAIN_UNTIL_ANNOTATION: "2000-01-01T00:00:00Z"},
    )
    c = classify(_paged_referrers(w), w.deployment(), d, now=NOW, coordinates_unproven=None)
    assert not c.eligible
    assert c.reason.startswith("multiple WORM_Receipt referrers")
    assert decoy in c.reason


def test_referrer_tree_collected_across_pages() -> None:
    w = World()
    d = w.add_bundle()
    c = classify(_paged_referrers(w), w.deployment(), d, now=NOW, coordinates_unproven=None)
    assert c.eligible, c.reason
    assert len(c.referrer_digests) == 5


def test_locked_version_on_a_later_page_refuses_under_worm() -> None:
    """[RETAIN-004.03] every page of ListObjectVersions is read for Object Lock dates."""
    w = World(worm=True)
    d = w.add_bundle()
    w.prefix_versions(d)[-1].retain_until = NOW + timedelta(days=1)
    c = classify(_paged_versions(w), w.deployment(), d, now=NOW, coordinates_unproven=None)
    assert not c.eligible
    assert "retention in force" in c.reason
    assert len(c.s3_versions) == 4


def test_retention_expires_exactly_at_retain_until() -> None:
    """[RETAIN-004.01] eligible at Retain_Until; one second earlier it is retained."""
    w = World()
    d = w.add_bundle(retain_until=NOW)
    assert classify(w.clients(), w.deployment(), d, now=NOW, coordinates_unproven=None).eligible
    early = classify(
        w.clients(), w.deployment(), d, now=NOW - timedelta(seconds=1), coordinates_unproven=None
    )
    assert not early.eligible
    assert early.retain_until == NOW


def test_worm_object_lock_later_than_receipt_value_wins() -> None:
    """[RETAIN-012] the authoritative date is the latest of receipt and Object Lock dates."""
    w = World(worm=True)
    d = w.add_bundle(retain_until=NOW - timedelta(days=1), lock_until=NOW + timedelta(days=3))
    c = _classify(w, d)
    assert not c.eligible
    assert c.retain_until == NOW + timedelta(days=3)


@pytest.mark.parametrize("body", [b"{not json", b"[1, 2]"])
def test_unparseable_s3_receipt_copy_is_a_failed_check(body: bytes) -> None:
    w = World()
    d = w.add_bundle()
    next(v for v in w.prefix_versions(d) if v.key.endswith("worm-receipt.json")).body = body
    c = _classify(w, d)
    assert not c.eligible
    assert c.check_failed
    assert "S3 GetObject" in c.reason


def test_s3_receipt_read_error_is_not_treated_as_absent() -> None:
    """[RETAIN-008.05] AccessDenied on the receipt copy is a failed check, not 'no copy'."""
    w = World()
    d = w.add_bundle()
    w.fail("get_object", f"{ORG}/{TENANT}/{PROJECT}/{d}/worm-receipt.json", "AccessDenied")
    c = _classify(w, d)
    assert not c.eligible
    assert c.check_failed
    assert "AccessDenied" in c.reason
    assert "no S3 receipt copy" not in c.reason


@pytest.mark.parametrize("method", ["get_object_retention", "get_object_legal_hold"])
def test_object_lock_read_error_is_a_failed_check(method: str) -> None:
    """A legal-hold or retention read that errors never counts as 'no hold' / 'no lock'."""
    w = World(worm=True)
    d = w.add_bundle()
    w.fail(method, f"{ORG}/{TENANT}/{PROJECT}/{d}/manifest.json", "AccessDenied")
    c = _classify(w, d)
    assert not c.eligible
    assert c.check_failed
    assert "AccessDenied" in c.reason


def test_malformed_s3_retain_until_refuses() -> None:
    w = World()
    d = w.add_bundle(receipt_in_ecr=False)
    receipt = next(v for v in w.prefix_versions(d) if v.key.endswith("worm-receipt.json"))
    doc = json.loads(receipt.body)
    doc["retainUntil"] = "2000-01-01"
    receipt.body = json.dumps(doc).encode()
    c = _classify(w, d)
    assert not c.eligible
    assert "not RFC 3339 UTC" in c.reason


def test_unparseable_ecr_manifest_is_a_failed_check() -> None:
    w = World()
    w.add_other_project_row()
    d = w.add_image("junk")
    clients = w.clients()
    clients.ecr.batch_get_image = lambda **_: {  # type: ignore[method-assign]
        "images": [{"imageManifest": "{nope", "imageManifestMediaType": "x"}]
    }
    c = classify(clients, w.deployment(), d, now=NOW, coordinates_unproven=None)
    assert not c.eligible
    assert c.check_failed
    assert "ECR BatchGetImage" in c.reason


def test_image_without_push_time_never_leaves_the_window() -> None:
    """[RETAIN-008.04] an unknown push time cannot prove the window elapsed."""
    w = World()
    w.add_other_project_row()
    d = w.add_image("junk")
    clients = w.clients()
    real = clients.ecr.describe_images

    def no_push_time(**kw):
        resp = real(**kw)
        for detail in resp["imageDetails"]:
            detail.pop("imagePushedAt", None)
        return resp

    clients.ecr.describe_images = no_push_time  # type: ignore[method-assign]
    c = classify(clients, w.deployment(), d, now=NOW, coordinates_unproven=None)
    assert not c.eligible
    assert "window" in c.reason


@pytest.mark.parametrize(
    ("method", "key", "expected"),
    [
        ("list_objects_v2", f"{ORG}/{TENANT}/{PROJECT}/", "S3 project prefix"),
        ("query", METADATA_TABLE, "Metadata_Index partition unreadable"),
    ],
)
def test_prove_coordinates_read_error_is_unproven(method: str, key: str, expected: str) -> None:
    w = World()
    w.add_other_project_row()
    w.fail(method, key, "AccessDenied")
    reason = prove_coordinates(w.clients(), w.deployment())
    assert reason is not None
    assert expected in reason and "AccessDenied" in reason


# ── Contract with the archive stage's real receipt ([RETAIN-012], [WORM-005]) ──


def _install_real_receipt(w: World, digest: str, *, archived_at: datetime, period: int) -> None:
    """Replace the fake receipt (S3 copy + ECR annotation) with archive_to_s3's real output."""
    from cairn_lambda_archive_to_s3.receipt import build_receipt, build_receipt_manifest
    from cairn_oci import RETAIN_UNTIL_ANNOTATION, WORM_RECEIPT_ARTIFACT_TYPE

    from tests.unit.cairn_cli.prune_fakes import BUCKET

    prefix = f"{ORG}/{TENANT}/{PROJECT}/{digest}/"
    retain = archived_at + timedelta(days=period)
    retain_str = retain.strftime("%Y-%m-%dT%H:%M:%SZ")
    body = build_receipt(
        manifest_digest=digest,
        archived_at=archived_at.isoformat(),
        s3_bucket=BUCKET,
        s3_key_prefix=prefix,
        archive_operator="arn:aws:sts::111111111111:assumed-role/archive/x",
        worm_enabled=w.worm,
        blob_count=3,
        integrity_verified=True,
        retain_until=retain_str,
    )
    for v in w.versions:
        if v.key == prefix + "worm-receipt.json":
            v.body = body
    manifest = build_receipt_manifest(
        receipt_body=body,
        subject_digest=digest,
        subject_size=1,
        subject_media_type="application/vnd.oci.image.manifest.v1+json",
        retain_until=retain_str,
    )
    for img in w.images.values():
        if (
            img.manifest.get("artifactType") == WORM_RECEIPT_ARTIFACT_TYPE
            and (img.manifest.get("subject") or {}).get("digest") == digest
        ):
            img.manifest["annotations"] = manifest["annotations"]
            assert manifest["annotations"][RETAIN_UNTIL_ANNOTATION] == retain_str


@pytest.mark.parametrize("worm", [False, True])
def test_real_archive_receipt_past_retain_until_is_a_candidate(worm: bool) -> None:
    w = World(worm=worm)
    archived = (NOW - timedelta(days=PERIOD + 1)).replace(microsecond=0)
    d = w.add_bundle(archived_at=archived)
    _install_real_receipt(w, d, archived_at=archived, period=PERIOD)
    c = _classify(w, d)
    assert c.eligible, c.reason
    assert c.item_class is ItemClass.EXPIRED_BUNDLE
    assert c.retain_until == archived + timedelta(days=PERIOD)


@pytest.mark.parametrize("worm", [False, True])
def test_real_archive_receipt_in_retention_is_refused(worm: bool) -> None:
    w = World(worm=worm)
    archived = (NOW - timedelta(days=PERIOD - 1)).replace(microsecond=0)
    d = w.add_bundle(archived_at=archived)
    _install_real_receipt(w, d, archived_at=archived, period=PERIOD)
    c = _classify(w, d)
    assert not c.eligible
    assert not c.check_failed
    assert "retention in force" in c.reason


@pytest.mark.parametrize(
    "archived_at",
    [
        # Fresh archive: second precision, ``+00:00`` (datetime.isoformat()).
        "2014-01-01T00:00:00+00:00",
        # Idempotent short-circuit re-emit: microseconds, ``+00:00``.
        "2014-01-01T00:00:00.123456+00:00",
    ],
)
def test_index_remnant_accepts_the_archive_stage_archived_at_formats(archived_at: str) -> None:
    """update-metadata stores the archive event's archivedAt verbatim."""
    w = World()
    d = w.add_bundle(archived_at=NOW - timedelta(days=PERIOD + 10))
    w.images.clear()
    w.versions.clear()
    w.items[(METADATA_TABLE, TENANT, f"{PROJECT}#{d}")]["archivedAt"] = {"S": archived_at}
    c = _classify(w, d)
    assert c.eligible, c.reason
    assert c.retain_until is not None
    assert c.retain_until >= datetime(2014, 1, 1, tzinfo=UTC) + timedelta(days=PERIOD)


def test_index_remnant_with_a_later_retry_archived_at_is_refused_until_it_passes() -> None:
    """A retry's later archivedAt can only delay eligibility, never advance it."""
    w = World()
    d = w.add_bundle(archived_at=NOW - timedelta(days=PERIOD + 10))
    w.images.clear()
    w.versions.clear()
    later = (NOW - timedelta(days=PERIOD - 1)).isoformat()
    w.items[(METADATA_TABLE, TENANT, f"{PROJECT}#{d}")]["archivedAt"] = {"S": later}
    c = _classify(w, d)
    assert not c.eligible
    assert "retention in force" in c.reason


# ── aggregate-gate tester gap fill ───────────────────────────────────────────


def test_s3_copy_without_retain_until_but_ecr_annotation_present_refuses() -> None:
    """[RETAIN-012.02] presence mismatch is a disagreement, in either direction."""
    w = World()
    d = w.add_bundle(retain_until=None, annotation="2000-01-01T00:00:00Z")
    c = _classify(w, d)
    assert not c.eligible
    assert "disagrees with the S3 receipt copy" in c.reason


def test_ecr_annotation_absent_while_s3_copy_records_retain_until_refuses() -> None:
    """[RETAIN-012.02] a receipt referrer with no annotation disagrees with an S3 value."""
    w = World()
    d = w.add_bundle(annotation=None)
    c = _classify(w, d)
    assert not c.eligible
    assert "disagrees with the S3 receipt copy" in c.reason


@pytest.mark.parametrize("archived_at", ["not-a-date", "2020-01-01T00:00:00+02:00", None])
def test_non_worm_receipt_without_any_usable_timestamp_refuses(archived_at) -> None:
    """[RETAIN-009.02] no retainUntil and no strict archivedAt: Retain_Until is unknown."""
    w = World()
    d = w.add_bundle(retain_until=None)
    receipt = next(v for v in w.prefix_versions(d) if v.key.endswith("worm-receipt.json"))
    doc = json.loads(receipt.body)
    if archived_at is None:
        doc.pop("archivedAt")
    else:
        doc["archivedAt"] = archived_at
    receipt.body = json.dumps(doc).encode()
    c = _classify(w, d)
    assert not c.eligible
    assert "cannot establish Retain_Until" in c.reason
    assert c.item_class is None


def test_worm_receipt_without_retain_until_and_no_locked_version_refuses() -> None:
    """[RETAIN-009.01] under WORM, a version with no Object Lock date refuses; never vacuous."""
    w = World(worm=True)
    d = w.add_bundle(retain_until=None, lock_until=None)
    c = _classify(w, d)
    assert not c.eligible
    assert "no Object Lock retention" in c.reason


def test_orphan_with_a_malformed_subject_digest_is_refused() -> None:
    """[RETAIN-008] item 2: a producer-written subject must be a sha256 digest."""
    w = World()
    w.add_other_project_row()
    d = w.add_image(
        "bad-subject", subject="sha256:NOT-HEX", artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE
    )
    c = _classify(w, d)
    assert not c.eligible
    assert "not a sha256 digest" in c.reason


def test_receipt_copy_naming_a_different_prefix_refuses() -> None:
    """[RETAIN-012.01] the S3 copy's own s3KeyPrefix must equal the derived prefix."""
    w = World()
    d = w.add_bundle()
    receipt = next(v for v in w.prefix_versions(d) if v.key.endswith("worm-receipt.json"))
    doc = json.loads(receipt.body)
    doc["s3KeyPrefix"] = f"{ORG}/{TENANT}/other-project/{d}/"
    receipt.body = json.dumps(doc).encode()
    c = _classify(w, d)
    assert not c.eligible
    assert "different bundle or prefix" in c.reason


def test_classify_rejects_a_non_digest_before_any_aws_call() -> None:
    w = World()
    with pytest.raises(ValueError, match="sha256"):
        classify(w.clients(), w.deployment(), "sha256:*", now=NOW, coordinates_unproven=None)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: _RFC3339_UTC_RE ends in `$` and is applied with re.match, so a value "
        "with a trailing newline parses as a valid timestamp. Use fullmatch or \\Z."
    ),
)
def test_parse_rfc3339_utc_rejects_a_trailing_newline() -> None:
    assert parse_rfc3339_utc("2033-05-24T12:00:00Z\n") is None

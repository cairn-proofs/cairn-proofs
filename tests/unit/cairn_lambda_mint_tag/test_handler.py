"""Tests for the mint-tag Lambda handler.

Covers, for the SINGLE nameless canonical tag form
``{artifact-kind}--_ref--{digest}``:

- builds the 3-segment form from the durable validate record's artifact-kind
  plus the Manifest_Digest prefix only; carries no producer free text and
  never consults a sequence counter;
- every kind-bearing record mints — there is no untagged outcome;
- same-digest re-drive is an idempotent no-op;
- a distinct-digest tag collision fails closed with a digest-naming message
  and NO producer free-text echo;
- the archived-but-untagged failure mode (metric + tag-failed + re-drive).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError

from tests.unit.cairn_lambda_mint_tag.conftest import (
    DIGEST,
    DIGEST_PREFIX,
    NAME,
    ORG_ID,
    OTHER_DIGEST,
    PROJECT_ID,
    TENANT_ID,
    archived_event,
    row_key,
    seed_validate_record,
)

EXPECTED_TAG = f"catalog--_ref--{DIGEST_PREFIX}"


def _emitted(handler: Any, detail_type: str) -> list[dict[str, Any]]:
    return [
        json.loads(e["Detail"])
        for e in handler._test_recorder.entries
        if e["DetailType"] == detail_type
    ]


# ---------------------------------------------------------------------------
# Builds the 3-segment nameless form from the record's
# artifact-kind + the Manifest_Digest prefix.
# ---------------------------------------------------------------------------


def test_builds_single_ref_form_from_record(reload_handler: Any, lambda_context: Any) -> None:
    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind="catalog")

    result = handler.lambda_handler(archived_event(), lambda_context)

    assert result["imageTag"] == EXPECTED_TAG
    assert result["newlyApplied"] is True
    # Exactly 3 segments: artifact-kind, reserved _ref token, digest prefix — no
    # name segment, no producer free text.
    assert result["imageTag"].split("--") == ["catalog", "_ref", DIGEST_PREFIX]
    # PutImage was issued against the existing digest with the canonical tag.
    assert handler._test_fake_ecr.put_image_calls == [{"imageTag": EXPECTED_TAG}]
    tagged = _emitted(handler, "artifact.tagged")
    assert len(tagged) == 1
    assert tagged[0]["manifestDigest"] == DIGEST
    assert tagged[0]["imageTag"] == EXPECTED_TAG


def test_digest_segment_is_manifest_digest_prefix(reload_handler: Any, lambda_context: Any) -> None:
    """The terminal segment is the Manifest_Digest hex prefix — not a sequence,
    not a uuid."""
    from cairn_oci import digest_prefix

    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind="catalog")

    result = handler.lambda_handler(archived_event(), lambda_context)

    assert result["imageTag"].endswith(f"--{digest_prefix(DIGEST)}")
    assert result["imageTag"].split("--")[-1] == DIGEST_PREFIX


# ---------------------------------------------------------------------------
# T-02 — the artifact-kind comes ONLY from the validate record, never the event.
# ---------------------------------------------------------------------------


def test_t02_identity_read_from_record_not_event(reload_handler: Any, lambda_context: Any) -> None:
    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind="catalog")
    # The event detail tries to assert a DIFFERENT (lying) artifact-kind and a
    # stray name — both ignored; mint reads neither from the event.
    lying = archived_event(
        detail={
            "configBlob": {
                "projectId": PROJECT_ID,
                "artifactKind": "governance-doc",
                "name": "evil",
            }
        }
    )
    result = handler.lambda_handler(lying, lambda_context)
    # Built from the RECORD's "catalog", not the event's lying configBlob; and no
    # "evil" free text reaches the (nameless) tag.
    assert result["imageTag"] == EXPECTED_TAG
    assert "evil" not in result["imageTag"]


def test_tagged_payload_carries_identity_triple(reload_handler: Any, lambda_context: Any) -> None:
    """The terminal artifact.tagged event carries the opaque identity triple
    (orgId/tenantId/projectId) sourced from the claim projection."""
    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind="catalog")

    result = handler.lambda_handler(archived_event(), lambda_context)

    assert result["tenantId"] == TENANT_ID
    assert result["projectId"] == PROJECT_ID
    assert result["orgId"] == ORG_ID
    tagged = _emitted(handler, "artifact.tagged")[0]
    assert tagged["tenantId"] == TENANT_ID
    assert tagged["projectId"] == PROJECT_ID
    assert tagged["orgId"] == ORG_ID
    # No AWS-account coordinate appears.
    assert "accountId" not in tagged


def test_tenant_id_metric_dimension_emitted(
    reload_handler: Any, lambda_context: Any, capsys: Any
) -> None:
    """The per-bundle TenantId dimension lands on the mint EMF line (design item 8)."""
    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind="catalog")

    handler.lambda_handler(archived_event(), lambda_context)

    docs = [json.loads(line) for line in capsys.readouterr().out.splitlines() if "_aws" in line]
    assert any(d.get("TenantId") == TENANT_ID for d in docs), (
        f"expected TenantId={TENANT_ID!r} on the EMF line; "
        f"saw {[d.get('TenantId') for d in docs]!r}"
    )


def test_missing_validate_record_fails(reload_handler: Any, lambda_context: Any) -> None:
    handler = reload_handler
    with pytest.raises(handler.MintError):
        handler.lambda_handler(archived_event(), lambda_context)
    assert _emitted(handler, "artifact.tag-failed")


def test_unvalidated_record_refuses_mint(reload_handler: Any, lambda_context: Any) -> None:
    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind="catalog", validate_status="pending")
    with pytest.raises(handler.MintError):
        handler.lambda_handler(archived_event(), lambda_context)


# ---------------------------------------------------------------------------
# 3.1 / 3.3 — every kind-bearing record mints; there is NO untagged path, and a
# record that put a name on it is tagged identically (the name is ignored).
# ---------------------------------------------------------------------------


def test_record_with_stray_name_mints_nameless_tag(
    reload_handler: Any, lambda_context: Any
) -> None:
    """A record carrying a stray ``name`` attribute mints the same nameless
    canonical tag — the name is neither read nor encoded. There is no
    "no derivable name → untagged" outcome to model."""
    handler = reload_handler
    # Seed the row directly with a stray name; mint must ignore it entirely.
    handler._test_table.put_item(
        Item={
            **row_key(DIGEST),
            "bundleTag": "pending",
            "artifactKind": "catalog",
            "name": NAME,
            "validateStatus": "validated",
            "createdAt": "2026-06-01T00:00:00Z",
        }
    )

    result = handler.lambda_handler(archived_event(), lambda_context)

    assert result["imageTag"] == EXPECTED_TAG
    assert result["newlyApplied"] is True
    assert "skipped" not in result  # no skip/untagged outcome exists anymore
    assert NAME not in result["imageTag"]
    assert _emitted(handler, "artifact.tagged")


@pytest.mark.parametrize("kind", ["catalog", "profile", "governance-doc", "assessment-results"])
def test_every_kind_bearing_record_mints(
    reload_handler: Any, lambda_context: Any, kind: str
) -> None:
    """Every record carrying an artifactKind mints the nameless form — the tag is
    a pure function of (artifact-kind, digest-prefix) with no untagged branch."""
    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind=kind)

    result = handler.lambda_handler(archived_event(), lambda_context)

    assert result["imageTag"] == f"{kind}--_ref--{DIGEST_PREFIX}"
    assert result["newlyApplied"] is True
    assert "skipped" not in result


def test_missing_artifact_kind_fails(reload_handler: Any, lambda_context: Any) -> None:
    handler = reload_handler
    # A record that somehow lost artifactKind cannot mint — artifact-kind is the
    # sole record-sourced tag input and is mandatory.
    handler._test_table.put_item(
        Item={
            **row_key(DIGEST),
            "validateStatus": "validated",
            "createdAt": "2026-06-01T00:00:00Z",
        }
    )
    with pytest.raises(handler.MintError):
        handler.lambda_handler(archived_event(), lambda_context)


# ---------------------------------------------------------------------------
# 3.3 / R5 — same-digest re-drive on an already-tagged digest is a no-op.
# ---------------------------------------------------------------------------


def test_redrive_on_already_tagged_digest_is_noop(reload_handler: Any, lambda_context: Any) -> None:
    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind="catalog")

    first = handler.lambda_handler(archived_event(), lambda_context)
    assert first["newlyApplied"] is True
    put_calls_after_first = len(handler._test_fake_ecr.put_image_calls)

    # Re-drive: the digest already carries the canonical tag → short-circuit,
    # no new PutImage, idempotent (anchored on the DIGEST, not the tag).
    second = handler.lambda_handler(archived_event(), lambda_context)
    assert second["newlyApplied"] is False
    assert len(handler._test_fake_ecr.put_image_calls) == put_calls_after_first
    assert second["imageTag"] == first["imageTag"]


def test_alias_tag_on_digest_does_not_short_circuit_canonical_mint(
    reload_handler: Any, lambda_context: Any
) -> None:
    """A digest that already carries a mutable alias tag (_als/_chn) but NO
    canonical _ref tag must still be minted: the alias is never the canonical
    address, so the existing-tag short-circuit must skip it.

    Regression for the grammar-widening seam — `parse_tag` now also accepts the
    `_als`/`_chn` alias forms, so a naive "first tag that parses" short-circuit
    would emit the mutable alias as the canonical imageTag.
    """
    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind="catalog")
    # The digest already carries an alias tag (e.g. an operator aliased it on a
    # prior, since-redriven run) but the canonical mint has not landed yet.
    handler._test_fake_ecr.tag_to_digest["catalog--_chn--latest"] = DIGEST

    result = handler.lambda_handler(archived_event(), lambda_context)

    # Not short-circuited: the canonical _ref tag is minted, not the alias.
    assert result["newlyApplied"] is True
    assert result["imageTag"] == EXPECTED_TAG
    assert result["imageTag"] != "catalog--_chn--latest"
    assert any(c["imageTag"] == EXPECTED_TAG for c in handler._test_fake_ecr.put_image_calls)


def test_concurrent_peer_same_digest_is_idempotent(
    reload_handler: Any, lambda_context: Any
) -> None:
    """A peer that wins the put_image race on the SAME digest → tag-immutability
    rejection → idempotent no-op, not a failure."""
    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind="catalog")
    # Simulate the race: the existing-tag short-circuit misses (peer applied
    # between our check and our put), so put_image is issued and rejected by tag
    # immutability — but digest_for_tag resolves the tag to the SAME digest, so
    # it is a benign idempotent no-op, not a tag collision.
    handler._test_fake_ecr.put_raises = ClientError(
        error_response={
            "Error": {"Code": "ImageTagAlreadyExistsException", "Message": "tag exists"}
        },
        operation_name="PutImage",
    )
    handler._test_fake_ecr.tag_to_digest[EXPECTED_TAG] = DIGEST

    result = handler.lambda_handler(archived_event(), lambda_context)
    assert result["newlyApplied"] is False
    assert not _emitted(handler, "artifact.tag-failed")
    assert _emitted(handler, "artifact.tagged")


def test_immutability_reject_with_unmapped_tag_redrives_not_tagged(
    reload_handler: Any, lambda_context: Any
) -> None:
    """A tag-immutability reject where the tag maps to NO digest (a transient race
    between the reject and the lookup) must re-raise — never emit artifact.tagged
    for a tag ECR is not actually serving. Re-drive is idempotent on the digest."""
    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind="catalog")
    # Reject as already-exists, but leave the tag mapped nowhere (digest_for_tag
    # → None): an ambiguous transient state, not a confirmed same-digest no-op.
    handler._test_fake_ecr.put_raises = ClientError(
        error_response={
            "Error": {"Code": "ImageTagAlreadyExistsException", "Message": "tag exists"}
        },
        operation_name="PutImage",
    )
    # NB: EXPECTED_TAG intentionally NOT seeded into tag_to_digest.

    with pytest.raises(ClientError):
        handler.lambda_handler(archived_event(), lambda_context)
    # No success claimed: no artifact.tagged emitted.
    assert not _emitted(handler, "artifact.tagged")
    assert _emitted(handler, "artifact.tag-failed")


# ---------------------------------------------------------------------------
# Distinct-digest tag collision fails closed.
# ---------------------------------------------------------------------------


def test_tag_collision_distinct_digest_fails_closed(
    reload_handler: Any, lambda_context: Any
) -> None:
    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind="catalog")
    # The canonical tag already points at a DIFFERENT digest (collision / forgery).
    handler._test_fake_ecr._pre_tag(EXPECTED_TAG, OTHER_DIGEST)

    with pytest.raises(handler.TagCollisionError) as exc_info:
        handler.lambda_handler(archived_event(), lambda_context)

    message = str(exc_info.value)
    assert "different digest" in message
    # Names the colliding DIGEST (content hash) so an operator can triage.
    assert OTHER_DIGEST in message
    assert DIGEST in message
    # MUST NOT echo the producer name free text.
    assert NAME not in message

    # Failure surfaced via the metric/tag-failed path; artifact stays addressable.
    failed = _emitted(handler, "artifact.tag-failed")
    assert len(failed) == 1
    assert failed[0]["manifestDigest"] == DIGEST
    # The tag-failed mintError carries the digest-naming message, no free text.
    assert NAME not in failed[0]["mintError"]
    assert OTHER_DIGEST in failed[0]["mintError"]


def test_tag_collision_failure_metric(
    reload_handler: Any, lambda_context: Any, capsys: Any
) -> None:
    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind="catalog")
    handler._test_fake_ecr._pre_tag(EXPECTED_TAG, OTHER_DIGEST)

    with pytest.raises(handler.TagCollisionError):
        handler.lambda_handler(archived_event(), lambda_context)

    out = capsys.readouterr().out
    assert "TagMintFailure" in out


def test_collision_leaves_colliding_artifacts_digest_addressable(
    reload_handler: Any, lambda_context: Any
) -> None:
    """Spec scenario 5 tail: after a fail-closed collision the SECOND artifact
    stays durable + digest-addressable AND the pre-existing (first) artifact's
    canonical tag mapping is left untouched.

    The collision is the tag-immutability rejection itself — ``put_image`` never
    overwrote the existing mapping — so ``EXPECTED_TAG`` must still resolve to the
    FIRST artifact's ``OTHER_DIGEST``, and no PutImage may have mutated state. The
    second artifact remains reachable by its own ``DIGEST`` (the durable address);
    its row was never deleted."""
    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind="catalog")
    handler._test_fake_ecr._pre_tag(EXPECTED_TAG, OTHER_DIGEST)

    with pytest.raises(handler.TagCollisionError):
        handler.lambda_handler(archived_event(), lambda_context)

    # The pre-existing artifact's canonical tag was NOT clobbered by the rejected
    # put — the first artifact stays addressable via the same tag.
    assert handler._test_fake_ecr.tag_to_digest[EXPECTED_TAG] == OTHER_DIGEST
    # The second (colliding) artifact stays durable + digest-addressable: its
    # validate row survives, so it remains reachable by its own content digest.
    row = handler._test_table.get_item(Key=row_key(DIGEST)).get("Item")
    assert row is not None
    assert row["artifactKind"] == "catalog"


def test_same_digest_prefix_different_kind_does_not_collide(
    reload_handler: Any, lambda_context: Any
) -> None:
    """Collision is keyed on the FULL ``(artifact-kind, digest-prefix)`` tuple,
    not the digest-prefix alone (the narrowed-entropy tuple).

    A neighbor canonical tag that shares the SAME digest-prefix but carries a
    DIFFERENT artifact-kind (``profile--_ref--aaaaaaaaaaaa`` vs the record's
    ``catalog--_ref--aaaaaaaaaaaa``) is a DISTINCT tag string, so it must NOT make
    the catalog mint fail closed: the kind is the leading segment, so the two
    artifacts get disjoint addresses. A regression that dropped the kind from the
    collision key (keying on digest-prefix alone) would wrongly reject here."""
    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind="catalog")
    # A same-prefix neighbor under a DIFFERENT kind already points elsewhere.
    neighbor_tag = f"profile--_ref--{DIGEST_PREFIX}"
    assert neighbor_tag != EXPECTED_TAG
    assert neighbor_tag.split("--")[-1] == EXPECTED_TAG.split("--")[-1]  # identical prefix
    handler._test_fake_ecr._pre_tag(neighbor_tag, OTHER_DIGEST)

    # The catalog mint proceeds: its tag string differs from the neighbor's, so
    # the (kind, digest-prefix) tuple does not collide.
    result = handler.lambda_handler(archived_event(), lambda_context)

    assert result["imageTag"] == EXPECTED_TAG
    assert result["newlyApplied"] is True
    assert not _emitted(handler, "artifact.tag-failed")
    assert _emitted(handler, "artifact.tagged")
    # The neighbor's mapping is untouched; the catalog tag landed on its own digest.
    assert handler._test_fake_ecr.tag_to_digest[neighbor_tag] == OTHER_DIGEST
    assert handler._test_fake_ecr.tag_to_digest[EXPECTED_TAG] == DIGEST


# ---------------------------------------------------------------------------
# R5 — transient mint-failure surfaces a metric + tag-failed; re-drivable.
# ---------------------------------------------------------------------------


def test_mint_failure_emits_metric_and_tag_failed(
    reload_handler: Any, lambda_context: Any, capsys: Any
) -> None:
    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind="catalog")
    handler._test_fake_ecr.put_raises = ClientError(
        error_response={"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        operation_name="PutImage",
    )
    with pytest.raises(ClientError):
        handler.lambda_handler(archived_event(), lambda_context)

    failed = _emitted(handler, "artifact.tag-failed")
    assert len(failed) == 1
    assert failed[0]["manifestDigest"] == DIGEST

    out = capsys.readouterr().out
    assert "TagMintFailure" in out


def test_mint_failure_then_redrive_succeeds(reload_handler: Any, lambda_context: Any) -> None:
    """After a transient mint failure, re-driving on the SAME digest succeeds
    (idempotent / re-drivable — R5)."""
    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind="catalog")
    handler._test_fake_ecr.put_raises = ClientError(
        error_response={"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        operation_name="PutImage",
    )
    with pytest.raises(ClientError):
        handler.lambda_handler(archived_event(), lambda_context)

    handler._test_fake_ecr.put_raises = None
    result = handler.lambda_handler(archived_event(), lambda_context)
    assert result["newlyApplied"] is True
    assert result["imageTag"] == EXPECTED_TAG


# ---------------------------------------------------------------------------
# Anchor-extraction guards (3.1) — missing addressing anchors fail closed.
# ---------------------------------------------------------------------------


_TRIPLE = {"orgId": ORG_ID, "tenantId": TENANT_ID, "projectId": PROJECT_ID}


@pytest.mark.parametrize(
    "bad_detail",
    [
        # no identity triple (claim projection absent) — tenantId/projectId missing
        {"manifestDigest": DIGEST, "repositoryName": "r", "registryId": "1"},
        # no manifestDigest
        {"repositoryName": "r", "registryId": "1", "attestation": _TRIPLE},
        # no registryId
        {"manifestDigest": DIGEST, "repositoryName": "r", "attestation": _TRIPLE},
    ],
)
def test_missing_anchor_fails(
    reload_handler: Any, lambda_context: Any, bad_detail: dict[str, Any]
) -> None:
    handler = reload_handler
    event = {
        "detail-type": "artifact.archived",
        "time": "2026-06-01T00:00:00Z",
        "detail": bad_detail,
    }
    with pytest.raises(handler.MintError):
        handler.lambda_handler(event, lambda_context)


# ---------------------------------------------------------------------------
# mint->grammar seam (tester gate): the tag mint emits MUST equal the tag the
# grammar authority (build_ref_tag) builds from the SAME recorded artifact-kind
# + the digest prefix. This pins the EXPECTED_TAG f-string against the single
# authority so the two can never silently drift, and proves the emitted tag
# round-trips back through parse_tag ([INGEST-021.09]) on the nameless form.
# ---------------------------------------------------------------------------


def test_minted_tag_equals_grammar_authority_from_record(
    reload_handler: Any, lambda_context: Any
) -> None:
    from cairn_oci import ArtifactLocator, digest_prefix

    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind="catalog")

    result = handler.lambda_handler(archived_event(), lambda_context)

    # The mint's output is exactly what the grammar authority builds from the
    # recorded artifact-kind + the Manifest_Digest prefix — never hand-formatted,
    # and build_ref_tag takes NO name argument.
    authoritative = ArtifactLocator().build_ref_tag(
        artifact_kind="catalog", digest=digest_prefix(DIGEST)
    )
    assert result["imageTag"] == authoritative
    assert result["imageTag"] == EXPECTED_TAG


def test_minted_tag_round_trips_through_parse(reload_handler: Any, lambda_context: Any) -> None:
    """The emitted nameless canonical tag parses back to the same artifact-kind
    and digest prefix with a None name segment ([INGEST-021.09] / grammar contract)."""
    from cairn_oci import ArtifactLocator, digest_prefix

    handler = reload_handler
    seed_validate_record(handler._test_table, artifact_kind="catalog")

    result = handler.lambda_handler(archived_event(), lambda_context)

    parsed = ArtifactLocator().parse_tag(result["imageTag"])
    assert parsed.artifact_kind == "catalog"
    assert parsed.digest == digest_prefix(DIGEST)
    # The canonical _ref form has no name segment.
    assert parsed.name is None
    assert parsed.form == "_ref"

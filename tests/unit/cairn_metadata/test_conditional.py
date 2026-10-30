"""Tests for cairn_metadata.conditional.

Covers [META-007.01] / [META-007.02] — conditional-write race prevention.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from cairn_metadata.conditional import (
    ConditionalUpdateFailed,
    add_to_string_set,
    create_or_merge,
    create_record,
    update_record_if_newer,
)

# ---------------------------------------------------------------------------
# create_record — happy path + [META-007.01] duplicate handling
# ---------------------------------------------------------------------------


def test_create_record_writes_item_on_first_call(dynamodb_table) -> None:
    create_record(
        dynamodb_table,
        {"pk": "alpha-bank", "sk": "sha256:" + "0" * 64, "status": "validated"},
    )
    response = dynamodb_table.get_item(Key={"pk": "alpha-bank", "sk": "sha256:" + "0" * 64})
    assert response["Item"]["status"] == "validated"


def test_property11_duplicate_create_fails_second_writer(dynamodb_table) -> None:
    """[META-007.01] — first writer wins; second writer surfaces ConditionalUpdateFailed."""
    item = {"pk": "alpha-bank", "sk": "sha256:" + "0" * 64, "status": "validated"}
    create_record(dynamodb_table, item)
    with pytest.raises(ConditionalUpdateFailed):
        create_record(dynamodb_table, item)


def test_create_record_reraises_non_conditional_client_errors() -> None:
    table = MagicMock()
    table.put_item.side_effect = ClientError(
        error_response={"Error": {"Code": "ThrottlingException", "Message": "rate limited"}},
        operation_name="PutItem",
    )
    with pytest.raises(ClientError) as excinfo:
        create_record(table, {"pk": "x"})
    assert excinfo.value.response["Error"]["Code"] == "ThrottlingException"


# ---------------------------------------------------------------------------
# create_or_merge — write-once row-completeness (row-completeness invariant)
# ---------------------------------------------------------------------------

_MERGE_SK = "sha256:" + "0" * 64


def test_create_or_merge_creates_row_when_absent(dynamodb_table) -> None:
    create_or_merge(
        dynamodb_table,
        {"pk": "alpha-bank", "sk": _MERGE_SK, "validateStatus": "validated", "name": "cat"},
    )
    item = dynamodb_table.get_item(Key={"pk": "alpha-bank", "sk": _MERGE_SK})["Item"]
    assert item["validateStatus"] == "validated"
    assert item["name"] == "cat"


def test_create_or_merge_fills_only_absent_attrs_on_collision(dynamodb_table) -> None:
    """On collision, complementary attrs are merged; an existing peer/transition
    field is never clobbered."""
    # Stage A wins the create with its marker fields + a mutable transition.
    create_or_merge(
        dynamodb_table,
        {
            "pk": "alpha-bank",
            "sk": _MERGE_SK,
            "validateStatus": "validated",
            "name": "marker-name",
            "signingStatus": "signed",  # peer/transition field already present
        },
    )
    # Stage B contributes its rich payload + (accidentally) a stale value for an
    # attribute Stage A already owns. if_not_exists must leave A's values intact.
    create_or_merge(
        dynamodb_table,
        {
            "pk": "alpha-bank",
            "sk": _MERGE_SK,
            "oscalLayers": ["catalog"],
            "controls": ["ac-1"],
            "name": "OVERWRITE-ATTEMPT",  # already present → must NOT clobber
            "signingStatus": "pending",  # transition field → must NOT regress
        },
    )
    item = dynamodb_table.get_item(Key={"pk": "alpha-bank", "sk": _MERGE_SK})["Item"]
    # B's complementary attrs landed.
    assert item["oscalLayers"] == ["catalog"]
    assert item["controls"] == ["ac-1"]
    # A's pre-existing values survived untouched.
    assert item["name"] == "marker-name"
    assert item["validateStatus"] == "validated"
    assert item["signingStatus"] == "signed"


def test_create_or_merge_repeated_application_is_noop(dynamodb_table) -> None:
    item = {"pk": "alpha-bank", "sk": _MERGE_SK, "validateStatus": "validated", "name": "cat"}
    create_or_merge(dynamodb_table, item)
    create_or_merge(dynamodb_table, item)
    create_or_merge(dynamodb_table, item)
    stored = dynamodb_table.get_item(Key={"pk": "alpha-bank", "sk": _MERGE_SK})["Item"]
    assert stored["validateStatus"] == "validated"
    assert stored["name"] == "cat"


def test_create_or_merge_does_not_disturb_mutable_transition_field(dynamodb_table) -> None:
    """A mutable status already on the row survives a later create_or_merge."""
    dynamodb_table.put_item(
        Item={
            "pk": "alpha-bank",
            "sk": _MERGE_SK,
            "signingStatus": "signed",
            "updatedAt": "2026-05-24T05:00:00Z",
        }
    )
    create_or_merge(
        dynamodb_table,
        {"pk": "alpha-bank", "sk": _MERGE_SK, "validateStatus": "validated"},
    )
    item = dynamodb_table.get_item(Key={"pk": "alpha-bank", "sk": _MERGE_SK})["Item"]
    assert item["signingStatus"] == "signed"  # mutable transition preserved
    assert item["updatedAt"] == "2026-05-24T05:00:00Z"  # not advanced by merge
    assert item["validateStatus"] == "validated"  # complementary marker merged


def test_create_or_merge_reraises_non_conditional_client_errors() -> None:
    table = MagicMock()
    table.put_item.side_effect = ClientError(
        error_response={"Error": {"Code": "ThrottlingException", "Message": "rate limited"}},
        operation_name="PutItem",
    )
    with pytest.raises(ClientError) as excinfo:
        create_or_merge(table, {"pk": "x", "sk": "y", "a": 1})
    assert excinfo.value.response["Error"]["Code"] == "ThrottlingException"


def test_create_or_merge_collision_with_key_only_item_is_clean_noop(dynamodb_table) -> None:
    """A key-only (pk/sk) item on collision contributes NOTHING beyond the key.

    After stripping pk/sk the contributed set is empty, so the merge path MUST
    short-circuit to a no-op rather than emit an UpdateItem with an empty SET
    clause (``UpdateExpression="SET "`` is a malformed expression DynamoDB
    rejects with ValidationException). Guards conditional.py lines 98-100.
    """
    # Seed the colliding row with a peer's field.
    dynamodb_table.put_item(
        Item={"pk": "alpha-bank", "sk": _MERGE_SK, "validateStatus": "validated"}
    )
    # A key-only second touch: PutItem fails the attribute_not_exists(pk)
    # condition, then the merge fallback finds nothing to contribute.
    create_or_merge(dynamodb_table, {"pk": "alpha-bank", "sk": _MERGE_SK})
    item = dynamodb_table.get_item(Key={"pk": "alpha-bank", "sk": _MERGE_SK})["Item"]
    # The pre-existing field is untouched and no spurious attribute appeared.
    assert item["validateStatus"] == "validated"
    assert set(item.keys()) == {"pk", "sk", "validateStatus"}


def test_create_or_merge_collision_key_only_item_does_not_call_update_item() -> None:
    """The key-only-collision short-circuit returns BEFORE issuing any UpdateItem.

    Pins that the empty-contribution branch never reaches ``table.update_item``
    (which with an empty SET clause would be a malformed-expression error). Uses
    a mock so we can assert update_item was never invoked even though moto would
    otherwise mask a missing call.
    """
    table = MagicMock()
    table.put_item.side_effect = ClientError(
        error_response={"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
        operation_name="PutItem",
    )
    create_or_merge(table, {"pk": "alpha-bank", "sk": _MERGE_SK})
    table.update_item.assert_not_called()


def test_create_or_merge_reraises_non_conditional_client_error_on_update_path() -> None:
    """The MERGE (UpdateItem) path also re-raises a non-conditional ClientError.

    The existing re-raise test only covers the PutItem path. A throttle on the
    fallback UpdateItem (after the create collides) must propagate unchanged, not
    be swallowed — a swallowed throttle would silently drop the merge and leave
    the row incomplete.
    """
    table = MagicMock()
    table.put_item.side_effect = ClientError(
        error_response={"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
        operation_name="PutItem",
    )
    table.update_item.side_effect = ClientError(
        error_response={
            "Error": {"Code": "ProvisionedThroughputExceededException", "Message": "no"}
        },
        operation_name="UpdateItem",
    )
    with pytest.raises(ClientError) as excinfo:
        create_or_merge(table, {"pk": "alpha-bank", "sk": _MERGE_SK, "validateStatus": "v"})
    assert excinfo.value.response["Error"]["Code"] == "ProvisionedThroughputExceededException"


def test_create_or_merge_merges_reserved_word_attribute_on_collision(dynamodb_table) -> None:
    """``name`` is a DynamoDB reserved word; the merge path MUST alias it.

    The SET clause references every contributed attribute via an
    ExpressionAttributeNames placeholder (``#a0``…), so a reserved-word attribute
    like ``name`` merges onto a colliding row without a ValidationException. This
    exercises the merge (UpdateItem) branch with ``name`` as the ABSENT attribute
    being filled — the existing collision test only used ``name`` as an
    already-present no-clobber field, never as the merged-on attribute that the
    UpdateExpression must name.
    """
    # A peer wins the create with a field that does NOT include name/artifactKind.
    dynamodb_table.put_item(Item={"pk": "alpha-bank", "sk": _MERGE_SK, "signingStatus": "signed"})
    # validate's marker write collides and must merge `name` (reserved word) +
    # `artifactKind` onto the row via the aliased SET clause.
    create_or_merge(
        dynamodb_table,
        {
            "pk": "alpha-bank",
            "sk": _MERGE_SK,
            "validateStatus": "validated",
            "artifactKind": "assessment-results",
            "name": "test-catalog",
        },
    )
    item = dynamodb_table.get_item(Key={"pk": "alpha-bank", "sk": _MERGE_SK})["Item"]
    assert item["name"] == "test-catalog"  # reserved word merged successfully
    assert item["artifactKind"] == "assessment-results"
    assert item["validateStatus"] == "validated"
    assert item["signingStatus"] == "signed"  # peer field preserved


# ---------------------------------------------------------------------------
# add_to_string_set — accumulating, idempotent membership set
# ---------------------------------------------------------------------------

_SET_SK = "sha256:" + "0" * 64
_ATT_A = "sha256:" + "a" * 64
_ATT_B = "sha256:" + "b" * 64


def test_add_to_string_set_first_add_creates_set_with_single_member(dynamodb_table) -> None:
    add_to_string_set(
        dynamodb_table,
        pk="alpha-bank",
        sk=_SET_SK,
        attr="validatedAttestationDigests",
        values={_ATT_A},
    )
    item = dynamodb_table.get_item(Key={"pk": "alpha-bank", "sk": _SET_SK})["Item"]
    assert item["validatedAttestationDigests"] == {_ATT_A}


def test_add_to_string_set_re_add_same_member_is_idempotent_noop(dynamodb_table) -> None:
    """Re-ADDing the SAME member leaves the set unchanged (one member)."""
    for _ in range(3):
        add_to_string_set(
            dynamodb_table,
            pk="alpha-bank",
            sk=_SET_SK,
            attr="validatedAttestationDigests",
            values={_ATT_A},
        )
    item = dynamodb_table.get_item(Key={"pk": "alpha-bank", "sk": _SET_SK})["Item"]
    assert item["validatedAttestationDigests"] == {_ATT_A}


def test_add_to_string_set_distinct_member_accumulates(dynamodb_table) -> None:
    """ADDing a DISTINCT member accumulates both, without removing the first."""
    add_to_string_set(
        dynamodb_table,
        pk="alpha-bank",
        sk=_SET_SK,
        attr="validatedAttestationDigests",
        values={_ATT_A},
    )
    add_to_string_set(
        dynamodb_table,
        pk="alpha-bank",
        sk=_SET_SK,
        attr="validatedAttestationDigests",
        values={_ATT_B},
    )
    item = dynamodb_table.get_item(Key={"pk": "alpha-bank", "sk": _SET_SK})["Item"]
    assert item["validatedAttestationDigests"] == {_ATT_A, _ATT_B}


def test_add_to_string_set_does_not_disturb_peer_attributes(dynamodb_table) -> None:
    """The ADD touches only the named set; peer/transition fields survive untouched."""
    dynamodb_table.put_item(
        Item={
            "pk": "alpha-bank",
            "sk": _SET_SK,
            "signingStatus": "signed",
            "updatedAt": "2026-05-24T05:00:00Z",
        }
    )
    add_to_string_set(
        dynamodb_table,
        pk="alpha-bank",
        sk=_SET_SK,
        attr="validatedAttestationDigests",
        values={_ATT_A},
    )
    item = dynamodb_table.get_item(Key={"pk": "alpha-bank", "sk": _SET_SK})["Item"]
    assert item["validatedAttestationDigests"] == {_ATT_A}
    assert item["signingStatus"] == "signed"  # peer untouched
    assert item["updatedAt"] == "2026-05-24T05:00:00Z"  # not advanced


def test_add_to_string_set_empty_values_raises_value_error(dynamodb_table) -> None:
    """DynamoDB has no empty String Set; an empty `values` is rejected, not written."""
    with pytest.raises(ValueError, match="non-empty"):
        add_to_string_set(
            dynamodb_table,
            pk="alpha-bank",
            sk=_SET_SK,
            attr="validatedAttestationDigests",
            values=set(),
        )
    # No phantom row was created by the rejected call.
    assert "Item" not in dynamodb_table.get_item(Key={"pk": "alpha-bank", "sk": _SET_SK})


def test_add_to_string_set_empty_values_does_not_call_update_item() -> None:
    """The empty-values guard returns BEFORE issuing any UpdateItem."""
    table = MagicMock()
    with pytest.raises(ValueError, match="non-empty"):
        add_to_string_set(table, pk="x", sk="y", attr="a", values=())
    table.update_item.assert_not_called()


def test_add_to_string_set_reraises_non_conditional_client_errors() -> None:
    table = MagicMock()
    table.update_item.side_effect = ClientError(
        error_response={
            "Error": {"Code": "ProvisionedThroughputExceededException", "Message": "no"}
        },
        operation_name="UpdateItem",
    )
    with pytest.raises(ClientError) as excinfo:
        add_to_string_set(table, pk="a", sk="b", attr="s", values={"m"})
    assert excinfo.value.response["Error"]["Code"] == "ProvisionedThroughputExceededException"


# ---------------------------------------------------------------------------
# update_record_if_newer — happy path + [META-007.02] stale rejection
# ---------------------------------------------------------------------------


def _seed(dynamodb_table, *, pk: str, sk: str, updated_at: str | None = None) -> None:
    item = {"pk": pk, "sk": sk, "status": "validated"}
    if updated_at:
        item["updatedAt"] = updated_at
    dynamodb_table.put_item(Item=item)


def test_update_succeeds_when_event_timestamp_newer(dynamodb_table) -> None:
    pk, sk = "alpha-bank", "sha256:" + "0" * 64
    _seed(dynamodb_table, pk=pk, sk=sk, updated_at="2026-05-24T00:00:00Z")
    update_record_if_newer(
        dynamodb_table,
        pk=pk,
        sk=sk,
        updates={"status": "signed", "signatureDigest": "sha256:abc"},
        event_timestamp="2026-05-24T01:00:00Z",
    )
    item = dynamodb_table.get_item(Key={"pk": pk, "sk": sk})["Item"]
    assert item["status"] == "signed"
    assert item["signatureDigest"] == "sha256:abc"
    assert item["updatedAt"] == "2026-05-24T01:00:00Z"


def test_property11_update_rejected_when_event_timestamp_older(dynamodb_table) -> None:
    """[META-007.02] — out-of-order older event MUST NOT overwrite newer state."""
    pk, sk = "alpha-bank", "sha256:" + "0" * 64
    _seed(dynamodb_table, pk=pk, sk=sk, updated_at="2026-05-24T05:00:00Z")
    with pytest.raises(ConditionalUpdateFailed):
        update_record_if_newer(
            dynamodb_table,
            pk=pk,
            sk=sk,
            updates={"status": "signed"},
            event_timestamp="2026-05-24T01:00:00Z",
        )
    item = dynamodb_table.get_item(Key={"pk": pk, "sk": sk})["Item"]
    assert item["status"] == "validated"  # unchanged
    assert item["updatedAt"] == "2026-05-24T05:00:00Z"


def test_update_succeeds_when_record_has_no_prior_updated_at(dynamodb_table) -> None:
    pk, sk = "alpha-bank", "sha256:" + "0" * 64
    _seed(dynamodb_table, pk=pk, sk=sk, updated_at=None)
    update_record_if_newer(
        dynamodb_table,
        pk=pk,
        sk=sk,
        updates={"status": "signed"},
        event_timestamp="2026-05-24T01:00:00Z",
    )
    item = dynamodb_table.get_item(Key={"pk": pk, "sk": sk})["Item"]
    assert item["status"] == "signed"


def test_update_record_if_newer_rejects_missing_record(dynamodb_table) -> None:
    with pytest.raises(ConditionalUpdateFailed):
        update_record_if_newer(
            dynamodb_table,
            pk="missing",
            sk="sha256:" + "0" * 64,
            updates={"status": "signed"},
            event_timestamp="2026-05-24T01:00:00Z",
        )


def test_update_record_rejects_timestamp_attr_in_updates(dynamodb_table) -> None:
    # API contract: the guard owns updatedAt; caller-supplied value would
    # let a stale event masquerade as newer.
    with pytest.raises(ValueError, match="updatedAt"):
        update_record_if_newer(
            dynamodb_table,
            pk="x",
            sk="y",
            updates={"updatedAt": "spoofed"},
            event_timestamp="2026-05-24T01:00:00Z",
        )


def test_update_record_supports_custom_timestamp_attr_name(dynamodb_table) -> None:
    pk, sk = "alpha-bank", "sha256:" + "0" * 64
    dynamodb_table.put_item(Item={"pk": pk, "sk": sk, "status": "v", "eventAt": "2026-01-01"})
    update_record_if_newer(
        dynamodb_table,
        pk=pk,
        sk=sk,
        updates={"status": "signed"},
        event_timestamp="2026-06-01",
        timestamp_attr="eventAt",
    )
    item = dynamodb_table.get_item(Key={"pk": pk, "sk": sk})["Item"]
    assert item["eventAt"] == "2026-06-01"
    assert item["status"] == "signed"


def test_update_record_reraises_non_conditional_client_errors() -> None:
    table = MagicMock()
    table.update_item.side_effect = ClientError(
        error_response={
            "Error": {"Code": "ProvisionedThroughputExceededException", "Message": "no"}
        },
        operation_name="UpdateItem",
    )
    with pytest.raises(ClientError) as excinfo:
        update_record_if_newer(
            table,
            pk="a",
            sk="b",
            updates={"status": "signed"},
            event_timestamp="2026-05-24T01:00:00Z",
        )
    assert excinfo.value.response["Error"]["Code"] == "ProvisionedThroughputExceededException"

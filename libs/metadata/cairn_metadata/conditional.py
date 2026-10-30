"""Conditional-write semantics for Metadata_Index records ([META-007.01], [META-007.02]).

- Create-on-validated: PutItem with `attribute_not_exists(pk)` — duplicate
  attempts fail rather than overwrite.
- Create-or-merge (write-once merge): UpdateItem with per-attribute
  `if_not_exists` — both validated-event writers contribute their disjoint,
  write-once fields onto the SAME digest-keyed row, converging it to the union
  regardless of create order, never clobbering a present value, and re-applying
  as a true no-op.
- Update-on-signed/archived/signoff: UpdateItem with an event-timestamp
  guard so an out-of-order older event cannot overwrite newer state.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError


class ConditionalUpdateFailed(Exception):
    """Raised when a DynamoDB conditional write's condition expression failed.

    Distinct from generic ClientError so callers can branch on "expected
    contention" (raced peer wrote first) vs "real failure" (network, IAM).
    """


def create_record(table: Any, item: dict[str, Any]) -> None:
    """PutItem with `attribute_not_exists(pk)` ([META-007.01]).

    `table` is a boto3 DynamoDB Table resource. `item` MUST contain the
    table's partition-key attribute under the name `pk`.

    Raises ConditionalUpdateFailed if a record already exists for the same pk;
    re-raises other ClientErrors unchanged.
    """
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ConditionalUpdateFailed(f"record already exists for pk={item.get('pk')!r}") from e
        raise


def create_or_merge(table: Any, item: dict[str, Any]) -> None:
    """Create-if-absent, else merge only-missing attributes ([META-007], row-completeness).

    For WRITE-ONCE fields ONLY. Two distinct pipeline stages contribute
    complementary (non-overlapping) field sets to the same Metadata_Index row
    off the same triggering event; this primitive lets the row converge to the
    UNION of both contributions regardless of which stage wins the create:

    - First touch: ``PutItem`` with ``attribute_not_exists(pk)`` lands every
      attribute (including the ``pk``/``sk`` key) in one write.
    - Collision (``ConditionalCheckFailedException``): fall back to an
      ``UpdateItem`` whose SET clause is ``#a = if_not_exists(#a, :v)`` per
      contributed attribute. Each stage fills only the attributes it owns and
      can NEVER overwrite a value already present — whether written by a peer
      stage or a later mutable transition. The merge is order-independent and
      clobber-safe by construction (no unconditional SET on this path), so
      repeated application is a true no-op.

    Because the SET path is per-attribute ``if_not_exists``, it does NOT
    clobber or advance ``updatedAt`` (or any peer/transition field) unless the
    caller explicitly contributes it as an absent attribute — callers on the
    write-once path must not. The ``pk``/``sk`` key attributes are addressed via
    ``Key=`` on the merge path and are NOT re-written by the SET clause (they
    already exist on the colliding row by definition).

    Do NOT use this for mutable status transitions (``signingStatus`` /
    ``archiveStatus`` and satellites) — those advance over time and need
    last-writer-by-event-time semantics; they stay on
    ``update_record_if_newer`` / ``upsert_record_if_newer``.

    ``table`` is a boto3 DynamoDB Table resource. ``item`` MUST contain the
    table's key attributes under ``pk`` and ``sk``. Re-raises non-conditional
    ``ClientError``s unchanged.
    """
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(pk)",
        )
        return
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise

    # Collision: the row already exists. Merge only the attributes that are
    # still absent, leaving pk/sk and any peer/transition field untouched.
    pk = item["pk"]
    sk = item["sk"]
    contributed = {k: v for k, v in item.items() if k not in ("pk", "sk")}
    if not contributed:
        # Nothing to merge beyond the key — the colliding row already exists.
        return
    set_clauses: list[str] = []
    expr_names: dict[str, str] = {}
    expr_values: dict[str, Any] = {}
    for i, (attr, value) in enumerate(contributed.items()):
        name_ph = f"#a{i}"
        value_ph = f":v{i}"
        set_clauses.append(f"{name_ph} = if_not_exists({name_ph}, {value_ph})")
        expr_names[name_ph] = attr
        expr_values[value_ph] = value

    table.update_item(
        Key={"pk": pk, "sk": sk},
        UpdateExpression="SET " + ", ".join(set_clauses),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


def add_to_string_set(
    table: Any,
    *,
    pk: str,
    sk: str,
    attr: str,
    values: Any,
) -> None:
    """UpdateItem ``ADD #a :vals`` onto a DynamoDB String Set — accumulating, idempotent.

    ``ADD`` on a String Set is set-union semantics: a member already present is a
    true no-op, distinct members accumulate, and the operation touches ONLY the
    named attribute (no peer/transition field is read, advanced, or clobbered).
    There is no event-timestamp guard and no existence precondition — the row is
    created with just the key + the set if it is absent, which is exactly the
    "membership accumulates over re-deliveries" property the caller wants.

    Contrast with the other writers in this module, which this primitive is
    deliberately NOT:

    - :func:`create_record` / :func:`create_or_merge` are WRITE-ONCE
      (``attribute_not_exists`` / per-attribute ``if_not_exists``) — a value
      lands exactly once and a second writer cannot change it. Use those for an
      immutable marker/field.
    - :func:`update_record_if_newer` / :func:`upsert_record_if_newer` are
      MUTABLE-TRANSITION writers gated last-writer-by-event-time — a scalar
      status advances forward and a stale event is rejected. Use those for a
      value that moves through states over time.

    This primitive is for neither: an *accumulating, idempotent membership set*
    where re-recording the same member must be a no-op and recording a new
    member must add it without disturbing prior members or peer attributes.

    ``values`` MUST be a non-empty iterable of strings. DynamoDB cannot store an
    empty String Set, so an empty ``values`` is rejected with ``ValueError``
    rather than silently issuing a malformed write — matching the defensive,
    fail-fast style the module's guards use (e.g. the ``timestamp_attr`` check).

    ``table`` is a boto3 DynamoDB Table resource. This write carries NO
    ``ConditionExpression`` (the ADD is unconditional by design), so DynamoDB
    raises no ``ConditionalCheckFailedException`` here — every ``ClientError``
    propagates unchanged, consistent with the module's other helpers.
    """
    member_set = set(values)
    if not member_set:
        raise ValueError("values must be a non-empty iterable (DynamoDB has no empty String Set)")
    table.update_item(
        Key={"pk": pk, "sk": sk},
        UpdateExpression="ADD #a :vals",
        ExpressionAttributeNames={"#a": attr},
        ExpressionAttributeValues={":vals": member_set},
    )


def update_record_if_newer(
    table: Any,
    *,
    pk: str,
    sk: str,
    updates: dict[str, Any],
    event_timestamp: str,
    timestamp_attr: str = "updatedAt",
) -> None:
    """UpdateItem with event-timestamp guard ([META-007.02]).

    Applies the SET expression for `updates` if and only if
    `event_timestamp > {timestamp_attr}` on the existing record. Used to
    prevent a delayed/retried older event from overwriting newer state.

    Also requires the record to already exist (`attribute_exists(pk)`); if
    you're creating a record, use `create_record` instead.

    Raises ConditionalUpdateFailed when either condition fails — the caller
    decides whether to log-and-drop (stale event) or alert.
    """
    if timestamp_attr in updates:
        raise ValueError(f"{timestamp_attr} is set by the guard; do not pass it in updates")
    set_clauses: list[str] = ["#ts = :event_ts"]
    expr_names: dict[str, str] = {"#ts": timestamp_attr}
    expr_values: dict[str, Any] = {":event_ts": event_timestamp}
    for i, (attr, value) in enumerate(updates.items()):
        name_ph = f"#a{i}"
        value_ph = f":v{i}"
        set_clauses.append(f"{name_ph} = {value_ph}")
        expr_names[name_ph] = attr
        expr_values[value_ph] = value

    try:
        table.update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression="SET " + ", ".join(set_clauses),
            ConditionExpression=(
                "attribute_exists(pk) AND (attribute_not_exists(#ts) OR #ts < :event_ts)"
            ),
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ConditionalUpdateFailed(
                f"record pk={pk!r} sk={sk!r} either missing or has updatedAt >= "
                f"{event_timestamp!r} (stale event ignored)"
            ) from e
        raise


def upsert_record_if_newer(
    table: Any,
    *,
    pk: str,
    sk: str,
    updates: dict[str, Any],
    event_timestamp: str,
    timestamp_attr: str = "updatedAt",
) -> None:
    """UpdateItem with event-timestamp guard but NO existence requirement.

    Used by post-validated event handlers (signing-failed, signed, archived,
    archive-failed) that need to land cleanly even when EventBridge happens
    to deliver them before the originating ``artifact.validated`` event
    reaches the update-metadata Lambda — a real race under at-least-once,
    out-of-order delivery.

    Semantics:
    - If the record exists and its ``timestamp_attr`` is strictly older
      than ``event_timestamp``, the SET expression applies. Same as
      ``update_record_if_newer``.
    - If the record exists but its ``timestamp_attr`` is at least as new,
      raises ``ConditionalUpdateFailed`` (stale event; caller logs + drops).
    - If the record does NOT exist, DynamoDB creates it with just the SET
      fields plus the key. The ``timestamp_attr`` guard's
      ``attribute_not_exists(#ts)`` branch admits the create cleanly.

    The narrow remaining race — ``signing-failed`` creates a stub, then the
    earlier-timestamped ``artifact.validated`` arrives and fails the
    timestamp guard, losing the validated payload's richer fields
    (manifestAnnotations, layers, etc.) — is a known
    limitation. Operators can recover by re-delivering the validated
    event from the DLQ, which forces a newer timestamp.
    """
    if timestamp_attr in updates:
        raise ValueError(f"{timestamp_attr} is set by the guard; do not pass it in updates")
    set_clauses: list[str] = ["#ts = :event_ts"]
    expr_names: dict[str, str] = {"#ts": timestamp_attr}
    expr_values: dict[str, Any] = {":event_ts": event_timestamp}
    for i, (attr, value) in enumerate(updates.items()):
        name_ph = f"#a{i}"
        value_ph = f":v{i}"
        set_clauses.append(f"{name_ph} = {value_ph}")
        expr_names[name_ph] = attr
        expr_values[value_ph] = value

    try:
        table.update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression="SET " + ", ".join(set_clauses),
            ConditionExpression="attribute_not_exists(#ts) OR #ts < :event_ts",
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ConditionalUpdateFailed(
                f"record pk={pk!r} sk={sk!r} has updatedAt >= {event_timestamp!r} "
                "(stale event ignored)"
            ) from e
        raise

"""Tests for cairn_metadata.pagination ([META-009.01] — 100-item cap, full enumeration)."""

from __future__ import annotations

import pytest
from cairn_metadata.pagination import MAX_PAGE_SIZE, Page, PageIterator, paginate_query
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

PK = "alpha-bank"


def _seed_items(table, count: int) -> list[dict[str, object]]:
    items = []
    for i in range(count):
        sk = f"sha256:{i:064x}"
        item = {"pk": PK, "sk": sk, "i": i}
        table.put_item(Item=item)
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# paginate_query — happy path
# ---------------------------------------------------------------------------


def test_paginate_query_returns_items_under_limit(dynamodb_table) -> None:
    _seed_items(dynamodb_table, 5)
    page = paginate_query(
        dynamodb_table,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": PK},
    )
    assert isinstance(page, Page)
    assert len(page.items) == 5
    assert page.next_token is None


def test_paginate_query_emits_next_token_when_more_pages(dynamodb_table) -> None:
    _seed_items(dynamodb_table, 25)
    page = paginate_query(
        dynamodb_table,
        limit=10,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": PK},
    )
    assert len(page.items) == 10
    assert page.next_token is not None


def test_paginate_query_token_round_trips(dynamodb_table) -> None:
    _seed_items(dynamodb_table, 25)
    first = paginate_query(
        dynamodb_table,
        limit=10,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": PK},
    )
    second = paginate_query(
        dynamodb_table,
        page_token=first.next_token,
        limit=10,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": PK},
    )
    # Different items between pages — no overlap.
    first_ids = {item["sk"] for item in first.items}
    second_ids = {item["sk"] for item in second.items}
    assert first_ids.isdisjoint(second_ids)


# ---------------------------------------------------------------------------
# paginate_query — limit guards
# ---------------------------------------------------------------------------


def test_paginate_query_rejects_zero_limit(dynamodb_table) -> None:
    with pytest.raises(ValueError):
        paginate_query(
            dynamodb_table,
            limit=0,
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": PK},
        )


def test_paginate_query_rejects_negative_limit(dynamodb_table) -> None:
    with pytest.raises(ValueError):
        paginate_query(
            dynamodb_table,
            limit=-1,
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": PK},
        )


def test_paginate_query_rejects_limit_above_max(dynamodb_table) -> None:
    """[META-009.01] — page size MUST be capped at 100."""
    with pytest.raises(ValueError):
        paginate_query(
            dynamodb_table,
            limit=MAX_PAGE_SIZE + 1,
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": PK},
        )


def test_paginate_query_accepts_max_page_size_exactly(dynamodb_table) -> None:
    _seed_items(dynamodb_table, 50)
    page = paginate_query(
        dynamodb_table,
        limit=MAX_PAGE_SIZE,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": PK},
    )
    assert len(page.items) == 50


# ---------------------------------------------------------------------------
# PageIterator
# ---------------------------------------------------------------------------


def test_page_iterator_yields_all_items_across_pages(dynamodb_table) -> None:
    seeded = _seed_items(dynamodb_table, 45)
    iterator = PageIterator(
        dynamodb_table,
        limit=10,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": PK},
    )
    seen = list(iterator)
    assert len(seen) == len(seeded)
    seen_ids = {item["sk"] for item in seen}
    expected_ids = {item["sk"] for item in seeded}
    assert seen_ids == expected_ids


def test_page_iterator_empty_table_stops_immediately(dynamodb_table) -> None:
    iterator = PageIterator(
        dynamodb_table,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": PK},
    )
    assert list(iterator) == []


# ---------------------------------------------------------------------------
# [META-009.01] — full enumeration matches concatenated pages with no
# duplicates / no omissions.
# ---------------------------------------------------------------------------


@given(
    count=st.integers(min_value=0, max_value=125),
    limit=st.integers(min_value=1, max_value=MAX_PAGE_SIZE),
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=10,
    deadline=None,
)
def test_property12_pagination_completeness(dynamodb_table, count: int, limit: int) -> None:
    """[META-009.01] — for any (count, limit), iterating page-by-page through
    paginate_query yields exactly the seeded set (no duplicates, no omissions),
    and never exceeds `limit` per page."""
    # Reset table state between examples.
    scan = dynamodb_table.scan()
    for item in scan.get("Items", []):
        dynamodb_table.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
    seeded = _seed_items(dynamodb_table, count)

    collected: list[dict[str, object]] = []
    token: str | None = None
    pages = 0
    while True:
        page = paginate_query(
            dynamodb_table,
            page_token=token,
            limit=limit,
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": PK},
        )
        assert len(page.items) <= limit  # page size cap
        collected.extend(page.items)
        token = page.next_token
        pages += 1
        if token is None:
            break
        # Defensive: avoid infinite loops if the lib regresses.
        assert pages < 1000

    expected_ids = {item["sk"] for item in seeded}
    collected_ids = [item["sk"] for item in collected]
    # No duplicates.
    assert len(collected_ids) == len(set(collected_ids))
    # Full enumeration.
    assert set(collected_ids) == expected_ids


def test_page_iterator_stops_cleanly_after_full_enumeration(dynamodb_table) -> None:
    _seed_items(dynamodb_table, 3)
    iterator = PageIterator(
        dynamodb_table,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": PK},
    )
    items = list(iterator)
    assert len(items) == 3
    with pytest.raises(StopIteration):
        next(iterator)

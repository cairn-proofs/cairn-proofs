"""Pagination with 100-item page size ([META-009.01], [META-010.01]).

DynamoDB's native LastEvaluatedKey is converted into an opaque base64
pagination token for the public API surface, and back to a key on the next
call.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

MAX_PAGE_SIZE = 100

# Cap the decoded JSON payload for a pagination token. Tokens originate from
# the public API surface (eventually an external caller). A 4 KiB ceiling
# fits any legitimate DynamoDB LastEvaluatedKey (pk + sk + GSI keys) by a
# wide margin while bounding the memory / DoS surface for attacker-controlled
# inputs.
_MAX_TOKEN_BYTES = 4096

# Only these keys may appear in a decoded pagination token. DynamoDB ignores
# extras silently, but we reject early so a crafted token cannot probe for
# unintended attribute shapes by way of error messages.
_ALLOWED_TOKEN_KEYS = frozenset({"pk", "sk", "gsi1pk", "gsi1sk"})


class InvalidPaginationToken(ValueError):
    """Raised when a caller-supplied page_token is malformed or out of bounds."""


@dataclass
class Page:
    items: list[dict[str, Any]]
    next_token: str | None  # None when there are no more pages


def _encode_token(last_key: dict[str, Any] | None) -> str | None:
    if not last_key:
        return None
    return base64.urlsafe_b64encode(json.dumps(last_key).encode("utf-8")).decode("ascii")


def _decode_token(token: str | None) -> dict[str, Any] | None:
    """Decode a base64+JSON pagination token with size + shape validation."""
    if not token:
        return None
    try:
        decoded_bytes = base64.urlsafe_b64decode(token.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as e:
        raise InvalidPaginationToken(f"pagination token is not valid base64: {e}") from e
    if len(decoded_bytes) > _MAX_TOKEN_BYTES:
        raise InvalidPaginationToken(
            f"pagination token decodes to {len(decoded_bytes)} bytes, exceeds "
            f"the {_MAX_TOKEN_BYTES}-byte limit"
        )
    try:
        decoded = json.loads(decoded_bytes)
    except json.JSONDecodeError as e:
        raise InvalidPaginationToken(f"pagination token is not valid JSON: {e}") from e
    if not isinstance(decoded, dict):
        raise InvalidPaginationToken(
            f"pagination token decodes to {type(decoded).__name__}; expected dict"
        )
    bad_keys = set(decoded) - _ALLOWED_TOKEN_KEYS
    if bad_keys:
        raise InvalidPaginationToken(
            f"pagination token contains unexpected keys: {sorted(bad_keys)}"
        )
    return decoded


def paginate_query(
    table: Any,
    *,
    page_token: str | None = None,
    limit: int = MAX_PAGE_SIZE,
    **query_kwargs: Any,
) -> Page:
    """Single-page DynamoDB Query with a 100-item cap ([META-009]).

    `table` is a boto3 DynamoDB Table resource. `query_kwargs` is forwarded
    verbatim to `table.query` (KeyConditionExpression, IndexName, etc.).

    Returns a Page whose `next_token` is None when iteration is complete.
    """
    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise ValueError(f"limit must be 1..{MAX_PAGE_SIZE}; got {limit}")
    if page_token:
        query_kwargs["ExclusiveStartKey"] = _decode_token(page_token)
    response = table.query(Limit=limit, **query_kwargs)
    return Page(
        items=response.get("Items", []),
        next_token=_encode_token(response.get("LastEvaluatedKey")),
    )


class PageIterator(Iterator[dict[str, Any]]):
    """Lazy iterator over every record across all pages.

    Use this when the caller wants all items, not raw page boundaries. Yields
    one item per `__next__`; transparently fetches the next page when the
    current page is exhausted.
    """

    def __init__(self, table: Any, *, limit: int = MAX_PAGE_SIZE, **query_kwargs: Any) -> None:
        self._table = table
        self._limit = limit
        self._query_kwargs = query_kwargs
        self._buffer: list[dict[str, Any]] = []
        self._next_token: str | None = None
        self._exhausted = False
        self._started = False

    def _fill(self) -> None:
        if self._exhausted:
            return
        if self._started and self._next_token is None:
            self._exhausted = True
            return
        page = paginate_query(
            self._table,
            page_token=self._next_token,
            limit=self._limit,
            **self._query_kwargs,
        )
        self._buffer.extend(page.items)
        self._next_token = page.next_token
        self._started = True
        if not page.items and self._next_token is None:
            self._exhausted = True

    def __next__(self) -> dict[str, Any]:
        while not self._buffer:
            if self._exhausted:
                raise StopIteration
            self._fill()
        return self._buffer.pop(0)

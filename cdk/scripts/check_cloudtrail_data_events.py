#!/usr/bin/env python3
"""CloudTrail data-event preflight for cdk/scripts/deploy.sh ([ACCESS-004.02], [ACCESS-004.03]).

Reads, on stdin, a JSON array of ``aws cloudtrail get-event-selectors`` results
for the account's ACTIVE trails. Exits 0 when the trails together record:

1. object-version deletes (``DeleteObject``) on the archive bucket, and
2. item deletes (``DeleteItem``, ``BatchWriteItem``, and ``TransactWriteItems``)
   on the Metadata_Index and ControlClaims tables.

Otherwise prints one line per missing data-event selector and exits 1.

The check is conservative: a selector counts only when every field it sets is
one this script understands and the field admits the delete. A selector with a
``Not*`` / ``EndsWith`` operator on a relevant field, or an unknown field, does
not count, so the preflight fails rather than passing on a selector it cannot
prove covers the deletes.

The tables are auto-named ``{stackName}-{logicalId}-{suffix}``, so table
coverage is proven against the ARN prefix ``arn:{partition}:dynamodb:{region}:
{account}:table/{stackName}-``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

S3_MISSING = (
    "S3 data events: DeleteObject on archive bucket {bucket} (resources.type AWS::S3::Object)"
)
DDB_MISSING = (
    "DynamoDB data events: DeleteItem, BatchWriteItem, and TransactWriteItems on the "
    "Metadata_Index and ControlClaims tables "
    "(resources.type AWS::DynamoDB::Table, ARN prefix {prefix})"
)

#: Data-event names under which CloudTrail records a delete. An item delete can
#: arrive as DeleteItem or inside a BatchWriteItem or TransactWriteItems call,
#: so a selector filtered on eventName must admit all three.
S3_DELETE_EVENTS = ("DeleteObject",)
DDB_DELETE_EVENTS = ("DeleteItem", "BatchWriteItem", "TransactWriteItems")

_KNOWN_FIELDS = {"eventCategory", "resources.type", "resources.ARN", "readOnly", "eventName"}


def _admits(field: dict[str, Any], value: str) -> bool:
    """True iff an Equals / StartsWith-only field selector admits ``value``."""
    operators = set(field) - {"Field"}
    if not operators <= {"Equals", "StartsWith"}:
        return False
    equals = field.get("Equals") or []
    starts = field.get("StartsWith") or []
    return value in equals or any(value.startswith(p) for p in starts)


def _arn_admits(field: dict[str, Any], target_prefix: str) -> bool:
    """True iff an ARN field selector admits every resource under ``target_prefix``.

    Only ``StartsWith`` with a prefix of ``target_prefix`` proves whole-prefix
    coverage; an ``Equals`` names single resources and so proves nothing here.
    """
    operators = set(field) - {"Field"}
    if operators != {"StartsWith"}:
        return False
    return any(target_prefix.startswith(p) for p in field["StartsWith"])


def _advanced_covers(
    selector: dict[str, Any],
    *,
    resource_type: str,
    event_names: tuple[str, ...],
    arn_prefix: str,
) -> bool:
    """True iff the selector records every event in ``event_names`` for the resources."""
    fields = {f.get("Field"): f for f in selector.get("FieldSelectors") or []}
    if not set(fields) <= _KNOWN_FIELDS:
        return False
    category = fields.get("eventCategory")
    if category is None or not _admits(category, "Data"):
        return False
    rtype = fields.get("resources.type")
    if rtype is None or not _admits(rtype, resource_type):
        return False
    if "readOnly" in fields and not _admits(fields["readOnly"], "false"):
        return False
    if "eventName" in fields and not all(_admits(fields["eventName"], n) for n in event_names):
        return False
    arn = fields.get("resources.ARN")
    return arn is None or _arn_admits(arn, arn_prefix)


def _basic_covers_s3(selector: dict[str, Any], *, object_prefix: str) -> bool:
    if selector.get("ReadWriteType", "All") not in {"All", "WriteOnly"}:
        return False
    return any(
        object_prefix.startswith(value)
        for resource in selector.get("DataResources") or []
        if resource.get("Type") == "AWS::S3::Object"
        for value in resource.get("Values") or []
    )


def missing_coverage(
    trails: list[dict[str, Any]],
    *,
    bucket: str,
    table_arn_prefix: str,
    partition: str = "aws",
) -> list[str]:
    """Return a description of each delete data-event class no trail records."""
    object_prefix = f"arn:{partition}:s3:::{bucket}/"
    s3_ok = False
    ddb_ok = False
    for trail in trails:
        for selector in trail.get("EventSelectors") or []:
            s3_ok = s3_ok or _basic_covers_s3(selector, object_prefix=object_prefix)
        for selector in trail.get("AdvancedEventSelectors") or []:
            s3_ok = s3_ok or _advanced_covers(
                selector,
                resource_type="AWS::S3::Object",
                event_names=S3_DELETE_EVENTS,
                arn_prefix=object_prefix,
            )
            ddb_ok = ddb_ok or _advanced_covers(
                selector,
                resource_type="AWS::DynamoDB::Table",
                event_names=DDB_DELETE_EVENTS,
                arn_prefix=table_arn_prefix,
            )
    missing: list[str] = []
    if not s3_ok:
        missing.append(S3_MISSING.format(bucket=bucket))
    if not ddb_ok:
        missing.append(DDB_MISSING.format(prefix=table_arn_prefix))
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", required=True, help="archive bucket name")
    parser.add_argument(
        "--table-arn-prefix", required=True, help="arn:...:table/{stackName}- prefix"
    )
    parser.add_argument("--partition", default="aws")
    args = parser.parse_args(argv)
    try:
        trails = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"could not parse event selectors JSON: {e}", file=sys.stderr)
        return 1
    if not isinstance(trails, list):
        print("event selectors input must be a JSON array", file=sys.stderr)
        return 1
    missing = missing_coverage(
        trails,
        bucket=args.bucket,
        table_arn_prefix=args.table_arn_prefix,
        partition=args.partition,
    )
    for line in missing:
        print(line)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())

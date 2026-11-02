"""Unit tests for the deploy-time CloudTrail data-event preflight
(cdk/scripts/check_cloudtrail_data_events.py; access-control [ACCESS-004.02],
[ACCESS-004.03]).

Pure-function tests over synthetic ``get-event-selectors`` payloads; no AWS.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PATH = _REPO_ROOT / "cdk" / "scripts" / "check_cloudtrail_data_events.py"
_spec = importlib.util.spec_from_file_location("check_cloudtrail_data_events_under_test", _PATH)
assert _spec and _spec.loader
checker = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = checker
_spec.loader.exec_module(checker)

BUCKET = "cairnproofs4af-artifact-archive-cmh-alpha"
TABLE_PREFIX = "arn:aws:dynamodb:us-east-2:111111111111:table/cairnproofs4af-cmh-alpha-"


def _field(name: str, **ops: list[str]) -> dict[str, Any]:
    return {"Field": name, **ops}


def _s3_advanced(**extra: dict[str, Any]) -> dict[str, Any]:
    fields = [
        _field("eventCategory", Equals=["Data"]),
        _field("resources.type", Equals=["AWS::S3::Object"]),
    ]
    fields.extend(extra.values())
    return {"Name": "s3", "FieldSelectors": fields}


def _ddb_advanced(**extra: dict[str, Any]) -> dict[str, Any]:
    fields = [
        _field("eventCategory", Equals=["Data"]),
        _field("resources.type", Equals=["AWS::DynamoDB::Table"]),
    ]
    fields.extend(extra.values())
    return {"Name": "ddb", "FieldSelectors": fields}


def _missing(trails: list[dict[str, Any]]) -> list[str]:
    return checker.missing_coverage(trails, bucket=BUCKET, table_arn_prefix=TABLE_PREFIX)


def test_advanced_selectors_for_both_classes_pass() -> None:
    trails = [{"AdvancedEventSelectors": [_s3_advanced(), _ddb_advanced()]}]
    assert _missing(trails) == []


def test_scoped_selectors_pass_when_they_cover_bucket_and_stack_tables() -> None:
    trails = [
        {
            "AdvancedEventSelectors": [
                _s3_advanced(
                    arn=_field("resources.ARN", StartsWith=[f"arn:aws:s3:::{BUCKET}/"]),
                    ro=_field("readOnly", Equals=["false"]),
                    name=_field("eventName", Equals=["DeleteObject", "DeleteObjects"]),
                ),
                _ddb_advanced(
                    arn=_field("resources.ARN", StartsWith=[TABLE_PREFIX]),
                    name=_field(
                        "eventName",
                        Equals=["DeleteItem", "BatchWriteItem", "TransactWriteItems"],
                    ),
                ),
            ]
        }
    ]
    assert _missing(trails) == []


def test_basic_s3_selector_counts_for_s3_but_not_dynamodb() -> None:
    trails = [
        {
            "EventSelectors": [
                {
                    "ReadWriteType": "All",
                    "IncludeManagementEvents": True,
                    "DataResources": [
                        {"Type": "AWS::S3::Object", "Values": [f"arn:aws:s3:::{BUCKET}/"]}
                    ],
                }
            ]
        }
    ]
    missing = _missing(trails)
    assert len(missing) == 1
    assert "DynamoDB" in missing[0]


def test_coverage_may_come_from_different_trails() -> None:
    trails = [
        {"AdvancedEventSelectors": [_s3_advanced()]},
        {"AdvancedEventSelectors": [_ddb_advanced()]},
    ]
    assert _missing(trails) == []


def test_management_only_trail_names_both_missing_selectors() -> None:
    trails = [{"EventSelectors": [{"ReadWriteType": "All", "IncludeManagementEvents": True}]}]
    missing = _missing(trails)
    assert len(missing) == 2
    assert BUCKET in missing[0]
    assert TABLE_PREFIX in missing[1]


@pytest.mark.parametrize(
    "selector",
    [
        # read-only data events never record a delete
        _s3_advanced(ro=_field("readOnly", Equals=["true"])),
        # another bucket
        _s3_advanced(arn=_field("resources.ARN", StartsWith=["arn:aws:s3:::other-bucket/"])),
        # a single object ARN proves nothing about the prefix
        _s3_advanced(arn=_field("resources.ARN", Equals=[f"arn:aws:s3:::{BUCKET}/x"])),
        # deletes excluded by name
        _s3_advanced(name=_field("eventName", Equals=["PutObject"])),
        # an operator the checker cannot prove admits the delete
        _s3_advanced(arn=_field("resources.ARN", NotStartsWith=["arn:aws:s3:::other/"])),
        # unknown field
        _s3_advanced(extra=_field("userIdentity.arn", Equals=["x"])),
        # management category
        {"FieldSelectors": [_field("eventCategory", Equals=["Management"])]},
    ],
)
def test_selectors_that_do_not_prove_s3_delete_coverage_fail(selector: dict[str, Any]) -> None:
    trails = [{"AdvancedEventSelectors": [selector, _ddb_advanced()]}]
    missing = _missing(trails)
    assert missing == [checker.S3_MISSING.format(bucket=BUCKET)]


def test_basic_read_only_s3_selector_fails() -> None:
    trails = [
        {
            "EventSelectors": [
                {
                    "ReadWriteType": "ReadOnly",
                    "DataResources": [{"Type": "AWS::S3::Object", "Values": ["arn:aws:s3"]}],
                }
            ],
            "AdvancedEventSelectors": [_ddb_advanced()],
        }
    ]
    assert _missing(trails) == [checker.S3_MISSING.format(bucket=BUCKET)]


def test_dynamodb_selector_scoped_to_other_tables_fails() -> None:
    other = _ddb_advanced(
        arn=_field(
            "resources.ARN",
            StartsWith=["arn:aws:dynamodb:us-east-2:111111111111:table/other-stack-"],
        )
    )
    trails = [{"AdvancedEventSelectors": [_s3_advanced(), other]}]
    assert _missing(trails) == [checker.DDB_MISSING.format(prefix=TABLE_PREFIX)]


def test_main_exit_codes_and_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--bucket", BUCKET, "--table-arn-prefix", TABLE_PREFIX]
    ok = json.dumps([{"AdvancedEventSelectors": [_s3_advanced(), _ddb_advanced()]}])
    monkeypatch.setattr(sys, "stdin", io.StringIO(ok))
    assert checker.main(args) == 0

    monkeypatch.setattr(sys, "stdin", io.StringIO("[{}]"))
    assert checker.main(args) == 1
    out = capsys.readouterr().out
    assert "S3 data events" in out and "DynamoDB data events" in out


@pytest.mark.parametrize("payload", ["not json", '{"a": 1}'])
def test_main_rejects_malformed_input(monkeypatch: pytest.MonkeyPatch, payload: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert checker.main(["--bucket", BUCKET, "--table-arn-prefix", TABLE_PREFIX]) == 1


def test_event_name_prefix_and_write_only_basic_selector_pass() -> None:
    trails = [
        {
            "EventSelectors": [
                {
                    "ReadWriteType": "WriteOnly",
                    "DataResources": [{"Type": "AWS::S3::Object", "Values": ["arn:aws:s3"]}],
                }
            ],
            "AdvancedEventSelectors": [
                _ddb_advanced(
                    name=_field("eventName", StartsWith=["Delete", "BatchWrite", "TransactWrite"])
                ),
            ],
        }
    ]
    assert _missing(trails) == []


@pytest.mark.parametrize(
    "selector",
    [
        # narrower than the stack prefix: covers one table, not both
        _ddb_advanced(arn=_field("resources.ARN", StartsWith=[TABLE_PREFIX + "MetadataTable"])),
        # wrong resource type
        {
            "FieldSelectors": [
                _field("eventCategory", Equals=["Data"]),
                _field("resources.type", Equals=["AWS::DynamoDB::Stream"]),
            ]
        },
        # no eventCategory field at all
        {"FieldSelectors": [_field("resources.type", Equals=["AWS::DynamoDB::Table"])]},
        # deletes excluded by an operator the checker cannot prove
        _ddb_advanced(name=_field("eventName", NotEquals=["DeleteItem"])),
    ],
)
def test_selectors_that_do_not_prove_dynamodb_delete_coverage_fail(
    selector: dict[str, Any],
) -> None:
    trails = [{"AdvancedEventSelectors": [_s3_advanced(), selector]}]
    assert _missing(trails) == [checker.DDB_MISSING.format(prefix=TABLE_PREFIX)]


def test_basic_selector_for_another_bucket_fails() -> None:
    trails = [
        {
            "EventSelectors": [
                {
                    "ReadWriteType": "All",
                    "DataResources": [
                        {"Type": "AWS::S3::Object", "Values": ["arn:aws:s3:::other-bucket/"]}
                    ],
                }
            ],
            "AdvancedEventSelectors": [_ddb_advanced()],
        }
    ]
    assert _missing(trails) == [checker.S3_MISSING.format(bucket=BUCKET)]


def test_basic_selector_for_a_narrower_object_prefix_fails() -> None:
    """A selector scoped to a sub-prefix of the bucket does not cover every object delete."""
    trails = [
        {
            "EventSelectors": [
                {
                    "ReadWriteType": "All",
                    "DataResources": [
                        {"Type": "AWS::S3::Object", "Values": [f"arn:aws:s3:::{BUCKET}/some/"]}
                    ],
                }
            ],
            "AdvancedEventSelectors": [_ddb_advanced()],
        }
    ]
    assert _missing(trails) == [checker.S3_MISSING.format(bucket=BUCKET)]


@pytest.mark.parametrize("other_delete_api", ["BatchWriteItem", "TransactWriteItems"])
def test_delete_item_only_event_name_selector_does_not_prove_item_delete_coverage(
    other_delete_api: str,
) -> None:
    """[ACCESS-004.02] item deletes also arrive as BatchWriteItem / TransactWriteItems."""
    narrow = _ddb_advanced(name=_field("eventName", Equals=["DeleteItem"]))
    trails = [{"AdvancedEventSelectors": [_s3_advanced(), narrow]}]
    # The selector would record DeleteItem but not `other_delete_api`.
    assert not checker._admits(narrow["FieldSelectors"][-1], other_delete_api)
    assert _missing(trails) == [checker.DDB_MISSING.format(prefix=TABLE_PREFIX)]

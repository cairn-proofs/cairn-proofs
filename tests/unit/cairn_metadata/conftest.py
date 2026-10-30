"""Shared fixtures for DynamoDB-backed metadata tests."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

TABLE_NAME = "MetadataIndexTest"
CONTROL_CLAIMS_TABLE_NAME = "ControlClaimsTest"
IDENTITY_REGISTRY_TABLE_NAME = "IdentityRegistryTest"


@pytest.fixture
def dynamodb_table(aws_region: str) -> Iterator[Any]:
    """Boot a moto-backed DynamoDB and return the Artifact_Bundle table.

    Tenant-PK key schema ([META-001]): ``pk = {tenant-id}`` /
    ``sk = {project-id}#{manifest-digest}``, no GSI/LSI. The Control Claims
    table and the Identity_Registry table are provisioned as siblings under
    the same `mock_aws` context so their facade tests can use them via
    ``boto3.resource("dynamodb")``."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=aws_region)
        resource.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        resource.create_table(
            TableName=CONTROL_CLAIMS_TABLE_NAME,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        resource.create_table(
            TableName=IDENTITY_REGISTRY_TABLE_NAME,
            KeySchema=[
                {"AttributeName": "id", "KeyType": "HASH"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield resource.Table(TABLE_NAME)


@pytest.fixture
def dynamodb_resource(aws_region: str, dynamodb_table: Any) -> Any:
    return boto3.resource("dynamodb", region_name=aws_region)

"""Shared fixtures for cairn_cli tests."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

# The resolved per-deployment appName the moto-backed SSM discovery parameter
# advertises; every name the CLI derives is prefixed with it.
APP_NAME = "cairnproofs4af"
METADATA_TABLE = f"{APP_NAME}-ArtifactMetadata-cmh-alpha"
# Physical names mirror DeploymentTarget.name_for(...); the control-claims
# projection table is `ControlClaims` and the control-plane Identity_Registry is
# a sibling table ([IDREG-001]).
CONTROL_CLAIMS_TABLE = f"{APP_NAME}-ControlClaims-cmh-alpha"
IDENTITY_REGISTRY_TABLE = f"{APP_NAME}-IdentityRegistry-cmh-alpha"
ECR_HOST = "123456789012.dkr.ecr.us-east-2.amazonaws.com"
SSM_APP_NAME_PARAM = "/cairnproofs/cmh-alpha/app-name"
SSM_HOST_PARAM = f"/{APP_NAME}/alpha/registry/ecr-host"


def subparser_help(parser: argparse.ArgumentParser, *command_path: str) -> str:
    """Render ``--help`` for the subparser at ``command_path`` within ``parser``.

    Walks the (possibly nested) subparser tree by command name — e.g.
    ``subparser_help(parser, "alias", "set")`` — and returns that leaf parser's
    formatted help. Used by the help-text assertions across the cairn_cli test
    suite so the parser-tree walk lives in one place rather than being
    copy-pasted per command module.
    """
    node = parser
    for name in command_path:
        action = next(a for a in node._subparsers._group_actions if hasattr(a, "choices"))
        node = action.choices[name]
    return node.format_help()


@pytest.fixture
def aws_stack(aws_region: str) -> Iterator[dict[str, Any]]:
    """Boot moto-backed SSM + DynamoDB and yield a dict of clients/resources.

    Each test gets its own ``mock_aws`` context so fixtures cannot leak
    state across parallel xdist workers.
    """
    with mock_aws():
        ssm = boto3.client("ssm", region_name=aws_region)
        # appName discovery parameter (token-independent path) — read first by
        # the CLI's resolve_app_name before any appName-prefixed lookup.
        ssm.put_parameter(
            Name=SSM_APP_NAME_PARAM,
            Value=APP_NAME,
            Type="String",
            Overwrite=True,
        )
        ssm.put_parameter(
            Name=SSM_HOST_PARAM,
            Value=ECR_HOST,
            Type="String",
            Overwrite=True,
        )
        dynamodb = boto3.resource("dynamodb", region_name=aws_region)
        dynamodb.create_table(
            TableName=METADATA_TABLE,
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
        dynamodb.create_table(
            TableName=CONTROL_CLAIMS_TABLE,
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
        # Control-plane Identity_Registry ([IDREG-001]) — single opaque-id PK.
        dynamodb.create_table(
            TableName=IDENTITY_REGISTRY_TABLE,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield {
            "ssm": ssm,
            "ecr": boto3.client("ecr", region_name=aws_region),
            "dynamodb": dynamodb,
        }

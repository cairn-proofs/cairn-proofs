"""validate-bundle Lambda package.

Entry point: ``cairn_lambda_validate_bundle.handler.lambda_handler``.

Validates Artifact_Bundles on ECR PUSH
EventBridge events and emits ``artifact.validated`` on success or
``artifact.validation-failed`` on permanent failure.
"""

from cairn_lambda_validate_bundle.handler import lambda_handler

__all__ = ["lambda_handler"]

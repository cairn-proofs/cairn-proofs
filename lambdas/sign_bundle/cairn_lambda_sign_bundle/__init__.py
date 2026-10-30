"""sign-bundle Lambda package.

Entry point: ``cairn_lambda_sign_bundle.handler.lambda_handler``.

Signs validated bundles via Notation + AWS Signer using the
artifact-pipeline-signing profile.
"""

from cairn_lambda_sign_bundle.handler import lambda_handler

__all__ = ["lambda_handler"]

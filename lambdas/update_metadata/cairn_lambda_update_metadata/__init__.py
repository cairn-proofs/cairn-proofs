"""update-metadata Lambda package.

Entry point: ``cairn_lambda_update_metadata.handler.lambda_handler``.

Applies conditional DynamoDB writes for every ``evidence.*`` event on the
pipeline bus.
"""

from cairn_lambda_update_metadata.handler import lambda_handler

__all__ = ["lambda_handler"]

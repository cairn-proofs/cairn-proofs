"""archive-to-s3 Lambda package.

Entry point: ``cairn_lambda_archive_to_s3.handler.lambda_handler``.

Archives signed Artifact_Bundles to S3,
generates a WORM_Receipt, attaches it as an OCI Referrer. WORM_Enabled
is read from the ``WORM_ENABLED`` env var (default false).
"""

from cairn_lambda_archive_to_s3.handler import lambda_handler

__all__ = ["lambda_handler"]

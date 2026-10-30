"""mint-tag Lambda package.

Entry point: ``cairn_lambda_mint_tag.handler.lambda_handler``.

The post-archive canonical-tag-minting stage. Triggered by
``artifact.archived``, it reads the validate-recorded ``artifactKind`` from the
durable Metadata_Index row (never re-deriving it from the event),
builds the single ``{artifact-kind}--_ref--{digest}`` tag through the grammar
authority (the ``digest`` segment being the Manifest_Digest prefix), and emits
an ECR ``PutImage``-with-tag on the already-stored manifest digest. The step is
idempotent / re-drivable on the digest.
"""

from cairn_lambda_mint_tag.handler import lambda_handler

__all__ = ["lambda_handler"]

"""Bundle validation library for the Cairn Provenance_System.

Implements [INGEST-017.07] (bundle validation completeness, criteria a-e), the
[PIPELINE-013] uniform org + tenant + project cross-check, and [PIPELINE-010.01]
(authenticated push principal capture).
"""

from cairn_validation.bundle import (
    BundleValidationError,
    validate_bundle,
    validate_claim,
)
from cairn_validation.crosscheck import (
    crosscheck_identity,
)
from cairn_validation.principal import (
    AuthenticatedPushPrincipal,
    extract_authenticated_push_principal,
)

__all__ = [
    "AuthenticatedPushPrincipal",
    "BundleValidationError",
    "crosscheck_identity",
    "extract_authenticated_push_principal",
    "validate_bundle",
    "validate_claim",
]

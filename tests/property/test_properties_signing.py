"""Property-based tests for the signing capability.

Covers: [SIGN-005.03], [SIGN-005.05] — currently SKIPPED, see the test docstring
for the surface-not-implemented justification.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(
    reason=(
        "[SIGN-005.03] / [SIGN-005.05] — the spec calls for an overall "
        "signing-status determination "
        "function that aggregates a list of per-signature `signatureDetails` "
        "entries into one of `signed`, `unsigned`, `partial`, or `revoked` "
        "(with the partial/revoked rules biconditional on per-entry status). "
        "the pipeline today only WRITES individual signingStatus values "
        "(sign_evidence writes 'signed' or 'unsigned'; a revocation propagation "
        "event would set 'revoked'). There is no library-level aggregation "
        "function to test against — the overall-status computation lives in the "
        "retrieval API surface ([SIGN-005]), which has not yet landed. Skip "
        "until the retrieval surface lands."
    )
)
def test_property_7_signing_status_aggregation_per_profile() -> None:
    """**WHEN** a bundle is retrieved that has at least one signature that
    verifies successfully and at least one signature whose Signing_Profile
    version has been revoked **THEN** the response carries `signingStatus:
    partial`, and `signatureDetails` records the surviving signatures as
    `valid` and the revoked signatures as `revoked` with no change in status
    to the surviving entries.

    Source: signing/spec.md:78-81, 88-91 ([SIGN-005.03] / [SIGN-005.05]).
    """
    raise AssertionError("skipped — see decorator")

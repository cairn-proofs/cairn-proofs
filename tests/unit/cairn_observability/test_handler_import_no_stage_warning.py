"""Regression guard for the cross-module duplicate `Stage` dimension warning.

Importing more than one Lambda handler module into a single Python process MUST
NOT emit `PowertoolsUserWarning: Dimension 'Stage' has already been added`.
Powertools' `Metrics` keeps `default_dimensions` on a process-wide singleton, so
a handler that set `Stage` directly would make the second-through-fourth handler
module collide on the already-set `Stage`.

Isolation strategy — **subprocess** (not in-process `catch_warnings`):

The four handler modules are import-cached per xdist worker, and the
Powertools singleton's `default_dimensions["Stage"]` is frequently already
populated by neighbouring handler tests sharing the worker. An in-process
`catch_warnings(record=True)` around a re-import would therefore observe
nothing (the modules are already in `sys.modules`) and could not exercise the
cross-module import-order interaction. A child `python` process gives true
single-process, single-shot import isolation independent of the parent
worker's state. Each measurement here runs in its OWN child process because
module import is cached within a process — re-importing in the same process
would not re-trigger the import-time `Stage` default registration.

What the assertion pins — the *delta*, not an absolute count:

Powertools 3.29.0 has an upstream double-add quirk: `set_default_dimensions`
emits the duplicate-dimension warning even on its very first effective set
against the provider, so importing a *single* handler already yields one
`Stage`-dup warning. That floor (currently 1) is an upstream artifact we
deliberately do NOT hardcode — pinning the magic `1` would couple this test
to Powertools' internals and spuriously break if they ever fix the
double-add (floor drops to 0).

Instead we measure the property we actually OWN: importing the three
*additional* handler modules contributes ZERO further `Stage`-dup warnings
beyond importing a single handler. We import one handler in one subprocess
and all four in another, then assert their dup counts are EQUAL. This is the
real regression guard:

  - it FAILS if a handler regresses to a direct
    `metrics.set_default_dimensions(Stage=...)` call (then four > single);
  - it SURVIVES an upstream Powertools fix (both floors go to 0, still equal);
  - it does not depend on the absolute floor being any particular number.

The absolute floor today is 1; we optionally assert `four <= 1` purely as
informational documentation of current reality, but the load-bearing
assertion is the relative `four == single`.
"""

from __future__ import annotations

import subprocess
import sys

# The four handler modules, in handler import order.
_HANDLER_MODULES = (
    "cairn_lambda_validate_bundle.handler",
    "cairn_lambda_sign_bundle.handler",
    "cairn_lambda_archive_to_s3.handler",
    "cairn_lambda_update_metadata.handler",
)

# Env the four handler module-preludes read at import (matches the per-package
# conftest `env` fixtures: STAGE=alpha plus the resource-name vars each handler
# resolves at import time).
_HANDLER_ENV = {
    "STAGE": "alpha",
    "REGION_CODE": "cmh",
    "ARCHIVE_BUCKET": "t",
    "CONTROLS_TABLE_NAME": "t",
    "METADATA_TABLE_NAME": "t",
    "EVENT_BUS_NAME": "t",
    "SIGNING_PROFILE_NAME": "t",
    "SIGNER_PROFILE_ARN": "arn:aws:signer:us-east-2:123456789012:/signing-profiles/t",
    "KMS_KEY_ID": "t",
    # Keep boto3 from reaching for a real profile during import-time client
    # construction, mirroring the root conftest's `_aws_env`.
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_DEFAULT_REGION": "us-east-2",
}

_DUP_MSG = "Dimension 'Stage' has already been added"


def _child_program(modules: tuple[str, ...]) -> str:
    """Build the child program: import the GIVEN handler modules in order
    under a recording warnings filter, then print (1) the count of
    duplicate-`Stage` warnings seen across the imports and (2) the singleton's
    final `Stage` default dimension."""
    return "\n".join(
        [
            "import warnings",
            "with warnings.catch_warnings(record=True) as records:",
            "    warnings.simplefilter('always')",
            *[f"    import {mod}" for mod in modules],
            "    captured = list(records)",
            f"dups = [w for w in captured if {_DUP_MSG!r} in str(w.message)]",
            "print('DUP_STAGE_COUNT=' + str(len(dups)))",
            "from cairn_observability import Metrics",
            "m = Metrics(namespace='Artifact/cmh-alpha', service='probe')",
            "print('STAGE_DEFAULT=' + repr(m.default_dimensions.get('Stage')))",
        ]
    )


def _child_lines(prefix: str, stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith(prefix)]


def _measure(modules: tuple[str, ...]) -> tuple[int, str]:
    """Run a fresh child process importing `modules` in order; return
    `(dup_stage_count, stage_default_repr)`. A separate process per call is
    required: module import is cached within a process, so the import-time
    `Stage` registration only fires once per process."""
    result = subprocess.run(
        [sys.executable, "-c", _child_program(modules)],
        env=_HANDLER_ENV,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        f"importing {modules} in one process should succeed, "
        f"but the child exited {result.returncode}.\n"
        f"--- child stdout ---\n{result.stdout}\n"
        f"--- child stderr ---\n{result.stderr}"
    )

    count_lines = _child_lines("DUP_STAGE_COUNT=", result.stdout)
    assert count_lines, f"child did not report DUP_STAGE_COUNT.\nstdout:\n{result.stdout}"
    dup_count = int(count_lines[-1].split("=", 1)[1])

    stage_lines = _child_lines("STAGE_DEFAULT=", result.stdout)
    assert stage_lines, f"child did not report STAGE_DEFAULT.\nstdout:\n{result.stdout}"
    stage_default = stage_lines[-1].split("=", 1)[1]

    return dup_count, stage_default


def test_extra_handler_imports_add_no_stage_dup_warnings() -> None:
    """Importing the three additional handler modules contributes ZERO extra
    `Stage`-dup warnings beyond importing a single handler.

    We measure two cases in two separate subprocesses (import is process-cached,
    so a fresh process is required for each): `single` imports only the first
    handler; `four` imports all four in order. The load-bearing assertion is the
    *relative* one — `four == single` — which would break if a handler set
    `Stage` directly (one extra warning per import past the first, making
    four > single) yet survives any future upstream Powertools fix to the
    first-set double-add (both floors drop to 0, still equal). We do NOT
    hardcode the absolute floor (currently 1, an upstream artifact). The
    singleton must end with `Stage` equal to the env value."""
    single_dup_count, _ = _measure(_HANDLER_MODULES[:1])
    four_dup_count, four_stage_default = _measure(_HANDLER_MODULES)

    # LOAD-BEARING: the three additional handler imports add no further
    # Stage-dup warnings. Catches a handler regressing to a direct
    # set_default_dimensions(Stage=...) call; survives an upstream fix.
    assert four_dup_count == single_dup_count, (
        "importing all four handlers should add no Stage-dup warnings beyond "
        f"importing a single handler, but saw four={four_dup_count} vs "
        f"single={single_dup_count} — the cross-module collision regressed."
    )

    # INFORMATIONAL ONLY (not load-bearing): documents that today's upstream
    # Powertools 3.29.0 floor is one Stage-dup warning. Safe to delete if a
    # future Powertools drops the first-set double-add.
    assert four_dup_count <= 1, (
        f"expected today's upstream floor of <=1 Stage-dup warning, saw {four_dup_count}"
    )

    assert four_stage_default == "'alpha'", (
        "after importing all four handlers the singleton's default Stage "
        f"dimension should equal the env value 'alpha', got {four_stage_default!r}"
    )

"""Tiny shared helper for one-shot ``print error + return exit code`` flows.

The four subcommand handlers exit non-zero in ~20 different places with the
same shape: ``print(f"error: ...", file=sys.stderr); return N``. ``fail``
collapses each occurrence to a single ``return fail(...)`` call so the
exit-code/message pair stays adjacent and the handlers read top-to-bottom.

Multi-line bodies are supported via the ``hint`` parameter (printed on a
following line, no ``error:`` prefix) so callers like ``push.py``'s
immutable-tag hint stay verbatim.
"""

from __future__ import annotations

import sys


def fail(message: str, *, exit_code: int, hint: str | None = None) -> int:
    print(f"error: {message}", file=sys.stderr)
    if hint is not None:
        print(hint, file=sys.stderr)
    return exit_code

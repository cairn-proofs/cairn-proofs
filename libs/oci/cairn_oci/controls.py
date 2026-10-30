"""Control-id grammar + family derivation primitives.

Two shared helpers, consumed on demand by the validation gate (the control-id
grammar check) and by the CLI/TUI (client-side fast-fail + family display):

- :data:`CONTROL_ID_RE` / :func:`is_valid_control_id` — the control-id grammar.
  A well-formed control id is one or more ASCII lowercase-alphanumeric segments
  joined by single ``.`` or ``-`` separators (NIST ``ac-2`` / ``ac-2.1``, ISO
  ``5.1`` / ``8.16``). Empty, uppercase, ``#`` / ``/`` / whitespace,
  consecutive / leading / trailing separators, and non-ASCII all fail. The regex
  is anchored at both ends and linear-time (no catastrophic backtracking).

- :func:`derive_families` — the projection of a set of control ids onto their
  uppercased families. Catalog-agnostic and total: a hyphenated id contributes
  the uppercased segment before the FIRST hyphen (``ac-2`` / ``ac-2.1`` -> ``AC``);
  a hyphenless id (ISO ``5.1``) contributes nothing. Never indexes/slices
  unconditionally, so it is defined for every well-formed id and never raises on
  a hyphenless one. Assumes already-validated ids (the gate validates upstream).
"""

import re
from collections.abc import Iterable
from typing import Final

__all__ = ["CONTROL_ID_RE", "derive_families", "is_valid_control_id"]

# ASCII lowercase-alphanumeric segments joined by single ``.``/``-`` separators.
# Anchored at both ends; linear-time (the ``*``-group is led by a mandatory
# separator class disjoint from the segment class, so no nested-quantifier
# ambiguity / ReDoS surface).
CONTROL_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+([.-][a-z0-9]+)*$")


def is_valid_control_id(s: str) -> bool:
    """Return True iff ``s`` is a well-formed control id per the grammar.

    False for non-``str`` input, the empty string, uppercase, ``#`` / ``/`` /
    whitespace, consecutive / leading / trailing separators, and non-ASCII.
    """
    if not isinstance(s, str):
        return False
    # ``fullmatch`` (not ``match``) so a single trailing newline cannot slip
    # through the ``$`` anchor — ``re.match(..., "ac-2\n")`` would WRONGLY accept
    # it, forging a distinct Controls-table PK ``{uuid}#ac-2\n``.
    return CONTROL_ID_RE.fullmatch(s) is not None


def derive_families(controls: Iterable[str]) -> set[str]:
    """Derive the set of uppercased control families from ``controls``.

    Catalog-agnostic and total: a hyphenated id yields the uppercased segment
    before the first hyphen (``ac-2`` / ``ac-2.1`` -> ``AC``); a hyphenless id
    (ISO numeric like ``5.1``) yields nothing. The result is the de-duplicated
    set of derivable families and may be empty even when ``controls`` is not.
    Assumes already-validated ids; never raises on a hyphenless id.
    """
    families: set[str] = set()
    for control_id in controls:
        prefix, sep, _ = control_id.partition("-")
        # Require a non-empty prefix too: defense-in-depth so a leading-hyphen id
        # (which a correct gate rejects) can never yield a junk ``""`` family.
        if sep and prefix:
            families.add(prefix.upper())
    return families

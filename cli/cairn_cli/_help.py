"""Shared help-formatting standard for the ``cairn`` CLI.

Single source of truth for the CLI's self-documenting help output. Every
subparser (``push``, ``pull``, ``verify``, ``query``, ``alias``, ``tui``)
and the root parser passes :data:`CairnHelpFormatter` as ``formatter_class``
and composes its epilog with :func:`build_epilog`, so the help standard lives
here rather than being copy-pasted across the command modules.

``CairnHelpFormatter`` subclasses :class:`argparse.RawDescriptionHelpFormatter`
so epilog blocks — which carry usage examples with intentional newlines and
indentation — render verbatim instead of being re-wrapped or collapsed.

No module-level mutable state: the formatter is a class and the builder is a
pure function of its arguments.
"""

from __future__ import annotations

import argparse

#: Heading that precedes the example block(s) in a composed epilog. A stable
#: marker callers and tests can key on without depending on exact example text.
EXAMPLES_HEADING = "Examples:"


class CairnHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Help formatter that preserves epilog whitespace verbatim.

    Inherits :class:`argparse.RawDescriptionHelpFormatter`'s raw handling of
    the description and epilog (no re-wrapping/collapsing), which is what keeps
    multi-line usage examples and their indentation intact. Option help is
    still wrapped by the base class. Defined as a named subclass (rather than
    using ``RawDescriptionHelpFormatter`` directly) so the whole CLI shares one
    ``formatter_class`` symbol and the standard has a single home.
    """


def build_epilog(examples: list[str], notes: str | None = None) -> str:
    """Compose a consistent epilog from usage examples and optional notes.

    The returned string is meant to be passed as ``epilog=`` to an
    ``add_parser(...)`` / ``ArgumentParser(...)`` call that also sets
    ``formatter_class=CairnHelpFormatter`` so the whitespace below survives
    rendering.

    Each entry in ``examples`` is emitted verbatim (its own internal newlines
    and indentation preserved) under a shared :data:`EXAMPLES_HEADING`,
    one example per line block. ``notes`` — e.g. an old→new migration note —
    is appended verbatim after a blank line when supplied.

    Args:
        examples: One or more usage-example strings rendered verbatim.
        notes: Optional trailing prose (e.g. a migration note), rendered
            verbatim after the examples.

    Returns:
        The composed epilog string. Empty when ``examples`` is empty and
        ``notes`` is ``None``.
    """
    blocks: list[str] = []
    if examples:
        example_lines = "\n".join(examples)
        blocks.append(f"{EXAMPLES_HEADING}\n{example_lines}")
    if notes is not None:
        blocks.append(notes)
    return "\n\n".join(blocks)

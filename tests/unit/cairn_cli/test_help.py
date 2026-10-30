"""Tests for cairn_cli._help — shared help formatter + epilog builder."""

from __future__ import annotations

import argparse

import pytest
from cairn_cli._help import EXAMPLES_HEADING, CairnHelpFormatter, build_epilog


def _format_help(epilog: str) -> str:
    """Render a parser's help with the shared formatter and return it."""
    parser = argparse.ArgumentParser(
        prog="cairn-test",
        description="formatter test parser",
        epilog=epilog,
        formatter_class=CairnHelpFormatter,
    )
    return parser.format_help()


def test_formatter_preserves_epilog_whitespace() -> None:
    """A multi-line, indented epilog renders verbatim (not re-wrapped)."""
    epilog = (
        "Examples:\n"
        "  cairn push --project-id acme report.json evidence.pdf\n"
        "    (two layers, period defaulted)\n"
        "  cairn push --project-id acme --assessment-period 2026-q2 a.json\n"
    )
    rendered = _format_help(epilog)
    # The exact multi-line block survives intact: indentation and newlines
    # are not collapsed or re-wrapped by the formatter.
    assert epilog in rendered


def test_formatter_is_raw_description_subclass() -> None:
    """The shared formatter is a RawDescriptionHelpFormatter subclass."""
    assert issubclass(CairnHelpFormatter, argparse.RawDescriptionHelpFormatter)


def test_build_epilog_renders_example_verbatim() -> None:
    """A supplied example string appears verbatim in the composed epilog."""
    example = "  cairn push --project-id acme report.json evidence.pdf"
    epilog = build_epilog([example])
    assert example in epilog
    assert EXAMPLES_HEADING in epilog


def test_build_epilog_example_survives_rendering() -> None:
    """The verbatim example also survives rendering through the formatter."""
    example = (
        "  cairn push --project-id acme \\\n"
        "    --assessment-period 2026-q2 report.json evidence.pdf"
    )
    epilog = build_epilog([example])
    rendered = _format_help(epilog)
    assert example in rendered


def test_build_epilog_appends_notes() -> None:
    """Optional notes render verbatim after the examples."""
    example = "  cairn push --project-id acme report.json"
    notes = "Migration: 'cairn push acme report.json' -> add --project-id."
    epilog = build_epilog([example], notes=notes)
    assert example in epilog
    assert notes in epilog
    # Notes follow the example block.
    assert epilog.index(example) < epilog.index(notes)


def test_build_epilog_empty_when_no_content() -> None:
    """No examples and no notes yields an empty epilog."""
    assert build_epilog([]) == ""


def test_build_epilog_notes_only_no_examples() -> None:
    """Notes WITHOUT any examples: the ``if examples:`` branch is skipped, the
    notes still render verbatim, and no Examples heading is emitted."""
    notes = "Migration: add --project-id; the old positional form is rejected."
    epilog = build_epilog([], notes=notes)
    assert epilog == notes
    assert EXAMPLES_HEADING not in epilog


@pytest.mark.parametrize(
    "examples",
    [
        ["  one-example"],
        ["  first-example", "  second-example"],
    ],
)
def test_build_epilog_handles_one_or_more_examples(examples: list[str]) -> None:
    """Every supplied example appears verbatim regardless of count."""
    epilog = build_epilog(examples)
    for example in examples:
        assert example in epilog

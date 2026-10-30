"""Tests for cairn_cli.main — top-level argparse dispatch."""

from __future__ import annotations

import pytest
from cairn_cli import __version__
from cairn_cli._help import EXAMPLES_HEADING
from cairn_cli.main import build_parser, main


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out


def test_help_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "push" in captured.out
    assert "pull" in captured.out
    assert "verify" in captured.out
    assert "query" in captured.out


def test_root_help_has_usage_description_and_examples(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Root help carries usage, the CLI description, and an examples block."""
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    # Stable substrings only — not the full help body.
    assert "usage:" in captured.out
    assert "Cairn Proofs CLI" in captured.out
    assert EXAMPLES_HEADING in captured.out
    assert "cairn push --target" in captured.out


def test_no_command_prints_help_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "cairn" in captured.out


def test_unknown_subcommand_returns_nonzero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["frobnicate"])
    assert excinfo.value.code != 0


def test_parser_has_all_subcommands() -> None:
    parser = build_parser()
    # Argparse stores subcommands on the dest action.
    sub_action = next(
        action for action in parser._actions if hasattr(action, "choices") and action.choices
    )
    assert {"push", "pull", "verify", "query"} <= set(sub_action.choices)

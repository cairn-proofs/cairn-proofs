"""Help-sweep coverage for the producer/operator subcommands.

The self-documenting help standard (description + options +
>= 1 usage example, via ``cairn_cli._help.build_epilog`` /
``CairnHelpFormatter``) applies across EVERY producer/operator subcommand, not
just ``push``. The push-specific self-documenting assertion lives in
``test_push.py``; this file pins the same contract for the other swept
commands so a future command that drops its epilog (or its example) fails a
test rather than silently shipping non-self-documenting help.

The spec scenario 'Producer command is self-documenting' is stated for a
producer-facing command generically (``e.g. cairn push --help``); these guard
the rest of the swept surface against regression. Assertions key on STABLE
substrings only (the EXAMPLES_HEADING marker and the verbatim ``cairn <cmd>``
example prefix), never on wrapped option-help wording.
"""

from __future__ import annotations

import pytest
from cairn_cli import main as main_module
from cairn_cli._help import EXAMPLES_HEADING

from tests.unit.cairn_cli.conftest import subparser_help


def _help(*command_path: str) -> str:
    return subparser_help(main_module.build_parser(), *command_path)


# (command_path, a verbatim example substring that MUST appear in that
# command's --help). One row per swept subcommand surface.
_SWEPT = [
    (("pull",), "cairn pull alpha-bank/assessment-results:2026-q1"),
    (("verify",), "cairn verify alpha-bank/assessment-results:2026-q1"),
    (("query",), "cairn query --tenant-id acme --project alpha-bank"),
    (("alias",), "cairn alias set my-project"),
    (("alias", "set"), "cairn alias set my-project"),
    (("alias", "move"), "cairn alias move my-project"),
    (("tui",), "cairn tui --stage alpha"),
]


@pytest.mark.parametrize(
    "command_path, example",
    _SWEPT,
    ids=[" ".join(cp) for cp, _ in _SWEPT],
)
def test_swept_command_is_self_documenting(command_path: tuple[str, ...], example: str) -> None:
    """Each swept command's --help renders usage, the Examples heading, and a
    concrete usage example verbatim."""
    rendered = _help(*command_path)
    assert "usage" in rendered
    assert EXAMPLES_HEADING in rendered
    assert example in rendered


def test_root_parser_is_self_documenting() -> None:
    """The root ``cairn`` parser itself carries the shared help standard: a
    description, the Examples heading, and the canonical push example."""
    rendered = main_module.build_parser().format_help()
    assert "Cairn Proofs CLI" in rendered
    assert EXAMPLES_HEADING in rendered
    assert "cairn push --target acme-co/acme/payments" in rendered

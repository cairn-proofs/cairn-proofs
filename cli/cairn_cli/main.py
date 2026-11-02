"""Entry point for the `cairn` CLI.

Dispatches to per-command handlers under ``cairn_cli.{push,pull,verify,query}``.
Each handler owns its own argparse subparser via ``add_subparser`` so the
parser tree stays modular.

No global state. The argparse Namespace passes per-invocation arguments
through to the handlers, which build boto3 clients lazily inside each
``run()`` — keeping all configuration explicit and per-invocation.
"""

from __future__ import annotations

import argparse
import sys

from cairn_cli import __version__, alias, config, prune, pull, push, query, sign, verify
from cairn_cli._help import CairnHelpFormatter, build_epilog
from cairn_cli.tui import command as tui


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn",
        description="Cairn Proofs CLI — push, pull, verify, and query artifact bundles",
        formatter_class=CairnHelpFormatter,
        epilog=build_epilog(
            [
                "  cairn push --target acme-co/acme/payments report.json evidence.pdf",
                "  cairn query --project acme",
            ],
            notes="Run 'cairn <command> --help' for command-specific options and examples.",
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"cairn {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")
    push.add_subparser(subparsers)
    pull.add_subparser(subparsers)
    verify.add_subparser(subparsers)
    sign.add_subparser(subparsers)
    query.add_subparser(subparsers)
    alias.add_subparser(subparsers)
    config.add_subparser(subparsers)
    prune.add_subparser(subparsers)
    tui.add_subparser(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())

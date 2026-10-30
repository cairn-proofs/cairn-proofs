"""`cairn tui` — launch the read-only operator Artifact_TUI.

This is the CLI entry point that wires the Textual app
(:class:`~cairn_cli.tui.app.ArtifactStoreApp`) to its read-only data layer
(:class:`~cairn_cli.tui._sources.ArtifactSources`) over the structurally
read-only AWS surface (:class:`~cairn_cli.tui._readonly_client.ReadOnlyAwsClient`).

Lazy-import discipline (WHY): ``cairn_cli.main`` imports this module at startup
so the subcommand registers in the parser tree, but the OTHER subcommands
(``push`` / ``pull`` / ``verify`` / ``query``) must NOT pay Textual's import
cost just to run. So the module top-level imports ONLY light things
(``argparse``, ``sys``, ``DeploymentTarget``, ``fail``); ``boto3``,
``ReadOnlyAwsClient``, ``ArtifactSources`` and ``ArtifactStoreApp`` (which pulls
in ``textual``) are imported lazily inside :func:`build_app` — never at module
load.

Pre-flight guards in :func:`run`, in order, BEFORE any AWS client is built:

1. **TTY check.** The TUI drives an interactive terminal; if stdout is not a
   TTY (piped/redirected/CI) we fail fast with exit code 2 rather than spinning
   up a Textual app against a non-terminal.
2. **Region resolution.** The SQS DLQ queue-URL construction
   (``_sources.dlq_health``) and every boto3 client need an explicit region; we
   resolve it from the boto3 session and fail with a hint when it is unset.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from cairn_cli._aws import (
    AppNameUnresolvedError,
    DeploymentTarget,
    resolve_app_name,
    resolve_table_names,
)
from cairn_cli._errors import fail
from cairn_cli._help import CairnHelpFormatter, build_epilog

if TYPE_CHECKING:
    # Type-only — never imported at runtime, so textual stays off the import
    # path of the non-TUI subcommands.
    from cairn_cli.tui._readonly_client import ReadOnlyAwsClient
    from cairn_cli.tui._sources import ArtifactSources
    from cairn_cli.tui.app import ArtifactStoreApp


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "tui",
        help="launch the read-only operator TUI over the Cairn Proofs",
        description=(
            "Launch the read-only operator TUI over the Cairn Proofs. Requires an "
            "interactive terminal (TTY) and a resolvable AWS region. Issues only "
            "allowlisted read operations; it never mutates artifacts."
        ),
        formatter_class=CairnHelpFormatter,
        epilog=build_epilog(
            [
                "cairn tui --stage alpha",
                "cairn tui --stage alpha --region-code cmh --watch-interval 5",
            ]
        ),
    )
    p.add_argument("--stage", default="alpha")
    p.add_argument("--region-code", default="cmh")
    p.add_argument(
        "--app-name",
        default=None,
        help="deployment appName (e.g. cairnproofs4af); overrides the "
        "CAIRN_APP_NAME env var and the SSM discovery lookup",
    )
    p.add_argument(
        "--watch-interval",
        type=float,
        default=3.0,
        help="watch-mode poll interval in seconds (clamped to a 1s floor)",
    )
    p.set_defaults(handler=run)


def build_app(
    args: argparse.Namespace,
    *,
    sources: ArtifactSources | None = None,
    region: str | None = None,
    client: ReadOnlyAwsClient | None = None,
) -> ArtifactStoreApp:
    """Construct the Textual app, optionally over an injected ``sources``.

    The ``sources`` seam exists for the end-to-end test: it injects a
    stub-backed :class:`ArtifactSources` so the app can be driven via Textual's
    ``run_test()`` pilot without touching AWS. When ``sources is None`` (the
    real CLI path) the read-only AWS stack is constructed here, using
    ``region`` — which :func:`run` has already resolved and validated. If a
    caller builds the real stack directly (``sources is None`` and ``region is
    None``), the region is resolved here as a fallback. The ``client`` seam lets
    a test inject a stub :class:`ReadOnlyAwsClient` (with a stub SSM) to exercise
    the appName-resolution path without AWS.

    When building the real stack, the deployment ``appName`` is resolved FIRST
    (explicit ``--app-name`` → ``CAIRN_APP_NAME`` env → SSM discovery) so the
    table/queue names that :class:`ArtifactSources` composes at construction
    are well-formed. :class:`AppNameUnresolvedError` propagates to :func:`run`,
    which renders it as a friendly CLI error rather than a traceback.

    Textual (via ``ArtifactStoreApp``) and ``boto3`` are imported HERE, not at
    module load, so importing this module from ``main`` stays cheap for the
    non-TUI subcommands.
    """
    # Lazy imports — keep textual/boto3 off the import path of `cairn push` etc.
    from cairn_cli.tui.app import ArtifactStoreApp

    # `--app-name` (when given) seeds the target; resolution still runs so the
    # env/SSM fallbacks apply when it is absent.
    target = DeploymentTarget(
        stage=args.stage,
        region_code=args.region_code,
        app_name=getattr(args, "app_name", None),
    )

    if sources is None:
        from cairn_cli.tui._readonly_client import ReadOnlyAwsClient
        from cairn_cli.tui._sources import ArtifactSources

        if region is None:
            region = _resolve_region()
        if client is None:
            client = ReadOnlyAwsClient(region_name=region)
        # Resolve appName BEFORE ArtifactSources reads target.metadata_table /
        # name_for("ControlClaims") / name_for("IdentityRegistry") / dlq_name —
        # those raise AppNameUnresolvedError while appName is unset (the crash
        # the bare-target path would hit).
        target = resolve_app_name(client, target, override=getattr(args, "app_name", None))
        # Resolve the auto-named table physical names from SSM (best-effort;
        # the read-only client's get_parameter backs this; falls back to
        # name_for reconstruction if a param is absent).
        target = resolve_table_names(client, target)
        sources = ArtifactSources(target=target, client=client, region=region)

    return ArtifactStoreApp(
        sources,
        watch_interval=args.watch_interval,
        deployment_target=target,
        region=region,
    )


def run(args: argparse.Namespace) -> int:
    # 1. TTY check FIRST — before constructing any AWS client. A non-interactive
    #    stdout (pipe/redirect/CI) cannot host the Textual app.
    if not sys.stdout.isatty():
        return fail(
            "cairn tui requires an interactive terminal (TTY); stdout is not a "
            "terminal. Run it directly in a terminal, not through a pipe, "
            "redirect, or CI runner.",
            exit_code=2,
        )

    # 2. Region must be resolvable — the SQS queue-URL construction and every
    #    boto3 client need it. Resolve/validate ONCE here, then hand the result
    #    to build_app so the session is not re-read.
    region = _resolve_region()
    if region is None:
        return fail(
            "no AWS region configured; cairn tui needs a region to build the "
            "queue URLs and AWS clients.",
            exit_code=2,
            hint="Set AWS_REGION (e.g. AWS_REGION=us-east-2) or configure a "
            "default region via `aws configure`.",
        )

    # 3. appName must resolve (explicit / env / SSM) before the read-only stack
    #    composes any physical table or queue name. Surface a failure as a
    #    friendly CLI error, not a traceback.
    try:
        app = build_app(args, region=region)
    except AppNameUnresolvedError as e:
        return fail(
            f"could not resolve the deployment appName: {e}",
            exit_code=2,
            hint="Pass --app-name (e.g. --app-name cairnproofs4af), set "
            "CAIRN_APP_NAME, or confirm the stack wrote the SSM discovery "
            "parameter /cairnproofs/{region-code}-{stage}/app-name.",
        )
    app.run()
    return 0


def _resolve_region() -> str | None:
    """Resolve the AWS region string from the boto3 session (None when unset).

    boto3 is imported lazily here so the module top-level stays free of the
    heavy AWS import for the non-TUI subcommands.
    """
    import boto3

    return boto3.session.Session().region_name

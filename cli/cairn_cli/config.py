"""``cairn config`` — persist and inspect the producer's default identity target.

The producer supplies three identity coordinates — the opaque ``org-id``,
``tenant-id``, and ``project-id`` — that populate the producer claim's identity
tuple and address the ECR repository path ([INGEST-032]). To avoid repeating
them on every ``cairn push``, the CLI resolves each coordinate from the first of
these sources that provides it:

1. an explicit command-line argument — ``--org-id`` / ``--tenant-id`` /
   ``--project-id``, or a single fully-qualified ``--target {org}/{tenant}/{project}``
   (an individual ``--org-id`` etc. overrides the matching ``--target`` segment);
2. the environment variables ``CAIRN_ORG_ID`` / ``CAIRN_TENANT_ID`` /
   ``CAIRN_PROJECT_ID``;
3. a persisted CLI configuration — a project-local ``.cairn.toml`` (searched
   from the current directory upward) overriding a per-user config under
   ``$XDG_CONFIG_HOME/cairn/config.toml`` (default ``~/.config/cairn/config.toml``),
   written by ``cairn config set-target``.

Explicit arguments override the environment, which overrides the configuration.
A coordinate that no source supplies makes the consuming command fail loud,
naming the unresolved coordinate(s); a file token is NEVER silently consumed as
an identity coordinate (identity is always a named switch / env / config value,
never a positional).

This module is the single home of that resolution chain
(:func:`resolve_target`) and the persisted-config read/write
(``cairn config set-target`` / ``cairn config show``). The opaque-id segment
grammar mirrors ``cairn_oci``'s ``ArtifactLocator`` segment rule; the locator
remains the ultimate authority at push time.

No module-level mutable state: the loaders read the filesystem per call and the
resolver is a pure function of its (optionally injected) inputs.
"""

from __future__ import annotations

import argparse
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cairn_cli._errors import fail
from cairn_cli._help import CairnHelpFormatter, build_epilog

#: The three identity coordinates, in ``{org-id}/{tenant-id}/{project-id}`` path
#: order ([INGEST-032]). Used as the canonical key set across the resolver, the
#: persisted-config table, and the ``--target`` parser.
COORDINATE_KEYS: tuple[str, str, str] = ("org-id", "tenant-id", "project-id")

#: Coordinate key → environment variable (resolution tier 2).
ENV_VARS: dict[str, str] = {
    "org-id": "CAIRN_ORG_ID",
    "tenant-id": "CAIRN_TENANT_ID",
    "project-id": "CAIRN_PROJECT_ID",
}

#: Opaque-id segment grammar — mirrors ``cairn_oci.categories._ID_RE`` (lowercase
#: alphanumeric, single internal hyphens, no leading/trailing hyphen,
#: 1-64 chars). This is a fail-loud UX pre-flight for ``set-target``; the
#: ``ArtifactLocator`` re-validates at push time and is the ultimate authority.
_SEGMENT_RE = re.compile(r"\A[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?\Z")

#: TOML table the persisted target lives under, and the config filenames.
_CONFIG_TABLE = "target"
PROJECT_CONFIG_FILENAME = ".cairn.toml"
USER_CONFIG_FILENAME = "config.toml"

#: XDG environment variable for the per-user config base directory.
_XDG_CONFIG_HOME = "XDG_CONFIG_HOME"


class TargetResolutionError(ValueError):
    """One or more identity coordinates could not be resolved (fail loud)."""


class InvalidCoordinateError(ValueError):
    """A coordinate value violates the opaque-id segment grammar."""

    def __init__(self, key: str, value: str) -> None:
        super().__init__(
            f"invalid {key} {value!r}: must be lowercase alphanumeric with hyphens, "
            "1-64 chars, no leading/trailing hyphen"
        )
        self.key = key
        self.value = value


@dataclass(frozen=True)
class ResolvedCoordinate:
    """A resolved coordinate value and the source tier that provided it."""

    value: str
    source: str  # "argument" | "environment" | "project-config" | "user-config"


def validate_coordinate(key: str, value: str) -> str:
    """Fail-loud check that ``value`` satisfies the opaque-id segment grammar.

    Returns ``value`` unchanged on success; raises :class:`InvalidCoordinateError`
    otherwise. ``key`` is one of :data:`COORDINATE_KEYS`, used only in the error
    message.
    """
    if not isinstance(value, str) or not _SEGMENT_RE.match(value):
        raise InvalidCoordinateError(key, value)
    return value


def parse_target(target: str) -> dict[str, str]:
    """Split a ``{org-id}/{tenant-id}/{project-id}`` target into its coordinates.

    Fails loud (:class:`TargetResolutionError`) on anything that is not exactly
    three non-empty ``/``-separated segments, so a malformed ``--target`` can
    never silently under-populate the identity tuple.
    """
    parts = target.split("/")
    if len(parts) != len(COORDINATE_KEYS) or not all(parts):
        raise TargetResolutionError(
            "--target must be exactly '{org-id}/{tenant-id}/{project-id}' "
            f"(three non-empty segments); got {target!r}"
        )
    return dict(zip(COORDINATE_KEYS, parts, strict=True))


def user_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the per-user config path ``$XDG_CONFIG_HOME/cairn/config.toml``.

    Falls back to ``~/.config/cairn/config.toml`` when ``XDG_CONFIG_HOME`` is
    unset, per the XDG Base Directory spec.
    """
    environ = os.environ if environ is None else environ
    xdg = environ.get(_XDG_CONFIG_HOME)
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "cairn" / USER_CONFIG_FILENAME


def find_project_config(cwd: Path | str | None = None) -> Path | None:
    """Find the nearest project-local ``.cairn.toml`` from ``cwd`` upward.

    Searches the starting directory and each ancestor (git-style), returning the
    first ``.cairn.toml`` found or ``None``. ``cwd`` defaults to the process
    working directory.
    """
    start = Path.cwd() if cwd is None else Path(cwd)
    for directory in (start, *start.parents):
        candidate = directory / PROJECT_CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _read_target_table(path: Path | None) -> dict[str, str]:
    """Read the ``[target]`` table from a TOML config, returning known coords only.

    A missing file, an unreadable/invalid TOML body, a missing/non-table
    ``[target]``, or non-string/empty values all degrade to "no coordinate from
    this source" rather than raising — a malformed persisted config never blocks
    a push whose coordinates come from a higher tier.
    """
    if path is None or not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    table = data.get(_CONFIG_TABLE)
    if not isinstance(table, dict):
        return {}
    out: dict[str, str] = {}
    for key in COORDINATE_KEYS:
        value = table.get(key)
        if isinstance(value, str) and value:
            out[key] = value
    return out


def load_project_config(cwd: Path | str | None = None) -> dict[str, str]:
    """Load the project-local ``.cairn.toml`` target table (empty if absent)."""
    return _read_target_table(find_project_config(cwd))


def load_user_config(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Load the per-user config target table (empty if absent)."""
    return _read_target_table(user_config_path(environ))


def resolve_target(
    *,
    org_id: str | None = None,
    tenant_id: str | None = None,
    project_id: str | None = None,
    target: str | None = None,
    environ: Mapping[str, str] | None = None,
    project_config: dict[str, str] | None = None,
    user_config: dict[str, str] | None = None,
    cwd: Path | str | None = None,
) -> dict[str, ResolvedCoordinate]:
    """Resolve all three identity coordinates via the [INGEST-032] precedence chain.

    Precedence, highest first: explicit argument (an individual ``org_id`` etc.
    overriding the matching ``target`` segment) > environment variable >
    project-local config > per-user config. Returns a mapping of each
    :data:`COORDINATE_KEYS` entry to a :class:`ResolvedCoordinate` (value +
    source tier).

    ``environ``/``project_config``/``user_config`` are injectable for testing;
    when omitted they are read from the process environment and the filesystem.
    A coordinate that no source supplies raises :class:`TargetResolutionError`
    naming every unresolved coordinate.
    """
    environ = os.environ if environ is None else environ
    if project_config is None:
        project_config = load_project_config(cwd)
    if user_config is None:
        user_config = load_user_config(environ)

    target_coords = parse_target(target) if target else {}
    individual = {"org-id": org_id, "tenant-id": tenant_id, "project-id": project_id}
    # Within the explicit-argument tier an individual switch wins over the same
    # segment of --target, so `--target O/T/P --project-id P2` overrides project.
    argument_layer = {key: (individual[key] or target_coords.get(key)) for key in COORDINATE_KEYS}
    environment_layer = {key: environ.get(ENV_VARS[key]) for key in COORDINATE_KEYS}

    layers: tuple[tuple[str, Mapping[str, str | None]], ...] = (
        ("argument", argument_layer),
        ("environment", environment_layer),
        ("project-config", project_config),
        ("user-config", user_config),
    )

    resolved: dict[str, ResolvedCoordinate] = {}
    unresolved: list[str] = []
    for key in COORDINATE_KEYS:
        for source, layer in layers:
            value = layer.get(key)
            if value:
                resolved[key] = ResolvedCoordinate(value=value, source=source)
                break
        else:
            unresolved.append(key)

    if unresolved:
        raise TargetResolutionError(
            "could not resolve identity coordinate(s): "
            + ", ".join(unresolved)
            + ". Supply via --org-id/--tenant-id/--project-id or "
            "--target {org-id}/{tenant-id}/{project-id}, the "
            "CAIRN_ORG_ID/CAIRN_TENANT_ID/CAIRN_PROJECT_ID environment "
            "variables, or persist a default with 'cairn config set-target'."
        )
    return resolved


def _render_target_toml(coords: Mapping[str, str]) -> str:
    """Serialize the three coordinates as a ``[target]`` TOML table.

    Values are pre-validated opaque ids (``_SEGMENT_RE``) — lowercase
    alphanumeric + hyphens — so they need no TOML string escaping; they are
    still emitted as quoted strings for forward-compatibility.
    """
    lines = [f"[{_CONFIG_TABLE}]"]
    for key in COORDINATE_KEYS:
        lines.append(f'{key} = "{coords[key]}"')
    return "\n".join(lines) + "\n"


def write_user_target(
    coords: Mapping[str, str], *, environ: Mapping[str, str] | None = None
) -> Path:
    """Persist ``coords`` as the default target in the per-user config.

    Validates every coordinate against the opaque-id grammar, creates the
    config directory (mode 0700, matching the project's per-user run-state
    convention), and writes the ``[target]`` table. Returns the written path.
    """
    for key in COORDINATE_KEYS:
        validate_coordinate(key, coords[key])
    path = user_config_path(environ)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(_render_target_toml(coords), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# CLI surface: `cairn config set-target` / `cairn config show`.
# --------------------------------------------------------------------------


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``cairn config`` command group (``set-target``, ``show``)."""
    p = subparsers.add_parser(
        "config",
        help="persist and inspect the default identity target ({org-id}/{tenant-id}/{project-id})",
        description=(
            "Manage the producer's default identity target so push/read commands "
            "need not repeat the {org-id}/{tenant-id}/{project-id} coordinates on "
            "every invocation."
        ),
        formatter_class=CairnHelpFormatter,
        epilog=build_epilog(
            [
                "  cairn config set-target acme-co/acme/payments",
                "  cairn config set-target --org-id acme-co --tenant-id acme --project-id payments",
                "  cairn config show",
            ],
            notes=(
                "set-target writes the per-user config "
                "($XDG_CONFIG_HOME/cairn/config.toml). A project-local .cairn.toml "
                "overrides it; explicit flags and CAIRN_* env vars override both."
            ),
        ),
    )
    config_subparsers = p.add_subparsers(dest="config_command")

    set_target = config_subparsers.add_parser(
        "set-target",
        help="persist the default {org-id}/{tenant-id}/{project-id} to the per-user config",
        formatter_class=CairnHelpFormatter,
        epilog=build_epilog(
            [
                "  cairn config set-target acme-co/acme/payments",
                "  cairn config set-target --org-id acme-co --tenant-id acme --project-id payments",
            ]
        ),
    )
    set_target.add_argument(
        "target",
        nargs="?",
        default=None,
        help="fully-qualified {org-id}/{tenant-id}/{project-id} (alternative to the three flags)",
    )
    set_target.add_argument("--org-id")
    set_target.add_argument("--tenant-id")
    set_target.add_argument("--project-id")
    set_target.set_defaults(handler=run_set_target)

    show = config_subparsers.add_parser(
        "show",
        help="show the resolved default target and each coordinate's source",
        formatter_class=CairnHelpFormatter,
        epilog=build_epilog(["  cairn config show"]),
    )
    show.set_defaults(handler=run_show)

    p.set_defaults(handler=run_config_group)


def run_config_group(args: argparse.Namespace) -> int:
    """Handler for a bare ``cairn config`` with no sub-command — fail loud."""
    return fail(
        "specify a config sub-command: 'set-target' or 'show' "
        "(run 'cairn config --help' for usage)",
        exit_code=2,
    )


def run_set_target(args: argparse.Namespace) -> int:
    """Persist the default identity target to the per-user config."""
    if args.target is not None:
        try:
            coords = parse_target(args.target)
        except TargetResolutionError as e:
            return fail(str(e), exit_code=2)
        # Individual flags refine the positional target's segments when both given.
        for key, value in (
            ("org-id", args.org_id),
            ("tenant-id", args.tenant_id),
            ("project-id", args.project_id),
        ):
            if value:
                coords[key] = value
    else:
        coords = {
            "org-id": args.org_id,
            "tenant-id": args.tenant_id,
            "project-id": args.project_id,
        }
        missing = [key for key in COORDINATE_KEYS if not coords[key]]
        if missing:
            return fail(
                "set-target requires all three coordinates: supply a positional "
                "'{org-id}/{tenant-id}/{project-id}' target or all of "
                "--org-id/--tenant-id/--project-id; missing: " + ", ".join(missing),
                exit_code=2,
            )

    try:
        path = write_user_target(coords)
    except InvalidCoordinateError as e:
        return fail(str(e), exit_code=2)

    print(
        f"wrote default target {coords['org-id']}/{coords['tenant-id']}/{coords['project-id']} "
        f"to {path}"
    )
    return 0


def run_show(args: argparse.Namespace) -> int:
    """Show the resolved default target and the source of each coordinate."""
    try:
        resolved = resolve_target()
    except TargetResolutionError as e:
        return fail(str(e), exit_code=2)
    for key in COORDINATE_KEYS:
        coordinate = resolved[key]
        print(f"{key} = {coordinate.value} ({coordinate.source})")
    return 0

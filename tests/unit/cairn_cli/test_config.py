"""Tests for cairn_cli.config — the [INGEST-032] identity-coordinate resolution
chain plus the ``cairn config set-target`` / ``cairn config show`` commands.

Resolution precedence (highest first): explicit argument (individual switch
over ``--target`` segment) > CAIRN_* environment > project-local ``.cairn.toml``
> per-user ``$XDG_CONFIG_HOME/cairn/config.toml``. An unresolved coordinate
fails loud; a file token is never consumed as a coordinate (enforced at the push
layer — see tests/unit/cairn_cli/test_push.py).
"""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

import pytest
from cairn_cli import config as config_module
from cairn_cli.config import (
    InvalidCoordinateError,
    ResolvedCoordinate,
    TargetResolutionError,
    add_subparser,
    find_project_config,
    load_project_config,
    load_user_config,
    parse_target,
    resolve_target,
    user_config_path,
    validate_coordinate,
    write_user_target,
)

_TRIPLE = {"org-id": "acme-co", "tenant-id": "acme", "project-id": "payments"}


# --------------------------------------------------------------------------
# parse_target
# --------------------------------------------------------------------------


def test_parse_target_splits_three_segments() -> None:
    assert parse_target("o/t/p") == {"org-id": "o", "tenant-id": "t", "project-id": "p"}


@pytest.mark.parametrize("bad", ["o/t", "o/t/p/x", "o//p", "", "o/t/"])
def test_parse_target_rejects_wrong_segment_count(bad: str) -> None:
    with pytest.raises(TargetResolutionError):
        parse_target(bad)


# --------------------------------------------------------------------------
# resolve_target — precedence (pure: layers injected)
# --------------------------------------------------------------------------


def _resolve(**kwargs):
    # Default the injected layers to empty so a test exercises only the tiers it sets.
    kwargs.setdefault("environ", {})
    kwargs.setdefault("project_config", {})
    kwargs.setdefault("user_config", {})
    return resolve_target(**kwargs)


def test_resolve_explicit_args_win() -> None:
    resolved = _resolve(org_id="o", tenant_id="t", project_id="p", environ={"CAIRN_ORG_ID": "ENV"})
    assert resolved["org-id"] == ResolvedCoordinate("o", "argument")
    assert {k: v.value for k, v in resolved.items()} == {
        "org-id": "o",
        "tenant-id": "t",
        "project-id": "p",
    }


def test_resolve_target_segments_populate_all_three() -> None:
    resolved = _resolve(target="o/t/p")
    assert {k: v.value for k, v in resolved.items()} == {
        "org-id": "o",
        "tenant-id": "t",
        "project-id": "p",
    }
    assert all(v.source == "argument" for v in resolved.values())


def test_resolve_individual_arg_overrides_target_segment() -> None:
    resolved = _resolve(target="o/t/p", project_id="p2")
    assert resolved["project-id"].value == "p2"
    assert resolved["org-id"].value == "o"


def test_resolve_environment_tier() -> None:
    resolved = _resolve(
        environ={"CAIRN_ORG_ID": "o", "CAIRN_TENANT_ID": "t", "CAIRN_PROJECT_ID": "p"}
    )
    assert {k: (v.value, v.source) for k, v in resolved.items()} == {
        "org-id": ("o", "environment"),
        "tenant-id": ("t", "environment"),
        "project-id": ("p", "environment"),
    }


def test_resolve_env_overrides_config() -> None:
    resolved = _resolve(
        environ={"CAIRN_ORG_ID": "env"},
        project_config={"org-id": "proj", "tenant-id": "t", "project-id": "p"},
    )
    assert resolved["org-id"] == ResolvedCoordinate("env", "environment")
    assert resolved["tenant-id"] == ResolvedCoordinate("t", "project-config")


def test_resolve_project_config_overrides_user_config() -> None:
    resolved = _resolve(
        project_config={"org-id": "proj"},
        user_config={"org-id": "user", "tenant-id": "t", "project-id": "p"},
    )
    assert resolved["org-id"] == ResolvedCoordinate("proj", "project-config")
    assert resolved["project-id"] == ResolvedCoordinate("p", "user-config")


def test_resolve_unresolved_coordinate_fails_loud_naming_each() -> None:
    with pytest.raises(TargetResolutionError) as excinfo:
        _resolve(org_id="o")  # tenant-id and project-id unresolved
    message = str(excinfo.value)
    assert "tenant-id" in message
    assert "project-id" in message


# --------------------------------------------------------------------------
# Filesystem loaders
# --------------------------------------------------------------------------


def test_user_config_path_uses_xdg_when_set(tmp_path: Path) -> None:
    path = user_config_path({"XDG_CONFIG_HOME": str(tmp_path)})
    assert path == tmp_path / "cairn" / "config.toml"


def test_user_config_path_defaults_to_home_dot_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    path = user_config_path({})
    assert path == Path("/home/tester/.config/cairn/config.toml")


def test_find_project_config_searches_upward(tmp_path: Path) -> None:
    (tmp_path / ".cairn.toml").write_text('[target]\norg-id = "o"\n')
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_project_config(nested) == tmp_path / ".cairn.toml"


def test_find_project_config_absent_returns_none(tmp_path: Path) -> None:
    assert find_project_config(tmp_path) is None


def test_load_project_config_reads_target_table(tmp_path: Path) -> None:
    (tmp_path / ".cairn.toml").write_text(
        '[target]\norg-id = "acme-co"\ntenant-id = "acme"\nproject-id = "payments"\n'
    )
    assert load_project_config(tmp_path) == _TRIPLE


def test_load_user_config_reads_target_table(tmp_path: Path) -> None:
    cfg = tmp_path / "cairn" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[target]\norg-id = "acme-co"\n')
    assert load_user_config({"XDG_CONFIG_HOME": str(tmp_path)}) == {"org-id": "acme-co"}


def test_load_config_tolerates_malformed_toml(tmp_path: Path) -> None:
    (tmp_path / ".cairn.toml").write_text("not = valid = toml = [[[")
    assert load_project_config(tmp_path) == {}


def test_load_config_ignores_non_table_target(tmp_path: Path) -> None:
    (tmp_path / ".cairn.toml").write_text('target = "scalar-not-a-table"\n')
    assert load_project_config(tmp_path) == {}


# --------------------------------------------------------------------------
# validate_coordinate / write_user_target round-trip
# --------------------------------------------------------------------------


@pytest.mark.parametrize("good", ["acme", "acme-co", "a1-b2-c3", "x", "0"])
def test_validate_coordinate_accepts_opaque_ids(good: str) -> None:
    assert validate_coordinate("org-id", good) == good


@pytest.mark.parametrize("bad", ["Acme", "acme_co", "-acme", "acme-", "a" * 65, "", "a/b"])
def test_validate_coordinate_rejects_malformed(bad: str) -> None:
    with pytest.raises(InvalidCoordinateError):
        validate_coordinate("org-id", bad)


def test_write_user_target_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = write_user_target(_TRIPLE)
    assert path == tmp_path / "cairn" / "config.toml"
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    assert data["target"] == _TRIPLE
    # And it loads back through the loader unchanged.
    assert load_user_config({"XDG_CONFIG_HOME": str(tmp_path)}) == _TRIPLE


def test_write_user_target_rejects_malformed_coordinate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(InvalidCoordinateError):
        write_user_target({"org-id": "Bad_Org", "tenant-id": "acme", "project-id": "payments"})


def test_write_user_target_dir_is_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = write_user_target(_TRIPLE)
    mode = path.parent.stat().st_mode & 0o777
    assert mode == 0o700


# --------------------------------------------------------------------------
# CLI surface: set-target / show
# --------------------------------------------------------------------------


def _config_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cairn")
    subparsers = parser.add_subparsers(dest="command")
    add_subparser(subparsers)
    return parser


def test_set_target_positional_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    parser = _config_parser()
    args = parser.parse_args(["config", "set-target", "acme-co/acme/payments"])
    rc = args.handler(args)
    assert rc == 0
    assert load_user_config({"XDG_CONFIG_HOME": str(tmp_path)}) == _TRIPLE
    assert "wrote default target acme-co/acme/payments" in capsys.readouterr().out


def test_set_target_flags_persist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    parser = _config_parser()
    args = parser.parse_args(
        [
            "config",
            "set-target",
            "--org-id",
            "acme-co",
            "--tenant-id",
            "acme",
            "--project-id",
            "payments",
        ]
    )
    assert args.handler(args) == 0
    assert load_user_config({"XDG_CONFIG_HOME": str(tmp_path)}) == _TRIPLE


def test_set_target_flag_refines_positional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    parser = _config_parser()
    args = parser.parse_args(["config", "set-target", "o/t/p", "--project-id", "p2"])
    assert args.handler(args) == 0
    persisted = load_user_config({"XDG_CONFIG_HOME": str(tmp_path)})
    assert persisted == {"org-id": "o", "tenant-id": "t", "project-id": "p2"}


def test_set_target_missing_coordinate_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    parser = _config_parser()
    args = parser.parse_args(["config", "set-target", "--org-id", "acme-co"])
    rc = args.handler(args)
    assert rc == 2
    err = capsys.readouterr().err
    assert "tenant-id" in err and "project-id" in err
    assert not user_config_path({"XDG_CONFIG_HOME": str(tmp_path)}).exists()


def test_set_target_malformed_fails_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    parser = _config_parser()
    args = parser.parse_args(["config", "set-target", "Bad_Org/acme/payments"])
    assert args.handler(args) == 2


def test_show_resolves_and_reports_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for env_var in ("CAIRN_ORG_ID", "CAIRN_TENANT_ID", "CAIRN_PROJECT_ID"):
        monkeypatch.delenv(env_var, raising=False)
    # Persist a user-config default, override one coordinate via the environment.
    write_user_target(_TRIPLE)
    monkeypatch.setenv("CAIRN_PROJECT_ID", "env-proj")
    # Isolate project-local discovery so a stray .cairn.toml never leaks in.
    monkeypatch.setattr(config_module, "load_project_config", lambda cwd=None: {})
    parser = _config_parser()
    args = parser.parse_args(["config", "show"])
    assert args.handler(args) == 0
    out = capsys.readouterr().out
    assert "org-id = acme-co (user-config)" in out
    assert "project-id = env-proj (environment)" in out


def test_show_unresolved_fails_loud(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for env_var in ("CAIRN_ORG_ID", "CAIRN_TENANT_ID", "CAIRN_PROJECT_ID"):
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setattr(config_module, "load_project_config", lambda cwd=None: {})
    monkeypatch.setattr(config_module, "load_user_config", lambda environ=None: {})
    parser = _config_parser()
    args = parser.parse_args(["config", "show"])
    assert args.handler(args) == 2
    assert "could not resolve" in capsys.readouterr().err


def test_bare_config_group_fails_loud(capsys: pytest.CaptureFixture[str]) -> None:
    parser = _config_parser()
    args = parser.parse_args(["config"])
    assert args.handler(args) == 2
    assert "sub-command" in capsys.readouterr().err


def test_config_help_self_documenting() -> None:
    from tests.unit.cairn_cli.conftest import subparser_help

    help_text = subparser_help(_config_parser(), "config")
    assert "usage" in help_text.lower()
    assert "Examples:" in help_text
    assert "set-target" in help_text


def test_set_target_help_has_example() -> None:
    from tests.unit.cairn_cli.conftest import subparser_help

    help_text = subparser_help(_config_parser(), "config", "set-target")
    assert "Examples:" in help_text
    assert "cairn config set-target" in help_text

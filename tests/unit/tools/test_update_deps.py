"""Unit tests for the dependency update task (tools/update_deps.py).

Every test builds its own small fixture repository under `tmp_path` and drives the
tool through a fake runner that stands in for `uv`, `npm`, `git` and `mise`. No test
contacts a registry, and no test shares state, so the suite is safe under `-n auto`.

A `# Covers: [ID]` comment above a test names the spec scenarios that it covers.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "update_deps_under_test", _REPO_ROOT / "tools" / "update_deps.py"
)
assert _spec and _spec.loader
ud = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ud  # required for dataclass annotation resolution
_spec.loader.exec_module(ud)

NOW = dt.datetime(2026, 11, 4, 12, 0, tzinfo=dt.UTC)
CUTOFF = "2026-10-28T12:00:00Z"
GOOD_REGISTRY = "https://pypi.org/simple"
FILES = "https://files.pythonhosted.org/packages/aa/bb"


# --- fixture builders -----------------------------------------------------------------


def uv_pkg(name: str, version: str, *, registry: str | None = GOOD_REGISTRY, **extra: str) -> str:
    """Render one `[[package]]` block of a uv.lock."""
    lines = ["[[package]]", f'name = "{name}"', f'version = "{version}"']
    if registry is None:
        lines.append(f'source = {{ editable = "{extra.get("editable", name)}" }}')
        return "\n".join(lines) + "\n"
    lines.append(f'source = {{ registry = "{registry}" }}')
    url = extra.get("url", f"{FILES}/{name}-{version}")
    file_hash = extra.get("hash", f"sha256:{name.encode().hex()[:16]}{version}")
    lines.append(f'sdist = {{ url = "{url}.tar.gz", hash = "{file_hash}", size = 1 }}')
    lines.append(f'wheels = [{{ url = "{url}-py3-none-any.whl", hash = "{file_hash}", size = 1 }}]')
    return "\n".join(lines) + "\n"


def uv_lock(*blocks: str, span: str | None = "P7D") -> str:
    head = ["version = 1", ""]
    if span:
        head += [
            "[options]",
            'exclude-newer = "0001-01-01T00:00:00Z"',
            f'exclude-newer-span = "{span}"',
            "",
        ]
    return "\n".join(head) + "\n" + "\n".join(blocks)


def npm_lock(entries: dict[str, dict]) -> str:
    return json.dumps({"lockfileVersion": 3, "packages": {"": {"name": "cdk"}, **entries}})


def npm_entry(version: str, *, resolved: str | None = "default", **extra: object) -> dict:
    entry: dict = {"version": version, "integrity": "sha512-abc", **extra}
    if resolved == "default":
        entry["resolved"] = f"https://registry.npmjs.org/x/-/x-{version}.tgz"
    elif resolved is not None:
        entry["resolved"] = resolved
    return entry


MEMBER = """[project]
name = "cairn-oci"
version = "0.1.0"
dependencies = ["pydantic>=2.13.4,<3"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""

ROOT_PYPROJECT = """[project]
name = "cairn-proofs"
version = "0.1.0"

[tool.uv]
package = false
exclude-newer = "7 days"

[tool.uv.workspace]
members = ["libs/*"]

[dependency-groups]
build = ["hatchling==1.33.0"]
dev = ["pytest>=9.0.3,<10", "cairn-oci"]

[tool.uv.sources]
cairn-oci = { workspace = true }
"""

PACKAGE_JSON = json.dumps(
    {"dependencies": {"constructs": "^10.4.0"}, "devDependencies": {"typescript": "~5.9.0"}}
)

BASE_UV = uv_lock(
    uv_pkg("pydantic", "2.13.4"),
    uv_pkg("pytest", "9.0.3"),
    uv_pkg("hatchling", "1.33.0"),
    uv_pkg("oscal-bindings", "0.3.1"),
    uv_pkg("cairn-oci", "0.1.0", registry=None, editable="libs/oci"),
    uv_pkg("cairn-proofs", "0.1.0", registry=None, editable="."),
)
BASE_NPM = npm_lock(
    {
        "node_modules/constructs": npm_entry("10.6.0"),
        "node_modules/typescript": npm_entry("5.9.3"),
    }
)


def make_repo(root: Path, *, uv: str = BASE_UV, npm: str = BASE_NPM) -> Path:
    (root / "libs" / "oci").mkdir(parents=True)
    (root / "cdk").mkdir()
    (root / "pyproject.toml").write_text(ROOT_PYPROJECT)
    (root / "libs" / "oci" / "pyproject.toml").write_text(MEMBER)
    (root / "uv.lock").write_text(uv)
    (root / "cdk" / "package.json").write_text(PACKAGE_JSON)
    (root / "cdk" / "package-lock.json").write_text(npm)
    return root


def ok(stdout: str = "") -> ud.Result:
    return ud.Result(0, stdout, "")


class FakeRunner:
    """Stand-in for uv, npm, git and mise: scripted per command, records every call."""

    def __init__(self, root: Path, **handlers: Callable[[Sequence[str], Path], ud.Result]) -> None:
        self.root = root
        self.calls: list[tuple[list[str], Path, bool]] = []
        self.handlers: dict[str, Callable[[Sequence[str], Path], ud.Result]] = {
            "git status": lambda argv, cwd: ok(""),
            "uv lock": lambda argv, cwd: ok(),
            "npm update": lambda argv, cwd: ok(),
            "npm ci": lambda argv, cwd: ok(),
            "npm audit": lambda argv, cwd: ok(),
            "mise run": lambda argv, cwd: ok(),
        }
        self.handlers.update({k.replace("_", " "): v for k, v in handlers.items()})

    def __call__(self, argv: Sequence[str], *, cwd: Path, capture: bool = True) -> ud.Result:
        self.calls.append((list(argv), cwd, capture))
        return self.handlers[" ".join(argv[:2])](argv, cwd)

    def commands(self) -> list[str]:
        return [" ".join(argv[:2]) for argv, _, _ in self.calls]


def writes(path: str, text: str, then: ud.Result | None = None):
    def handler(argv: Sequence[str], cwd: Path) -> ud.Result:
        (cwd if cwd.name != "cdk" else cwd.parent).joinpath(path).write_text(text)
        return then or ok()

    return handler


NEW_UV = uv_lock(
    uv_pkg("pydantic", "2.14.2"),
    uv_pkg("pytest", "9.2.0"),
    uv_pkg("hatchling", "1.33.0"),
    uv_pkg("oscal-bindings", "0.3.1"),
    uv_pkg("newdep", "1.0.0"),
    uv_pkg("cairn-oci", "0.1.0", registry=None, editable="libs/oci"),
    uv_pkg("cairn-proofs", "0.1.0", registry=None, editable="."),
)
NEW_NPM = npm_lock(
    {
        "node_modules/constructs": npm_entry("10.8.1"),
        "node_modules/typescript": npm_entry("5.9.3"),
    }
)


def run(root: Path, runner: FakeRunner, tmp_path: Path, out=None, err=None) -> tuple[int, str, str]:
    out_buf, err_buf = io.StringIO(), io.StringIO()
    code = ud.run_update(
        root,
        runner,
        now=NOW,
        out=out or out_buf,
        err=err or err_buf,
        lock_path=tmp_path / "locks" / "update-deps.lock",
    )
    return code, out_buf.getvalue(), err_buf.getvalue()


def snapshot_bytes(root: Path) -> dict[str, bytes]:
    return {rel: (root / rel).read_bytes() for rel in (ud.UV_LOCK, ud.NPM_LOCK)}


def happy_runner(root: Path, **overrides) -> FakeRunner:
    handlers = {
        "uv_lock": writes("uv.lock", NEW_UV),
        "npm_update": writes("cdk/package-lock.json", NEW_NPM),
    }
    handlers.update(overrides)
    return FakeRunner(root, **handlers)


# --- 3.1 runner and environment ---------------------------------------------------------


def test_child_env_keeps_only_allowlisted_names() -> None:
    # Covers: [TOOLS-020.02]
    environ = {
        "PATH": "/bin",
        "HOME": "/h",
        "TMPDIR": "/t",
        "LANG": "C",
        "TERM": "xterm",
        "XDG_RUNTIME_DIR": "/run",
        "MISE_ENV": "x",
        "AWS_ACCESS_KEY_ID": "k",
        "AWS_PROFILE": "p",
        "UV_INDEX_URL": "https://evil.example",
        "npm_config_registry": "https://evil.example",
        "NPM_TOKEN": "t",
        "GITHUB_TOKEN": "t",
    }
    kept = ud.child_env(environ)
    assert set(kept) == {"PATH", "HOME", "TMPDIR", "LANG", "TERM", "XDG_RUNTIME_DIR", "MISE_ENV"}


def test_runner_uses_argv_list_no_shell_and_minimal_env(monkeypatch, tmp_path) -> None:
    # Covers: [TOOLS-020.02]
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 0, "out", "err")

    monkeypatch.setattr(ud.subprocess, "run", fake_run)
    runner = ud.make_runner({"PATH": "/bin", "AWS_SECRET_ACCESS_KEY": "s"}, timeout=7)
    result = runner(["uv", "lock"], cwd=tmp_path)
    assert result == ud.Result(0, "out", "err")
    assert seen["argv"] == ["uv", "lock"]
    assert seen["shell"] is False
    assert seen["env"] == {"PATH": "/bin"}
    assert seen["timeout"] == 7


def test_runner_reports_timeout_and_missing_command(monkeypatch, tmp_path) -> None:
    def hang(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(ud.subprocess, "run", hang)
    with pytest.raises(ud.UpdateError, match="timed out after 5 seconds"):
        ud.make_runner({}, timeout=5)(["npm", "update"], cwd=tmp_path)

    def missing(argv, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(ud.subprocess, "run", missing)
    with pytest.raises(ud.UpdateError, match="command not found: uv"):
        ud.make_runner({})(["uv", "lock"], cwd=tmp_path)


def test_esc_escapes_control_characters() -> None:
    assert ud.esc("pkg\x1b[31m\nname\x00") == "pkg\\x1b[31m\\x0aname\\x00"


# --- 3.2 pre-resolution checks ------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "majors"),
    [
        (">=2.13.4,<3", {2}),
        (">=8.2.7,<9", {8}),
        (">=0.63b1,<1", {0}),
        ("==0.3.1", {0}),
        ("~=2.5", {2}),
        (">=1.0,<=1.9", {1}),
        (">=1.0,!=1.5,<2", {1}),
    ],
)
def test_python_range_accepts_bounded_ranges(spec: str, majors: set[int]) -> None:
    # Covers: [TOOLS-015.03]
    assert ud.check_python_range("pkg", spec, majors) is None


@pytest.mark.parametrize(
    ("spec", "majors"),
    [
        (">=1.0", {1}),
        ("", {1}),
        (">=1.0,<3", {1}),
        (">=1.0,<=2", {1}),
        ("~=2", {2}),
        (">=1.0,<2 junk", {1}),
    ],
)
def test_python_range_rejects_ranges_that_admit_the_next_major(spec: str, majors: set[int]) -> None:
    # Covers: [TOOLS-015.02]
    assert ud.check_python_range("pkg", spec, majors) is not None


@pytest.mark.parametrize("spec", ["^1.2.3", "~5.9.0", "1.2.3", "^0.5.21", "1.0.0-rc.1"])
def test_npm_range_accepts_exact_tilde_and_caret(spec: str) -> None:
    assert ud.check_npm_range("pkg", spec) is None


@pytest.mark.parametrize(
    "spec",
    ["*", "latest", ">=1.0.0", "1.x", "^1.2", "git+https://x/y.git", "file:../y", "npm:other@1"],
)
def test_npm_range_rejects_other_forms(spec: str) -> None:
    # Covers: [TOOLS-015.02]
    assert ud.check_npm_range("pkg", spec) is not None


def test_preflight_passes_and_reports_direct_dependencies(tmp_path) -> None:
    root = make_repo(tmp_path)
    pre = ud.preflight(root)
    assert pre.cooldown_days == 7
    assert pre.python_direct == {"pydantic", "pytest", "hatchling"}
    assert pre.npm_direct == {"constructs", "typescript"}
    assert set(pre.hashes) == {"pyproject.toml", "libs/oci/pyproject.toml", "cdk/package.json"}


def test_preflight_passes_on_the_committed_manifests() -> None:
    # Covers: [TOOLS-015.02]
    assert ud.preflight(_REPO_ROOT).cooldown_days >= 1


def test_preflight_names_every_unbounded_dependency(tmp_path) -> None:
    # Covers: [TOOLS-015.02]
    root = make_repo(tmp_path)
    member = root / "libs" / "oci" / "pyproject.toml"
    member.write_text(member.read_text().replace('"pydantic>=2.13.4,<3"', '"pydantic>=2.5.0"'))
    package_json = root / "cdk" / "package.json"
    package_json.write_text(json.dumps({"dependencies": {"left-pad": ">=1.0.0"}}))
    with pytest.raises(ud.UpdateError) as caught:
        ud.preflight(root)
    text = str(caught.value)
    assert "libs/oci/pyproject.toml" in text and "pydantic" in text
    assert "cdk/package.json" in text and "left-pad" in text


def test_preflight_rejects_direct_url_requirement(tmp_path) -> None:
    # Covers: [TOOLS-015.05]
    root = make_repo(tmp_path)
    member = root / "libs" / "oci" / "pyproject.toml"
    member.write_text(
        member.read_text().replace('"pydantic>=2.13.4,<3"', '"evil @ https://example.com/evil.whl"')
    )
    with pytest.raises(ud.UpdateError, match=r"libs/oci/pyproject.toml.*direct URL"):
        ud.preflight(root)


def test_preflight_rejects_non_workspace_source_and_unread_tables(tmp_path) -> None:
    # Covers: [TOOLS-015.05]
    root = make_repo(tmp_path)
    pyproject = root / "pyproject.toml"
    text = pyproject.read_text()
    pyproject.write_text(
        text.replace(
            "cairn-oci = { workspace = true }", 'cairn-oci = { git = "https://example.com/x.git" }'
        )
    )
    with pytest.raises(ud.UpdateError, match="cairn-oci is not 'workspace = true'"):
        ud.preflight(root)
    pyproject.write_text(
        text.replace("package = false", 'package = false\ndev-dependencies = ["x"]')
    )
    with pytest.raises(ud.UpdateError, match="dev-dependencies is not read"):
        ud.preflight(root)


def test_preflight_rejects_unread_dependency_group_entry_and_npm_overrides(tmp_path) -> None:
    # Covers: [TOOLS-015.05]
    root = make_repo(tmp_path)
    pyproject = root / "pyproject.toml"
    pyproject.write_text(
        ROOT_PYPROJECT.replace('dev = ["pytest', 'dev = [{ weird = "x" }, "pytest')
    )
    with pytest.raises(ud.UpdateError, match="entry that is not read"):
        ud.preflight(root)
    pyproject.write_text(
        ROOT_PYPROJECT.replace('dev = ["pytest', 'dev = [{ include-group = "build" }, "pytest')
    )
    assert ud.preflight(root).cooldown_days == 7
    (root / "cdk" / "package.json").write_text(
        json.dumps({"dependencies": {"a": "^1.0.0"}, "overrides": {"a": "2.0.0"}})
    )
    with pytest.raises(ud.UpdateError, match="overrides is not read"):
        ud.preflight(root)


def test_preflight_checks_override_and_constraint_tables(tmp_path) -> None:
    # Covers: [TOOLS-015.05]
    root = make_repo(tmp_path)
    pyproject = root / "pyproject.toml"
    pyproject.write_text(
        ROOT_PYPROJECT.replace(
            "package = false", 'package = false\noverride-dependencies = ["urllib3>=2"]'
        )
    )
    with pytest.raises(ud.UpdateError, match=r"tool\.uv\.override-dependencies: urllib3"):
        ud.preflight(root)


def test_preflight_rejects_member_with_another_build_backend(tmp_path) -> None:
    # Covers: [TOOLS-015.07]
    root = make_repo(tmp_path)
    member = root / "libs" / "oci" / "pyproject.toml"
    member.write_text(
        member.read_text().replace('requires = ["hatchling"]', 'requires = ["setuptools"]')
    )
    with pytest.raises(ud.UpdateError, match=r"libs/oci/pyproject.toml.*hatchling"):
        ud.preflight(root)


@pytest.mark.parametrize("value", [None, "7", "7 weeks", "soon", "2026-10-01", "7  days"])
def test_preflight_rejects_missing_or_malformed_cooldown(tmp_path, value) -> None:
    # Covers: [TOOLS-019.04]
    root = make_repo(tmp_path)
    text = ROOT_PYPROJECT
    text = text.replace(
        'exclude-newer = "7 days"\n', "" if value is None else f'exclude-newer = "{value}"\n'
    )
    (root / "pyproject.toml").write_text(text)
    with pytest.raises(ud.UpdateError, match="exclude-newer"):
        ud.preflight(root)


def test_preflight_accepts_one_day_cooldown(tmp_path) -> None:
    root = make_repo(tmp_path)
    (root / "pyproject.toml").write_text(ROOT_PYPROJECT.replace("7 days", "1 day"))
    assert ud.preflight(root).cooldown_days == 1


@pytest.mark.parametrize(
    ("relative", "content", "needle"),
    [
        ("uv.toml", 'index-url = "https://evil.example/simple"\n', "uv.toml sets index-url"),
        ("cdk/uv.toml", '[[index]]\nurl = "https://x"\n', "cdk/uv.toml sets index"),
        (".npmrc", "registry=https://evil.example/\n", ".npmrc sets a registry"),
        ("cdk/.npmrc", "@scope:registry=https://evil.example/\n", "cdk/.npmrc sets a registry"),
        (
            "pip.conf",
            "[global]\nextra-index-url = https://evil.example\n",
            "pip.conf sets a package index",
        ),
    ],
)
def test_preflight_rejects_index_or_registry_files(tmp_path, relative, content, needle) -> None:
    # Covers: [TOOLS-020.03]
    root = make_repo(tmp_path)
    (root / relative).write_text(content)
    with pytest.raises(ud.UpdateError, match=needle):
        ud.preflight(root)


def test_preflight_accepts_harmless_config_files(tmp_path) -> None:
    root = make_repo(tmp_path)
    (root / ".npmrc").write_text("fund=false\n")
    (root / "uv.toml").write_text('python-preference = "managed"\n')
    assert ud.preflight(root).cooldown_days == 7


def test_preflight_rejects_index_in_tool_uv(tmp_path) -> None:
    # Covers: [TOOLS-020.03]
    root = make_repo(tmp_path)
    (root / "pyproject.toml").write_text(
        ROOT_PYPROJECT.replace(
            "package = false", 'package = false\nindex-url = "https://evil.example"'
        )
    )
    with pytest.raises(ud.UpdateError, match="sets index-url"):
        ud.preflight(root)


def test_manifest_change_after_the_check_is_detected(tmp_path) -> None:
    # Covers: [TOOLS-015.06]
    root = make_repo(tmp_path)
    pre = ud.preflight(root)
    ud.recheck_manifests(root, pre.hashes)
    (root / "libs" / "oci" / "pyproject.toml").write_text(MEMBER + "\n# edited\n")
    with pytest.raises(ud.UpdateError, match=r"libs/oci/pyproject.toml changed"):
        ud.recheck_manifests(root, pre.hashes)


# --- 3.3 lock audit ----------------------------------------------------------------------


def audit(uv: str, npm: str, tmp_path: Path) -> list[str]:
    root = make_repo(tmp_path, uv=uv, npm=npm)
    return ud.audit_locks(root)[0]


def test_audit_passes_for_good_lockfiles(tmp_path) -> None:
    # Covers: [TOOLS-021.01]
    assert audit(BASE_UV, BASE_NPM, tmp_path) == []


def test_audit_passes_for_the_committed_lockfiles() -> None:
    # Covers: [TOOLS-021.01]
    problems, summary = ud.audit_locks(_REPO_ROOT)
    assert problems == [] and "uv packages" in summary


def test_audit_accepts_bundled_npm_entries_without_resolved(tmp_path) -> None:
    # Covers: [TOOLS-021.01]
    npm = npm_lock(
        {
            "node_modules/a": npm_entry("1.0.0"),
            "node_modules/a/node_modules/b": {"version": "2.0.0", "inBundle": True},
        }
    )
    assert audit(BASE_UV, npm, tmp_path) == []


def test_audit_names_package_with_foreign_registry(tmp_path) -> None:
    # Covers: [TOOLS-021.02]
    uv = uv_lock(uv_pkg("badpkg", "1.0.0", registry="https://evil.example/simple"))
    problems = audit(uv, BASE_NPM, tmp_path)
    assert any("badpkg" in p and "not PyPI" in p for p in problems)


def test_audit_names_package_with_missing_hash_and_foreign_file_url(tmp_path) -> None:
    # Covers: [TOOLS-021.02]
    uv = uv_lock(
        uv_pkg("nohash", "1.0.0", hash="md5:abc"),
        uv_pkg("badurl", "1.0.0", url="https://evil.example/files/badurl-1.0.0"),
    )
    problems = audit(uv, BASE_NPM, tmp_path)
    assert any("nohash" in p and "sha256" in p for p in problems)
    assert any("badurl" in p and "pythonhosted" in p for p in problems)


@pytest.mark.parametrize(
    "source", ['{ git = "https://x/y.git" }', '{ url = "https://x/y.whl" }', '{ path = "../y" }']
)
def test_audit_rejects_git_url_and_path_sources(tmp_path, source) -> None:
    # Covers: [TOOLS-021.02]
    uv = f'version = 1\n\n[[package]]\nname = "gitpkg"\nversion = "1.0.0"\nsource = {source}\n'
    assert any("gitpkg" in p for p in audit(uv, BASE_NPM, tmp_path))


def test_audit_rejects_non_editable_cairn_package(tmp_path) -> None:
    # Covers: [TOOLS-021.02]
    uv = uv_lock(uv_pkg("cairn-oci", "0.1.0"))
    assert any("cairn-oci" in p and "editable" in p for p in audit(uv, BASE_NPM, tmp_path))


def test_audit_names_npm_packages_with_bad_source_or_integrity(tmp_path) -> None:
    # Covers: [TOOLS-021.02]
    npm = npm_lock(
        {
            "node_modules/hosted": npm_entry("1.0.0", resolved="https://evil.example/h.tgz"),
            "node_modules/gitdep": npm_entry("1.0.0", resolved="git+ssh://git@x/y.git"),
            "node_modules/linked": {"version": "1.0.0", "link": True, "resolved": "../linked"},
            "node_modules/noint": {
                "version": "1.0.0",
                "resolved": "https://registry.npmjs.org/n.tgz",
            },
            "node_modules/noresolved": npm_entry("1.0.0", resolved=None),
        }
    )
    text = "\n".join(audit(BASE_UV, npm, tmp_path))
    for name in ("hosted", "gitdep", "linked", "noint", "noresolved"):
        assert name in text


def test_audit_command_exit_codes(tmp_path, capsys) -> None:
    # Covers: [TOOLS-021.01] [TOOLS-021.02]
    good = make_repo(tmp_path / "good")
    assert ud.audit_command(good) == 0
    assert "lock-audit: OK" in capsys.readouterr().out
    bad = make_repo(
        tmp_path / "bad",
        uv=uv_lock(uv_pkg("badpkg", "1.0.0", registry="https://evil.example/simple")),
    )
    assert ud.audit_command(bad) == 1
    assert "badpkg" in capsys.readouterr().err


# --- 3.4 snapshot and restore -----------------------------------------------------------


def test_atomic_write_replaces_content_and_keeps_mode(tmp_path) -> None:
    target = tmp_path / "f.lock"
    target.write_text("old")
    target.chmod(0o640)
    ud.atomic_write(target, b"new")
    assert target.read_bytes() == b"new"
    assert target.stat().st_mode & 0o777 == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["f.lock"]


def test_run_refuses_when_a_lockfile_has_uncommitted_changes(tmp_path) -> None:
    # Covers: [TOOLS-016.03]
    root = make_repo(tmp_path / "repo")
    before = snapshot_bytes(root)
    runner = FakeRunner(root, git_status=lambda argv, cwd: ok(" M uv.lock\n"))
    code, _, err = run(root, runner, tmp_path)
    assert code == 1
    assert "git restore uv.lock cdk/package-lock.json" in err
    assert runner.commands() == ["git status"]
    assert snapshot_bytes(root) == before


def test_failure_in_uv_step_leaves_lockfiles_unchanged_and_names_package(tmp_path) -> None:
    # Covers: [TOOLS-016.02] [TOOLS-020.01]
    root = make_repo(tmp_path / "repo")
    before = snapshot_bytes(root)
    failing = ud.Result(
        2, "", "error: Distribution `sdistonly==1.0.0` has no wheel and --no-build is set\n"
    )
    runner = FakeRunner(root, uv_lock=writes("uv.lock", NEW_UV, failing))
    code, _, err = run(root, runner, tmp_path)
    assert code == 1
    assert "sdistonly" in err
    assert snapshot_bytes(root) == before
    assert "mise run" not in runner.commands()


def test_failure_in_second_ecosystem_restores_the_first(tmp_path) -> None:
    # Covers: [TOOLS-016.01]
    root = make_repo(tmp_path / "repo")
    before = snapshot_bytes(root)
    failing = ud.Result(
        1, "", "npm error notarget No matching version found for left-pad@^9.9.9.\n"
    )
    runner = happy_runner(root, npm_update=writes("cdk/package-lock.json", NEW_NPM, failing))
    code, _, err = run(root, runner, tmp_path)
    assert code == 1
    assert "npm update" in err and "left-pad" in err
    assert snapshot_bytes(root) == before


def test_unresolvable_dependency_is_named(tmp_path) -> None:
    # Covers: [TOOLS-016.02]
    root = make_repo(tmp_path / "repo")
    before = snapshot_bytes(root)
    failing = ud.Result(1, "", "  x No solution found: because `pydantic>=99` is unavailable\n")
    runner = FakeRunner(root, uv_lock=lambda argv, cwd: failing)
    code, _, err = run(root, runner, tmp_path)
    assert code == 1 and "pydantic" in err
    assert snapshot_bytes(root) == before


@pytest.mark.parametrize("signal_number", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_termination_signals_restore_both_lockfiles(tmp_path, signal_number) -> None:
    # Covers: [TOOLS-016.04]
    root = make_repo(tmp_path / "repo")
    before = snapshot_bytes(root)

    def interrupted(argv, cwd):
        (root / "uv.lock").write_text(NEW_UV)
        signal.raise_signal(signal_number)
        return ok()

    previous = signal.getsignal(signal_number)
    runner = FakeRunner(root, uv_lock=interrupted)
    code, _, err = run(root, runner, tmp_path)
    assert code == 130
    assert "interrupted" in err
    assert snapshot_bytes(root) == before
    assert signal.getsignal(signal_number) == previous


def test_failed_restore_of_first_file_does_not_skip_the_second(tmp_path, monkeypatch) -> None:
    # Covers: [TOOLS-016.05]
    root = make_repo(tmp_path / "repo")
    before = snapshot_bytes(root)
    real = ud.atomic_write

    def flaky(path: Path, data: bytes) -> None:
        if path.name == "uv.lock":
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(ud, "atomic_write", flaky)
    runner = happy_runner(root, npm_update=lambda argv, cwd: ud.Result(1, "", "boom"))
    code, _, err = run(root, runner, tmp_path)
    assert code == 1
    assert "could not restore" in err and "uv.lock" in err and "disk full" in err
    assert "git restore uv.lock cdk/package-lock.json" in err
    assert (root / "cdk" / "package-lock.json").read_bytes() == before["cdk/package-lock.json"]


def test_second_concurrent_run_stops_without_changing_files(tmp_path) -> None:
    # Covers: [TOOLS-016.06]
    root = make_repo(tmp_path / "repo")
    before = snapshot_bytes(root)
    lock_path = tmp_path / "locks" / "update-deps.lock"
    with ud.run_lock(lock_path):
        runner = happy_runner(root)
        code, _, err = run(root, runner, tmp_path)
    assert code == 1 and "another update-deps run is active" in err
    assert runner.calls == []
    assert snapshot_bytes(root) == before
    with ud.run_lock(lock_path):  # released after the first holder exits
        pass


def test_lock_directory_must_be_private(tmp_path) -> None:
    directory = tmp_path / "shared"
    directory.mkdir()
    directory.chmod(0o777)
    with pytest.raises(ud.UpdateError, match="not private"):
        with ud.run_lock(directory / "x.lock"):
            pass


def test_default_lock_path_uses_runtime_dir_then_tmpdir() -> None:
    uid = os.getuid()
    assert ud.default_lock_path({"XDG_RUNTIME_DIR": "/run/u"}) == Path(
        f"/run/u/cairn-update-deps-{uid}/update-deps.lock"
    )
    assert ud.default_lock_path({"TMPDIR": "/var/t"}).parent == Path(
        f"/var/t/cairn-update-deps-{uid}"
    )


# --- 3.5 resolution steps -----------------------------------------------------------------


def test_happy_path_runs_steps_in_order_with_expected_options(tmp_path) -> None:
    # Covers: [TOOLS-014.01] [TOOLS-019.01]
    root = make_repo(tmp_path / "repo")
    runner = happy_runner(root)
    code, out, _ = run(root, runner, tmp_path)
    assert code == 0
    assert runner.commands() == [
        "git status",
        "uv lock",
        "npm update",
        "npm ci",
        "npm audit",
        "mise run",
    ]
    by_name = {" ".join(argv[:2]): (argv, cwd, capture) for argv, cwd, capture in runner.calls}
    assert by_name["uv lock"][0] == [
        "uv",
        "lock",
        "--upgrade",
        "--no-build",
        "--default-index",
        "https://pypi.org/simple",
    ]
    npm_argv = by_name["npm update"][0]
    assert {"--package-lock-only", "--ignore-scripts"} <= set(npm_argv)
    assert npm_argv[npm_argv.index("--registry") + 1] == "https://registry.npmjs.org/"
    audit_argv = by_name["npm audit"][0]
    assert audit_argv[audit_argv.index("--registry") + 1] == "https://registry.npmjs.org/"
    assert npm_argv[npm_argv.index("--before") + 1] == CUTOFF
    assert by_name["npm update"][1] == root / "cdk" and by_name["npm audit"][1] == root / "cdk"
    assert by_name["mise run"][0] == ["mise", "run", "check"] and by_name["mise run"][2] is False
    assert "pydantic 2.13.4 -> 2.14.2" in out and "constructs 10.6.0 -> 10.8.1" in out
    assert (root / "uv.lock").read_text() == NEW_UV


def test_exact_pins_do_not_appear_in_the_report(tmp_path) -> None:
    # Covers: [TOOLS-014.02]
    root = make_repo(tmp_path / "repo")
    _, out, _ = run(root, happy_runner(root), tmp_path)
    assert "oscal-bindings" not in out


def test_direct_dependency_crossing_a_major_version_fails_and_restores(tmp_path) -> None:
    # Covers: [TOOLS-015.01]
    root = make_repo(tmp_path / "repo")
    before = snapshot_bytes(root)
    crossing = uv_lock(
        uv_pkg("pydantic", "3.0.0"),
        uv_pkg("pytest", "9.0.3"),
        uv_pkg("hatchling", "1.33.0"),
        uv_pkg("cairn-oci", "0.1.0", registry=None, editable="libs/oci"),
    )
    runner = happy_runner(root, uv_lock=writes("uv.lock", crossing))
    code, _, err = run(root, runner, tmp_path)
    assert code == 1
    assert "pydantic" in err and "major version changed" in err
    assert snapshot_bytes(root) == before
    assert "mise run" not in runner.commands()


def test_npm_direct_dependency_major_change_fails(tmp_path) -> None:
    # Covers: [TOOLS-015.01]
    root = make_repo(tmp_path / "repo")
    before = snapshot_bytes(root)
    crossing = npm_lock(
        {
            "node_modules/constructs": npm_entry("11.0.0"),
            "node_modules/typescript": npm_entry("5.9.3"),
        }
    )
    runner = happy_runner(root, npm_update=writes("cdk/package-lock.json", crossing))
    code, _, err = run(root, runner, tmp_path)
    assert code == 1 and "constructs" in err
    assert snapshot_bytes(root) == before


def test_zero_x_minor_move_of_a_direct_dependency_is_allowed(tmp_path) -> None:
    # Covers: [TOOLS-015.03]
    uv = BASE_UV.replace('version = "2.13.4"', 'version = "0.63b1"', 1)
    new = NEW_UV.replace('version = "2.14.2"', 'version = "0.66b2"', 1)
    root = make_repo(tmp_path / "repo", uv=uv)
    # a locked 0.x version needs a matching bound in the fixture manifest
    member = root / "libs" / "oci" / "pyproject.toml"
    member.write_text(member.read_text().replace(">=2.13.4,<3", ">=0.63b1,<1"))
    code, out, _ = run(root, happy_runner(root, uv_lock=writes("uv.lock", new)), tmp_path)
    assert code == 0 and "pydantic 0.63b1 -> 0.66b2" in out


def test_transitive_major_change_is_reported_not_blocked(tmp_path) -> None:
    # Covers: [TOOLS-015.04]
    uv = BASE_UV + uv_pkg("protobuf", "6.33.6")
    new = NEW_UV + uv_pkg("protobuf", "7.36.2")
    root = make_repo(tmp_path / "repo", uv=uv)
    code, out, _ = run(root, happy_runner(root, uv_lock=writes("uv.lock", new)), tmp_path)
    assert code == 0 and "protobuf 6.33.6 -> 7.36.2" in out


def test_source_only_package_fails_naming_it(tmp_path) -> None:
    # Covers: [TOOLS-020.01]
    root = make_repo(tmp_path / "repo")
    before = snapshot_bytes(root)
    result = ud.Result(
        2, "", "error: Distribution `legacy==2.0` is marked `--no-build` but has no wheel\n"
    )
    code, _, err = run(root, FakeRunner(root, uv_lock=lambda argv, cwd: result), tmp_path)
    assert code == 1 and "legacy" in err
    assert snapshot_bytes(root) == before


def test_failed_signature_check_restores_both_lockfiles(tmp_path) -> None:
    # Covers: [TOOLS-021.03]
    root = make_repo(tmp_path / "repo")
    before = snapshot_bytes(root)
    bad = ud.Result(1, "", "npm error 1 package has an invalid registry signature: badsig@1.0.0\n")
    code, _, err = run(root, happy_runner(root, npm_audit=lambda argv, cwd: bad), tmp_path)
    assert code == 1 and "npm audit signatures" in err and "badsig" in err
    assert snapshot_bytes(root) == before


def test_failed_lock_audit_after_update_restores_both_lockfiles(tmp_path) -> None:
    # Covers: [TOOLS-021.03]
    root = make_repo(tmp_path / "repo")
    before = snapshot_bytes(root)
    foreign = NEW_UV + uv_pkg("foreignpkg", "1.0.0", registry="https://evil.example/simple")
    runner = happy_runner(root, uv_lock=writes("uv.lock", foreign))
    code, _, err = run(root, runner, tmp_path)
    assert code == 1 and "lock audit failed" in err and "foreignpkg" in err
    assert snapshot_bytes(root) == before
    assert "mise run" not in runner.commands()


def test_update_that_drops_the_cooldown_record_fails(tmp_path) -> None:
    # Covers: [TOOLS-019.02]
    root = make_repo(tmp_path / "repo")
    before = snapshot_bytes(root)
    no_span = "version = 1\n" + "".join("[[package]]" + b for b in NEW_UV.split("[[package]]")[1:])
    code, _, err = run(root, happy_runner(root, uv_lock=writes("uv.lock", no_span)), tmp_path)
    assert code == 1 and "7-day cooldown" in err
    assert snapshot_bytes(root) == before


def test_manifest_edited_during_the_run_fails_the_run(tmp_path) -> None:
    # Covers: [TOOLS-015.06]
    root = make_repo(tmp_path / "repo")
    before = snapshot_bytes(root)

    def edit_manifest(argv, cwd):
        (root / "libs" / "oci" / "pyproject.toml").write_text(MEMBER + "\n# changed\n")
        return ok("")

    runner = happy_runner(root, git_status=edit_manifest)
    code, _, err = run(root, runner, tmp_path)
    assert code == 1 and "changed after the range check" in err
    assert "uv lock" not in runner.commands()
    assert snapshot_bytes(root) == before


def test_range_failure_stops_before_any_command(tmp_path) -> None:
    # Covers: [TOOLS-015.02]
    root = make_repo(tmp_path / "repo")
    (root / "pyproject.toml").write_text(ROOT_PYPROJECT.replace("<10", ""))
    runner = happy_runner(root)
    code, _, err = run(root, runner, tmp_path)
    assert code == 1 and "pytest" in err and "pyproject.toml" in err
    assert runner.calls == []


def test_describe_failure_names_package_or_falls_back_to_the_step() -> None:
    named = ud.describe_failure(
        "uv lock", ud.Result(1, "", "x because `numpy>=2` conflicts\nlast line")
    )
    assert "uv lock failed for numpy" in named and "last line" in named
    plain = ud.describe_failure("npm update", ud.Result(1, "", "network down"))
    assert plain.startswith("npm update failed:") and "network down" in plain
    assert "\x1b" not in ud.describe_failure("uv lock", ud.Result(1, "", "bad \x1b[31m text"))


def test_cutoff_date_subtracts_whole_days_in_utc() -> None:
    assert ud.cutoff_date(NOW, 7) == CUTOFF
    local = dt.datetime(2026, 11, 4, 23, 30, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    assert ud.cutoff_date(local, 1) == "2026-11-04T04:30:00Z"


# --- 3.6 report and gate ------------------------------------------------------------------


def test_report_lists_changed_added_and_removed_packages() -> None:
    # Covers: [TOOLS-017.01]
    old = ud.parse_uv_lock(ud.tomllib.loads(BASE_UV))
    new = ud.parse_uv_lock(ud.tomllib.loads(NEW_UV.replace(uv_pkg("hatchling", "1.33.0"), "")))
    report = ud.build_report(old, new, [], [], CUTOFF)
    assert "pydantic 2.13.4 -> 2.14.2" in report
    assert "newdep added 1.0.0" in report
    assert "hatchling removed 1.33.0" in report
    assert f"Release cutoff: {CUTOFF}" in report
    assert report.rstrip().endswith(ud.REVIEW_LINE)
    assert "CDK (cdk/package-lock.json):\n  no changes" in report


def test_report_lists_source_and_hash_change_at_equal_version() -> None:
    # Covers: [TOOLS-017.03]
    old = ud.parse_uv_lock(ud.tomllib.loads(uv_lock(uv_pkg("same", "1.0.0"))))
    changed = uv_lock(uv_pkg("same", "1.0.0", hash="sha256:ffffffff"))
    new = ud.parse_uv_lock(ud.tomllib.loads(changed))
    old_npm = ud.parse_npm_lock(json.loads(npm_lock({"node_modules/n": npm_entry("1.0.0")})))
    new_npm = ud.parse_npm_lock(
        json.loads(
            npm_lock({"node_modules/n": npm_entry("1.0.0", resolved="https://other.example/n.tgz")})
        )
    )
    report = ud.build_report(old, new, old_npm, new_npm, CUTOFF)
    assert "same 1.0.0 source or hash changed at an equal version" in report
    assert "n 1.0.0 source or hash changed at an equal version" in report


def test_report_escapes_control_characters_in_names() -> None:
    # Covers: [TOOLS-017.03]
    evil = ud.Pkg("evil\x1b[2J\nname", "1.0.0", "s", "d")
    report = ud.build_report([], [evil], [], [], CUTOFF)
    assert "\x1b" not in report
    assert "evil\\x1b[2J\\x0aname added 1.0.0" in report


def test_report_is_printed_before_the_gate_runs(tmp_path) -> None:
    # Covers: [TOOLS-017.01]
    root = make_repo(tmp_path / "repo")
    out = io.StringIO()
    seen = {}

    def gate(argv, cwd):
        seen["printed"] = out.getvalue()
        return ok()

    code, _, _ = run(root, happy_runner(root, mise_run=gate), tmp_path, out=out)
    assert code == 0
    assert "pydantic 2.13.4 -> 2.14.2" in seen["printed"]


def test_gate_failure_keeps_the_update_and_returns_the_gate_status(tmp_path) -> None:
    # Covers: [TOOLS-017.02]
    root = make_repo(tmp_path / "repo")
    runner = happy_runner(root, mise_run=lambda argv, cwd: ud.Result(3))
    code, out, err = run(root, runner, tmp_path)
    assert code == 3
    assert "Gate failed" in err and "git restore uv.lock cdk/package-lock.json" in err
    assert "pydantic 2.13.4 -> 2.14.2" in out
    assert (root / "uv.lock").read_text() == NEW_UV
    assert (root / "cdk" / "package-lock.json").read_text() == NEW_NPM


def test_negative_gate_status_still_fails(tmp_path) -> None:
    root = make_repo(tmp_path / "repo")
    code, _, _ = run(root, happy_runner(root, mise_run=lambda argv, cwd: ud.Result(-9)), tmp_path)
    assert code == 1


def test_gate_runner_error_is_reported_and_fails(tmp_path) -> None:
    root = make_repo(tmp_path / "repo")

    def missing(argv, cwd):
        raise ud.UpdateError("command not found: mise")

    code, _, err = run(root, happy_runner(root, mise_run=missing), tmp_path)
    assert code == 1 and "command not found: mise" in err


class ClosedPipe(io.StringIO):
    def write(self, text: str) -> int:
        raise BrokenPipeError

    def flush(self) -> None:
        raise BrokenPipeError


def test_closed_output_pipe_does_not_hide_the_gate_status(tmp_path) -> None:
    # Covers: [TOOLS-017.05]
    root = make_repo(tmp_path / "repo")
    runner = happy_runner(root, mise_run=lambda argv, cwd: ud.Result(4))
    code, _, _ = run(root, runner, tmp_path, out=ClosedPipe(), err=ClosedPipe())
    assert code == 4
    assert "mise run" in runner.commands()


def test_report_file_is_written_on_success(tmp_path) -> None:
    # Covers: [TOOLS-017.04]
    root = make_repo(tmp_path / "repo")
    before = snapshot_bytes(root)
    code, _, _ = run(root, happy_runner(root), tmp_path)
    text = (root / ud.REPORT_FILE).read_text()
    assert code == 0
    assert "pydantic 2.13.4 -> 2.14.2" in text and "Lock audit: passed" in text
    assert "Restore: not needed" in text
    for rel, data in before.items():
        assert f"sha256 {rel}: before {ud.hashlib.sha256(data).hexdigest()}" in text


def test_report_file_is_written_on_failure_with_the_restore_result(tmp_path) -> None:
    # Covers: [TOOLS-017.04]
    root = make_repo(tmp_path / "repo")
    failing = ud.Result(1, "", "npm error boom for `somepkg`")
    code, _, _ = run(root, happy_runner(root, npm_update=lambda argv, cwd: failing), tmp_path)
    text = (root / ud.REPORT_FILE).read_text()
    assert code == 1
    assert "Error: npm update failed for somepkg" in text
    assert "Restore: both lockfiles restored" in text


def test_report_file_is_written_when_the_run_refuses_to_start(tmp_path) -> None:
    # Covers: [TOOLS-017.04]
    root = make_repo(tmp_path / "repo")
    run(root, FakeRunner(root, git_status=lambda argv, cwd: ok(" M uv.lock\n")), tmp_path)
    assert "uncommitted changes" in (root / ud.REPORT_FILE).read_text()


def test_main_audit_flag_runs_the_offline_audit(capsys) -> None:
    assert ud.main(["--audit-locks"]) == 0
    assert "lock-audit: OK" in capsys.readouterr().out


def test_main_reports_update_errors(monkeypatch, capsys) -> None:
    def boom(root, runner, **kwargs):
        raise ud.UpdateError("something clear")

    monkeypatch.setattr(ud, "run_update", boom)
    assert ud.main([]) == 1
    assert "update-deps: something clear" in capsys.readouterr().err


# --- review regressions --------------------------------------------------------------------


@pytest.mark.parametrize("signal_number", [signal.SIGINT, signal.SIGTERM])
def test_second_signal_during_restore_cannot_stop_the_restore(
    tmp_path, monkeypatch, signal_number
) -> None:
    # Covers: [TOOLS-016.04]
    root = make_repo(tmp_path / "repo")
    before = snapshot_bytes(root)
    real = ud.atomic_write
    seen = {"calls": 0}

    def write_then_signal(path: Path, data: bytes) -> None:
        seen["calls"] += 1
        if seen["calls"] == 1:
            signal.raise_signal(signal_number)  # a second signal arrives mid-restore
        real(path, data)

    def first_signal(argv, cwd):
        (root / "uv.lock").write_text(NEW_UV)
        signal.raise_signal(signal_number)
        return ok()

    monkeypatch.setattr(ud, "atomic_write", write_then_signal)
    runner = FakeRunner(root, uv_lock=first_signal)
    code, _, err = run(root, runner, tmp_path)
    assert code == 130 and "interrupted" in err
    assert seen["calls"] == 2
    assert snapshot_bytes(root) == before


def test_signal_handlers_are_put_back_after_the_restore(tmp_path) -> None:
    before = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    with ud.signals_ignored():
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN
    assert {n: signal.getsignal(n) for n in before} == before


def test_sigterm_during_the_gate_stops_the_gate_and_keeps_the_update(tmp_path) -> None:
    # Covers: [TOOLS-017.02]
    root = make_repo(tmp_path / "repo")

    def terminated(argv, cwd):
        signal.raise_signal(signal.SIGTERM)
        return ok()

    code, _, err = run(root, happy_runner(root, mise_run=terminated), tmp_path)
    assert code == 130 and "interrupted during the gate" in err
    assert (root / "uv.lock").read_text() == NEW_UV


def test_zero_day_cooldown_is_rejected(tmp_path) -> None:
    # Covers: [TOOLS-019.04]
    root = make_repo(tmp_path)
    (root / "pyproject.toml").write_text(ROOT_PYPROJECT.replace("7 days", "0 days"))
    with pytest.raises(ud.UpdateError, match="at least '1 day'"):
        ud.preflight(root)


def test_nested_npm_copies_are_not_direct_dependencies(tmp_path) -> None:
    # Covers: [TOOLS-015.01]
    old = npm_lock(
        {
            "node_modules/typescript": npm_entry("5.9.3"),
            "node_modules/constructs": npm_entry("10.6.0"),
            "node_modules/tool/node_modules/typescript": npm_entry("4.9.5"),
        }
    )
    new = npm_lock(
        {
            "node_modules/typescript": npm_entry("5.9.3"),
            "node_modules/constructs": npm_entry("10.8.1"),
            "node_modules/tool/node_modules/typescript": npm_entry("6.0.0"),
        }
    )
    root = make_repo(tmp_path / "repo", npm=old)
    code, out, _ = run(
        root, happy_runner(root, npm_update=writes("cdk/package-lock.json", new)), tmp_path
    )
    assert code == 0
    assert "typescript 4.9.5 -> 6.0.0" in out  # still reported, not blocked


def test_top_level_npm_major_change_still_fails_with_nested_copies_present(tmp_path) -> None:
    # Covers: [TOOLS-015.01]
    old = npm_lock(
        {
            "node_modules/typescript": npm_entry("5.9.3"),
            "node_modules/constructs": npm_entry("10.6.0"),
            "node_modules/tool/node_modules/typescript": npm_entry("5.9.3"),
        }
    )
    new = npm_lock(
        {
            "node_modules/typescript": npm_entry("6.0.0"),
            "node_modules/constructs": npm_entry("10.6.0"),
            "node_modules/tool/node_modules/typescript": npm_entry("5.9.3"),
        }
    )
    root = make_repo(tmp_path / "repo", npm=old)
    before = snapshot_bytes(root)
    runner = happy_runner(root, npm_update=writes("cdk/package-lock.json", new))
    code, _, err = run(root, runner, tmp_path)
    assert code == 1 and "typescript" in err
    assert snapshot_bytes(root) == before


def test_error_messages_keep_line_breaks_but_escape_control_characters() -> None:
    assert ud.esc_text("first\n  - second\tcol") == "first\n  - second\tcol"
    assert ud.esc_text("bad\x1b[2Jname\x00") == "bad\\x1b[2Jname\\x00"
    assert ud.esc("a\nb") == "a\\x0ab"


def test_multi_line_failure_is_printed_on_separate_lines(tmp_path) -> None:
    root = make_repo(tmp_path / "repo")
    (root / "pyproject.toml").write_text(ROOT_PYPROJECT.replace("<10", "").replace("<3", ""))
    member = root / "libs" / "oci" / "pyproject.toml"
    member.write_text(member.read_text().replace(",<3", ""))
    _, _, err = run(root, happy_runner(root), tmp_path)
    assert "\n  - " in err and "\\x0a" not in err


def test_report_file_escapes_control_characters_from_package_data(tmp_path) -> None:
    # Covers: [TOOLS-017.04]
    root = make_repo(tmp_path / "repo")
    failing = ud.Result(1, "", "error: Failed to build `evil\x1b[2Jpkg==1.0` for the target")
    run(root, happy_runner(root, uv_lock=lambda argv, cwd: failing), tmp_path)
    text = (root / ud.REPORT_FILE).read_text()
    assert "\x1b" not in text and "evil" in text


def test_range_check_reads_optional_dependency_tables(tmp_path) -> None:
    # Covers: [TOOLS-015.05]
    root = make_repo(tmp_path)
    member = root / "libs" / "oci" / "pyproject.toml"
    member.write_text(
        member.read_text().replace(
            "[build-system]",
            '[project.optional-dependencies]\nextra = ["pytest>=9"]\n\n[build-system]',
        )
    )
    with pytest.raises(ud.UpdateError, match=r"project\.optional-dependencies\.extra: pytest"):
        ud.preflight(root)


def test_audit_reports_an_unhashed_package_once_per_problem(tmp_path) -> None:
    # Covers: [TOOLS-021.02]
    uv = uv_lock(uv_pkg("nohash", "1.0.0", hash="md5:abc", url="https://evil.example/x"))
    problems = audit(uv, BASE_NPM, tmp_path)
    assert sum("nohash" in p for p in problems) == 4  # sdist and wheel, each for hash and URL


def test_default_lock_path_falls_back_to_tmp() -> None:
    uid = os.getuid()
    assert ud.default_lock_path({}).parts[-2] == f"cairn-update-deps-{uid}"


def test_package_exemption_in_the_cooldown_table_passes_the_checks(tmp_path) -> None:
    # Covers: [TOOLS-019.03]
    root = make_repo(tmp_path / "repo")
    (root / "pyproject.toml").write_text(
        ROOT_PYPROJECT.replace(
            'exclude-newer = "7 days"',
            'exclude-newer = "7 days"\n'
            'exclude-newer-package = { pydantic = "2026-12-31T00:00:00Z" }',
        )
    )
    assert ud.preflight(root).cooldown_days == 7
    code, out, _ = run(root, happy_runner(root), tmp_path)
    assert code == 0 and "pydantic 2.13.4 -> 2.14.2" in out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "fetch https://user:s3cret@registry.example/x failed",
            "fetch https://registry.example/x failed",
        ),
        ("git+https://ghp_token123@github.com/o/r.git", "git+https://github.com/o/r.git"),
        ("see HTTPS://admin:pw@host/path and http://a@b/c", "see HTTPS://host/path and http://b/c"),
        (
            "no credentials at https://pypi.org/simple/pkg",
            "no credentials at https://pypi.org/simple/pkg",
        ),
        ("mail me@example.com about it", "mail me@example.com about it"),
    ],
)
def test_scrub_urls_removes_user_info_only(text: str, expected: str) -> None:
    assert ud.scrub_urls(text) == expected


def test_failure_text_never_prints_url_credentials(tmp_path) -> None:
    # Covers: [TOOLS-020.02]
    root = make_repo(tmp_path / "repo")
    leaked = "error: failed to fetch https://__token__:pypi-AgEIsecret@internal.example/simple/x"
    failing = ud.Result(1, "", leaked)
    code, out, err = run(root, FakeRunner(root, uv_lock=lambda argv, cwd: failing), tmp_path)
    report = (root / ud.REPORT_FILE).read_text()
    assert code == 1
    for text in (out, err, report):
        assert "pypi-AgEIsecret" not in text and "__token__" not in text
    assert "internal.example/simple/x" in err


def test_resolution_commands_name_the_default_registries(tmp_path) -> None:
    # Covers: [TOOLS-020.03]
    root = make_repo(tmp_path / "repo")
    runner = happy_runner(root)
    run(root, runner, tmp_path)
    resolve_calls = [argv for argv, _, _ in runner.calls if argv[0] in ("uv", "npm")]
    assert len(resolve_calls) == 4
    for argv in resolve_calls:
        assert "--default-index" in argv or "--registry" in argv


def test_signatures_are_checked_after_the_refreshed_lock_is_installed(tmp_path) -> None:
    # Covers: [TOOLS-021.03]
    root = make_repo(tmp_path / "repo")
    runner = happy_runner(root)
    run(root, runner, tmp_path)
    names = runner.commands()
    assert names.index("npm update") < names.index("npm ci") < names.index("npm audit")
    install = next(argv for argv, _, _ in runner.calls if argv[:2] == ["npm", "ci"])
    assert "--ignore-scripts" in install
    assert install[install.index("--registry") + 1] == "https://registry.npmjs.org/"


def test_failed_install_of_the_refreshed_lock_restores_the_lockfiles(tmp_path) -> None:
    # Covers: [TOOLS-016.01]
    root = make_repo(tmp_path / "repo")
    before = snapshot_bytes(root)
    failing = ud.Result(1, "", "npm error code EINTEGRITY for `tampered@1.0.0`")
    runner = happy_runner(root)
    runner.handlers["npm ci"] = lambda argv, cwd: failing
    code, _, err = run(root, runner, tmp_path)
    assert code == 1 and "npm ci failed for tampered" in err
    assert snapshot_bytes(root) == before
    assert "npm audit" not in runner.commands()

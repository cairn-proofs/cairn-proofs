"""Tests for the locked, hashed, isolated Lambda bundle build and the CI checks.

They run the real `cdk/scripts/build-lambdas.sh` and `cdk/scripts/deploy.sh` inside a
fixture repository under `tmp_path`. A stub `uv` sits first on PATH. It records every
call's arguments and environment and can be told to fail a named step through a
`control` file. The stub reads the control file, not the environment, because the
script starts uv with `env -i`.

Every test uses its own TMPDIR so the per-Lambda `flock` files never collide when the
suite runs under `-n auto`. Nothing here contacts a registry.

The tests prove the script's commands, options, environment, failure handling and
cleanup. They do not prove that real wheels match real hashes; the one-time real
build in the change's verification task covers that.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
LAMBDAS = {
    "validate_bundle": "cairn-lambda-validate-bundle",
    "sign_bundle": "cairn-lambda-sign-bundle",
    "archive_to_s3": "cairn-lambda-archive-to-s3",
    "update_metadata": "cairn-lambda-update-metadata",
    "mint_tag": "cairn-lambda-mint-tag",
}

STUB_UV = r"""#!/usr/bin/env bash
here="$(cd "$(dirname "$0")" && pwd)"
{
  echo "ARGS: $*"
  env | sort | sed 's/^/ENV: /'
  echo "---"
} >> "$here/calls.log"
has() { grep -qx "$1" "$here/control" 2>/dev/null; }
cmd="$1"; shift
case "$cmd" in
  export)
    out=""; prev=""
    for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
    if has fail-export; then echo "stub uv: lock out of date" >&2; exit 2; fi
    case " $* " in *" --only-emit-workspace "*) has fail-members-export && exit 2;; esac
    case " $* " in *" --only-group "*) has fail-build-export && exit 2;; esac
    printf 'pkg==1.0.0\n' > "$out"
    ;;
  pip)
    shift
    target=""; hashes=0; prev=""
    for a in "$@"; do
      [ "$prev" = "--target" ] && target="$a"
      [ "$a" = "--require-hashes" ] && hashes=1
      prev="$a"
    done
    if [ "$hashes" = 1 ]; then
      if has fail-third-party; then echo "stub uv: hash mismatch" >&2; exit 1; fi
      mkdir -p "$target/boto3" "$target/jmespath" "$target/six-1.0.dist-info" \
        "$target/bin" "$target/pkg/__pycache__"
      touch "$target/boto3/__init__.py" "$target/jmespath/__init__.py" "$target/six.py" \
        "$target/bin/tool" "$target/pkg/__pycache__/x.pyc" "$target/pkg/mod.py"
    else
      if has fail-members; then echo "stub uv: backend build failed" >&2; exit 1; fi
      mkdir -p "$target/cairn_member"
      touch "$target/cairn_member/__init__.py"
    fi
    ;;
esac
exit 0
"""

STUB_AWS = "#!/usr/bin/env bash\nexit 1\n"


@dataclass
class Fixture:
    root: Path
    stub_dir: Path
    tmp: Path

    def control(self, *lines: str) -> None:
        (self.stub_dir / "control").write_text("\n".join(lines) + "\n")

    def calls(self) -> list[dict[str, object]]:
        """Return one record per uv call: its argv text, argv list and environment."""
        log = self.stub_dir / "calls.log"
        if not log.exists():
            return []
        records = []
        for block in log.read_text().split("---\n"):
            lines = [line for line in block.splitlines() if line]
            if not lines:
                continue
            args = lines[0].removeprefix("ARGS: ")
            env = dict(
                line.removeprefix("ENV: ").split("=", 1) for line in lines[1:] if "=" in line
            )
            records.append({"args": args.split(), "text": args, "env": env})
        return records

    def run(
        self, script: str = "build-lambdas.sh", **extra_env: str
    ) -> subprocess.CompletedProcess[str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("CI", "XDG_RUNTIME_DIR") and not key.startswith(("UV_", "AWS_"))
        }
        env["PATH"] = f"{self.stub_dir}:{env['PATH']}"
        env["TMPDIR"] = str(self.tmp)
        env.update(extra_env)
        return subprocess.run(
            [str(self.root / "cdk" / "scripts" / script)],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )


def git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    root = tmp_path / "repo"
    (root / "cdk" / "scripts").mkdir(parents=True)
    for name in ("build-lambdas.sh", "deploy.sh"):
        shutil.copy2(_REPO_ROOT / "cdk" / "scripts" / name, root / "cdk" / "scripts" / name)
    (root / "cdk" / "collector.yaml").write_text("receivers: {}\n")
    for name in LAMBDAS:
        (root / "lambdas" / name).mkdir(parents=True)
        (root / "lambdas" / name / "handler.py").write_text("x = 1\n")
    (root / "libs" / "oci").mkdir(parents=True)
    (root / "libs" / "oci" / "mod.py").write_text("x = 1\n")
    (root / "cli").mkdir()
    (root / "cli" / "main.py").write_text("x = 1\n")
    (root / "uv.lock").write_text("version = 1\n")
    (root / "pyproject.toml").write_text('[project]\nname = "x"\n')
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    for name, body in (("uv", STUB_UV), ("aws", STUB_AWS)):
        (stub_dir / name).write_text(body)
        (stub_dir / name).chmod(0o755)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    return Fixture(root, stub_dir, tmp)


def commit_all(fx: Fixture) -> None:
    git(fx.root, "init", "-q")
    git(fx.root, "add", "-A")
    git(fx.root, "commit", "-q", "-m", "fixture")


def bundle(fx: Fixture, name: str) -> Path:
    return fx.root / "lambda-bundles" / name


# --- 5.1 locked, hashed, isolated build -------------------------------------------------


def test_build_exports_the_lock_and_installs_with_the_expected_options(fx: Fixture) -> None:
    # Covers: [TOOLS-018.01] [TOOLS-022.02]
    result = fx.run()
    assert result.returncode == 0, result.stderr
    calls = fx.calls()
    first = calls[0]["args"]
    assert first[:1] == ["export"] and "--only-group" in first and "build" in first
    assert {"--no-emit-workspace", "--no-emit-project", "--locked"} <= set(first)
    for name, package in LAMBDAS.items():
        mine = [
            c["args"]
            for c in calls
            if package in c["args"] or f"{name}.third-party.txt" in c["text"]
        ]
        exports = [a for a in mine if a[0] == "export"]
        assert len(exports) == 2, name
        third, members = exports
        assert third[third.index("--package") + 1] == package
        assert {"--locked", "--no-dev", "--no-editable", "--no-emit-workspace"} <= set(third)
        assert {
            "--locked",
            "--no-dev",
            "--no-editable",
            "--only-emit-workspace",
            "--no-hashes",
        } <= set(members)
        assert bundle(fx, name).is_dir()
    installs = [c["args"] for c in calls if c["args"][:2] == ["pip", "install"]]
    assert len(installs) == 2 * len(LAMBDAS)
    third_party = [a for a in installs if "--require-hashes" in a]
    members = [a for a in installs if "--build-constraints" in a]
    assert len(third_party) == len(members) == len(LAMBDAS)
    for argv in installs:
        assert {"--no-deps", "--no-config", "--python-platform", "--python-version"} <= set(argv)
        assert argv[argv.index("--default-index") + 1] == "https://pypi.org/simple"
        assert argv[argv.index("--python-platform") + 1] == "aarch64-manylinux_2_17"
    for argv in third_party:
        assert "--only-binary" in argv and argv[argv.index("--only-binary") + 1] == ":all:"
        assert "--build-constraints" not in argv
    for argv in members:
        assert "--require-hashes" not in argv
        assert argv[argv.index("--build-constraints") + 1].endswith("build-constraints.txt")


def test_workspace_members_build_from_current_source_not_the_uv_cache(fx: Fixture) -> None:
    # uv reuses a cached wheel of a local package while its pyproject.toml is
    # unchanged, so a source-only edit would ship the previous build. The member
    # install must bypass the cache; the hashed third-party install keeps it.
    result = fx.run()
    assert result.returncode == 0, result.stderr
    installs = [c["args"] for c in fx.calls() if c["args"][:2] == ["pip", "install"]]
    members = [a for a in installs if "--build-constraints" in a]
    third_party = [a for a in installs if "--require-hashes" in a]
    assert members and all("--no-cache" in argv for argv in members)
    assert third_party and all("--no-cache" not in argv for argv in third_party)


def test_uv_export_never_receives_no_config(fx: Fixture) -> None:
    # Covers: [TOOLS-022.01]
    assert fx.run().returncode == 0
    exports = [c["args"] for c in fx.calls() if c["args"][0] == "export"]
    assert exports and all("--no-config" not in argv for argv in exports)


def test_ambient_uv_and_cloud_variables_do_not_reach_uv(fx: Fixture) -> None:
    # Covers: [TOOLS-022.01]
    result = fx.run(
        UV_INDEX_URL="https://evil.example/simple",
        UV_EXTRA_INDEX_URL="https://evil.example/extra",
        UV_NO_VERIFY_HASHES="1",
        AWS_ACCESS_KEY_ID="AKIAEXAMPLE",
        AWS_SECRET_ACCESS_KEY="secret",
        NPM_TOKEN="token",
    )
    assert result.returncode == 0, result.stderr
    calls = fx.calls()
    assert calls
    for call in calls:
        names = set(call["env"])
        assert not [n for n in names if n.startswith(("UV_", "AWS_", "NPM_"))], names
        assert {"PATH", "HOME", "TMPDIR", "LANG"} <= names
    assert "evil.example" not in " ".join(str(c["text"]) for c in calls)


def test_each_bundle_records_only_the_lock_digest(fx: Fixture) -> None:
    # Covers: [TOOLS-022.03]
    commit_all(fx)
    assert fx.run().returncode == 0
    digest = subprocess.run(
        ["shasum", "-a", "256", str(fx.root / "uv.lock")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    for name in LAMBDAS:
        info = json.loads((bundle(fx, name) / "BUILD-INFO.json").read_text())
        assert info == {"uv_lock_sha256": digest}
        assert (bundle(fx, name) / "collector.yaml").is_file()
        assert str(fx.root) not in (bundle(fx, name) / "BUILD-INFO.json").read_text()


def test_bundle_strips_runtime_packages_scripts_and_bytecode(fx: Fixture) -> None:
    assert fx.run().returncode == 0
    target = bundle(fx, "validate_bundle")
    assert (target / "boto3").is_dir() and (target / "cairn_member").is_dir()
    for stripped in ("jmespath", "six.py", "six-1.0.dist-info", "bin"):
        assert not (target / stripped).exists(), stripped
    assert not list(target.rglob("__pycache__")) and not list(target.rglob("*.pyc"))


def test_stale_lockfile_stops_the_build_and_names_the_lock(fx: Fixture) -> None:
    # Covers: [TOOLS-018.02]
    fx.control("fail-export")
    result = fx.run()
    assert result.returncode != 0
    assert "uv.lock may be out of date" in result.stderr
    assert not (fx.root / "lambda-bundles").exists() or not any(
        (fx.root / "lambda-bundles").iterdir()
    )


def test_stale_lock_detected_on_a_lambda_export_leaves_no_bundle(fx: Fixture) -> None:
    # Covers: [TOOLS-018.02]
    fx.control("fail-members-export")
    result = fx.run()
    assert result.returncode != 0 and "out of date" in result.stderr
    assert not bundle(fx, "validate_bundle").exists()


def test_hash_mismatch_stops_the_build_and_leaves_no_bundle(fx: Fixture) -> None:
    # Covers: [TOOLS-018.03]
    fx.control("fail-third-party")
    result = fx.run()
    assert result.returncode != 0
    assert "hash mismatch or missing wheel" in result.stderr
    for name in LAMBDAS:
        assert not bundle(fx, name).exists(), name


def test_build_backend_failure_stops_the_build_and_leaves_no_bundle(fx: Fixture) -> None:
    # Covers: [TOOLS-022.02]
    fx.control("fail-members")
    result = fx.run()
    assert result.returncode != 0 and "workspace members" in result.stderr
    assert not bundle(fx, "validate_bundle").exists()


def test_missing_build_constraints_stop_the_build_before_any_bundle(fx: Fixture) -> None:
    # Covers: [TOOLS-022.02]
    fx.control("fail-build-export")
    result = fx.run()
    assert result.returncode != 0 and "build dependency group" in result.stderr
    assert not (fx.root / "lambda-bundles").exists()
    assert all(c["args"][:2] != ["pip", "install"] for c in fx.calls())


def test_failed_rebuild_removes_the_previous_bundle(fx: Fixture) -> None:
    # Covers: [TOOLS-018.03]
    assert fx.run().returncode == 0 and bundle(fx, "validate_bundle").is_dir()
    fx.control("fail-third-party")
    assert fx.run().returncode != 0
    assert not bundle(fx, "validate_bundle").exists()


def test_script_removes_its_temporary_work_directory(fx: Fixture) -> None:
    assert fx.run().returncode == 0
    assert not list(fx.tmp.glob("cairn-build-lambdas.*"))
    fx.control("fail-export")
    assert fx.run().returncode != 0
    assert not list(fx.tmp.glob("cairn-build-lambdas.*"))


# --- 5.2 CI checks --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "listed"),
    [
        (lambda r: (r / "lambdas" / "mint_tag" / "handler.py").write_text("x = 2\n"), "handler.py"),
        (lambda r: (r / "libs" / "oci" / "new_module.py").write_text("x = 1\n"), "new_module.py"),
        (lambda r: (r / "uv.lock").write_text("version = 2\n"), "uv.lock"),
        (lambda r: (r / "cli" / "main.py").write_text("x = 2\n"), "main.py"),
        (lambda r: (r / "pyproject.toml").write_text("changed = 1\n"), "pyproject.toml"),
    ],
)
def test_ci_build_fails_on_uncommitted_source_or_lockfiles(fx: Fixture, change, listed) -> None:
    # Covers: [TOOLS-023.01]
    commit_all(fx)
    change(fx.root)
    result = fx.run(CI="true")
    assert result.returncode != 0
    assert "uncommitted changes" in result.stderr and listed in result.stderr
    assert fx.calls() == []
    assert not (fx.root / "lambda-bundles").exists()


def test_ci_build_passes_on_a_clean_checkout(fx: Fixture) -> None:
    # Covers: [TOOLS-023.01]
    commit_all(fx)
    result = fx.run(CI="1")
    assert result.returncode == 0, result.stderr
    assert bundle(fx, "mint_tag").is_dir()


def test_ci_build_ignores_changes_outside_the_checked_paths(fx: Fixture) -> None:
    # Covers: [TOOLS-023.01]
    commit_all(fx)
    (fx.root / "notes.txt").write_text("scratch\n")
    assert fx.run(CI="1").returncode == 0


def test_developer_build_with_uncommitted_changes_runs_without_the_check(fx: Fixture) -> None:
    # Covers: [TOOLS-023.03]
    commit_all(fx)
    (fx.root / "lambdas" / "mint_tag" / "handler.py").write_text("x = 2\n")
    (fx.root / "libs" / "oci" / "new_module.py").write_text("x = 1\n")
    result = fx.run()
    assert result.returncode == 0, result.stderr
    assert "uncommitted" not in result.stderr


def test_empty_ci_variable_counts_as_unset(fx: Fixture) -> None:
    # Covers: [TOOLS-023.03]
    commit_all(fx)
    (fx.root / "uv.lock").write_text("version = 2\n")
    assert fx.run(CI="").returncode == 0


@pytest.mark.parametrize(
    "name", ["uv.lock", "cdk/package-lock.json", "cdk/package.json", "pyproject.toml"]
)
def test_ci_deploy_fails_when_a_lockfile_or_manifest_differs_from_head(
    fx: Fixture, name: str
) -> None:
    # Covers: [TOOLS-023.02]
    (fx.root / "cdk" / "package-lock.json").write_text("{}\n")
    (fx.root / "cdk" / "package.json").write_text("{}\n")
    commit_all(fx)
    (fx.root / name).write_text((fx.root / name).read_text() + "\n# changed\n")
    result = fx.run("deploy.sh", CI="true")
    assert result.returncode == 1
    assert "differ from HEAD" in result.stderr and name in result.stderr
    assert "CloudTrail" not in result.stdout + result.stderr
    assert fx.calls() == []


def test_ci_deploy_proceeds_past_the_check_when_files_match_head(fx: Fixture) -> None:
    # Covers: [TOOLS-023.02]
    (fx.root / "cdk" / "package-lock.json").write_text("{}\n")
    (fx.root / "cdk" / "package.json").write_text("{}\n")
    commit_all(fx)
    result = fx.run("deploy.sh", CI="true")
    assert "differ from HEAD" not in result.stderr
    assert "CloudTrail preflight" in result.stdout  # reached the step after the CI check


def test_developer_deploy_skips_the_check(fx: Fixture) -> None:
    # Covers: [TOOLS-023.03]
    (fx.root / "cdk" / "package-lock.json").write_text("{}\n")
    (fx.root / "cdk" / "package.json").write_text("{}\n")
    commit_all(fx)
    (fx.root / "uv.lock").write_text("version = 2\n")
    result = fx.run("deploy.sh")
    assert "differ from HEAD" not in result.stderr
    assert "CloudTrail preflight" in result.stdout  # reached the step after the CI check


def test_scripts_avoid_bash_four_features() -> None:
    """The scripts must run on the stock macOS bash 3.2."""
    for name in ("build-lambdas.sh", "deploy.sh"):
        text = (_REPO_ROOT / "cdk" / "scripts" / name).read_text()
        for banned in ("mapfile", "readarray", "declare -A", "${!", ",,}", "^^}"):
            assert banned not in text, f"{name} uses {banned}"


def test_ci_build_fails_with_a_message_when_git_cannot_run(fx: Fixture) -> None:
    # Covers: [TOOLS-023.01]
    result = fx.run(CI="true")  # the fixture is not a git repository
    assert result.returncode != 0
    assert "could not run 'git status'" in result.stderr
    assert fx.calls() == []


def test_ci_deploy_fails_with_a_message_when_git_cannot_run(fx: Fixture) -> None:
    # Covers: [TOOLS-023.02]
    result = fx.run("deploy.sh", CI="true")  # the fixture is not a git repository
    assert result.returncode != 0
    assert "could not run 'git diff'" in result.stderr
    assert fx.calls() == []


def test_cdk_dependencies_install_without_lifecycle_scripts() -> None:
    # Covers: [TOOLS-024.01]
    deploy = (_REPO_ROOT / "cdk" / "scripts" / "deploy.sh").read_text()
    install_lines = [line for line in deploy.splitlines() if line.strip().startswith("npm ci")]
    assert install_lines and all("--ignore-scripts" in line for line in install_lines)
    mise = (_REPO_ROOT / "mise.toml").read_text()
    task = mise.split("[tasks.cdk-install]", 1)[1].split("\n[", 1)[0]
    run_lines = [line for line in task.splitlines() if line.startswith("run =")]
    assert run_lines == ['run = "npm ci --ignore-scripts"']

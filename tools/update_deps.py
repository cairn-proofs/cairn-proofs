#!/usr/bin/env python3
"""Refresh the dependency lockfiles of the Cairn workspace in one command.

`mise run update-deps` re-resolves `uv.lock` and `cdk/package-lock.json` to the newest
versions that the declared ranges and the release cooldown allow, prints what changed,
and then runs the pre-merge gate. `--audit-locks` runs only the offline lockfile audit.

The tool is dependency-free (stdlib only). It talks to `uv`, `npm` and `mise` through one
runner function, so unit tests replace the tools with fakes. Behavior is specified by
the `toolchain` capability (TOOLS-014 to TOOLS-021).

Rules in short:

1. Checks that need no network run first and change nothing: every direct dependency
   range excludes its next major version, no manifest uses a form the check cannot
   read, and the cooldown is set in `[tool.uv] exclude-newer`.
2. A run starts only when both lockfiles match `HEAD`, and only one run exists at a time.
3. Resolution runs no package code (`uv --no-build`, `npm --ignore-scripts`) and every
   child process gets a minimal environment.
4. Any failure or termination signal before the gate restores both lockfiles.
5. The change report is printed before the gate runs. A gate failure keeps the update.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Protocol

REPO_ROOT = Path(__file__).resolve().parent.parent
UV_LOCK = "uv.lock"
NPM_LOCK = "cdk/package-lock.json"
NPM_MANIFEST = "cdk/package.json"
REPORT_FILE = ".update-deps-report.txt"
REVIEW_LINE = "Review git diff uv.lock cdk/package-lock.json before you commit."
RESTORE_COMMAND = "git restore uv.lock cdk/package-lock.json"

CHILD_TIMEOUT_SECONDS = 900
CHILD_ENV_NAMES = frozenset({"PATH", "HOME", "TMPDIR", "LANG", "TERM"})
CHILD_ENV_PREFIXES = ("XDG_", "MISE_")

PYPI_REGISTRY = "https://pypi.org/simple"
NPM_REGISTRY = "https://registry.npmjs.org/"
PYPI_FILES_PREFIX = "https://files.pythonhosted.org/"
NPM_REGISTRY_PREFIX = "https://registry.npmjs.org/"

UV_KEYS_READ = frozenset(
    {
        "package",
        "workspace",
        "exclude-newer",
        "exclude-newer-package",
        "sources",
        "override-dependencies",
        "constraint-dependencies",
        "build-constraint-dependencies",
    }
)
UV_INDEX_KEYS = frozenset(
    {"index", "index-url", "extra-index-url", "find-links", "default-index", "pip"}
)
NPM_DEPENDENCY_SECTIONS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)
NPM_UNREAD_SECTIONS = ("overrides", "resolutions")


class UpdateError(Exception):
    """A step failed in a way that the user must see; the message names the cause."""


# --- child processes ---------------------------------------------------------


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    def __call__(self, argv: Sequence[str], *, cwd: Path, capture: bool = True) -> Result: ...


def child_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Return only the allowlisted variables; everything else stays out of children."""
    return {
        name: value
        for name, value in environ.items()
        if name in CHILD_ENV_NAMES or name.startswith(CHILD_ENV_PREFIXES)
    }


def make_runner(
    environ: Mapping[str, str] | None = None, timeout: int = CHILD_TIMEOUT_SECONDS
) -> Runner:
    """Build the runner: argv list, no shell, minimal environment, bounded run time."""
    source = os.environ if environ is None else environ

    def run(argv: Sequence[str], *, cwd: Path, capture: bool = True) -> Result:
        label = " ".join(argv[:2])
        try:
            # argv is a list built from constants, so no shell and no untrusted text.
            proc = subprocess.run(  # noqa: S603
                list(argv),
                cwd=cwd,
                env=child_env(source),
                shell=False,
                check=False,
                capture_output=capture,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise UpdateError(f"{label} timed out after {timeout} seconds") from exc
        except FileNotFoundError as exc:
            raise UpdateError(f"command not found: {argv[0]}") from exc
        return Result(proc.returncode, proc.stdout or "", proc.stderr or "")

    return run


@contextlib.contextmanager
def signals_ignored() -> Iterator[None]:
    """Ignore SIGINT, SIGTERM and SIGHUP, so that a second signal cannot stop a restore."""
    previous: dict[int, Any] = {}
    try:
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            number = getattr(signal, name, None)
            if number is not None:
                previous[number] = signal.signal(number, signal.SIG_IGN)
    except ValueError:  # not the main thread: nothing to ignore
        previous.clear()
    try:
        yield
    finally:
        for number, old in previous.items():
            signal.signal(number, old)


@contextlib.contextmanager
def interrupts_raise_keyboard_interrupt() -> Iterator[None]:
    """Treat SIGTERM and SIGHUP like Ctrl-C, so the restore code runs."""

    def handler(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    previous: dict[int, Any] = {}
    try:
        for name in ("SIGTERM", "SIGHUP"):
            number = getattr(signal, name, None)
            if number is not None:
                previous[number] = signal.signal(number, handler)
    except ValueError:  # not the main thread: nothing to install
        previous.clear()
    try:
        yield
    finally:
        for number, old in previous.items():
            signal.signal(number, old)


# --- output ------------------------------------------------------------------


def esc(text: str) -> str:
    """Escape every control character in one untrusted value, such as a package name."""
    return re.sub(r"[\x00-\x1f\x7f]", lambda m: f"\\x{ord(m.group()):02x}", text)


_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")


def scrub_urls(text: str) -> str:
    """Remove user names and passwords from URLs, so that a token cannot reach a log."""
    return _URL_USERINFO.sub(r"\1", text)


def esc_text(text: str) -> str:
    """Escape control characters in an assembled message, but keep its tabs and line breaks."""
    text = scrub_urls(text)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", lambda m: f"\\x{ord(m.group()):02x}", text)


class Writer:
    """Write lines, and survive a closed pipe so that the gate still runs."""

    def __init__(self, stream: IO[str]) -> None:
        self.stream = stream
        self.broken = False

    def __call__(self, text: str = "") -> None:
        if self.broken:
            return
        try:
            self.stream.write(text + "\n")
            self.stream.flush()
        except (BrokenPipeError, OSError, ValueError):
            self.broken = True
            if self.stream in (sys.stdout, sys.stderr):
                with contextlib.suppress(OSError, ValueError):
                    os.dup2(os.open(os.devnull, os.O_WRONLY), self.stream.fileno())


# --- versions and requirements -------------------------------------------------


def release_tuple(version: str) -> tuple[int, ...]:
    match = re.match(r"v?(\d+(?:\.\d+)*)", version.strip())
    if not match:
        raise UpdateError(f"cannot read the version {version!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def major_of(version: str) -> int:
    return release_tuple(version)[0]


def canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _trim_zeros(parts: tuple[int, ...]) -> tuple[int, ...]:
    trimmed = list(parts)
    while len(trimmed) > 1 and trimmed[-1] == 0:
        trimmed.pop()
    return tuple(trimmed)


_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)$")
_CLAUSE = re.compile(r"^(===|==|~=|!=|<=|>=|<|>)\s*([A-Za-z0-9*.+!_-]+)$")
_NPM_RANGE = re.compile(r"^[~^]?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def parse_requirement(text: str) -> tuple[str, str]:
    """Split a PEP 508 string into (name, version clauses). Reject direct URLs."""
    requirement = text.split(";", 1)[0]
    if "@" in requirement:
        raise UpdateError(f"{text!r} is a direct URL requirement")
    match = _REQUIREMENT.match(requirement)
    if not match:
        raise UpdateError(f"cannot read the requirement {text!r}")
    spec = match.group(2).strip()
    if spec.startswith("("):
        raise UpdateError(f"{text!r} uses a parenthesized version clause")
    return match.group(1), spec


def check_python_range(name: str, spec: str, locked_majors: set[int]) -> str | None:
    """Return a problem text when the range admits the next major version."""
    parsed: list[tuple[str, str]] = []
    for clause in (c.strip() for c in spec.split(",")):
        if not clause:
            continue
        match = _CLAUSE.match(clause)
        if not match:
            return f"{name}: cannot read the version clause {clause!r}"
        parsed.append((match.group(1), match.group(2)))
    for op, value in parsed:
        if op in ("==", "==="):
            return None
        if op == "~=":
            if len(release_tuple(value)) >= 2:
                return None
            return f"{name}: '~={value}' needs two release parts"
    lower_majors = [major_of(value) for op, value in parsed if op in (">=", ">")]
    if locked_majors:
        current = min(locked_majors)
    elif lower_majors:
        current = min(lower_majors)
    else:
        return f"{name}: no locked version and no lower bound, so its next major is unknown"
    limit = (current + 1,)
    for op, value in parsed:
        bound = _trim_zeros(release_tuple(value))
        if (op == "<" and bound <= limit) or (op == "<=" and bound < limit):
            return None
    return f"{name}: the range {spec!r} admits major version {current + 1}"


def check_npm_range(name: str, spec: str) -> str | None:
    if _NPM_RANGE.match(spec):
        return None
    return f"{name}: the npm range {spec!r} is not an exact, tilde or caret range"


# --- manifests -----------------------------------------------------------------


def load_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise UpdateError(f"cannot read {path.name}: {exc}") from exc


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"cannot read {path.name}: {exc}") from exc


def member_manifests(root: Path) -> list[Path]:
    """Return the root manifest and every workspace member manifest."""
    root_data = load_toml(root / "pyproject.toml")
    patterns = root_data.get("tool", {}).get("uv", {}).get("workspace", {}).get("members", [])
    found = []
    for pattern in patterns:
        for directory in sorted(root.glob(pattern)):
            manifest = directory / "pyproject.toml"
            if manifest.is_file():
                found.append(manifest)
    return [root / "pyproject.toml", *found]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_hashes(root: Path) -> dict[str, str]:
    paths = [*member_manifests(root), root / NPM_MANIFEST]
    return {str(path.relative_to(root)): sha256_file(path) for path in paths}


def recheck_manifests(root: Path, expected: Mapping[str, str]) -> None:
    for rel, digest in expected.items():
        if sha256_file(root / rel) != digest:
            raise UpdateError(f"{rel} changed after the range check read it")


_COOLDOWN = re.compile(r"^(\d+) days?$")


def read_cooldown_days(root_data: Mapping[str, Any]) -> int:
    value = root_data.get("tool", {}).get("uv", {}).get("exclude-newer")
    if not isinstance(value, str) or not (match := _COOLDOWN.match(value.strip())):
        raise UpdateError(
            "pyproject.toml [tool.uv] exclude-newer is missing or not of the form 'N days' "
            f"(found {value!r})"
        )
    days = int(match.group(1))
    if days < 1:
        raise UpdateError("pyproject.toml [tool.uv] exclude-newer must be at least '1 day'")
    return days


_NPMRC_REGISTRY = re.compile(r"^\s*(?:@[^:\s]+:)?registry\s*=", re.MULTILINE)
_PIP_INDEX = re.compile(r"^\s*(?:index-url|extra-index-url|find-links)\s*=", re.MULTILINE)


def check_index_config(root: Path) -> list[str]:
    """Report every file that sets a package index or registry for uv, pip or npm."""
    problems: list[str] = []
    for rel in ("uv.toml", "cdk/uv.toml"):
        path = root / rel
        if path.is_file():
            data = load_toml(path)
            problems += [f"{rel} sets {key}" for key in sorted(UV_INDEX_KEYS & data.keys())]
    for rel in (".npmrc", "cdk/.npmrc"):
        path = root / rel
        if path.is_file() and _NPMRC_REGISTRY.search(path.read_text(encoding="utf-8")):
            problems.append(f"{rel} sets a registry")
    for rel in ("pip.conf", "cdk/pip.conf"):
        path = root / rel
        if path.is_file() and _PIP_INDEX.search(path.read_text(encoding="utf-8")):
            problems.append(f"{rel} sets a package index")
    return problems


def _requirement_lists(data: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """Yield (table, requirement text) for every table that uv reads."""
    project = data.get("project", {})
    for text in project.get("dependencies", []):
        yield "project.dependencies", text
    for extra, texts in project.get("optional-dependencies", {}).items():
        for text in texts:
            yield f"project.optional-dependencies.{extra}", text
    for group, entries in data.get("dependency-groups", {}).items():
        for entry in entries:
            if isinstance(entry, str):
                yield f"dependency-groups.{group}", entry
            elif not (isinstance(entry, dict) and set(entry) == {"include-group"}):
                raise UpdateError(f"dependency-groups.{group} holds an entry that is not read")
    uv_table = data.get("tool", {}).get("uv", {})
    for key in (
        "override-dependencies",
        "constraint-dependencies",
        "build-constraint-dependencies",
    ):
        for text in uv_table.get(key, []):
            yield f"tool.uv.{key}", text


def _check_uv_table(rel: str, data: Mapping[str, Any]) -> list[str]:
    uv_table = data.get("tool", {}).get("uv", {})
    problems = []
    for key in sorted(uv_table):
        if key in UV_INDEX_KEYS:
            problems.append(f"{rel}: [tool.uv] sets {key}, which is a package index")
        elif key not in UV_KEYS_READ:
            problems.append(f"{rel}: [tool.uv] {key} is not read by the range check")
    for name, source in uv_table.get("sources", {}).items():
        if source != {"workspace": True}:
            problems.append(f"{rel}: [tool.uv.sources] {name} is not 'workspace = true'")
    return problems


def uv_locked_majors(lock_data: Mapping[str, Any]) -> dict[str, set[int]]:
    majors: dict[str, set[int]] = {}
    for package in lock_data.get("package", []):
        if package.get("version"):
            majors.setdefault(canon(package["name"]), set()).add(major_of(package["version"]))
    return majors


@dataclass(frozen=True)
class Preflight:
    cooldown_days: int
    python_direct: frozenset[str]
    npm_direct: frozenset[str]
    hashes: dict[str, str] = field(default_factory=dict)


def preflight(root: Path) -> Preflight:
    """Run every check that needs no network. Raise one error that lists all problems."""
    problems: list[str] = []
    root_data = load_toml(root / "pyproject.toml")
    try:
        cooldown_days = read_cooldown_days(root_data)
    except UpdateError as exc:
        cooldown_days = 0
        problems.append(str(exc))
    problems += check_index_config(root)

    manifests = member_manifests(root)
    members: dict[Path, dict[str, Any]] = {path: load_toml(path) for path in manifests}
    member_names = {
        canon(data["project"]["name"]) for data in members.values() if "project" in data
    }
    majors = uv_locked_majors(load_toml(root / UV_LOCK))

    python_direct: set[str] = set()
    for path, data in members.items():
        rel = str(path.relative_to(root))
        problems += _check_uv_table(rel, data)
        if path != root / "pyproject.toml" and data.get("build-system", {}).get("requires") != [
            "hatchling"
        ]:
            problems.append(f"{rel}: [build-system] requires is not ['hatchling']")
        try:
            for table, text in _requirement_lists(data):
                name, spec = parse_requirement(text)
                if canon(name) in member_names:
                    continue
                python_direct.add(canon(name))
                problem = check_python_range(name, spec, majors.get(canon(name), set()))
                if problem:
                    problems.append(f"{rel} {table}: {problem}")
        except UpdateError as exc:
            problems.append(f"{rel}: {exc}")

    package_json = load_json(root / NPM_MANIFEST)
    npm_direct: set[str] = set()
    for section in NPM_UNREAD_SECTIONS:
        if section in package_json:
            problems.append(f"{NPM_MANIFEST}: {section} is not read by the range check")
    for section in NPM_DEPENDENCY_SECTIONS:
        for name, spec in package_json.get(section, {}).items():
            npm_direct.add(name)
            problem = check_npm_range(name, str(spec))
            if problem:
                problems.append(f"{NPM_MANIFEST} {section}: {problem}")

    if problems:
        raise UpdateError("pre-resolution checks failed:\n  - " + "\n  - ".join(problems))
    return Preflight(
        cooldown_days, frozenset(python_direct), frozenset(npm_direct), manifest_hashes(root)
    )


# --- lockfiles -----------------------------------------------------------------


@dataclass(frozen=True)
class Pkg:
    name: str
    version: str
    source: str
    digest: str
    nested: bool = False  # an npm copy below another package's node_modules


def _distribution_files(package: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [*([package["sdist"]] if "sdist" in package else []), *package.get("wheels", [])]


def parse_uv_lock(data: Mapping[str, Any]) -> list[Pkg]:
    packages = []
    for package in data.get("package", []):
        digest = ",".join(sorted(str(f.get("hash", "")) for f in _distribution_files(package)))
        source = json.dumps(package.get("source", {}), sort_keys=True)
        packages.append(
            Pkg(canon(package["name"]), str(package.get("version", "")), source, digest)
        )
    return packages


def parse_npm_lock(data: Mapping[str, Any]) -> list[Pkg]:
    packages = []
    for key, entry in data.get("packages", {}).items():
        if not key:
            continue
        name = key.rsplit("node_modules/", 1)[-1]
        source = "bundled" if entry.get("inBundle") else str(entry.get("resolved", ""))
        nested = key.count("node_modules/") > 1
        packages.append(
            Pkg(name, str(entry.get("version", "")), source, entry.get("integrity", ""), nested)
        )
    return packages


def majors_by_name(packages: Sequence[Pkg]) -> dict[str, set[int]]:
    majors: dict[str, set[int]] = {}
    for package in packages:
        if package.version and not package.nested:
            majors.setdefault(package.name, set()).add(major_of(package.version))
    return majors


def compare_majors(
    old: Sequence[Pkg], new: Sequence[Pkg], direct: frozenset[str], label: str
) -> list[str]:
    old_majors, new_majors = majors_by_name(old), majors_by_name(new)
    problems = []
    for name in sorted(direct):
        if name in old_majors and name in new_majors and old_majors[name] != new_majors[name]:
            problems.append(
                f"{label} {name}: major version changed "
                f"{sorted(old_majors[name])} -> {sorted(new_majors[name])}"
            )
    return problems


def _audit_registry_package(name: str, package: Mapping[str, Any]) -> list[str]:
    problems = []
    registry = package["source"]["registry"]
    if registry != PYPI_REGISTRY:
        problems.append(f"uv.lock {name}: registry {registry!r} is not PyPI")
    files = _distribution_files(package)
    if not files:
        problems.append(f"uv.lock {name}: no distribution file is recorded")
    for item in files:
        if not str(item.get("hash", "")).startswith("sha256:"):
            problems.append(f"uv.lock {name}: a distribution file has no sha256 hash")
        if not str(item.get("url", "")).startswith(PYPI_FILES_PREFIX):
            problems.append(f"uv.lock {name}: a file URL is not on files.pythonhosted.org")
    return problems


def audit_uv_lock(data: Mapping[str, Any]) -> list[str]:
    problems = []
    for package in data.get("package", []):
        name, source = package["name"], package.get("source", {})
        is_member = canon(name).startswith("cairn-")
        if "registry" in source:
            if is_member:
                problems.append(f"uv.lock {name}: a cairn-* package must be an editable member")
            problems += _audit_registry_package(name, package)
        elif not (is_member and ("editable" in source or "virtual" in source)):
            kind = next(iter(source), "unknown")
            problems.append(f"uv.lock {name}: source {kind!r} is not allowed")
    return problems


def audit_npm_lock(data: Mapping[str, Any]) -> list[str]:
    problems = []
    for key, entry in data.get("packages", {}).items():
        if not key or entry.get("inBundle"):
            continue
        name = key.rsplit("node_modules/", 1)[-1]
        resolved = str(entry.get("resolved", ""))
        if entry.get("link") or resolved.startswith(("git", "file:")):
            problems.append(f"package-lock.json {name}: link, git or file source")
        elif not resolved.startswith(NPM_REGISTRY_PREFIX):
            problems.append(f"package-lock.json {name}: resolved URL is not the npm registry")
        if not str(entry.get("integrity", "")).startswith("sha512-"):
            problems.append(f"package-lock.json {name}: no sha512 integrity value")
    return problems


def audit_locks(root: Path) -> tuple[list[str], str]:
    """Return (problems, summary). Reads both lockfiles, needs no network."""
    uv_data, npm_data = load_toml(root / UV_LOCK), load_json(root / NPM_LOCK)
    problems = audit_uv_lock(uv_data) + audit_npm_lock(npm_data)
    summary = (
        f"{len(uv_data.get('package', []))} uv packages, "
        f"{len(npm_data.get('packages', {})) - 1} npm entries"
    )
    return problems, summary


# --- snapshot and restore --------------------------------------------------------


def atomic_write(path: Path, data: bytes) -> None:
    """Replace a file through a temporary file in the same directory."""
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@dataclass
class Snapshot:
    root: Path
    files: dict[str, bytes]

    @classmethod
    def capture(cls, root: Path) -> Snapshot:
        return cls(root, {rel: (root / rel).read_bytes() for rel in (UV_LOCK, NPM_LOCK)})

    def digests(self) -> dict[str, str]:
        return {rel: hashlib.sha256(data).hexdigest() for rel, data in self.files.items()}

    def restore(self) -> list[str]:
        """Write every saved file back. A failure on one file does not skip the others."""
        errors = []
        for rel, data in self.files.items():
            try:
                atomic_write(self.root / rel, data)
            except Exception as exc:  # report and continue with the next file
                errors.append(f"{rel}: {exc}")
        return errors


def default_lock_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = env.get("XDG_RUNTIME_DIR") or env.get("TMPDIR") or "/tmp"  # noqa: S108
    return Path(base) / f"cairn-update-deps-{os.getuid()}" / "update-deps.lock"


@contextlib.contextmanager
def run_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock so that only one update-deps run exists at a time."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise UpdateError(f"lock directory {path.parent} is not private to this user")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("another update-deps run is active") from exc
        yield
    finally:
        os.close(fd)


def ensure_lockfiles_clean(root: Path, runner: Runner) -> None:
    result = runner(["git", "status", "--porcelain", "--", UV_LOCK, NPM_LOCK], cwd=root)
    if result.returncode != 0:
        raise UpdateError(f"git status failed: {esc(result.stderr.strip())}")
    if result.stdout.strip():
        raise UpdateError(
            "a lockfile has uncommitted changes, so the run cannot restore the original. "
            f"Undo them first with: {RESTORE_COMMAND}"
        )


# --- resolution ---------------------------------------------------------------------

_BACKTICKED = re.compile(r"`([A-Za-z0-9][A-Za-z0-9._-]*)(?=[=<>!~\s`\[])")
_NAME_AT = re.compile(r"((?:@[\w.-]+/)?[\w.-]+)@[~^<>=*\dvx]")


def describe_failure(step: str, result: Result) -> str:
    text = f"{result.stderr}\n{result.stdout}"
    found = _BACKTICKED.search(text) or _NAME_AT.search(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    detail = esc(scrub_urls(lines[-1]))[:300] if lines else "no output"
    subject = f" for {esc(found.group(1))}" if found else ""
    return f"{step} failed{subject}: {detail}"


def cutoff_date(now: dt.datetime, days: int) -> str:
    moment = now.astimezone(dt.UTC) - dt.timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve(root: Path, runner: Runner, cooldown_days: int, now: dt.datetime) -> str:
    """Re-resolve both lockfiles and verify signatures. Return the cutoff date."""
    cutoff = cutoff_date(now, cooldown_days)
    steps = [
        (
            "uv lock",
            ["uv", "lock", "--upgrade", "--no-build", "--default-index", PYPI_REGISTRY],
            root,
        ),
        (
            "npm update",
            [
                "npm",
                "update",
                "--package-lock-only",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--registry",
                NPM_REGISTRY,
                "--before",
                cutoff,
            ],
            root / "cdk",
        ),
        (
            # Install the refreshed lock, so that the signature check sees the new
            # versions. `npm audit signatures` only inspects installed packages.
            "npm ci",
            [
                "npm",
                "ci",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--registry",
                NPM_REGISTRY,
            ],
            root / "cdk",
        ),
        (
            "npm audit signatures",
            ["npm", "audit", "signatures", "--registry", NPM_REGISTRY],
            root / "cdk",
        ),
    ]
    for step, argv, cwd in steps:
        result = runner(argv, cwd=cwd)
        if result.returncode != 0:
            raise UpdateError(describe_failure(step, result))
    options = load_toml(root / UV_LOCK).get("options", {})
    if options.get("exclude-newer-span") != f"P{cooldown_days}D":
        raise UpdateError(f"uv.lock does not record the {cooldown_days}-day cooldown")
    return cutoff


# --- report ---------------------------------------------------------------------------


def diff_packages(old: Sequence[Pkg], new: Sequence[Pkg]) -> list[str]:
    """List changed, added and removed packages, and changes at an equal version."""

    def group(packages: Sequence[Pkg]) -> dict[str, dict[str, set[tuple[str, str]]]]:
        grouped: dict[str, dict[str, set[tuple[str, str]]]] = {}
        for package in packages:
            versions = grouped.setdefault(package.name, {})
            versions.setdefault(package.version, set()).add((package.source, package.digest))
        return grouped

    old_group, new_group = group(old), group(new)
    lines = []
    for name in sorted(old_group.keys() | new_group.keys()):
        old_versions, new_versions = old_group.get(name, {}), new_group.get(name, {})
        removed = sorted(old_versions.keys() - new_versions.keys())
        added = sorted(new_versions.keys() - old_versions.keys())
        label = esc(name)
        if len(removed) == 1 and len(added) == 1:
            lines.append(f"{label} {esc(removed[0])} -> {esc(added[0])}")
        else:
            lines += [f"{label} removed {esc(version)}" for version in removed]
            lines += [f"{label} added {esc(version)}" for version in added]
        for version in sorted(old_versions.keys() & new_versions.keys()):
            if old_versions[version] != new_versions[version]:
                lines.append(f"{label} {esc(version)} source or hash changed at an equal version")
    return lines


def build_report(
    old_uv: Sequence[Pkg],
    new_uv: Sequence[Pkg],
    old_npm: Sequence[Pkg],
    new_npm: Sequence[Pkg],
    cutoff: str,
) -> str:
    sections = [f"Release cutoff: {cutoff} (releases published later are not selected)"]
    for title, old, new in (
        ("Python (uv.lock)", old_uv, new_uv),
        ("CDK (cdk/package-lock.json)", old_npm, new_npm),
    ):
        changes = diff_packages(old, new)
        sections += ["", f"{title}:", *(f"  {line}" for line in changes or ["no changes"])]
    sections += ["", REVIEW_LINE]
    return "\n".join(sections)


@dataclass
class RunRecord:
    report: str = ""
    audit: str = "not run"
    hashes_before: dict[str, str] = field(default_factory=dict)
    hashes_after: dict[str, str] = field(default_factory=dict)
    restore: str = "not needed"
    error: str = ""

    def render(self) -> str:
        lines = ["update-deps run record", ""]
        if self.error:
            lines += [f"Error: {esc_text(self.error)}", ""]
        if self.report:
            lines += [self.report, ""]
        lines.append(f"Lock audit: {self.audit}")
        for rel in sorted(self.hashes_before):
            after = self.hashes_after.get(rel, "unknown")
            lines.append(f"sha256 {rel}: before {self.hashes_before[rel]} after {after}")
        lines.append(f"Restore: {self.restore}")
        return "\n".join(lines) + "\n"


def write_report_file(root: Path, record: RunRecord) -> None:
    with contextlib.suppress(OSError):
        (root / REPORT_FILE).write_text(record.render(), encoding="utf-8")


# --- the run ------------------------------------------------------------------------------


def _resolve_and_verify(
    root: Path, runner: Runner, pre: Preflight, now: dt.datetime, record: RunRecord
) -> tuple[str, list[Pkg], list[Pkg]]:
    recheck_manifests(root, pre.hashes)
    cutoff = resolve(root, runner, pre.cooldown_days, now)
    problems, summary = audit_locks(root)
    if problems:
        record.audit = "failed"
        raise UpdateError("lock audit failed:\n  - " + "\n  - ".join(problems))
    record.audit = f"passed ({summary})"
    return (
        cutoff,
        parse_uv_lock(load_toml(root / UV_LOCK)),
        parse_npm_lock(load_json(root / NPM_LOCK)),
    )


def _run_locked(
    root: Path,
    runner: Runner,
    now: dt.datetime,
    say: Callable[[str], None],
    fail: Callable[[str], None],
    record: RunRecord,
) -> int:
    pre = preflight(root)
    ensure_lockfiles_clean(root, runner)
    snapshot = Snapshot.capture(root)
    record.hashes_before = snapshot.digests()
    old_uv = parse_uv_lock(load_toml(root / UV_LOCK))
    old_npm = parse_npm_lock(load_json(root / NPM_LOCK))

    with interrupts_raise_keyboard_interrupt():
        try:
            cutoff, new_uv, new_npm = _resolve_and_verify(root, runner, pre, now, record)
            problems = compare_majors(old_uv, new_uv, pre.python_direct, "uv.lock")
            problems += compare_majors(old_npm, new_npm, pre.npm_direct, "package-lock.json")
            if problems:
                raise UpdateError(
                    "a direct dependency changed its major version:\n  - " + "\n  - ".join(problems)
                )
        except BaseException as exc:
            with signals_ignored():  # a second signal must not stop the restore
                errors = snapshot.restore()
                record.hashes_after = Snapshot.capture(root).digests()
            record.restore = (
                "both lockfiles restored" if not errors else "FAILED: " + "; ".join(errors)
            )
            interrupted = isinstance(exc, KeyboardInterrupt)
            if not isinstance(exc, (Exception, KeyboardInterrupt)):
                raise
            record.error = "interrupted" if interrupted else str(exc)
            fail(f"update-deps: {esc_text(record.error)}")
            if errors:
                fail(f"update-deps: could not restore {'; '.join(errors)}")
                fail(f"update-deps: recover with: {RESTORE_COMMAND}")
            return 130 if interrupted else 1

    record.hashes_after = Snapshot.capture(root).digests()
    record.report = build_report(old_uv, new_uv, old_npm, new_npm, cutoff)
    say(record.report)
    with interrupts_raise_keyboard_interrupt():
        return _run_gate(root, runner, fail, record)


def _run_gate(root: Path, runner: Runner, fail: Callable[[str], None], record: RunRecord) -> int:
    try:
        code = runner(["mise", "run", "check"], cwd=root, capture=False).returncode
    except KeyboardInterrupt:
        fail("update-deps: interrupted during the gate. The new lockfiles stay in place.")
        return 130
    except UpdateError as exc:
        fail(f"update-deps: {esc_text(str(exc))}")
        code = 1
    if code != 0:
        record.error = "gate failed"
        fail(
            "update-deps: Gate failed. The new lockfiles are not checked. "
            f"Undo the update with: {RESTORE_COMMAND}"
        )
        return code if code > 0 else 1
    return 0


def run_update(
    root: Path,
    runner: Runner,
    *,
    now: dt.datetime | None = None,
    out: IO[str] | None = None,
    err: IO[str] | None = None,
    lock_path: Path | None = None,
) -> int:
    say, fail = Writer(out or sys.stdout), Writer(err or sys.stderr)
    moment = now or dt.datetime.now(dt.UTC)
    record = RunRecord()
    try:
        with run_lock(lock_path or default_lock_path()):
            return _run_locked(root, runner, moment, say, fail, record)
    except UpdateError as exc:
        record.error = str(exc)
        fail(f"update-deps: {esc_text(str(exc))}")
        return 1
    finally:
        write_report_file(root, record)


def audit_command(root: Path) -> int:
    problems, summary = audit_locks(root)
    if problems:
        print(f"lock-audit: FAILED ({len(problems)} problem(s)):", file=sys.stderr)
        for problem in problems:
            print(f"  - {esc_text(problem)}", file=sys.stderr)
        return 1
    print(f"lock-audit: OK ({summary})")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--audit-locks",
        action="store_true",
        help="audit uv.lock and cdk/package-lock.json offline, then exit",
    )
    args = parser.parse_args(argv)
    try:
        if args.audit_locks:
            return audit_command(REPO_ROOT)
        return run_update(REPO_ROOT, make_runner())
    except UpdateError as exc:
        print(f"update-deps: {esc_text(str(exc))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

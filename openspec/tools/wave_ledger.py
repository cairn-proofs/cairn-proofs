"""Wave-status ledger for the opsx-apply orchestration flow.

The `subagent` tool runs the whole Task Dependency Graph in a single *blocking*
call: it returns only when every wave has settled, so the orchestrator cannot
flip `tasks.md` checkboxes mid-run and a wave that dies mid-flight is invisible
until the end. This helper gives waves a side channel that does *not* depend on
that consolidated return:

- each wave writes its OWN status file (`running` -> `done`/`failed`) the moment
  it transitions, so the ledger is the durable "where do we stand against the
  plan" record once the blocking call returns;
- the orchestrator reads the consolidated view to reconcile `tasks.md` and to
  decide which waves to RE-DISPATCH (failed / never-finished), instead of
  redoing the whole DAG;
- a re-dispatched wave consults the completed-task set on entry and skips work
  already done — the idempotency/resumability half.

Per-wave files (one `wave-<id>.json` per wave) keep writes file-disjoint, so
concurrent waves never contend on a shared file (mirrors the file-disjoint wave
invariant in AGENTS.md). Writes are atomic via a temp file + `os.replace`, so a
reader never observes a half-written record. The ledger lives in an ephemeral,
per-user run directory OUTSIDE the repo tree (it is transient state, never
committed), following the same 0700 run-dir convention as the build scripts.

The decision logic lives in pure functions (`build_record`, `summarize`,
`completed_task_ids`, `outstanding_waves`) so it is unit-testable without the
filesystem; the filesystem layer (`run_dir`, `write_record`, `read_records`) is
thin, and `main()` is the CLI wrapper.

Usage (invoked via `uv run python openspec/tools/wave_ledger.py ...`):
    # a wave, on entry:
    wave_ledger.py record <change> <wave-id> running --tasks 1.1,1.2
    # a wave, on success / failure:
    wave_ledger.py record <change> <wave-id> done   --files libs/oci/x.py --tests "pytest: 12 ok"
    wave_ledger.py record <change> <wave-id> failed  --reason "moto stub raised on put_object"
    # a wave, on entry, to skip already-done work (idempotency):
    wave_ledger.py completed-tasks <change>
    # the orchestrator, after the blocking DAG call returns:
    wave_ledger.py status      <change> --json
    wave_ledger.py outstanding <change> --waves wave-1,wave-2,wave-3
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# A wave moves running -> {done, failed}. Any other token is a caller bug: fail
# closed rather than write an unrecognized status the orchestrator can't reason
# about.
VALID_STATUSES = frozenset({"running", "done", "failed"})

# Path components (change name, wave id) come from the orchestrator's prompt and
# are interpolated into a filesystem path. Constrain them to a safe charset so a
# value like "../../etc" can never escape the run directory.
_SAFE_COMPONENT = re.compile(r"\A[A-Za-z0-9._-]+\Z")


# --- pure logic ------------------------------------------------------------


def _now_iso(now: float | None = None) -> str:
    """Return an ISO-8601 UTC timestamp (second precision, trailing Z)."""
    secs = time.time() if now is None else now
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(secs))


def _split_csv(value: str | None) -> list[str]:
    """Parse a comma-separated CLI value into a trimmed, non-empty list."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def build_record(
    wave: str,
    status: str,
    *,
    change: str | None = None,
    tasks: list[str] | None = None,
    files: list[str] | None = None,
    tests: str | None = None,
    reason: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Construct a schema-correct, validated ledger record.

    Fail closed on an unknown status, and require a `reason` for `failed` — a
    failure with no reason is useless to the orchestrator re-dispatch decision.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {sorted(VALID_STATUSES)}")
    if status == "failed" and not (reason and reason.strip()):
        raise ValueError("a 'failed' record requires a non-empty --reason")
    return {
        "wave": wave,
        "change": change,
        "status": status,
        "tasks": list(tasks or []),
        "files": list(files or []),
        "tests": tests,
        "reason": reason,
        "updatedAt": _now_iso(now),
    }


def completed_task_ids(records: list[dict[str, Any]]) -> set[str]:
    """Union of task ids across waves recorded `done` — the idempotency set.

    A wave consults this on entry and skips any of its assigned tasks already
    present, so a re-dispatch redoes only the unfinished work.
    """
    done: set[str] = set()
    for rec in records:
        if rec.get("status") == "done":
            done.update(rec.get("tasks") or [])
    return done


def outstanding_waves(records: list[dict[str, Any]], expected: list[str]) -> list[str]:
    """Expected wave ids that are NOT recorded `done` (failed / running / absent).

    These are exactly the waves the orchestrator must re-dispatch after the
    blocking DAG call returns. Order follows `expected` for stable output.
    """
    done = {rec.get("wave") for rec in records if rec.get("status") == "done"}
    return [wave for wave in expected if wave not in done]


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Consolidated "where do we stand against the plan" view over all records."""
    by_status: dict[str, list[str]] = {s: [] for s in sorted(VALID_STATUSES)}
    for rec in records:
        status = rec.get("status")
        if status in by_status:
            by_status[status].append(rec.get("wave"))
    return {
        "waves": len(records),
        "byStatus": {s: sorted(filter(None, w)) for s, w in by_status.items()},
        "completedTasks": sorted(completed_task_ids(records)),
    }


# --- filesystem layer ------------------------------------------------------


def _check_component(name: str, value: str) -> str:
    # `.`/`..` pass the charset check but are directory traversal references.
    if value in {".", ".."} or not _SAFE_COMPONENT.match(value):
        raise ValueError(f"unsafe {name} {value!r}: allowed [A-Za-z0-9._-], and not . or ..")
    return value


def _base_dir() -> Path:
    """Ephemeral base for the ledger.

    Prefer `XDG_RUNTIME_DIR` (per-user, tmpfs on Linux); fall back to the
    platform temp dir via `tempfile.gettempdir()` (honours TMPDIR) rather than a
    hardcoded `/tmp`.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    return Path(xdg) if xdg else Path(tempfile.gettempdir())


def run_dir(change: str, *, base: Path | None = None, create: bool = False) -> Path:
    """Resolve (and optionally create 0700) the run directory for a change.

    Layout: <base>/cairn-opsx-<uid>/<change>/ — the per-user parent isolates one
    user's ledgers from another's on a shared temp dir (the /tmp-symlink attack
    class the build scripts also guard against).
    """
    _check_component("change", change)
    root = (base or _base_dir()) / f"cairn-opsx-{os.getuid()}"
    target = root / change
    if create:
        # Create each level explicitly with restrictive perms; chmod after to
        # defeat a permissive umask.
        for path in (root, target):
            path.mkdir(mode=0o700, parents=False, exist_ok=True)
            path.chmod(0o700)
    return target


def _wave_path(change: str, wave: str, *, base: Path | None = None, create: bool = False) -> Path:
    _check_component("wave", wave)
    return run_dir(change, base=base, create=create) / f"wave-{wave}.json"


def write_record(record: dict[str, Any], *, base: Path | None = None) -> Path:
    """Atomically persist a record to its wave file via temp + os.replace."""
    change = record.get("change")
    if not change:
        raise ValueError("record must carry a 'change' to resolve its run directory")
    wave = record["wave"]
    target = _wave_path(change, wave, base=base, create=True)
    payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
    # Temp file in the SAME directory so os.replace is a same-filesystem rename
    # (atomic); a reader thus sees either the old file or the whole new one.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        # Never leave a stray temp file behind on failure.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return target


def read_records(change: str, *, base: Path | None = None) -> list[dict[str, Any]]:
    """Read every wave record for a change, sorted by wave file name.

    Fail closed on a malformed file (a half-written or hand-corrupted record):
    the orchestrator must not silently treat an unreadable wave as "not done"
    and risk re-dispatching completed work, nor as "done" and skip real work.
    """
    directory = run_dir(change, base=base, create=False)
    if not directory.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("wave-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"malformed ledger record {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"malformed ledger record {path}: expected a JSON object")
        records.append(data)
    return records


# --- CLI -------------------------------------------------------------------


def _cmd_record(args: argparse.Namespace) -> int:
    record = build_record(
        args.wave,
        args.status,
        change=args.change,
        tasks=_split_csv(args.tasks),
        files=_split_csv(args.files),
        tests=args.tests,
        reason=args.reason,
    )
    path = write_record(record, base=args.base)
    print(path)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    summary = summarize(read_records(args.change, base=args.base))
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"change: {args.change}  waves: {summary['waves']}")
        for status, waves in summary["byStatus"].items():
            print(f"  {status:8} {len(waves)}  {', '.join(waves)}")
        print(f"  completed tasks: {', '.join(summary['completedTasks']) or '(none)'}")
    return 0


def _cmd_completed_tasks(args: argparse.Namespace) -> int:
    for task in sorted(completed_task_ids(read_records(args.change, base=args.base))):
        print(task)
    return 0


def _cmd_outstanding(args: argparse.Namespace) -> int:
    expected = _split_csv(args.waves)
    for wave in outstanding_waves(read_records(args.change, base=args.base), expected):
        print(wave)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--base",
        type=Path,
        default=None,
        help="Override the ledger base dir (test/CI); defaults to XDG_RUNTIME_DIR or temp dir.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="write a wave's status (running|done|failed)")
    rec.add_argument("change")
    rec.add_argument("wave")
    rec.add_argument("status", choices=sorted(VALID_STATUSES))
    rec.add_argument("--tasks", help="comma-separated task ids this wave owns")
    rec.add_argument("--files", help="comma-separated files this wave touched")
    rec.add_argument("--tests", help="free-form scoped-test result, e.g. 'pytest: 12 passed'")
    rec.add_argument("--reason", help="failure reason (required when status=failed)")
    rec.set_defaults(func=_cmd_record)

    st = sub.add_parser("status", help="consolidated view of all waves for a change")
    st.add_argument("change")
    st.add_argument("--json", action="store_true", help="emit JSON instead of text")
    st.set_defaults(func=_cmd_status)

    ct = sub.add_parser("completed-tasks", help="task ids already done (idempotency set)")
    ct.add_argument("change")
    ct.set_defaults(func=_cmd_completed_tasks)

    out = sub.add_parser("outstanding", help="expected waves not yet done (re-dispatch set)")
    out.add_argument("change")
    out.add_argument("--waves", required=True, help="comma-separated expected wave ids")
    out.set_defaults(func=_cmd_outstanding)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

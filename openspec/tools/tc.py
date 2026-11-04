"""Threat-model reconcile, gate and report for OpenSpec changes.

A small, dependency-free helper that reads a change's private Threat Composer v1
model at `threat-models/<change>.tc.json`. It never writes the model: the
`threat-composer` MCP server is the only writer. The commands are:

- **reconcile** — read the change's `tasks.md` and the `task:<N.M>` tags on the
  model's mitigations, and print the status changes the session must apply with
  the MCP's `tm_set_status`. A mitigation is resolved when every task it is
  tagged with is checked. A threat is resolved when every mitigation linked to
  it is resolved.
- **gate** — the fail-closed check: a non-zero exit if a threat has a missing or
  unrecognized `Priority`, a High threat has no mitigation, or an open threat
  still depends on an unresolved mitigation.
- **report** — write `openspec/changes/<change>/threat-model-report.md`, a
  sanitized summary that is safe to commit to the public repo. It holds no
  threat, mitigation, asset or system text. `--check` fails if the committed
  report does not match the model.

The logic lives in pure functions so it is unit-testable without touching the
filesystem; `main()` is the thin CLI wrapper.

Usage:
    python3 openspec/tools/tc.py reconcile <change>
    python3 openspec/tools/tc.py gate <change>
    python3 openspec/tools/tc.py report <change> [--check]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

MODEL_DIR = Path("threat-models")
CHANGES_DIR = Path("openspec/changes")
REPORT_NAME = "threat-model-report.md"

# A mitigation in either of these built-in states no longer holds back a threat:
# `mitigationResolved` = implemented or already present; `...WillNotAction` =
# risk accepted.
RESOLVED_MITIGATION_STATES = frozenset({"mitigationResolved", "mitigationResolvedWillNotAction"})
# States reconcile may advance from. Human-set terminal states are left alone.
OPEN_MITIGATION_STATES = frozenset({"mitigationIdentified", "mitigationInProgress"})
# A dismissed threat is a recorded decision, not a risk; the gate skips it.
DISMISSED_THREAT_STATE = "threatResolvedNotUseful"
# A value outside this set (or absent) is a malformed model: fail closed rather
# than treat e.g. "high" as benign because it is not the exact string "High".
VALID_PRIORITIES = frozenset({"High", "Medium", "Low"})

TASK_TAG_PREFIX = "task:"
_TASK_LINE = re.compile(r"^\s*- \[([ xX])\] (\d+(?:\.\d+)+)\b")


def _priority(threat: dict[str, Any]) -> Any:
    """Return the threat's built-in `Priority` metadata value, or None if unset."""
    for entry in threat.get("metadata", []) or []:
        if entry.get("key") == "Priority":
            return entry.get("value")
    return None


def _threat_to_mitigations(model: dict[str, Any]) -> dict[str, list[str]]:
    """Map each threat id to the ids of the mitigations linked to it."""
    out: dict[str, list[str]] = defaultdict(list)
    for link in model.get("mitigationLinks", []) or []:
        out[link.get("linkedId")].append(link.get("mitigationId"))
    return out


def _task_tags(mitigation: dict[str, Any]) -> list[str]:
    """Return the task ids a mitigation is tagged with (`task:3.2` -> `3.2`)."""
    return [
        tag[len(TASK_TAG_PREFIX) :]
        for tag in mitigation.get("tags", []) or []
        if isinstance(tag, str) and tag.startswith(TASK_TAG_PREFIX)
    ]


def parse_tasks(tasks_md: str) -> dict[str, bool]:
    """Map each numbered checklist task id in `tasks.md` to whether it is checked."""
    tasks: dict[str, bool] = {}
    for line in tasks_md.splitlines():
        match = _TASK_LINE.match(line)
        if match:
            tasks[match.group(2)] = match.group(1) in "xX"
    return tasks


def reconcile(model: dict[str, Any], tasks: dict[str, bool]) -> dict[str, list[str]]:
    """Compute the status changes that the shipped tasks justify. Never mutates.

    Returns {"mitigations": [...], "threats": [...], "errors": [...]}:
    - `mitigations`: open mitigations to set to `mitigationResolved` — each has
      at least one `task:` tag and every tagged task is checked.
    - `threats`: `threatIdentified` threats to set to `threatResolved` — each has
      at least one linked mitigation and all of them are resolved once the
      mitigation changes above are applied.
    - `errors`: `task:` tags that name a task absent from `tasks.md`. A non-empty
      list means the mapping is stale (for example, tasks were renumbered); the
      caller must not apply the changes.
    """
    errors: list[str] = []
    advanced: list[str] = []
    for mitigation in model.get("mitigations", []) or []:
        mid = mitigation.get("id")
        task_ids = _task_tags(mitigation)
        unknown = [t for t in task_ids if t not in tasks]
        if unknown:
            errors.append(f"mitigation {mid}: task tag(s) {', '.join(unknown)} not in tasks.md")
            continue
        if mitigation.get("status") not in OPEN_MITIGATION_STATES or not task_ids:
            continue
        if all(tasks[t] for t in task_ids):
            advanced.append(mid)

    resolved = {
        m.get("id")
        for m in model.get("mitigations", []) or []
        if m.get("status") in RESOLVED_MITIGATION_STATES
    } | set(advanced)
    links = _threat_to_mitigations(model)

    threats: list[str] = []
    for threat in model.get("threats", []) or []:
        tid = threat.get("id")
        linked = links.get(tid, [])
        if threat.get("status") != "threatIdentified" or not linked:
            continue
        if all(mid in resolved for mid in linked):
            threats.append(tid)

    return {"mitigations": advanced, "threats": threats, "errors": errors}


def gate(model: dict[str, Any]) -> list[str]:
    """Return a list of fail-closed violations; empty means the gate passes.

    Dismissed (`threatResolvedNotUseful`) threats are skipped. For every other
    threat, three conditions fail the gate:
      1. Its `Priority` is missing or not High/Medium/Low.
      2. It is High priority and has no linked mitigation.
      3. It is not `threatResolved` and a linked mitigation is not resolved.
    """
    violations: list[str] = []
    by_id = {m.get("id"): m for m in model.get("mitigations", []) or []}
    links = _threat_to_mitigations(model)

    for threat in model.get("threats", []) or []:
        if threat.get("status") == DISMISSED_THREAT_STATE:
            continue
        tid = threat.get("id")
        priority = _priority(threat)
        linked = links.get(tid, [])
        if priority not in VALID_PRIORITIES:
            violations.append(
                f"threat {tid}: Priority {priority!r} not recognized (expected High/Medium/Low)"
            )
        elif priority == "High" and not linked:
            violations.append(f"threat {tid}: High priority threat has no mitigation")
        if threat.get("status") == "threatResolved":
            continue
        for mid in linked:
            status = (by_id.get(mid) or {}).get("status")
            if status not in RESOLVED_MITIGATION_STATES:
                violations.append(
                    f"threat {tid}: depends on unresolved mitigation {mid} (status={status})"
                )
    return violations


_REPORT_TEMPLATE = """\
# Threat model report

<!-- Generated by `python3 openspec/tools/tc.py report`. Do not edit. -->

| Field | Value |
|---|---|
| Model | `{model}` |
| Model SHA-256 | `{sha256}` |
| Gate | {gate} |
| Threats | {threats} |
| Mitigations | {mitigations} |
| Assumptions | {assumptions} |
"""

# What each template placeholder may expand to.
_PLACEHOLDER_PATTERNS = {
    "model": r"[a-z0-9][a-z0-9-]*\.tc\.json",
    "sha256": r"[0-9a-f]{64}",
    "gate": r"(?:PASSED|FAILED)",
    "threats": r"\d+",
    "mitigations": r"\d+",
    "assumptions": r"\d+",
}


def _line_pattern(line: str) -> str:
    pattern = re.escape(line)
    for name, value in _PLACEHOLDER_PATTERNS.items():
        pattern = pattern.replace(re.escape("{" + name + "}"), value)
    return pattern


# Every line a report may contain. A report with any other line (for example,
# hand-added threat text) fails `check_report_shape`.
_REPORT_LINES = [_line_pattern(line) for line in _REPORT_TEMPLATE.splitlines()]


def render_report(model_name: str, model_bytes: bytes, model: dict[str, Any]) -> str:
    """Render the sanitized report: counts, gate result and model hash only."""
    return _REPORT_TEMPLATE.format(
        model=model_name,
        sha256=hashlib.sha256(model_bytes).hexdigest(),
        gate="FAILED" if gate(model) else "PASSED",
        threats=len(model.get("threats", []) or []),
        mitigations=len(model.get("mitigations", []) or []),
        assumptions=len(model.get("assumptions", []) or []),
    )


def check_report_shape(text: str) -> list[str]:
    """Return problems if `text` is not exactly the sanitized report shape."""
    lines = text.splitlines()
    if len(lines) != len(_REPORT_LINES):
        return [f"expected {len(_REPORT_LINES)} lines, found {len(lines)}"]
    return [
        f"line {n}: unexpected content"
        for n, (line, pattern) in enumerate(zip(lines, _REPORT_LINES, strict=True), start=1)
        if not re.fullmatch(pattern, line)
    ]


def _model_path(change: str) -> Path:
    return MODEL_DIR / f"{change}.tc.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tc", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {
        "reconcile": "print the status changes that checked tasks justify",
        "gate": "fail-closed gate (non-zero exit on violations)",
        "report": "write the sanitized threat-model report",
    }
    subparsers = {name: sub.add_parser(name, help=text) for name, text in commands.items()}
    for p in subparsers.values():
        p.add_argument("change", help="change name; the model is threat-models/<change>.tc.json")
    subparsers["report"].add_argument(
        "--check", action="store_true", help="fail if the committed report is stale"
    )
    args = parser.parse_args(argv)

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.change):
        print(f"error: invalid change name {args.change!r}", file=sys.stderr)
        return 2
    path = _model_path(args.change)
    if not path.is_file():
        print(f"error: threat model not found at {path}", file=sys.stderr)
        return 2
    model_bytes = path.read_bytes()
    model = json.loads(model_bytes)

    if args.command == "reconcile":
        tasks_path = CHANGES_DIR / args.change / "tasks.md"
        if not tasks_path.is_file():
            print(f"error: tasks not found at {tasks_path}", file=sys.stderr)
            return 2
        result = reconcile(model, parse_tasks(tasks_path.read_text(encoding="utf-8")))
        if result["errors"]:
            print("RECONCILE FAILED: stale task tags; apply nothing:", file=sys.stderr)
            for e in result["errors"]:
                print(f"  - {e}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {"mitigationResolved": result["mitigations"], "threatResolved": result["threats"]},
                indent=2,
            )
        )
        return 0

    if args.command == "gate":
        violations = gate(model)
        if violations:
            print(f"GATE FAILED ({len(violations)} violation(s)):", file=sys.stderr)
            for v in violations:
                print(f"  - {v}", file=sys.stderr)
            return 1
        print("GATE PASSED")
        return 0

    # report
    report_path = CHANGES_DIR / args.change / REPORT_NAME
    expected = render_report(path.name, model_bytes, model)
    if args.check:
        if not report_path.is_file() or report_path.read_text(encoding="utf-8") != expected:
            print(f"REPORT STALE: regenerate {report_path}", file=sys.stderr)
            return 1
        print(f"REPORT CURRENT: {report_path}")
        return 0
    if not report_path.parent.is_dir():
        print(f"error: change directory not found at {report_path.parent}", file=sys.stderr)
        return 2
    report_path.write_text(expected, encoding="utf-8")
    print(f"wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

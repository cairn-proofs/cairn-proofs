"""Threat-model reconcile + archive gate for OpenSpec changes.

A small, dependency-free helper for the two mechanical, fail-closed operations
the `/opsx:verify` and `/opsx:archive` flows perform on a change's
`threat-model.tc.json` (Threat Composer v1):

- **reconcile** — flip shipped mitigations to `mitigationResolved`, then cascade
  a threat to `threatResolved` iff *all* its linked mitigations are resolved AND
  its residual risk is acceptable (not Critical/High). Writes the file back.
- **gate** — the fail-closed archive check: a non-zero exit if any threat carries
  a Critical/High residual, or any not-yet-`threatResolved` threat still depends
  on an unresolved mitigation. This is the same check both opsx flows run by hand.

The logic lives in pure functions (`reconcile`, `gate`) so it is unit-testable
without touching the filesystem; `main()` is the thin CLI wrapper.

Usage:
    python3 openspec/tools/tc.py gate <change>
    python3 openspec/tools/tc.py reconcile <change> --shipped <id,id,...>
    python3 openspec/tools/tc.py reconcile <change> --shipped all
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# A mitigation in either of these built-in states no longer holds back a threat:
# `mitigationResolved` = implemented & verified; `...WillNotAction` = risk accepted.
RESOLVED_MITIGATION_STATES = frozenset({"mitigationResolved", "mitigationResolvedWillNotAction"})
UNACCEPTABLE_RESIDUALS = frozenset({"Critical", "High"})
# The four-level residual rating. A value outside this set (or absent) is a
# malformed model: fail closed rather than silently treat e.g. "medium" as
# acceptable just because it is not the exact string "Critical"/"High".
VALID_RESIDUALS = frozenset({"Critical", "High", "Medium", "Low"})


def _residual(threat: dict[str, Any]) -> str | None:
    """Return the threat's `custom:residualRisk` value, or None if unset."""
    for entry in threat.get("metadata", []) or []:
        if entry.get("key") == "custom:residualRisk":
            return entry.get("value")
    return None


def _threat_to_mitigations(model: dict[str, Any]) -> dict[str, list[str]]:
    """Map each threat id to the ids of the mitigations linked to it."""
    out: dict[str, list[str]] = defaultdict(list)
    for link in model.get("mitigationLinks", []) or []:
        out[link.get("linkedId")].append(link.get("mitigationId"))
    return out


def unknown_shipped_ids(model: dict[str, Any], shipped: set[str]) -> set[str]:
    """Return the `shipped` mitigation ids that do not exist in the model.

    A non-empty result is a fail-closed input error: a typo'd or stale `--shipped`
    id would otherwise silently no-op (the mitigation never advances), leaving the
    gate to fail later with a confusing "contingent on unresolved mitigation".
    """
    known = {m.get("id") for m in model.get("mitigations", []) or []}
    return {mid for mid in shipped if mid not in known}


def reconcile(model: dict[str, Any], shipped: set[str] | None) -> dict[str, list[str]]:
    """Mutate `model` in place: resolve shipped mitigations, cascade threats.

    `shipped` is the set of mitigation ids whose implementing tasks have shipped
    and whose tests pass; pass None to treat every currently-`mitigationIdentified`
    mitigation as shipped. A mitigation is only advanced *from* `mitigationIdentified`
    — states a human deliberately set (e.g. `...WillNotAction`) are left untouched.

    Returns a summary of what changed: {"mitigations": [...], "threats": [...]}.
    """
    advanced_mitigations: list[str] = []
    for mitigation in model.get("mitigations", []) or []:
        mid = mitigation.get("id")
        if mitigation.get("status") != "mitigationIdentified":
            continue
        if shipped is None or mid in shipped:
            mitigation["status"] = "mitigationResolved"
            advanced_mitigations.append(mid)

    resolved = {
        m.get("id")
        for m in model.get("mitigations", []) or []
        if m.get("status") in RESOLVED_MITIGATION_STATES
    }
    links = _threat_to_mitigations(model)

    advanced_threats: list[str] = []
    for threat in model.get("threats", []) or []:
        tid = threat.get("id")
        mitigations = links.get(tid, [])
        if not mitigations:
            continue
        if threat.get("status") == "threatResolved":
            continue
        residual = _residual(threat)
        if residual not in VALID_RESIDUALS or residual in UNACCEPTABLE_RESIDUALS:
            continue  # malformed/missing or unacceptable residual — never auto-resolve
        if all(mid in resolved for mid in mitigations):
            threat["status"] = "threatResolved"
            advanced_threats.append(tid)

    return {"mitigations": advanced_mitigations, "threats": advanced_threats}


def gate(model: dict[str, Any]) -> list[str]:
    """Return a list of fail-closed violations; empty means the model may archive.

    Three conditions block archive (mirroring the `/opsx:archive` guardrails):
      1. Any threat with a Critical/High `custom:residualRisk`.
      2. Any threat whose `custom:residualRisk` is missing or not one of the four
         valid levels — a malformed rating is failed closed, not assumed benign.
      3. Any not-yet-`threatResolved` threat with a linked mitigation that is not
         in a resolved state — its acceptable residual still depends on an
         unverified mitigation.
    """
    violations: list[str] = []
    by_id = {m.get("id"): m for m in model.get("mitigations", []) or []}
    links = _threat_to_mitigations(model)

    for threat in model.get("threats", []) or []:
        tid = threat.get("id")
        residual = _residual(threat)
        if residual in UNACCEPTABLE_RESIDUALS:
            violations.append(f"threat {tid}: {residual} residual risk must not reach archive")
        elif residual not in VALID_RESIDUALS:
            violations.append(
                f"threat {tid}: residualRisk {residual!r} not recognized "
                f"(expected one of Critical/High/Medium/Low)"
            )
        if threat.get("status") == "threatResolved":
            continue
        for mid in links.get(tid, []):
            status = (by_id.get(mid) or {}).get("status")
            if status not in RESOLVED_MITIGATION_STATES:
                violations.append(
                    f"threat {tid}: residual contingent on unresolved mitigation "
                    f"{mid} (status={status})"
                )
    return violations


def _resolve_path(change_or_path: str) -> Path:
    """Accept either a change name or a direct path to the threat model file."""
    candidate = Path(change_or_path)
    if candidate.is_file():
        return candidate
    return Path("openspec/changes") / change_or_path / "threat-model.tc.json"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, model: dict[str, Any]) -> None:
    path.write_text(json.dumps(model, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tc", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("gate", help="fail-closed archive gate (non-zero exit on violations)")
    g.add_argument("change", help="change name or path to threat-model.tc.json")

    r = sub.add_parser("reconcile", help="resolve shipped mitigations and cascade threats")
    r.add_argument("change", help="change name or path to threat-model.tc.json")
    r.add_argument(
        "--shipped",
        required=True,
        help="comma-separated mitigation ids that shipped, or 'all' for every identified one",
    )

    args = parser.parse_args(argv)
    path = _resolve_path(args.change)
    if not path.is_file():
        print(f"error: threat model not found at {path}", file=sys.stderr)
        return 2
    model = _load(path)

    if args.command == "gate":
        violations = gate(model)
        if violations:
            print(f"GATE FAILED ({len(violations)} violation(s)):", file=sys.stderr)
            for v in violations:
                print(f"  - {v}", file=sys.stderr)
            return 1
        print("GATE PASSED: no Critical/High residual, no contingent mitigation.")
        return 0

    # reconcile
    shipped = (
        None
        if args.shipped.strip() == "all"
        else {s.strip() for s in args.shipped.split(",") if s.strip()}
    )
    if shipped is not None:
        unknown = unknown_shipped_ids(model, shipped)
        if unknown:
            print(
                f"error: --shipped names mitigation id(s) absent from {path}: "
                f"{', '.join(sorted(unknown))}",
                file=sys.stderr,
            )
            return 2
    changed = reconcile(model, shipped)
    _save(path, model)
    print(
        f"reconciled {path}: "
        f"{len(changed['mitigations'])} mitigation(s) -> mitigationResolved, "
        f"{len(changed['threats'])} threat(s) -> threatResolved"
    )
    violations = gate(model)
    if violations:
        print(f"WARNING: gate still failing after reconcile ({len(violations)}):", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1
    print("GATE PASSED after reconcile.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

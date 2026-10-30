"""Unit tests for the threat-model reconcile + gate helper (openspec/tools/tc.py).

Pure-function tests — no filesystem, parallel-safe (each builds its own model).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_TC_PATH = Path(__file__).resolve().parents[3] / "openspec" / "tools" / "tc.py"
_spec = importlib.util.spec_from_file_location("openspec_tools_tc", _TC_PATH)
assert _spec and _spec.loader
tc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tc)


def _threat(tid: str, residual: str, status: str = "threatIdentified") -> dict[str, Any]:
    return {
        "id": tid,
        "status": status,
        "metadata": [{"key": "custom:residualRisk", "value": residual}],
    }


def _mitigation(mid: str, status: str = "mitigationIdentified") -> dict[str, Any]:
    return {"id": mid, "status": status}


def _model(threats, mitigations, links) -> dict[str, Any]:
    return {
        "threats": threats,
        "mitigations": mitigations,
        "mitigationLinks": [{"mitigationId": m, "linkedId": t} for m, t in links],
    }


# --- reconcile -------------------------------------------------------------


def test_reconcile_resolves_shipped_and_cascades_threat():
    model = _model(
        threats=[_threat("t1", "Low")],
        mitigations=[_mitigation("m1")],
        links=[("m1", "t1")],
    )
    changed = tc.reconcile(model, {"m1"})
    assert changed == {"mitigations": ["m1"], "threats": ["t1"]}
    assert model["mitigations"][0]["status"] == "mitigationResolved"
    assert model["threats"][0]["status"] == "threatResolved"


def test_reconcile_all_treats_every_identified_as_shipped():
    model = _model(
        threats=[_threat("t1", "Low")],
        mitigations=[_mitigation("m1"), _mitigation("m2")],
        links=[("m1", "t1"), ("m2", "t1")],
    )
    tc.reconcile(model, None)
    assert all(m["status"] == "mitigationResolved" for m in model["mitigations"])
    assert model["threats"][0]["status"] == "threatResolved"


def test_reconcile_does_not_cascade_when_a_linked_mitigation_unshipped():
    model = _model(
        threats=[_threat("t1", "Low")],
        mitigations=[_mitigation("m1"), _mitigation("m2")],
        links=[("m1", "t1"), ("m2", "t1")],
    )
    tc.reconcile(model, {"m1"})  # m2 stays mitigationIdentified
    assert model["mitigations"][0]["status"] == "mitigationResolved"
    assert model["mitigations"][1]["status"] == "mitigationIdentified"
    assert model["threats"][0]["status"] == "threatIdentified"  # not cascaded


def test_reconcile_never_cascades_high_residual():
    model = _model(
        threats=[_threat("t1", "High")],
        mitigations=[_mitigation("m1")],
        links=[("m1", "t1")],
    )
    tc.reconcile(model, {"m1"})
    assert model["threats"][0]["status"] == "threatIdentified"


def test_reconcile_leaves_will_not_action_untouched():
    model = _model(
        threats=[_threat("t1", "Low")],
        mitigations=[_mitigation("m1", "mitigationResolvedWillNotAction")],
        links=[("m1", "t1")],
    )
    changed = tc.reconcile(model, None)
    assert changed["mitigations"] == []  # not advanced from a human-set state
    assert model["mitigations"][0]["status"] == "mitigationResolvedWillNotAction"
    assert model["threats"][0]["status"] == "threatResolved"  # accepted mitigation counts


def test_reconcile_ignores_threat_with_no_linked_mitigations():
    model = _model(threats=[_threat("t1", "Low")], mitigations=[], links=[])
    changed = tc.reconcile(model, None)
    assert changed["threats"] == []
    assert model["threats"][0]["status"] == "threatIdentified"


# --- gate ------------------------------------------------------------------


def test_gate_passes_when_all_resolved_and_acceptable():
    model = _model(
        threats=[_threat("t1", "Low", status="threatResolved")],
        mitigations=[_mitigation("m1", "mitigationResolved")],
        links=[("m1", "t1")],
    )
    assert tc.gate(model) == []


def test_gate_flags_high_residual_even_if_threat_resolved():
    model = _model(
        threats=[_threat("t1", "High", status="threatResolved")],
        mitigations=[_mitigation("m1", "mitigationResolved")],
        links=[("m1", "t1")],
    )
    violations = tc.gate(model)
    assert len(violations) == 1
    assert "High residual" in violations[0]


def test_gate_flags_contingent_unresolved_mitigation():
    model = _model(
        threats=[_threat("t1", "Low")],  # still threatIdentified
        mitigations=[_mitigation("m1", "mitigationIdentified")],
        links=[("m1", "t1")],
    )
    violations = tc.gate(model)
    assert len(violations) == 1
    assert "contingent on unresolved mitigation" in violations[0]


def test_gate_resolved_threat_does_not_require_mitigation_check():
    # A threatResolved with an acceptable residual is clean even if we don't
    # re-examine its mitigations (they were checked at cascade time).
    model = _model(
        threats=[_threat("t1", "Medium", status="threatResolved")],
        mitigations=[_mitigation("m1", "mitigationResolved")],
        links=[("m1", "t1")],
    )
    assert tc.gate(model) == []


def test_reconcile_then_gate_clean_end_to_end():
    model = _model(
        threats=[_threat("t1", "Low"), _threat("t2", "Medium")],
        mitigations=[_mitigation("m1"), _mitigation("m2")],
        links=[("m1", "t1"), ("m2", "t2")],
    )
    tc.reconcile(model, None)
    assert tc.gate(model) == []


# --- fail-closed: malformed residual ---------------------------------------


def test_gate_flags_unrecognized_residual():
    # A typo'd residual (lower-case "medium") must not slip through as benign.
    model = _model(
        threats=[_threat("t1", "medium", status="threatResolved")],
        mitigations=[_mitigation("m1", "mitigationResolved")],
        links=[("m1", "t1")],
    )
    violations = tc.gate(model)
    assert len(violations) == 1
    assert "not recognized" in violations[0]


def test_gate_flags_missing_residual():
    model = {
        "threats": [{"id": "t1", "status": "threatResolved", "metadata": []}],
        "mitigations": [],
        "mitigationLinks": [],
    }
    violations = tc.gate(model)
    assert len(violations) == 1
    assert "not recognized" in violations[0]


def test_reconcile_does_not_cascade_unrecognized_residual():
    model = _model(
        threats=[_threat("t1", "medium")],  # typo — not a valid level
        mitigations=[_mitigation("m1")],
        links=[("m1", "t1")],
    )
    tc.reconcile(model, {"m1"})
    assert model["mitigations"][0]["status"] == "mitigationResolved"
    assert model["threats"][0]["status"] == "threatIdentified"  # never auto-resolved


# --- fail-closed: unknown --shipped ids ------------------------------------


def test_unknown_shipped_ids_flags_absent_ids():
    model = _model(threats=[], mitigations=[_mitigation("m1"), _mitigation("m2")], links=[])
    assert tc.unknown_shipped_ids(model, {"m1", "m2"}) == set()
    assert tc.unknown_shipped_ids(model, {"m1", "m-typo"}) == {"m-typo"}

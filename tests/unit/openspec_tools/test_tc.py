"""Unit tests for the threat-model reconcile, gate and report helper (openspec/tools/tc.py).

Pure-function tests build their own models. CLI tests run in a per-test tmp_path
via monkeypatch.chdir, so they are parallel-safe.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[3]
_TC_PATH = _REPO / "openspec" / "tools" / "tc.py"
_spec = importlib.util.spec_from_file_location("openspec_tools_tc", _TC_PATH)
assert _spec and _spec.loader
tc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tc)


def _threat(tid: str, priority: Any = "Medium", status: str = "threatIdentified") -> dict[str, Any]:
    metadata = [] if priority is None else [{"key": "Priority", "value": priority}]
    return {"id": tid, "status": status, "metadata": metadata}


def _mitigation(
    mid: str, tasks: tuple[str, ...] = (), status: str = "mitigationIdentified"
) -> dict[str, Any]:
    return {"id": mid, "status": status, "tags": ["Preventative", *(f"task:{t}" for t in tasks)]}


def _model(threats, mitigations, links, assumptions=()) -> dict[str, Any]:
    return {
        "threats": threats,
        "mitigations": mitigations,
        "assumptions": list(assumptions),
        "mitigationLinks": [{"mitigationId": m, "linkedId": t} for m, t in links],
    }


# --- parse_tasks -----------------------------------------------------------


def test_parse_tasks_reads_checked_and_unchecked_ids():
    md = "## 1. Setup\n\n- [x] 1.1 Do a thing\n- [ ] 1.2 Do another\n- [X] 2.1 Third\nprose\n"
    assert tc.parse_tasks(md) == {"1.1": True, "1.2": False, "2.1": True}


def test_parse_tasks_ignores_unnumbered_and_malformed_lines():
    md = "- [x] no number\n- [] 1.1 bad box\n* [x] 1.2 wrong bullet\n- [x] 3 single level\n"
    assert tc.parse_tasks(md) == {}


# --- reconcile -------------------------------------------------------------


def test_reconcile_resolves_mitigation_when_all_tagged_tasks_checked_and_cascades():
    model = _model([_threat("t1")], [_mitigation("m1", ("1.1", "1.2"))], [("m1", "t1")])
    result = tc.reconcile(model, {"1.1": True, "1.2": True})
    assert result == {"mitigations": ["m1"], "threats": ["t1"], "errors": []}


def test_reconcile_does_not_mutate_the_model():
    model = _model([_threat("t1")], [_mitigation("m1", ("1.1",))], [("m1", "t1")])
    before = json.dumps(model, sort_keys=True)
    tc.reconcile(model, {"1.1": True})
    assert json.dumps(model, sort_keys=True) == before


def test_reconcile_holds_mitigation_while_a_tagged_task_is_unchecked():
    model = _model([_threat("t1")], [_mitigation("m1", ("1.1", "1.2"))], [("m1", "t1")])
    result = tc.reconcile(model, {"1.1": True, "1.2": False})
    assert result == {"mitigations": [], "threats": [], "errors": []}


def test_reconcile_leaves_untagged_mitigation_open():
    model = _model([_threat("t1")], [_mitigation("m1")], [("m1", "t1")])
    assert tc.reconcile(model, {"1.1": True})["mitigations"] == []


def test_reconcile_fails_closed_on_tag_for_missing_task():
    model = _model([_threat("t1")], [_mitigation("m1", ("9.9",))], [("m1", "t1")])
    result = tc.reconcile(model, {"1.1": True})
    assert result["mitigations"] == []
    assert len(result["errors"]) == 1
    assert "9.9" in result["errors"][0]


def test_reconcile_advances_in_progress_mitigation():
    model = _model(
        [_threat("t1")], [_mitigation("m1", ("1.1",), "mitigationInProgress")], [("m1", "t1")]
    )
    assert tc.reconcile(model, {"1.1": True})["mitigations"] == ["m1"]


def test_reconcile_leaves_will_not_action_untouched_but_counts_it_resolved():
    model = _model(
        [_threat("t1")],
        [_mitigation("m1", ("1.1",), "mitigationResolvedWillNotAction")],
        [("m1", "t1")],
    )
    result = tc.reconcile(model, {"1.1": True})
    assert result["mitigations"] == []
    assert result["threats"] == ["t1"]


def test_reconcile_cascades_only_when_every_linked_mitigation_resolves():
    model = _model(
        [_threat("t1")],
        [_mitigation("m1", ("1.1",)), _mitigation("m2", ("1.2",))],
        [("m1", "t1"), ("m2", "t1")],
    )
    result = tc.reconcile(model, {"1.1": True, "1.2": False})
    assert result["mitigations"] == ["m1"]
    assert result["threats"] == []


def test_reconcile_cascades_high_priority_threat_once_mitigated():
    model = _model([_threat("t1", "High")], [_mitigation("m1", ("1.1",))], [("m1", "t1")])
    assert tc.reconcile(model, {"1.1": True})["threats"] == ["t1"]


def test_reconcile_skips_dismissed_and_unlinked_threats():
    model = _model(
        [_threat("t1", status="threatResolvedNotUseful"), _threat("t2")],
        [_mitigation("m1", ("1.1",))],
        [("m1", "t1")],
    )
    assert tc.reconcile(model, {"1.1": True})["threats"] == []


# --- gate ------------------------------------------------------------------


def test_gate_passes_when_all_threats_resolved():
    model = _model(
        [_threat("t1", "High", "threatResolved")],
        [_mitigation("m1", status="mitigationResolved")],
        [("m1", "t1")],
    )
    assert tc.gate(model) == []


def test_gate_passes_low_or_medium_threat_without_mitigation():
    model = _model([_threat("t1", "Low"), _threat("t2", "Medium")], [], [])
    assert tc.gate(model) == []


def test_gate_flags_high_threat_without_mitigation():
    violations = tc.gate(_model([_threat("t1", "High")], [], []))
    assert len(violations) == 1
    assert "no mitigation" in violations[0]


def test_gate_flags_open_threat_depending_on_unresolved_mitigation():
    model = _model([_threat("t1")], [_mitigation("m1")], [("m1", "t1")])
    violations = tc.gate(model)
    assert len(violations) == 1
    assert "unresolved mitigation m1" in violations[0]


def test_gate_flags_dangling_mitigation_link():
    model = _model([_threat("t1")], [], [("m-missing", "t1")])
    violations = tc.gate(model)
    assert len(violations) == 1
    assert "status=None" in violations[0]


@pytest.mark.parametrize("priority", [None, "high", "Critical", 3])
def test_gate_fails_closed_on_missing_or_unrecognized_priority(priority):
    violations = tc.gate(_model([_threat("t1", priority, "threatResolved")], [], []))
    assert len(violations) == 1
    assert "not recognized" in violations[0]


def test_gate_skips_dismissed_threat():
    model = _model([_threat("t1", None, "threatResolvedNotUseful")], [_mitigation("m1")], [])
    assert tc.gate(model) == []


_DEFERRED = "deferred:tm-private#12"


def _deferred(mid: str, *tags: str) -> dict[str, Any]:
    return {"id": mid, "status": "mitigationIdentified", "tags": ["Preventative", *tags]}


def test_gate_treats_deferred_mitigation_as_not_blocking():
    model = _model([_threat("t1", "High")], [_deferred("m1", _DEFERRED)], [("m1", "t1")])
    assert tc.gate(model) == []


def test_gate_still_blocks_on_unbuilt_mitigation_beside_a_deferred_one():
    model = _model(
        [_threat("t1")],
        [_deferred("m1", _DEFERRED), _mitigation("m2", ("1.1",))],
        [("m1", "t1"), ("m2", "t1")],
    )
    violations = tc.gate(model)
    assert len(violations) == 1
    assert "unresolved mitigation m2" in violations[0]


@pytest.mark.parametrize(
    "tag",
    [
        "deferred:#12",
        "deferred:cairn-proofs/cairn-proofs#12",
        "deferred:tm-private#0",
        "deferred:tm-private#12x",
        "deferred:",
    ],
)
def test_gate_fails_closed_on_malformed_deferred_tag(tag):
    model = _model([_threat("t1", status="threatResolved")], [_deferred("m1", tag)], [])
    violations = tc.gate(model)
    assert len(violations) == 1
    assert "malformed deferred tag" in violations[0]


def test_gate_malformed_deferred_tag_does_not_count_as_deferred():
    model = _model([_threat("t1")], [_deferred("m1", "deferred:#12")], [("m1", "t1")])
    violations = tc.gate(model)
    assert any("malformed" in v for v in violations)
    assert any("unresolved mitigation m1" in v for v in violations)


def test_gate_rejects_mitigation_both_tasked_and_deferred():
    model = _model(
        [_threat("t1", status="threatResolved")],
        [_deferred("m1", _DEFERRED, "task:1.1")],
        [],
    )
    violations = tc.gate(model)
    assert len(violations) == 1
    assert "both task: and deferred:" in violations[0]


def test_reconcile_never_resolves_deferred_mitigation():
    model = _model([_threat("t1")], [_deferred("m1", _DEFERRED)], [("m1", "t1")])
    assert tc.reconcile(model, {"1.1": True}) == {"mitigations": [], "threats": [], "errors": []}


def test_gate_propose_stage_ignores_unbuilt_mitigations():
    model = _model([_threat("t1", "High")], [_mitigation("m1", ("1.1",))], [("m1", "t1")])
    assert tc.gate(model, "propose") == []
    assert len(tc.gate(model, "apply")) == 1


def test_gate_propose_stage_still_flags_unmitigated_high_and_bad_priority():
    model = _model([_threat("t1", "High"), _threat("t2", "high")], [], [])
    violations = tc.gate(model, "propose")
    assert len(violations) == 2


def test_gate_propose_stage_still_flags_malformed_deferred_tag():
    model = _model([], [_deferred("m1", "deferred:#1")], [])
    assert len(tc.gate(model, "propose")) == 1


def test_gate_rejects_unknown_stage():
    with pytest.raises(ValueError):
        tc.gate(_model([], [], []), "archive")


def test_render_report_marks_propose_stage_and_passes_shape():
    model = _model([_threat("t1")], [_mitigation("m1", ("1.1",))], [("m1", "t1")])
    report = tc.render_report("my-change.tc.json", b"{}", model, "propose")
    assert "| Gate | PASSED (propose) |" in report
    assert tc.check_report_shape(report) == []
    assert "| Gate | FAILED |" in tc.render_report("my-change.tc.json", b"{}", model)


def test_reconcile_then_gate_clean_end_to_end():
    model = _model(
        [_threat("t1", "High"), _threat("t2", "Low")],
        [_mitigation("m1", ("1.1",)), _mitigation("m2", ("1.2",))],
        [("m1", "t1"), ("m2", "t2")],
    )
    result = tc.reconcile(model, {"1.1": True, "1.2": True})
    for m in model["mitigations"]:
        if m["id"] in result["mitigations"]:
            m["status"] = "mitigationResolved"
    for t in model["threats"]:
        if t["id"] in result["threats"]:
            t["status"] = "threatResolved"
    assert tc.gate(model) == []


# --- report ----------------------------------------------------------------


def test_render_report_is_sanitized_and_matches_shape():
    model = _model(
        [{**_threat("t1", "High"), "statement": "SECRET threat text"}],
        [{**_mitigation("m1"), "content": "SECRET control text"}],
        [("m1", "t1")],
        assumptions=[{"id": "a1", "content": "SECRET assumption"}],
    )
    report = tc.render_report("my-change.tc.json", b"{}", model)
    assert "SECRET" not in report
    assert "t1" not in report and "m1" not in report
    assert "| Gate | FAILED |" in report
    assert "| Threats | 1 |" in report
    assert "| Assumptions | 1 |" in report
    assert tc.check_report_shape(report) == []


def test_render_report_gate_passes_for_clean_model():
    report = tc.render_report("my-change.tc.json", b"{}", _model([], [], []))
    assert "| Gate | PASSED |" in report


def test_check_report_shape_rejects_added_line():
    report = tc.render_report("my-change.tc.json", b"{}", _model([], [], []))
    problems = tc.check_report_shape(report + "| Threat | SECRET |\n")
    assert problems


def test_check_report_shape_rejects_edited_value():
    report = tc.render_report("my-change.tc.json", b"{}", _model([], [], []))
    tampered = report.replace("| Gate | PASSED |", "| Gate | PASSED, see T3 |")
    assert tc.check_report_shape(tampered) == ["line 9: unexpected content"]


def test_report_template_matches_shape_check():
    template = (_REPO / "openspec/schemas/cairn/templates/threat-model-report.md").read_text(
        encoding="utf-8"
    )
    filled = tc.render_report("example-change.tc.json", b"{}", _model([], [], []))
    assert template.splitlines()[0] == filled.splitlines()[0]
    assert len(template.splitlines()) == len(filled.splitlines())


def test_committed_reports_contain_only_sanitized_fields():
    reports = sorted((_REPO / "openspec" / "changes").glob(f"**/{tc.REPORT_NAME}"))
    problems = {
        str(p.relative_to(_REPO)): tc.check_report_shape(p.read_text(encoding="utf-8"))
        for p in reports
    }
    assert {k: v for k, v in problems.items() if v} == {}


# --- CLI -------------------------------------------------------------------


def _write_change(root: Path, change: str, model: dict[str, Any], tasks_md: str | None) -> Path:
    (root / "threat-models").mkdir(exist_ok=True)
    model_path = root / "threat-models" / f"{change}.tc.json"
    model_path.write_text(json.dumps(model), encoding="utf-8")
    change_dir = root / "openspec" / "changes" / change
    change_dir.mkdir(parents=True)
    if tasks_md is not None:
        (change_dir / "tasks.md").write_text(tasks_md, encoding="utf-8")
    return model_path


def test_cli_reconcile_prints_changes_and_does_not_write_model(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    model = _model([_threat("t1")], [_mitigation("m1", ("1.1",))], [("m1", "t1")])
    model_path = _write_change(tmp_path, "c", model, "- [x] 1.1 Build the control\n")
    before = model_path.read_bytes()
    assert tc.main(["reconcile", "c"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "mitigationResolved": ["m1"],
        "threatResolved": ["t1"],
    }
    assert model_path.read_bytes() == before


def test_cli_reconcile_exits_2_on_stale_task_tag(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    model = _model([_threat("t1")], [_mitigation("m1", ("4.2",))], [("m1", "t1")])
    _write_change(tmp_path, "c", model, "- [x] 1.1 Build the control\n")
    assert tc.main(["reconcile", "c"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "4.2" in captured.err


def test_cli_reconcile_exits_2_without_tasks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_change(tmp_path, "c", _model([], [], []), None)
    assert tc.main(["reconcile", "c"]) == 2


def test_cli_gate_exit_codes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_change(tmp_path, "ok", _model([_threat("t1", "Low")], [], []), None)
    _write_change(tmp_path, "bad", _model([_threat("t1", "High")], [], []), None)
    assert tc.main(["gate", "ok"]) == 0
    assert tc.main(["gate", "bad"]) == 1


def test_cli_report_writes_then_check_detects_staleness(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    model_path = _write_change(tmp_path, "c", _model([_threat("t1", "Low")], [], []), None)
    assert tc.main(["report", "c", "--check"]) == 1  # no report yet
    assert tc.main(["report", "c"]) == 0
    report = (tmp_path / "openspec/changes/c" / tc.REPORT_NAME).read_text(encoding="utf-8")
    assert tc.check_report_shape(report) == []
    assert tc.main(["report", "c", "--check"]) == 0
    model_path.write_text(json.dumps(_model([], [], [])), encoding="utf-8")
    assert tc.main(["report", "c", "--check"]) == 1  # model changed, report stale


def test_cli_gate_and_report_honor_stage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    model = _model([_threat("t1")], [_mitigation("m1", ("1.1",))], [("m1", "t1")])
    _write_change(tmp_path, "c", model, None)
    assert tc.main(["gate", "c", "--stage", "propose"]) == 0
    assert tc.main(["gate", "c"]) == 1
    assert tc.main(["report", "c", "--stage", "propose"]) == 0
    report = (tmp_path / "openspec/changes/c" / tc.REPORT_NAME).read_text(encoding="utf-8")
    assert "| Gate | PASSED (propose) |" in report
    assert tc.main(["report", "c", "--stage", "propose", "--check"]) == 0
    assert tc.main(["report", "c", "--check"]) == 1  # apply-stage rendering differs


def test_cli_report_exits_2_without_change_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "threat-models").mkdir()
    (tmp_path / "threat-models" / "c.tc.json").write_text("{}", encoding="utf-8")
    assert tc.main(["report", "c"]) == 2


def test_cli_exits_2_when_model_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert tc.main(["gate", "absent"]) == 2


@pytest.mark.parametrize("change", ["../escape", "Upper", "a/b", ""])
def test_cli_rejects_invalid_change_name(tmp_path, monkeypatch, change):
    monkeypatch.chdir(tmp_path)
    assert tc.main(["gate", change]) == 2

"""Unit tests for the threat-model Mermaid->PNG render helper (openspec/tools/tc_render.py).

Pure-function tests — the `mmdc` subprocess is always monkeypatched out, so the
suite needs no Chromium and stays parallel-safe (each test builds its own model
and patches its own seams).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_TCR_PATH = Path(__file__).resolve().parents[3] / "openspec" / "tools" / "tc_render.py"
_spec = importlib.util.spec_from_file_location("openspec_tools_tc_render", _TCR_PATH)
assert _spec and _spec.loader
tcr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tcr)


# --- extract_mermaid_blocks ------------------------------------------------


def test_extract_none() -> None:
    assert tcr.extract_mermaid_blocks("no fenced blocks here") == []
    assert tcr.extract_mermaid_blocks("") == []
    assert tcr.extract_mermaid_blocks(None) == []  # type: ignore[arg-type]


def test_extract_single_block_is_trimmed() -> None:
    md = "intro\n\n```mermaid\nflowchart LR\n  A-->B\n```\n\noutro"
    assert tcr.extract_mermaid_blocks(md) == ["flowchart LR\n  A-->B"]


def test_extract_multiple_blocks_in_order() -> None:
    md = "```mermaid\nsequenceDiagram\n  A->>B: x\n```\n```mermaid\nflowchart LR\n  C-->D\n```\n"
    blocks = tcr.extract_mermaid_blocks(md)
    assert blocks == ["sequenceDiagram\n  A->>B: x", "flowchart LR\n  C-->D"]


def test_extract_ignores_non_mermaid_fences() -> None:
    md = "```python\nprint(1)\n```\n\n```mermaid\nflowchart LR\n  A-->B\n```\n"
    assert tcr.extract_mermaid_blocks(md) == ["flowchart LR\n  A-->B"]


# --- to_data_uri -----------------------------------------------------------


def test_to_data_uri_prefix_and_roundtrip() -> None:
    import base64

    payload = b"\x89PNG\r\n\x1a\nfake-bytes"
    uri = tcr.to_data_uri(payload)
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == payload


# --- render_capped_data_uri (cap enforcement) ------------------------------


def test_render_capped_skips_over_cap_image(monkeypatch: pytest.MonkeyPatch) -> None:
    # A PNG so large its base64 always exceeds the cap, at every scale.
    huge = b"\x00" * (tcr.MAX_BASE64_LEN)
    monkeypatch.setattr(tcr, "render_mermaid_to_png", lambda src, scale=1.0: huge)
    assert tcr.render_capped_data_uri("flowchart LR\n A-->B") is None


def test_render_capped_returns_uri_when_small(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tcr, "render_mermaid_to_png", lambda src, scale=1.0: b"tiny-png")
    uri = tcr.render_capped_data_uri("flowchart LR\n A-->B")
    assert uri is not None and uri.startswith("data:image/png;base64,")


def test_render_capped_returns_none_when_render_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tcr, "render_mermaid_to_png", lambda src, scale=1.0: None)
    assert tcr.render_capped_data_uri("flowchart LR\n A-->B") is None


def test_render_capped_walks_ladder_high_to_low_until_under_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Higher scale -> bigger PNG. The top rungs blow the cap; the first rung that
    # fits should win, and the ladder must be tried highest-DPI first.
    assert tcr.RENDER_SCALE_LADDER[0] > tcr.RENDER_SCALE_LADDER[-1]
    tried: list[float] = []

    def fake_render(src: str, scale: float = 1.0) -> bytes:
        tried.append(scale)
        # Make only scales <= 2.0 fit under the cap.
        over = scale > 2.0
        return b"\x00" * (tcr.MAX_BASE64_LEN if over else 16)

    monkeypatch.setattr(tcr, "render_mermaid_to_png", fake_render)
    uri = tcr.render_capped_data_uri("flowchart LR\n A-->B")

    assert uri is not None and uri.startswith("data:image/png;base64,")
    assert tried == sorted(tried, reverse=True)  # high -> low
    assert tried[0] == tcr.RENDER_SCALE_LADDER[0]  # tried the crispest first
    assert tried[-1] == 2.0  # stopped at the first rung that fit


# --- strip_mermaid_fences --------------------------------------------------


def test_strip_mermaid_fences_noop_without_fence() -> None:
    md = "### Narrative\n\nplain prose, no diagram\n"
    assert tcr.strip_mermaid_fences(md) is md  # unchanged object, fast path
    assert tcr.strip_mermaid_fences("") == ""


def test_strip_mermaid_fences_removes_block_keeps_prose() -> None:
    md = (
        "### Data flow narrative\n\n"
        "intro prose\n\n"
        "```mermaid\nflowchart LR\n  A-->B\n```\n\n"
        "trailing reading paragraph\n"
    )
    out = tcr.strip_mermaid_fences(md)
    assert "```mermaid" not in out
    assert "flowchart LR" not in out
    assert "### Data flow narrative" in out
    assert "intro prose" in out
    assert "trailing reading paragraph" in out
    assert "\n\n\n" not in out  # leftover blank lines collapsed


# --- embed_images (slot mapping) -------------------------------------------


def _model_with_dfd() -> dict:
    return {
        "schema": 1,
        "dataflow": {"description": "flow notes\n\n```mermaid\nflowchart LR\n  T-->G\n```\n"},
    }


def test_embed_populates_both_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tcr, "render_mermaid_to_png", lambda src, scale=1.0: b"png:" + src.encode())
    model = _model_with_dfd()
    design = "```mermaid\nsequenceDiagram\n  P->>Q: push\n```\n"

    populated = tcr.embed_images(model, design)

    assert populated == {"dataflow": True, "architecture": True}
    assert model["dataflow"]["image"].startswith("data:image/png;base64,")
    assert model["architecture"]["image"].startswith("data:image/png;base64,")
    # The rendered DFD's mermaid source is stripped from the narrative, prose kept.
    assert "```mermaid" not in model["dataflow"]["description"]
    assert "flow notes" in model["dataflow"]["description"]


def test_embed_keeps_mermaid_source_when_render_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fail-open: no image embedded -> the mermaid source must remain as the
    # reviewer-facing diagram.
    monkeypatch.setattr(tcr, "render_mermaid_to_png", lambda src, scale=1.0: None)
    model = _model_with_dfd()

    populated = tcr.embed_images(model, "```mermaid\nsequenceDiagram\n  P->>Q: x\n```\n")

    assert populated == {"dataflow": False, "architecture": False}
    assert "```mermaid" in model["dataflow"]["description"]


def test_embed_skips_design_block_identical_to_dfd(monkeypatch: pytest.MonkeyPatch) -> None:
    # design.md carries only a copy of the DFD (the agent duplicates it there);
    # architecture.image must stay empty rather than re-embed the DFD.
    monkeypatch.setattr(tcr, "render_mermaid_to_png", lambda src, scale=1.0: b"png")
    model = _model_with_dfd()
    design = "```mermaid\nflowchart LR\n  T-->G\n```\n"  # same source as the DFD

    populated = tcr.embed_images(model, design)

    assert populated == {"dataflow": True, "architecture": False}
    assert "image" not in model.get("architecture", {})


def test_embed_noop_when_mmdc_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real render path, but mmdc reported absent -> render returns None, no slots.
    monkeypatch.setattr(tcr, "mmdc_available", lambda: False)
    model = _model_with_dfd()
    design = "```mermaid\nsequenceDiagram\n  P->>Q: push\n```\n"

    populated = tcr.embed_images(model, design)

    assert populated == {"dataflow": False, "architecture": False}
    assert "image" not in model["dataflow"]
    assert "architecture" not in model


def test_embed_no_dfd_no_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tcr, "render_mermaid_to_png", lambda src, scale=1.0: b"png")
    model = {"schema": 1, "dataflow": {"description": "prose only, no diagram"}}
    assert tcr.embed_images(model, "also prose only") == {"dataflow": False, "architecture": False}


# --- main (CLI) ------------------------------------------------------------


def test_main_writes_when_rendered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tc_path = tmp_path / "threat-model.tc.json"
    tc_path.write_text(json.dumps(_model_with_dfd()), encoding="utf-8")
    (tmp_path / "design.md").write_text(
        "```mermaid\nsequenceDiagram\n  P->>Q: push\n```\n", encoding="utf-8"
    )
    monkeypatch.setattr(tcr, "mmdc_available", lambda: True)
    monkeypatch.setattr(tcr, "render_mermaid_to_png", lambda src, scale=1.0: b"png")

    assert tcr.main([str(tc_path)]) == 0
    written = json.loads(tc_path.read_text(encoding="utf-8"))
    assert written["dataflow"]["image"].startswith("data:image/png;base64,")
    assert written["architecture"]["image"].startswith("data:image/png;base64,")


def test_main_exits_zero_and_no_write_when_mmdc_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tc_path = tmp_path / "threat-model.tc.json"
    original = json.dumps(_model_with_dfd())
    tc_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(tcr, "mmdc_available", lambda: False)

    assert tcr.main([str(tc_path)]) == 0
    assert tc_path.read_text(encoding="utf-8") == original  # untouched


def test_main_missing_file_returns_2(tmp_path: Path) -> None:
    assert tcr.main([str(tmp_path / "nope" / "threat-model.tc.json")]) == 2

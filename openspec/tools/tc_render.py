"""Render a change's threat-model Mermaid diagrams to embedded PNG data URIs.

A best-effort companion to `tc.py`. The `threat-model` agent authors its
trust-boundary Data Flow Diagram as a ```mermaid block inside
`dataflow.description`, and the change's `design.md` typically carries an
architecture diagram. Threat Composer's UI cannot render Mermaid source, so this
helper pre-renders those blocks to PNG (via `mmdc`, the mermaid-cli) and embeds
them as base64 data URIs in the `dataflow.image` / `architecture.image` slots of
`threat-model.tc.json` so the model renders in the Threat Composer UI.

Slot mapping (single image per slot in the Threat Composer schema):
  - first ```mermaid block in `dataflow.description`        -> dataflow.image
  - first ```mermaid block in `design.md` that isn't the DFD -> architecture.image

Rendering is **fail-open**: if `mmdc` is unavailable, a render fails, or the
result exceeds the Threat Composer base64 cap, the affected slot is left empty
and the command still exits 0 — the Mermaid source in `dataflow.description` +
`design.md` remains the reviewer-facing diagram, and the agent's closing
`validate_tc_schema` gate still passes. Images are an enhancement, never a
hard requirement.

The logic lives in pure functions (`extract_mermaid_blocks`, `to_data_uri`,
`embed_images`) so it is unit-testable without a browser; `main()` is the thin
CLI wrapper. Stdlib-only, matching `tc.py`.

Usage:
    python3 openspec/tools/tc_render.py <change>   # change name or path to .tc.json
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# Threat Composer v1 caps a base64 image at 1,000,000 chars (~750KB). An
# over-cap data URI would fail schema validation, so we skip the slot instead of
# embedding a string the closing `validate_tc_schema` gate would reject.
MAX_BASE64_LEN = 1_000_000

# mmdc renders at deviceScaleFactor 1 by default (~700px wide), which is too low
# to read embedded in Threat Composer. Render at a high DPI factor for crisp text
# and step DOWN only if a large diagram would blow the base64 cap. Typical DFDs
# are tens of KB even at 4x, so the high rung almost always wins; the lower rungs
# are the safety ladder for unusually large diagrams.
RENDER_SCALE_LADDER = (4.0, 3.0, 2.0, 1.0)

# Fenced ```mermaid block; group 1 is the diagram source (fence info-string may
# carry trailing whitespace).
_MERMAID_BLOCK = re.compile(r"```mermaid[ \t]*\n(.*?)\n```", re.DOTALL)

# Puppeteer config shipped alongside this tool (`--no-sandbox` for restricted /
# headless contexts). Optional — mmdc runs with its defaults if it is absent.
_PUPPETEER_CONFIG = Path(__file__).resolve().parent / "mmdc-puppeteer.json"


def extract_mermaid_blocks(markdown: str) -> list[str]:
    """Return the source of every fenced ```mermaid block, in document order."""
    return [m.group(1).strip() for m in _MERMAID_BLOCK.finditer(markdown or "")]


def to_data_uri(png: bytes) -> str:
    """Encode PNG bytes as a `data:image/png;base64,...` URI."""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def mmdc_available() -> bool:
    """True iff the `mmdc` (mermaid-cli) binary is on PATH."""
    return shutil.which("mmdc") is not None


def render_mermaid_to_png(src: str, scale: float = 1.0) -> bytes | None:
    """Render Mermaid source to PNG bytes via `mmdc`; None on any failure.

    Writes to uniquely-named tempfiles (parallel-safe, no flock needed) and runs
    mmdc with a white background (PNG is otherwise transparent) and the project
    Puppeteer config. Returns None — never raises — if mmdc is absent or exits
    non-zero, so callers can degrade gracefully.
    """
    if not mmdc_available():
        return None
    with tempfile.TemporaryDirectory(prefix="cairn-mmdc-") as tmp:
        in_path = Path(tmp) / "diagram.mmd"
        out_path = Path(tmp) / "diagram.png"
        in_path.write_text(src, encoding="utf-8")
        cmd = [
            "mmdc",
            "-i",
            str(in_path),
            "-o",
            str(out_path),
            "-b",
            "white",
            "-s",
            str(scale),
            "-q",
        ]
        if _PUPPETEER_CONFIG.is_file():
            cmd += ["-p", str(_PUPPETEER_CONFIG)]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"warning: mmdc render failed: {exc}", file=sys.stderr)
            stderr = getattr(exc, "stderr", b"") or b""
            if b"Could not find Chrome" in stderr:
                print(
                    "hint: mmdc is installed but its headless browser is missing; "
                    "run `mise run mmdc-setup` once to provision it.",
                    file=sys.stderr,
                )
            return None
        if not out_path.is_file():
            print("warning: mmdc produced no output file", file=sys.stderr)
            return None
        return out_path.read_bytes()


def render_capped_data_uri(src: str) -> str | None:
    """Render `src` to a PNG data URI under the Threat Composer cap, or None.

    Walks `RENDER_SCALE_LADDER` from the highest DPI down, returning the first
    render that fits the base64 cap — so the result is as crisp as the cap allows.
    A diagram that exceeds the cap even at the lowest scale is skipped (slot left
    empty) rather than embedded and later rejected by schema validation.
    """
    for scale in RENDER_SCALE_LADDER:
        png = render_mermaid_to_png(src, scale=scale)
        if png is None:
            return None
        uri = to_data_uri(png)
        if len(uri) <= MAX_BASE64_LEN:
            return uri
        print(
            f"warning: rendered PNG over cap at scale={scale} "
            f"({len(uri)} > {MAX_BASE64_LEN} chars), retrying at a lower scale",
            file=sys.stderr,
        )
    print(
        "warning: diagram still exceeds Threat Composer image cap; leaving slot empty",
        file=sys.stderr,
    )
    return None


def strip_mermaid_fences(markdown: str) -> str:
    """Remove fenced ```mermaid blocks from `markdown`, tidying leftover blanks.

    Once a section's diagram is embedded as a rendered PNG (`*.image`), the raw
    mermaid source in its `description` is dead weight: Threat Composer renders
    the description as markdown and does NOT render mermaid, so a reader just sees
    a wall of fenced code beside the image. We drop it; the editable source lives
    on in the change's `design.md`. No-op when there is no mermaid fence.
    """
    if not markdown or "```mermaid" not in markdown:
        return markdown
    stripped = _MERMAID_BLOCK.sub("", markdown)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip() + "\n"


def embed_images(model: dict[str, Any], design_md: str) -> dict[str, bool]:
    """Populate `dataflow.image` and `architecture.image` in place.

    `dataflow.image` <- first ```mermaid block in `dataflow.description` (the DFD).
    `architecture.image` <- first ```mermaid block in `design_md` that is not the
    DFD (the agent duplicates the DFD into design.md, so we skip that copy).

    When a slot's image is embedded, the now-redundant mermaid source is stripped
    from that section's `description` so the Threat Composer narrative reads clean
    (the source remains in `design.md`). A slot is only set — and its description
    only stripped — on a successful, under-cap render. Returns
    {"dataflow": bool, "architecture": bool} flagging which slots were populated.
    """
    populated = {"dataflow": False, "architecture": False}

    dfd_blocks = extract_mermaid_blocks((model.get("dataflow") or {}).get("description", ""))
    dfd_src = dfd_blocks[0] if dfd_blocks else None
    if dfd_src:
        uri = render_capped_data_uri(dfd_src)
        if uri:
            dataflow = model.setdefault("dataflow", {})
            dataflow["image"] = uri
            dataflow["description"] = strip_mermaid_fences(dataflow.get("description", ""))
            populated["dataflow"] = True

    arch_src = next((b for b in extract_mermaid_blocks(design_md) if b != dfd_src), None)
    if arch_src:
        uri = render_capped_data_uri(arch_src)
        if uri:
            architecture = model.setdefault("architecture", {})
            architecture["image"] = uri
            architecture["description"] = strip_mermaid_fences(architecture.get("description", ""))
            populated["architecture"] = True

    return populated


def _resolve_paths(change_or_path: str) -> tuple[Path, Path]:
    """Return (threat-model.tc.json, design.md) for a change name or direct path."""
    candidate = Path(change_or_path)
    if candidate.is_file():
        return candidate, candidate.parent / "design.md"
    change_dir = Path("openspec/changes") / change_or_path
    return change_dir / "threat-model.tc.json", change_dir / "design.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tc_render", description=__doc__)
    parser.add_argument("change", help="change name or path to threat-model.tc.json")
    args = parser.parse_args(argv)

    tc_path, design_path = _resolve_paths(args.change)
    if not tc_path.is_file():
        print(f"error: threat model not found at {tc_path}", file=sys.stderr)
        return 2

    if not mmdc_available():
        print(
            "warning: mmdc (mermaid-cli) not on PATH; leaving image slots empty. "
            "Run `mise install` then `mise run mmdc-setup` to provision it.",
            file=sys.stderr,
        )
        return 0

    model = json.loads(tc_path.read_text(encoding="utf-8"))
    design_md = design_path.read_text(encoding="utf-8") if design_path.is_file() else ""

    populated = embed_images(model, design_md)
    if populated["dataflow"] or populated["architecture"]:
        tc_path.write_text(json.dumps(model, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"rendered {tc_path}: "
        f"dataflow.image={'set' if populated['dataflow'] else 'empty'}, "
        f"architecture.image={'set' if populated['architecture'] else 'empty'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

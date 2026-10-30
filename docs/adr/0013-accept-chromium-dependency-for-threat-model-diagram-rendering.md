# ADR-0013: Accept a Chromium/Puppeteer dependency for threat-model diagram rendering

## Status

Accepted (2026-06-06)

## Context

The `threat-model` agent (`.claude/agents/threat-model.md`) authors a `threat-model.tc.json` (Threat Composer v1) per OpenSpec change. The model includes a trust-boundary **Data Flow Diagram**, written as a ```mermaid block in `dataflow.description` and mirrored into the change's `design.md`. The Threat Composer schema has two image slots: `architecture.image` and `dataflow.image`. Each slot accepts a base64 data URI (`image/*`, capped at 1,000,000 base64 chars ≈ 750KB).

The Threat Composer **UI cannot render Mermaid source**. It renders only the image slots. We investigated native Mermaid support in the Threat Composer build and judged it non-trivial. So until now the agent left both image slots empty. A reviewer who opened a model in the Threat Composer UI saw only prose. The diagram was visible only on GitHub (which renders the ```mermaid block in `design.md`) or in the raw source. We want the diagram to render in Threat Composer itself.

The only way to close that gap is to **pre-render the Mermaid to a raster/vector image** and embed it in the image slot. That forces a sourcing decision for the renderer, and the options are constrained:

1. **Faithful Mermaid rendering requires the Mermaid JS engine running against a DOM.** No faithful browser-free Mermaid renderer exists. The canonical tool, `mmdc` (`@mermaid-js/mermaid-cli`), drives Puppeteer + headless Chromium.
2. **SaaS renderers are disqualified on confidentiality grounds.** Kroki, mermaid.ink, and similar HTTP services would receive the DFD. The DFD *is* the system's trust-boundary map for a security evidence store. To send it to a third party is unacceptable for this artifact class.
3. **The project toolchain doctrine** (AGENTS.md, ADR-0011) is that `mise install` is the whole setup story, and that **no asset on the routine `deploy.sh` build path may require a container daemon/VM**. A headless-browser dependency comes close to conflict with the spirit of "lightweight, browser-free toolchain."

The reconciling observation: threat-model rendering is **dev-time, propose-flow only**. It runs inside the `threat-model` agent during `/opsx:propose` (and via `mise run tm-render`). It never runs in `deploy.sh`, in `mise run check`, or in the `tc.py` archive gate. It is also a pure enhancement, because the Mermaid source remains the canonical, reviewer-readable diagram. So it can fail open.

## Decision

Add `mmdc` (`npm:@mermaid-js/mermaid-cli`) to `mise.toml` as a pinned toolchain entry. The mise npm backend does **not** run Puppeteer's postinstall browser download. Also, mmdc's headless mode renders via the separate `chrome-headless-shell` browser. So an explicit, idempotent **`mise run mmdc-setup`** task provisions the browser (it wraps `openspec/tools/provision-mmdc-browser.sh`). `mise install` does not auto-download it.

A new stdlib-only helper, `openspec/tools/tc_render.py`, does three things:

1. It extracts the Mermaid blocks.
2. It renders them to PNG via `mmdc`.
3. It embeds them as base64 data URIs in `dataflow.image` (the DFD) and `architecture.image` (the first non-DFD diagram from `design.md`).

The `threat-model` agent calls it after it writes its artifacts and before the closing `validate_tc_schema` gate.

**Why an explicit task, not a `mise install` auto-download or a `postinstall` hook:** rendering is dev-time only. A `postinstall` hook (or a browser bundled into `mise install`) would force every `mise install` to pull ~160MB of browser that it will never use. That includes CI and any deploy-prep environment. An opt-in task for the browser keeps CI and `deploy.sh` lean. It also leaves `mise install` as the whole story for the *binary* toolchain. Threat-model authors run the one extra task once.

The render step is **fail-open**. Assume that `mmdc` is absent, a render fails, or a diagram exceeds the Threat Composer image cap. Then the affected slot stays empty, the command exits 0, and the Mermaid source remains the reviewer-facing diagram.

This is a toolchain/technology decision that changes **no** Requirement. The Threat Composer image fields are optional and below the Requirement layer, and the threat-model artifact contract does not change. Therefore this ADR records it, rather than an opsx change, per the AGENTS.md ADR-vs-opsx boundary.

### Why this does not violate ADR-0011

ADR-0011's load-bearing rule is that **routine deploys must be runnable with PATH tools alone — no mandatory container daemon/VM on the `deploy.sh` path.** This decision honors that:

- Chromium is a **browser binary that mise fetches**, not a container runtime (Docker/Finch). No daemon runs.
- It is **off the deploy path entirely**. `deploy.sh`, `mise run check`, and the archive gate never call it.
- It stays **mise-managed**: `mmdc` via `mise install` and the browser via `mise run mmdc-setup`, not a per-machine `brew install`/`curl | sh`.
- It **degrades gracefully**. A contributor without it (or in an environment where Chromium can't launch) loses only the embedded PNG, not any gate.

## Consequences

- `mise install` now also provisions the `mmdc` binary. Threat-model authors run `mise run mmdc-setup` once to fetch `chrome-headless-shell` (~160MB one-time, idempotent). `DEVELOPING.md` "Local development" documents this. CI and deploy environments skip the task and pay nothing.
- New threat models rendered with `mmdc` present carry embedded PNGs and render in the Threat Composer UI. Models authored without it do not change and still validate. Their image fields stay empty.
- **No retroactive change** to the ~30 archived `threat-model.tc.json` files or to in-flight changes. The image field is optional, so all existing models remain schema-valid. A backfill of them is out of scope (archived changes are immutable by convention).
- `openspec/tools/mmdc-puppeteer.json` (`--no-sandbox`) lets `mmdc` run in restricted/headless/CI contexts.
- `tc_render.py` writes uniquely-named tempfiles, so concurrent renders are safe without `flock` (unlike the fixed-path build scripts).

## Revisit Triggers

- **Native Mermaid in Threat Composer.** If the Threat Composer build gains Mermaid support (or we vendor a plugin), the pre-render step becomes redundant, and a later ADR supersedes this one. The agent would keep only the Mermaid source.
- **A browser-free faithful Mermaid renderer** (e.g. a maintained WASM/native Mermaid engine). It would let us drop the Chromium dependency and keep embedded images. Swap the `render_mermaid_to_png` backend and retire the mise entry.
- **The dependency creeps onto the deploy/`check` path.** That would violate ADR-0011. It must be re-decided, not done silently.

## Alternatives Considered

- **SaaS renderer (Kroki / mermaid.ink).** Rejected. It sends the trust-boundary DFD of a security evidence store to a third party. Disqualifying for this artifact.
- **`mmdc` + system Chrome (skip the bundled Chromium download).** Rejected. It reintroduces a non-deterministic per-machine dependency that mise is meant to eliminate. It defeats "`mise install` is the whole story."
- **Embed SVG instead of PNG.** SVG is smaller and crisper, and comfortably under the cap. But it still requires `mmdc`/Puppeteer to produce. Also, some `<img>`/React sanitizers refuse `image/svg+xml` data URIs, so the image risks non-render in the very UI we target. PNG is the safe default. SVG can be revisited if verified against the actual Threat Composer build.
- **Status quo — leave image slots empty.** Rejected, because this is the explicit thing this change fixes. The diagram was invisible in the Threat Composer UI.

## References

- `.claude/agents/threat-model.md` — agent contract; the render step and the closing `validate_tc_schema` gate.
- `openspec/tools/tc_render.py` — the render helper (`extract_mermaid_blocks`, `render_mermaid_to_png`, `render_capped_data_uri`, `embed_images`).
- `openspec/tools/mmdc-puppeteer.json` — Puppeteer `--no-sandbox` config.
- `openspec/tools/provision-mmdc-browser.sh` — the idempotent `chrome-headless-shell` provisioner behind `mise run mmdc-setup`.
- `mise.toml` — `npm:@mermaid-js/mermaid-cli` tool pin; `tm-render` and `mmdc-setup` tasks.
- `docs/adr/0011-defer-lambda-packaging-consolidation.md` — the "no mandatory container runtime on the routine build path" rule against which this decision is checked.
- Threat Composer v1 schema (`mcp__threat-composer-ai__threat_modeling_get_tc_schema`) — `Architecture.image` / `Dataflow.image` (base64 cap 1,000,000 chars).

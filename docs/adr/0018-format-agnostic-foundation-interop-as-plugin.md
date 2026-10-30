# ADR-0018: Format-agnostic foundation; interop modules plug in (OSCAL is the first)

## Status

Accepted (2026-06-16)

## Context

Cairn stores, signs, archives, indexes, and verifies audit artifacts. During MVP it gained deep, native OSCAL support:

1. a dedicated media-type taxonomy,
2. SDK-based metadata extraction,
3. content-derived schema gating.
4. a strict extraction-failure-is-permanent gate.

That support is non-negotiable and stays. But the spec set was authored OSCAL-first and patched toward format-agnosticism afterward. That left OSCAL **warping the foundation's framing** in three ways:

1. oscal-interop read as a privileged base layer, not as a capability that sits on the foundation.
2. One genuinely universal Requirement (artifact-reference generation, "for every bundle, OSCAL-bearing or not") lived inside the OSCAL capability.
3. The foundation specs carried defensive "OSCAL is not special" disclaimers, whose very presence implied the opposite.

The triggering question: **is OSCAL part of the foundation, or a module on top of it?** The system already behaves format-agnostically. Every artifact is content-addressed, signed over its OCI Manifest_Digest, archived, and referenced on identical terms regardless of format. So the foundation does not in fact depend on OSCAL. The framing simply had not caught up.

OSCAL is also explicitly intended to be the *first* of a family. The `native-format-expansion` backlog item (CycloneDX/SARIF/SPDX) is the concrete roadmap for the siblings.

This ADR records the architectural stance. It changes **no** Requirement. The `decenter-oscal-format-agnostic-foundation` change carries the Requirement-level moves that realign the specs to it (WHY of those moves in that change's `design.md`, WHAT in its spec deltas). Per the ADR boundary in `AGENTS.md`, the scope-unbounded *standing* decision lives here. The per-Requirement edits do not.

## Decision

**Cairn is a format-agnostic foundation. Format awareness is added by interop modules that plug into the foundation at named seams. The dependency is one-way: interop → foundation. The foundation never depends on any interop module. OSCAL is the first such module — fundamental and non-negotiable, but not a base layer.**

1. **One-way dependency.** The foundation capabilities (artifact-ingestion, processing-pipeline, signing, worm-archive, metadata-index, artifact-verifiability) MUST be correct and expressible with zero interop modules installed. An interop module MAY depend on the foundation. No foundation capability may depend on, be titled after, or special-case a specific format.

2. **Module ownership, not privilege.** An interop module claims a content-format family (OSCAL claims `application/oscal.*`) and is fully responsible for it. It owns extraction, schema gating, and the rule that a declared-but-unprocessable artifact of that family is a hard validation failure. This is what "first-class, non-negotiable OSCAL support" means: ownership of a family, not elevation above other formats at the foundation layer.

3. **The seam catalog.** Interop modules attach only at these foundation extension points:
   1. **Media-type routing** — the foundation routes layers by media type. A module registers for a media-type family.
   2. **Per-layer enrichment slot** — the foundation projects every layer's universal OCI descriptor. It exposes an optional per-entry enrichment sub-object that a module MAY populate (never a parallel, format-specific list).
   3. **Per-in-band-timestamp temporal check** — the foundation runs temporal consistency against whatever in-band authoring timestamp a module extracts (OSCAL supplies `metadata.last-modified`). The check skips a layer with no such timestamp gracefully.
   4. **Permanent-failure gate** — the foundation's permanent-failure classification. A module contributes its extraction-failure-is-permanent rule.
   5. **Universal artifact-reference** — the foundation generates, attaches (as an OCI Referrer), and projects a portable self-describing reference for *every* artifact. A module supplies the *encoding* of the reference body.

4. **OSCAL is the chosen reference-encoding vocabulary (lingua-franca).** Every artifact's reference body is an OSCAL back-matter `resource` element, even a non-OSCAL artifact's. The reason is that it is a documented, neutral schema for "pointer + hashes + rlinks." This is a vocabulary choice, not OSCAL privilege. The referrer's wire `artifactType` is already format-neutral (`application/vnd.cairnproofs.artifact.resource.v1`).

5. **Defer the generic `interop` capability until module #2.** Do NOT create an `interop` capability spec now. There is exactly one module and no plugin-registration machinery. So a generic-framework spec would describe unbuilt architecture, which violates the current-state-only rule for `openspec/specs/`. The seams above are expressed as universal foundation Requirements (as-built).

   When a second module (pdf/ocsf/cyclonedx-interop) actually lands and the contract has proven out, that change MAY promote this seam catalog into a real `interop` capability and supersede this ADR. Until then, sibling-module work is deferred as future work (the `native-format-expansion` capability).

## Consequences

1. The specs say what the system does: a format-agnostic foundation with OSCAL as a fundamental module on top. No foundation Requirement is titled after OSCAL or disclaims it.
2. A future interop module (pdf/ocsf/cyclonedx) is a sibling of oscal-interop with a known shape. It plugs into the five seams, so it is not a fresh negotiation each time.
3. OSCAL's support is undiminished. Ownership of `application/oscal.*`, the hard-failure gate, and the lingua-franca reference encoding all remain.
4. The seam catalog is documentation here, not yet a normative `interop` contract. Until module #2 promotes the contract, review discipline carries the obligation to keep the foundation independent of any module.
5. With the "OSCAL is not special" disclaimers removed, the foundation Requirements now state the format-agnostic guarantee *positively*. Reviewers must keep it stated positively and must not reintroduce defensive comparisons.

## Alternatives considered

1. **Create a generic `interop` capability now.** Rejected: N=1 module, no registration machinery. It would spec unbuilt architecture against the current-state-only rule. Revisit when module #2 lands.
2. **Emit a format-neutral native reference and demote OSCAL-resource to optional enrichment.** Rejected: it is a behavior change that requires migration. It has no benefit over OSCAL-as-lingua-franca, given that the wire type is already neutral.
3. **Leave the framing as-is and rely on the existing disclaimers.** Rejected: the disclaimers are self-defeating. Their presence is the strongest signal that the specs still treat OSCAL as special.

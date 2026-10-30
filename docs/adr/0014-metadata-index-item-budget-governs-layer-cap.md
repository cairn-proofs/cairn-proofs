# ADR-0014: The Metadata_Index item budget governs the content-layer cap

## Status

Accepted (2026-06-07)

To be superseded when the single-row Metadata_Index item is no longer the binding constraint on layer count. That happens when per-layer projections move to a dedicated table (see Revisit Triggers).

## Context

The pipeline projects a bundle's content layers into a **single** Metadata_Index DynamoDB item. There is one row per bundle. The row carries pk/sk, a claim projection, the evidence reference, validation/temporal statuses, and a per-layer projection for every content layer. DynamoDB caps a single item at **400 KB**.

That cap is the real, cross-cutting constraint on how many content layers a bundle may carry. Per-layer projection cost × layer-count must fit under 400 KB, with headroom for the rest of the row.

This is a **sizing invariant**, not a policy knob. It holds independently of any particular numeric cap. Whatever the content-layer limit is, the worst-case per-layer projection multiplied by that limit must stay below the item budget.

The specific cap value (currently 300) is a Requirement-level decision. The `artifact-ingestion` spec records it. This ADR documents only the Requirement-*independent* constraint that any such cap must respect. A future reader who changes the cap can then understand what governs the ceiling.

### The per-layer sizing model

Each content layer projects a descriptor into the row. The dominant terms at their worst case:

| Term       | Worst-case bytes | Notes                                          |
| ---------- | ---------------- | ---------------------------------------------- |
| digest     | ~71              | `sha256:` (7) + 64 hex chars                   |
| mediaType  | ~64              | an OSCAL/OCI media-type string                 |
| size       | ~11              | JSON integer, up to 10 GiB ≈ 11 digits         |
| title      | ≤ 1024           | layer title, capped at `MAX_LAYER_TITLE_LEN`   |
| structural | ~3               | JSON braces/quotes/commas beyond the above     |

- **Typical** per-layer cost is ~150 B (short titles, short media types).
- **Worst-case** per-layer cost is ~1173 B. A layer reaches it when the title sits at the 1024-byte `MAX_LAYER_TITLE_LEN` cap (`lambdas/validate_evidence/cairn_lambda_validate_evidence/handler.py`).

The worst case crosses the 400 KB item limit at roughly **344 layers** (`400 * 1024 / 1173 ≈ 349`, less the row-remainder for pk/sk + claim + evidence reference + statuses). A cap of **300** sits safely below that crossover (~352 KB worst-case projection). It leaves headroom for the rest of the row.

The `MAX_LAYER_TITLE_LEN` cap is the growth-sensitive lever. Raising it shrinks the safe layer count, so the invariant ties the two together.

## Decision

Treat the single-row Metadata_Index item budget as the **governing constraint** on the content-layer cap. Any content-layer cap MUST satisfy:

```
MAX_CONTENT_LAYERS × WORST_CASE_PER_LAYER_BYTES  <  400 KB − row-remainder headroom
```

`tests/unit/cairn_oci/test_bounds.py` makes this invariant **executable**. The test builds the worst-case per-layer figure from named components: digest, mediaType, size, structural overhead, and `MAX_LAYER_TITLE_LEN` imported from its canonical definition. It then asserts that the product stays under a safe item ceiling (400 KB minus a documented ~48 KB row-remainder reservation).

A future change can raise the cap or grow the per-layer projection past that ceiling. If it does, the test fails. The failure forces a revisit of this model, so the write path does not silently overrun the DynamoDB item limit.

The shared bounds live as constants in `libs/oci/cairn_oci/manifest.py` (`MAX_CONTENT_LAYERS`, `MAX_ARTIFACT_SIZE_BYTES`). Because of this, the client, validate server, and archive stage cannot drift apart.

This cross-cutting sizing constraint changes **no** Requirement on its own. The cap value itself is the Requirement, and it lives in the spec. Per the AGENTS.md ADR-vs-opsx boundary, this ADR therefore records the constraint, not an opsx change.

## Consequences

- The content-layer cap is no longer a free parameter. The item budget bounds it above, and the test in `tests/unit/cairn_oci/test_bounds.py` keeps the two in lockstep. To raise the cap, either shrink the per-layer projection (e.g. lower `MAX_LAYER_TITLE_LEN`) or relocate layers off the single row.
- The current cap **300** lives in the `artifact-ingestion` spec ([INGEST-009] / [INGEST-010]). This ADR explains *why* the cap is 300 and not an arbitrary larger number.
- The `oscal.metadata` axis is a write-only projection on the same row. Removing it (by moving OSCAL metadata to a canonical entity graph) frees row-remainder budget. It does not change the per-layer invariant itself.

## Revisit Triggers

- **Layers move off the single row.** If per-layer projections go into their own table keyed by bundle, the 400 KB-per-bundle ceiling on layer count dissolves. When that lands, the single Metadata_Index item is no longer the binding constraint and **this ADR is superseded**. The new table's access patterns and [INGEST-003.04] policy would then govern the cap, not the item limit.
- **`MAX_LAYER_TITLE_LEN` is raised.** A larger title cap shrinks the safe layer count. The test will catch an unsafe combination, but the sizing model here must be re-derived.
- **A higher layer cap is genuinely needed.** That need points at moving layers off the single row, not at shaving the per-layer projection.

## References

- `libs/oci/cairn_oci/manifest.py` — `MAX_CONTENT_LAYERS`, `MAX_ARTIFACT_SIZE_BYTES`, and the `OciManifest.layers` `max_length` guard.
- `tests/unit/cairn_oci/test_bounds.py` — the executable item-budget invariant.
- `lambdas/validate_evidence/cairn_lambda_validate_evidence/handler.py` — `MAX_LAYER_TITLE_LEN` (the growth-sensitive per-layer term).
- `openspec/specs/artifact-ingestion/spec.md` — [INGEST-009] / [INGEST-010], where the cap value (300) lives as a Requirement.
- A future dedicated per-layer table — moving layers off the single row is the path that supersedes this ADR.
- AGENTS.md — the ADR-vs-opsx boundary (WHY in the ADR, WHAT in the spec).

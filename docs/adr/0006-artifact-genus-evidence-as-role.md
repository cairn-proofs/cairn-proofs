# ADR-0006: Artifact is the genus; "evidence" is a reference role (OSCAL-grounded taxonomy)

## Status

Accepted (2026-05-31); superseded in part (2026-06-09).

Promoted from Proposed on approval of the implementing naming-reframe. WHY lives here. WHAT (the Requirement/glossary renames) lives in the spec, cross-linked — never duplicated (ADR-0001 boundary).

**Superseded in part (2026-06-09).** The core Artifact-genus / evidence-as-role taxonomy stands unchanged. Two peripheral calls originally made below were later reversed or completed. This section records their rationale so it stands on its own:

- **(a) "Keep *Evidence Store* as the marketing name" is reversed — the product is rebranded *Cairn Proofs* (system concept `Provenance_System`).** The original call preserved a brand/wire split that would otherwise freeze permanently. Stored bytes embed wire identifiers and derived-identity seeds. These become immutable once any non-disposable deployment exists. So re-anchoring them is now-or-never, and the disposable alpha (no consumers) is the only cheap window. Portable, externally-verifiable attestation artifacts should name their *format authority*, not a corporate umbrella. The supply-chain neighborhood anchors to the product/format domain (Sigstore `dev.sigstore.*`, SLSA `slsa.dev`). So the wire/identity authority re-anchors to the product domain `cairnproofs.dev`.
- **(b) The deferred full rename is completed.** The genus rename extends across code, infrastructure, glossary, and spec content. It happens in a single hard cut, taken while alpha is disposable, rather than staying as a permanent concept↔wire seam.

**Five-name separation (containment rationale).** The reframe keeps five names distinct, each with one home and one mutability class:

1. the technical concept (`Provenance_System`)
2. the maker and CLI (`cairn`, retained)
3. the product brand (*Cairn Proofs*, marketing surfaces only)
4. the wire/identity authority (`cairnproofs.dev`, frozen in stored bytes)
5. the per-deployment infrastructure naming token (`appName`, configurable, never in stored bytes)

The reframe deliberately confines "Proof(s)" to the brand. If it became a genus, this ADR would re-run with a stronger word. It is safe as a brand precisely because it is not an audit term of art. Auditors deal in *evidence* and *reasonable assurance*, so "proof" cannot be mistaken for a governed type. It reads as cryptographic proof-of-custody, which is what signing + WORM + timestamping deliver. The uniform per-deployment resource-naming scheme (`{appName}-{resource}-{qualifier}`, generated once and frozen) lives in the infrastructure layer, not in this record.

## Context

Cairn's governed vocabulary uses **"evidence" as the genus**: `Evidence_Store`, `Evidence_Bundle`, `Evidence_Category`, `Evidence_Producer`, `Evidence_TUI`. But the store demonstrably holds artifacts that are **not** evidence:

- **System-minted** Notation signatures, WORM receipts, and OCI image indexes. The validate stage deliberately *skips* them (`config.mediaType` discriminator). The phrase "non-evidence artifacts" had to be coined precisely because the vocabulary had no word for them.
- **Producer-supplied reference artifacts.** The in-flight `tag-grammar-generalization` introduces the `_ref` form for "OSCAL catalogs and profiles, policy documents," and `alias-tags` adds mutable `_als`/`_chn` views.

So "evidence" is the genus word for a system that stores more than evidence.

**OSCAL grounding** (NIST OSCAL Assessment Results model, retrieved via the OSCAL MCP server, 2026-05-31): in the authoritative domain model, *evidence is not a type*. It appears only as `relevant-evidence`, a **leaf `href` reference** from an `observation` to a resolvable resource. The only required field is a human `description`, and `href` is optional. The stored thing is a back-matter `resource` (`uuid`, `rlinks`, `base64`). **Being referenced confers evidence-ness, not being a kind.** Strip the citation and the same scan report is just a resource. Two of Cairn's seven "evidence categories" (`assessment-results`, `poam-evidence`) are themselves OSCAL *model* names. The rest are generic artifact-kind descriptors. Consequently:

- `Evidence_Category` is an **artifact-kind classification** wearing the wrong name (it over-claims "evidence" before anything cites the artifact, and collides with OSCAL's own model names).
- `tag-grammar-generalization`'s `_ref` form mints a *reference* artifact (an OSCAL catalog — exactly *what evidence cites*) with a leading `{evidence-category}` segment. Tagging a catalog with an "evidence-category" **inverts** the OSCAL relationship. The correct name for that leading segment is `artifact-kind`.

This is the limiting case ADR-0003 anticipated. The specs *constrain* the term's meaning (the seven-value enum), but the concept itself was mis-named at the genus level.

## Decision

**Adopt `Artifact` as the genus** for the stored, digest-addressed unit (≈ OSCAL `resource`). **Retain "evidence" as a reference/role term**, not the genus or the stored thing's type. An artifact *is* relevant-evidence *for* an observation that cites it. This is a relation, not an intrinsic type, and the in-flight `oscal-entity-graph` models it.

Name two axes that already exist physically but were never named:

```
Artifact_Type  (a manifest — has a digest, has identity)
   ├─ evidence       producer-supplied PAYLOAD
   ├─ reference      producer-supplied REFERENT (≈ OSCAL resource; _ref form)
   ├─ signature      system-minted (referrer)
   ├─ worm-receipt   system-minted (referrer)
   └─ oci-index      system-minted
   discriminated by config.mediaType + reserved tag tokens

Alias  (a tag pointer — NO digest, a mutable VIEW over an Artifact)
   ├─ _als--{name}              named alias
   └─ {kind}--_chn--{channel}   channel pointer
   NOT an Artifact_Type; created by operator action, never minted
```

- **`Evidence_Category` → `Artifact_Kind`**: the producer-supplied classification of an artifact (the seven-value enum). The leading tag segment is `artifact-kind`.
- **Governance**: ADR-0003's nominal-authoritative / normative-indexed model governs the new genus terms. The glossary *names* the concept. The enum and tag-format constraints stay in their spec Requirement (single-source preserved).
- **Product / marketing name retained**: "Evidence Store" and the `CairnEvidenceStore-cmh-alpha` stack name are a **marketing** choice and are explicitly **out of scope** of this reframe.

### Tiering

| Tier | Surface | Properties |
|---|---|---|
| **1 — concept/symbol** | glossary terms, ADR-0003 term map, Python/TS symbols, spec prose + Requirement names | behavior-free, reversible, no stored bytes touched |
| **2 — wire/data-plane** | `com.cairn.evidence.*` annotations (14 keys), `vnd.cairn.evidence.*` media types, `evidence-{qualifier}/` ECR repo prefix | breaking; embedded in every stored manifest; cheapest while alpha but deferred |

The seven enum **values** (`assessment-results`, …) and the wire keys do **not** change in Tier 1. This leaves an intentional concept↔wire seam (concept `artifact-kind` / wire `bundle-type`) until Tier 2 — the same flavor as the `evidence-` repo prefix persisting.

### Sequencing relative to in-flight work

```
① naming reframe (NEW change, Tier-1, behavior-free) ── lands FIRST
     Artifact_Kind term + Artifact_Type axis + evidence-as-role:
     the governed term (its governing Requirement + by-name refs in
     processing-pipeline/retention-lifecycle/account-onboarding), the
     glossary, and code symbols (EvidenceCategory → ArtifactKind).
     Does NOT touch ECR Tag Format ([INGEST-021]) — see refinement below.
     Scoped to exactly what ② needs — NOT a system-wide rename.
        │
② tag-grammar-generalization ── authored in the new vocab on top of ①
        │  (_ref = reference Artifact_Type; leading segment = artifact-kind)
        │  OWNS the {evidence-category}→{artifact-kind} segment rename in
        │  its [INGEST-021] rewrite (refinement, 2026-05-31, decision "B")
③ alias-tags ── trails ② (already gated on it)

── later, deliberate, decoupled ──
④ Tier-2 wire migration (its own ADR + change; alpha = cheapest window)
⑤ broader technical renames: Evidence_Bundle/Producer/TUI, capability
   dir names (artifact-ingestion, artifact-tui). Optional follow-ons.
```

- **`tag-grammar-generalization` does NOT proceed first.** No *functional* dependency exists. Its mechanics (digest-first push, server-side minting, sequence counter, idempotency, `_ref` discrimination) work under either name. But it is the change that first introduces a producer-supplied *non-evidence* artifact. If it goes first, it bakes the "catalog tagged as evidence-category" infelicity into brand-new grammar code (`categories.py`'s `EvidenceCategory` symbol and friends). ① would then have to rename that code, which means churn on the most subtle, fail-closed code in the system. ① renames *stable* code symbols first. ② then rewrites [INGEST-021] (adding `_ref` + the segment rename) on top, in the new vocabulary. The diffs are sequential and non-overlapping — neither change rewrites the other's lines.
- **The reframe is NOT folded into `tag-grammar-generalization`.** Two reasons apply:
  1. Footprint mismatch. The reframe spans the glossary, ADR-0003, and `artifact-tui`, `account-onboarding`, `deployment-modes`, beyond tag-grammar's three capabilities.
  2. An un-reviewable diff. A system-wide mechanical rename would tangle with subtle push-semantics behavior and defeat bisectable history.

## Consequences

**Positive**

- Vocabulary matches the domain authority (OSCAL). The stored unit is an artifact, and "evidence" is the reference relation it can play.
- `_ref` / reference artifacts become coherent. The vocabulary no longer labels a catalog "evidence."
- The `Artifact_Type` (identity-bearing manifest) vs `Alias` (mutable view) distinction — implicit in the `tag-grammar` + `alias-tags` reserved-token registry — gains a name.
- Single-source preserved: the enum/format stays in its spec Requirement per ADR-0003. The glossary only names the concept.
- Changes stay reviewable, and the push-collision fix does not entangle with a rename.

**Negative**

- Intentional concept↔wire mismatch until Tier 2 (`artifact-kind` concept vs `bundle-type` wire). Acceptable, but a visible seam.
- Sequencing delays `tag-grammar-generalization` (and its real `--tag-sequence` bulk-push relief) behind ①. Mitigated: ① is mechanical and behavior-free, so it should land fast.
- Even Tier-1 is a large mechanical rename (hundreds of sites). This creates a scope-creep risk. ① must resist becoming "rename everything," which would recreate the mega-change this ADR avoids. ① settles *only* `Artifact_Kind` + the `Artifact_Type` axis name + evidence-as-role. It does **not** rename `Evidence_Bundle`/`Evidence_Producer`. Those names do not block ②, and they ride ⑤.
- Inherits ADR-0003's index-pointer fragility (the glossary→Requirement anchor is implicit, unenforced by `openspec validate`).

## Alternatives Considered

- **Flatten — "evidence" is just one value of a flat `Artifact_Type` enum, no special status.** Rejected. It erases the load-bearing producer-supplied/payload vs system-minted/mechanism distinction. Also, OSCAL models evidence as a *relation*, not a peer value. The flattening would lose information the specs need anyway: only evidence is validated, and only evidence carries OSCAL/controls.
- **Keep "evidence" as the genus; treat non-evidence as exceptions.** Rejected. It is already false on the ground. It forces "non-evidence artifact" coinages and mislabels `_ref`.
- **Fold the reframe into `tag-grammar-generalization`.** Rejected: mega-change, un-reviewable diff (see Decision).
- **`tag-grammar-generalization` first, reframe after.** Rejected. It bakes the catalog-as-evidence infelicity into new grammar code and forces a rewrite of the system's most subtle code. No functional dependency exists, but the coherence + churn argument is decisive.
- **Full rename now, including wire identifiers and the product name.** Rejected for now, for two reasons. First, Tier-2 wire is breaking and data-plane. It is deferred to its own deliberate change: alpha is the cheapest window, but it is still a distinct decision. Second, the product name "Evidence Store" is a marketing choice the owner has chosen to keep.

## References

- ADR-0001 — ADR adoption. It sets the requirement-change-goes-through-opsx vs no-requirement-change-is-an-ADR boundary that scopes this record to WHY.
- ADR-0003 — Glossary authority model. It governs the new `Artifact`/`Artifact_Kind` genus terms (nominal-authoritative, normative-indexed).
- NIST OSCAL Assessment Results model — `relevant-evidence` (observation leaf `href`), back-matter `resource`. Retrieved via the OSCAL MCP server, 2026-05-31.
- In-flight changes: `tag-grammar-generalization` (proposal/design — `_ref` reference artifacts, the D1a `_RESERVED_TOKENS` registry), `alias-tags` (`_als`/`_chn` alias views), `oscal-entity-graph` (the evidence-as-relation edge).
- `/opsx:explore` session, 2026-05-31 — taxonomy + sequencing.
- `AGENTS.md` — single-source-of-truth convention.
- The implementing naming-reframe proposal ("①") — to be created via `/opsx:propose`. WHAT lives there, cross-linked to this ADR.

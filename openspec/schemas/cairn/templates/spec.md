## Purpose

<!-- New capabilities only: one or two sentences (50+ characters) on what this
     capability is for. Delete this section for an existing (MODIFIED) capability. -->

## ADDED Requirements

### Requirement: <!-- requirement name -->
<!-- requirement text. Use SHALL/MUST for normative behavior (avoid should/may).
     Every requirement needs at least one scenario. -->

<!-- ⚠ Scenario headers MUST use EXACTLY four hashtags (`#### Scenario:`).
     Three hashtags or a bullet parse as a new requirement and fail SILENTLY —
     openspec validate will NOT catch it. -->
#### Scenario: <!-- scenario name -->
- **WHEN** <!-- condition -->
- **THEN** <!-- expected outcome -->

<!-- For a MODIFIED capability, replace the ADDED block above with the sections
     below. Copy the ENTIRE existing requirement block from
     openspec/specs/<capability>/spec.md (header through all scenarios), then
     edit — partial content silently DROPS the omitted scenarios at archive. The
     `### Requirement:` header text MUST match the canonical heading exactly
     (validate --strict does NOT verify this — grep-cross-check before archive).

## MODIFIED Requirements

### Requirement: <existing name, verbatim from canonical spec>
<full updated requirement text>

#### Scenario: <copied + edited; keep ALL existing scenarios>
- **WHEN** <condition>
- **THEN** <expected outcome>

## REMOVED Requirements

### Requirement: <existing name>
**Reason**: <why it is being removed>
**Migration**: <what replaces it / how callers adapt>
-->


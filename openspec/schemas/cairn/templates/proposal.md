## Why

<!-- Explain the motivation for this change. What problem does this solve? Why now? -->

## Personas served

<!-- REQUIRED. Cite at least one persona GOAL or FRUSTRATION from
     openspec/personas/personas.md that this change advances — quote the
     goal/frustration, not a bare name (a name is back-fillable; a goal is the
     value check). e.g. "Marcus — External Auditor: independently verify
     signature status without trusting an operator's assertion."
     If no persona goal applies, write "none" with a one-line reason — a VALID
     answer that flags the change for review (speculative work, or a persona not
     yet modeled), not a blocker. Do NOT launder a name to pass the check. -->
1. <persona — role: goal/frustration this advances>

## What Changes

<!-- Numbered list (1., 2., ...) so each change is referenceable downstream.
     Be specific about new capabilities, modifications, or removals. -->
1. <change — new capability, modification, or removal>

## Capabilities

### New Capabilities
<!-- Capabilities being introduced. Name each capability for a cohesive system
     behavior that can own related requirements as the system evolves. Do not name
     implementation tasks or proposal sections. Avoid broad catch-all names. Use
     kebab-case for path segments you introduce (e.g., user-auth or identity/user-auth)
     that follow the project's existing spec organization. Each creates
     specs/<capability-path>/spec.md. -->
1. `<capability-path>`: <brief description of what this capability covers>

### Modified Capabilities
<!-- Existing capabilities whose REQUIREMENTS are changing (not just implementation).
     Only list here if spec-level behavior changes. Each needs a delta spec file.
     Use the exact existing path under openspec/specs/. Leave empty if no requirement
     changes. A change with no capabilities at all (pure refactor, tooling, docs)
     must set `skip_specs: true` in its .openspec.yaml — openspec validate rejects
     a zero-delta change without that marker. Do not invent a requirement just to
     satisfy validation. -->
1. `<existing-capability-path>`: <what requirement is changing>

## Impact

<!-- Affected code, APIs, dependencies, systems -->

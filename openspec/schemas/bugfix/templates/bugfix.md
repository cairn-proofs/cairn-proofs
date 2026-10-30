# Bugfix: <!-- one-line summary -->

## Severity & Impact

<!-- One paragraph. Who's affected, how often, what they see, blast radius.
     Reference real signals where possible: alarm name, log query, ticket
     ID, customer report. Be specific. -->

## Reproduction

<!-- Concrete, copy-pasteable steps. Include:
     - Environment (stage, region, account)
     - Inputs (request payload, file fixture, env vars)
     - Observed output (error message, wrong value, missing record)
     - Minimal trigger if the bug requires load or specific data shape -->

1. <!-- step -->
2. <!-- step -->
3. <!-- observed: -->

## Current Behavior (Defect)

<!-- One or more WHEN/THEN clauses describing the bug as it exists today.
     No SHALL here - this is descriptive, not normative.
     One clause per NUMBERED item (downstream cites them as "clause #N"). Split compounds. -->

1. **WHEN** <!-- condition --> **THEN** the system <!-- incorrect behavior -->

## Expected Behavior (Correct)

<!-- One or more WHEN/THEN SHALL clauses defining "fixed".
     Every clause must be verifiable by an automated test.
     One clause per NUMBERED item (downstream cites them as "clause #N").
     This is the test contract. -->

1. **WHEN** <!-- condition --> **THEN** the system **SHALL** <!-- correct behavior -->

## Unchanged Behavior (Regression Prevention)

<!-- One or more WHEN/THEN SHALL CONTINUE TO clauses.
     Enumerate behaviors at risk from the proposed fix - adjacent code paths,
     shared state, callers of the modified function.
     One clause per NUMBERED item (downstream cites them as "clause #N").
     This is the seed list for the regression test suite. -->

1. **WHEN** <!-- condition --> **THEN** the system **SHALL CONTINUE TO** <!-- existing behavior -->

## Spec Impact

<!-- One of:

     None - the spec already mandates the Expected Behavior; this is purely
     an implementation defect.

     OR

     Spec amendment needed - the <capability> spec is ambiguous about
     <condition>; the fix will be accompanied by a delta spec updating
     Requirement <name>. -->

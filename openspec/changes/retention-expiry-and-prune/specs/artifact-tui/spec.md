## ADDED Requirements

### Requirement: [TUI-016] Retention status in the bundle detail view

WHEN an operator selects an archived Artifact_Bundle, THE Artifact_TUI SHALL display the bundle's Retain_Until and whether it has passed. It SHALL read the value only from the `dev.cairnproofs.artifact.retain-until` annotation of the WORM_Receipt referrer returned by the `ecr:ListImageReferrers` read it already issues, with no additional AWS operation. It SHALL render the value through the terminal-render safety rule, show *unknown* when the value is absent or not RFC 3339 UTC, and offer no prune or delete action.

#### Scenario: [TUI-016.01] Expired bundle shows its Retain_Until and an expired status

- **WHEN** an operator selects an archived bundle whose WORM_Receipt referrer annotation `dev.cairnproofs.artifact.retain-until` is in the past
- **THEN** the detail view shows that Retain_Until and marks retention as expired, with no AWS call beyond the referrer read already issued

#### Scenario: [TUI-016.02] Missing or malformed annotation shows unknown

- **WHEN** an operator selects an archived bundle whose WORM_Receipt referrer has no `dev.cairnproofs.artifact.retain-until` annotation, or one that is not a valid RFC 3339 UTC timestamp
- **THEN** the detail view shows retention status *unknown* and renders no value taken from the malformed annotation as a date

#### Scenario: [TUI-016.03] TUI offers no delete action

- **WHEN** an operator views an expired bundle in the Artifact_TUI
- **THEN** the TUI offers no prune or delete action and surfaces the `cairn prune` report command for the bundle's project for manual use

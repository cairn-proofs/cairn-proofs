## REMOVED Requirements

### Requirement: [META-006] Retain metadata records for the full retention period

**Reason**: The requirement restated the retention-lifecycle *Metadata_Index records retained for the full retention period* Requirement, and its premise (an ECR copy removed by lifecycle policy) no longer holds: no lifecycle rule removes ECR manifests. Keeping two statements of one rule invites drift.

**Migration**: The retention-lifecycle *Metadata_Index records retained for the full retention period* Requirement is the single statement of record retention, including that the record is removed only as the last step of an operator-initiated prune. At archive, record `META-006` as `relocated` to `RETAIN-002` and `META-006.01` as `relocated` to `RETAIN-002.01` in this spec's `## Relocated and Retired IDs` table.

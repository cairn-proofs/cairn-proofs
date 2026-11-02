## REMOVED Requirements

### Requirement: [INGEST-027] ECR Lifecycle for Untagged Manifests

**Reason**: Age-based expiry of untagged manifests can delete evidence. Every referrer (the Notation signature, the WORM_Receipt, the Producer_Attestation_Referrer, and the resource referrer) is untagged, and so is an archived bundle whose tag mint failed. ECR is the long-term verification surface, so these manifests have to stay for the bundle's whole life.

**Migration**: The retention-lifecycle *No automatic deletion of artifacts* Requirement owns the rule: no artifact ECR repository carries a lifecycle rule, and untagged manifests are removed only through the retention-lifecycle *Operator-initiated prune* Requirement. Remove the `TagStatus.UNTAGGED` lifecycle rule from `cdk/lib/constructs/repositories.ts`. At archive, record `INGEST-027` as `relocated` to `RETAIN-006` and `INGEST-027.01` as `withdrawn` in this spec's `## Relocated and Retired IDs` table.

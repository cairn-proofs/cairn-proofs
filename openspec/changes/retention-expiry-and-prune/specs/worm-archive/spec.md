## MODIFIED Requirements

### Requirement: [WORM-005] WORM_Receipt generation and OCI Referrer attachment

WHEN archiving completes successfully, THE Processing_Pipeline SHALL generate a **WORM_Receipt** containing the S3 location, archive timestamp, blob count, integrity verification result, and the bundle's Retain_Until (see the retention-lifecycle *Single retention clock for every archived bundle* Requirement), and SHALL attach the receipt as an OCI Referrer to the original ECR artifact. The receipt SHALL record Retain_Until whatever the deployment's `WORM_Enabled` setting. THE Processing_Pipeline SHALL also carry Retain_Until on the receipt referrer's manifest as the annotation `dev.cairnproofs.artifact.retain-until`, in RFC 3339 UTC form, so a reader of the OCI Referrers API obtains it without fetching the receipt body.

#### Scenario: [WORM-005.01] WORM_Receipt attached as OCI Referrer
- **WHEN** archive completes successfully for an Artifact_Bundle
- **THEN** the Processing_Pipeline writes a WORM_Receipt artifact recording the S3 location, the archive timestamp, the blob count, the integrity verification result, and Retain_Until, and attaches it as an OCI Referrer to the original ECR artifact

#### Scenario: [WORM-005.02] Receipt annotation carries Retain_Until in both modes
- **WHEN** archive completes successfully in a deployment with either `WORM_Enabled` value
- **THEN** the WORM_Receipt referrer's manifest carries the annotation `dev.cairnproofs.artifact.retain-until` whose value equals the Retain_Until in the receipt body, and that value is visible in the OCI Referrers API listing for the bundle

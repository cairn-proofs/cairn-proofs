# artifact-tui Specification

## Purpose
A read-only operator TUI (`cairn tui`) for inspecting a deployed Provenance_System. It lists bundles by project and control scope through sanctioned access paths, shows bundle lifecycle and cross-reference detail, navigates and groups OCI referrers, reports DLQ health, refreshes live in watch mode, and renders untrusted stored values safely. It provisions nothing and mutates nothing.

*Domain terms (`Capitalized_Underscore`) used below are defined in [GLOSSARY.md](../../GLOSSARY.md), which names the governing requirement for each.*
## Requirements
### Requirement: [TUI-001] Read-only AWS access surface

The **Artifact_TUI** is the read-only terminal UI for listing bundles, controls, and pipeline health via the Metadata_Index. THE Artifact_TUI SHALL access AWS only through a fixed allowlist of read operations: `dynamodb:Query` / `dynamodb:GetItem`, `ecr:DescribeRepositories`, ECR image/manifest reads, `ecr:ListImageReferrers` (the OCI Referrers read API), `s3:GetObject` / `s3:HeadObject`, and `sqs:GetQueueAttributes`. THE Artifact_TUI SHALL NOT issue any mutating operation (create, update, delete, put, tag, redrive) and SHALL NOT issue `sqs:ReceiveMessage` or any other call that consumes or alters dead-letter-queue messages. `ecr:ListImageReferrers` SHALL be treated as a read operation only: it discovers the OCI Referrers attached to a subject manifest and SHALL NOT mutate, tag, or delete any artifact. This invariant SHALL be enforced structurally so the codepath cannot issue a non-allowlisted operation, whatever the caller's credentials permit.

#### Scenario: [TUI-001.01] TUI session issues only allowlisted read operations

- **WHEN** an operator runs a full Artifact_TUI session that lists bundles, opens a bundle's detail, views the bundle's OCI referrers, views a control, and reads DLQ health
- **THEN** every AWS call issued belongs to the documented read allowlist (including `ecr:ListImageReferrers` for the referrer view), and no mutating or message-consuming operation is invoked at any point in the session

#### Scenario: [TUI-001.02] Attempt to issue a non-allowlisted call is structurally prevented

- **WHEN** any Artifact_TUI codepath attempts an AWS operation outside the read allowlist (for example a `PutItem`, `DeleteObject`, `ReceiveMessage`, or any ECR tag/delete op)
- **THEN** the access layer refuses the call rather than forwarding it to AWS, so the read-only invariant holds regardless of the caller's IAM grants

### Requirement: [TUI-002] Bundle listing via sanctioned access paths

THE Artifact_TUI SHALL populate its all-bundles list as follows:

1. It SHALL enumerate artifact repositories via `ecr:DescribeRepositories`, and SHALL validate each enumerated repository name against the expected `{appName}-artifact-{deploymentQualifier}/{org-id}/{tenant-id}/{project-id}` grammar before deriving any identifier — skipping any name that does not match that shape rather than deriving an unintended query scope from a malformed name.
2. From each conforming repository name it SHALL derive the distinct opaque `{org-id}`, `{tenant-id}`, and `{project-id}` identifiers (one repository per project, no artifact-kind path segment; see artifact-ingestion *ECR Repository Path*), and SHALL issue the existing project-scoped Metadata_Index query (`query_by_project`) for each distinct project.
3. It SHALL resolve the human display name for each `{org-id}`, `{tenant-id}`, and `{project-id}` from the control-plane identity registry by its opaque id — never from the repository path, which carries opaque identifiers only — and SHALL render every resolved display name through the terminal-render-safety rule (see *Terminal-render safety for untrusted stored values*) before display.
4. It SHALL NOT issue an unscoped DynamoDB `Scan` to build the list, consistent with the metadata-index prohibition on unscoped full-table query paths.

#### Scenario: [TUI-002.01] All-bundles list is assembled from ECR-derived projects

- **WHEN** an operator opens the Artifact_TUI without specifying a project
- **THEN** the TUI enumerates the artifact repositories, derives the distinct opaque `{org-id}`/`{tenant-id}`/`{project-id}` identifiers from their three-segment repo-path prefixes, runs the project-scoped Metadata_Index query for each project, resolves each identifier's display name from the control-plane identity registry by opaque id, and presents the merged set of bundles

#### Scenario: [TUI-002.02] List construction never scans the table

- **WHEN** the Artifact_TUI builds or refreshes the all-bundles list
- **THEN** no `dynamodb:Scan` is issued against the Metadata_Index; the list is sourced only from per-project key-condition queries

#### Scenario: [TUI-002.03] Malformed repository name is skipped, not used to scope a query

- **WHEN** ECR enumeration returns a repository name that does not match the `{appName}-artifact-{deploymentQualifier}/{org-id}/{tenant-id}/{project-id}` grammar
- **THEN** the Artifact_TUI skips that repository when deriving identifiers and issues no Metadata_Index query scoped to a value derived from the malformed name

#### Scenario: [TUI-002.04] Display names are resolved from the identity registry, not the path

- **WHEN** the Artifact_TUI presents an org, tenant, or project whose repository-path segment is an opaque identifier
- **THEN** the Artifact_TUI resolves the human display name from the control-plane identity registry by that opaque id, renders it through the terminal-render-safety rule, and never derives a human-meaningful name from the repository path

### Requirement: [TUI-003] Terminal-render safety for untrusted stored values

THE Artifact_TUI SHALL treat all externally-influenced strings as untrusted content and SHALL strip or neutralize C0/C1 control bytes and ANSI/OSC/DCS escape sequences from every such value before writing it to the terminal. Untrusted strings include ECR repository names, control-plane identity-registry display names (resolved by opaque id for an org, tenant, or project), OSCAL control identifiers and annotations, cross-reference strings (ECR/S3/Signing_Profile references, digests, ARNs, UUIDs), any free-text Metadata_Index field, and the entire pretty-printed raw OCI manifest text. No producer-supplied value SHALL emit active terminal control sequences through the Artifact_TUI. Render-safety SHALL be applied through a single shared neutralization step across every view — list (including nested/grouped referrer rows), detail, the raw-manifest drill-down, control, and the surfaced command string — so the behavior is testable in one place.

#### Scenario: [TUI-003.01] Stored value containing escape sequences renders as inert text

- **WHEN** a bundle's Metadata_Index record or OSCAL annotation contains ANSI/OSC/DCS escape sequences or C0/C1 control bytes (for example a crafted project name, control description, or annotation value)
- **THEN** the Artifact_TUI renders the value as inert, visible text with the control and escape bytes stripped or neutralized, and the operator's terminal state (title, clipboard, cursor position, color state) is not altered by the stored content

#### Scenario: [TUI-003.02] Render safety applies across all views

- **WHEN** producer-influenced values are displayed in the bundle list (including nested/grouped referrer rows), the bundle detail pane, the raw-manifest drill-down, the control view, or the surfaced `cairn verify` command string
- **THEN** the same control-byte and escape-sequence neutralization is applied in each of those views before any value reaches the terminal

### Requirement: [TUI-004] Caller-scoped read access

THE Artifact_TUI SHALL operate entirely under the invoking operator's IAM credentials and SHALL NOT assume any additional role or widen access beyond what those credentials grant. THE Artifact_TUI SHALL resolve table names — including the control-plane identity-registry table — the ECR host, and queue names from the `(stage, regionCode)` deployment target, and SHALL read tenant- and project-scoped data keyed on the opaque identifiers derived from the repository path rather than on an account-identifier partition prefix.

#### Scenario: [TUI-004.01] TUI runs under the operator's credentials

- **WHEN** an operator launches the Artifact_TUI against the `cmh-alpha` deployment
- **THEN** all reads execute under the operator's existing credentials, the deployment target derives the metadata, control-claims, and identity-registry table names and the DLQ names, and no role assumption or privilege escalation occurs

### Requirement: [TUI-005] Bundle lifecycle and cross-reference detail

WHEN an operator selects a bundle, THE Artifact_TUI SHALL render, from the bundle's Metadata_Index record, two things:
1. the bundle's pipeline lifecycle state (validate → sign → archive → metadata — the four lifecycle stages surfaced for display; the pipeline's fifth stage, mint-tag, is not surfaced as a lifecycle column); and
2. its cross-references (ECR repository/tag/digest, S3 WORM bucket/key, Signing_Profile ARN, and OSCAL controls plus catalog-or-profile UUID).

THE Artifact_TUI SHALL render the OSCAL section ONLY when the selected record carries at least one OSCAL field (catalog-or-profile UUID, controls, or OSCAL references). A record with none of these — a non-OSCAL artifact bundle, or a drilled-into referrer child (signature / WORM_Receipt / resource-reference) — SHALL omit the OSCAL section entirely rather than render a block of empty placeholders. All rendered cross-reference and OSCAL strings SHALL pass through the terminal-render safety rule (see *Terminal-render safety for untrusted stored values*).

THE Artifact_TUI SHALL NOT perform live cryptographic re-verification of the bundle. Instead it SHALL surface the `cairn verify` invocation an operator can run to verify the bundle. The surfaced command string SHALL be built only from format-validated identifiers, rendered through the render-safety rule, and presented for the operator to run manually; THE Artifact_TUI SHALL NOT execute it automatically.

THE Artifact_TUI SHALL also offer a drill-down to the selected bundle's pretty-printed raw OCI manifest, AND to the raw OCI manifest of a focused referrer when one is selected. The raw manifest SHALL be retrieved via the allowlisted `ecr:BatchGetImage` read only — the bundle's manifest reusing the read already issued on selection, a focused referrer's manifest riding one additional lazy `ecr:BatchGetImage` — with no `dynamodb:Scan`, no mutating or message-consuming operation, and no live cryptographic re-verification. The entire raw-manifest text is producer-controlled content and SHALL pass through the terminal-render safety rule before display.

#### Scenario: [TUI-005.01] Detail pane renders lifecycle and cross-references from the metadata row

- **WHEN** an operator selects a bundle whose Metadata_Index record shows `signingStatus=signed` and `archiveStatus=archived`
- **THEN** the detail pane shows the lifecycle stages reached, the ECR/S3/Signing_Profile cross-references, and the OSCAL controls and catalog-or-profile UUID, and offers the `cairn verify` command rather than re-verifying inline

#### Scenario: [TUI-005.02] OSCAL section is omitted when the bundle carries no OSCAL data

- **WHEN** an operator selects a record that carries no catalog-or-profile UUID, no controls, and no OSCAL references (a non-OSCAL artifact bundle, or a drilled-into referrer child such as a signature, WORM_Receipt, or resource-reference)
- **THEN** the detail pane omits the OSCAL section entirely rather than rendering an `== OSCAL ==` block of empty `—` placeholders

#### Scenario: [TUI-005.03] Suggested verify command is inert and not auto-executed

- **WHEN** the detail pane surfaces the `cairn verify` invocation for the selected bundle
- **THEN** the command string is built from format-validated identifiers, neutralized by the render-safety rule, and presented for the operator to copy and run manually, and the Artifact_TUI never executes it automatically

#### Scenario: [TUI-005.04] ECR manifest and S3 object detail are fetched lazily on selection

- **WHEN** the all-bundles list is displayed but no bundle is selected
- **THEN** no per-bundle ECR manifest read or S3 `HeadObject` is issued; those reads occur only after a bundle is selected, and only for the selected bundle

#### Scenario: [TUI-005.05] Operator drills into the bundle's pretty-printed raw manifest

- **WHEN** an operator drills into the raw manifest of a selected bundle
- **THEN** the Artifact_TUI displays the bundle's OCI manifest pretty-printed, sourced from the `ecr:BatchGetImage` read already issued on selection (no additional AWS call), and the operator can return to the bundle detail without leaving the detail flow

#### Scenario: [TUI-005.06] Raw manifest renders as inert text

- **WHEN** the pretty-printed raw manifest of a bundle or focused referrer would contain ANSI/OSC/DCS escape sequences or C0/C1 control bytes anywhere in its producer-controlled content
- **THEN** the Artifact_TUI renders the entire manifest as inert, visible text with the control and escape bytes stripped, and the operator's terminal state is not altered

### Requirement: [TUI-006] Control-scoped bundle listing

THE Artifact_TUI SHALL support listing the bundles that cite a given control by querying the Control Claims table on the composite key `{catalogOrProfileUuid}#{controlId}`, returning one entry per bundle within the caller's scope that cites that control under that catalog or profile.

#### Scenario: [TUI-006.01] Control view lists bundles citing a control

- **WHEN** an operator views control `ac-2` against catalog-or-profile UUID `<uuid>`
- **THEN** the TUI queries the Control Claims table on `{<uuid>}#ac-2` and lists the bundles citing that control, without returning bundles that cite the same control id under a different catalog or profile

### Requirement: [TUI-007] Dead-letter-queue health from queue depth

THE Artifact_TUI SHALL surface pipeline health by reading the approximate message depth (`sqs:GetQueueAttributes`) of the four pipeline dead-letter queues (validate-bundle, sign-bundle, archive-to-s3, update-metadata) and SHALL indicate when any queue depth is non-zero. THE Artifact_TUI SHALL derive this signal from queue attributes only and SHALL NOT consume, receive, or otherwise alter any dead-letter-queue message.

#### Scenario: [TUI-007.01] Health bar shows per-DLQ depth

- **WHEN** an operator views the Artifact_TUI while the sign-bundle DLQ holds messages and the other three are empty
- **THEN** the health bar shows the approximate depth of each of the four DLQs and flags the sign-bundle queue as non-empty, using only `GetQueueAttributes` reads

#### Scenario: [TUI-007.02] DLQ health never consumes messages

- **WHEN** the Artifact_TUI reads DLQ health, including under watch-mode refresh
- **THEN** no `ReceiveMessage` is issued and no message's receive-count or visibility is altered, leaving the queues available for an operator redrive

### Requirement: [TUI-008] Paginated listing without truncation

WHEN a project's or control's result set exceeds one Metadata_Index page, THE Artifact_TUI SHALL retrieve additional pages using the metadata-index pagination token rather than silently truncating the displayed set. The operator SHALL be able to reach every matching bundle across all pages.

#### Scenario: [TUI-008.01] Multi-page result set is fully reachable

- **WHEN** an operator lists a project whose bundles span more than one 100-item page
- **THEN** the TUI uses the pagination token to load further pages on demand, and every bundle in the result set is reachable in the view with none silently dropped

### Requirement: [TUI-009] Surface ECR-present bundles absent from the Metadata_Index

THE Artifact_TUI SHALL surface, in its all-bundles view, every image present in an artifact repository in ECR that has no corresponding Metadata_Index record, and SHALL identify each such unindexed entry by what it is rather than by a raw image tag.

For each artifact repository it already enumerates to derive project identifiers, THE Artifact_TUI SHALL read the repository's image references via allowlisted image reads (`ecr:DescribeImages`) and compute the set of image digests that match no `manifestDigest` among the Metadata_Index records for the corresponding project. For each such unindexed image, THE Artifact_TUI SHALL read its OCI manifest via the allowlisted `ecr:BatchGetImage` and CLASSIFY it from manifest content — the top-level `mediaType`, the `config.mediaType`, the top-level `artifactType`, and (for grouping) the `subject` digest — into one of:
1. a Notation **signature**;
2. a **WORM_Receipt** referrer;
3. a **resource-reference** referrer (labelled `resource-ref` — the OSCAL `Resource` envelope minted for every bundle as its archived-location reference; the wire `artifactType` is type-neutral `…artifact.resource.v1`, "resource" not "oscal");
4. an OCI image **index**; or
5. an **unvalidated bundle** (a producer Artifact_Bundle that was pushed but did not complete the validate→sign→archive→update-metadata→mint-tag pipeline — i.e. no pipeline-internal media type matched and no referrer `artifactType` is present).

THE Artifact_TUI SHALL present each unindexed entry labelled with its classification, while still surfacing its tag and digest, rather than omitting it or showing only its raw tag. The resource-reference referrer carries the oci-empty `config.mediaType`, so it SHALL be distinguished by its top-level `artifactType` (as the WORM_Receipt referrer already is) and SHALL NOT be mis-bucketed as an image **index**. The classification SHALL reuse the same pipeline-internal media-type rule the ingestion pipeline applies (notary-signature / oci-empty / image-index) and SHALL draw every referrer `artifactType` token from a single source of truth shared with the pipeline's own mint sites (drift-guarded by test), so the TUI's identification cannot drift from the pipeline's.

THE Artifact_TUI SHALL render the signing and archive status columns for an unindexed entry by its classification, so the columns distinguish an expected state from a problem rather than marking every unindexed entry identically:
1. for a pipeline-internal artifact (a signature, WORM_Receipt, resource-reference referrer, or image index — never themselves signed or archived as evidence) the signing and archive columns SHALL read *not applicable*;
2. for an unvalidated bundle or an unclassified entry (which has no Metadata_Index record and therefore no determinable pipeline status) the columns SHALL read *unknown*.

THE Artifact_TUI SHALL NOT label an unindexed entry's signing column with a determinate status (such as *signed* or *unsigned*) that it has not read from a Metadata_Index record. THE Artifact_TUI SHALL NOT issue a `dynamodb:Scan` to compute this set, SHALL NOT introduce any AWS operation outside the read allowlist, and SHALL NOT consume, validate, re-drive, or otherwise alter any pipeline state to classify or surface an unindexed entry. All values surfaced for an unindexed entry (classification label, repository name, image tag, digest) SHALL pass through the terminal-render-safety rule (see *Terminal-render safety for untrusted stored values*) before display. WHEN the manifest read for an unindexed image fails (transport error, access denial, or a delete race), THE Artifact_TUI SHALL present that entry as an unclassified unindexed artifact and remain interactive rather than terminating the session or blanking the list.

#### Scenario: [TUI-009.01] A did-not-land bundle is classified as an unvalidated bundle

- **WHEN** an operator opens the all-bundles view and an artifact repository contains an image whose digest matches no Metadata_Index `manifestDigest` for that project, and whose manifest matches no pipeline-internal media type and carries no referrer `artifactType` (a producer bundle that failed validation and never reached `update_metadata`)
- **THEN** the Artifact_TUI lists that image as a distinct entry classified as an `unvalidated bundle`, showing its tag and digest, so the operator can see a producer bundle exists in ECR but did not complete the pipeline

#### Scenario: [TUI-009.02] A pipeline-internal artifact is identified by what it is

- **WHEN** the all-bundles view includes an unindexed image that is the pipeline's own artifact — a Notation signature (`config.mediaType = application/vnd.cncf.notary.signature`), a WORM_Receipt referrer (top-level `artifactType = application/vnd.cairnproofs.artifact.worm-receipt.v1`), or an OCI image index (`mediaType = application/vnd.oci.image.index.v1+json`)
- **THEN** the Artifact_TUI labels the entry `signature`, `worm-receipt`, or `index` respectively — never an empty cell or a bare `sha256-…` tag — so the operator can distinguish an expected pipeline artifact from a did-not-land bundle

#### Scenario: [TUI-009.03] Resource-reference referrer is identified by artifactType, not mis-bucketed as index

- **WHEN** the all-bundles view includes an unindexed image that is a resource-reference referrer (`artifactType = application/vnd.cairnproofs.artifact.resource.v1`) carrying the oci-empty `config.mediaType`
- **THEN** the Artifact_TUI labels the entry `resource-ref` by its top-level `artifactType` — NOT `index` — so the operator sees what the artifact is rather than a mislabel

#### Scenario: [TUI-009.04] Signing and archive columns reflect classification, not a blanket marker

- **WHEN** the all-bundles view shows a pipeline-internal artifact (a `signature`, `worm-receipt`, `resource-ref`, or `index`) alongside an `unvalidated bundle` entry
- **THEN** the pipeline-internal artifact's signing and archive columns read *not applicable* (it is never signed or archived as evidence — an expected state), while the `unvalidated bundle` (and any `unclassified`) entry's signing and archive columns read *unknown* (no Metadata_Index record exists to determine its status), and no unindexed entry's signing column is labelled with a determinate *signed*/*unsigned* status that was not read from a Metadata_Index record

#### Scenario: [TUI-009.05] Classification rides only sanctioned read paths

- **WHEN** the Artifact_TUI classifies the unindexed images for the all-bundles view
- **THEN** it derives the set from per-repository image reads (`ecr:DescribeImages`) and per-image manifest reads (`ecr:BatchGetImage`) and per-project Metadata_Index key-condition queries only, issuing no `dynamodb:Scan` and no mutating or message-consuming operation

#### Scenario: [TUI-009.06] A manifest-read failure degrades to an unclassified label

- **WHEN** the manifest read (`ecr:BatchGetImage`) for an unindexed image raises an AWS client or transport error, or the image was deleted between enumeration and the read
- **THEN** the Artifact_TUI presents that entry as an unclassified unindexed artifact (still showing its tag/digest), keeps the session interactive, and does not blank the list or raise

#### Scenario: [TUI-009.07] Classification label renders as inert text

- **WHEN** an unindexed entry's classification is derived from manifest fields and any surfaced value (tag, digest, label) would contain ANSI/OSC/DCS escape sequences or C0/C1 control bytes
- **THEN** the Artifact_TUI renders the entry as inert, visible text with the control and escape bytes stripped, and the operator's terminal state is not altered

#### Scenario: [TUI-009.08] Validated bundles remain sourced from the Metadata_Index

- **WHEN** a bundle has a Metadata_Index record (it completed the pipeline)
- **THEN** the Artifact_TUI continues to render it from its Metadata_Index record with its stored `artifactKind` (the artifact-kind), it is not re-classified by a manifest read, and it is not duplicated as an unindexed entry

### Requirement: [TUI-010] Live watch refresh

THE Artifact_TUI SHALL provide a watch mode that re-runs the active view's read operations on a recurring interval, so an operator can observe a bundle advancing through pipeline stages and DLQ depth changing over time. Watch mode SHALL use only the read allowlist and SHALL NOT subscribe to the EventBridge bus or any pipeline-internal event source. WHEN an operator toggles watch mode or invokes an on-demand refresh, THE Artifact_TUI SHALL surface an operator-visible indication of the action:
1. toggling watch SHALL display the resulting watch state — enabled (including the active interval) or disabled;
2. an on-demand refresh SHALL display that the refresh was invoked, and a later indication when it completes.

This feedback affordance SHALL NOT alter the set of read operations issued, the watch interval, or the interval clamp; it adds only the visible indication.

#### Scenario: [TUI-010.01] Watch mode refreshes the active view from reads alone

- **WHEN** an operator enables watch mode while a bundle is mid-pipeline
- **THEN** the TUI periodically re-reads the Metadata_Index row and DLQ depths and updates the lifecycle and health display, using only allowlisted read calls and without tapping the event bus

#### Scenario: [TUI-010.02] Toggling watch surfaces the resulting state

- **WHEN** an operator presses the watch-toggle hotkey
- **THEN** the Artifact_TUI displays a visible indication of the resulting watch state — enabled (including the active refresh interval) or disabled — and the watch read cadence and interval clamp are unchanged by the indication

#### Scenario: [TUI-010.03] On-demand refresh surfaces invocation and completion

- **WHEN** an operator presses the refresh hotkey
- **THEN** the Artifact_TUI displays a visible indication that the refresh was invoked and, when the refresh read workers complete, a subsequent visible indication of completion, while issuing only the same allowlisted reads the active view already performs

### Requirement: [TUI-011] Project-scoped bundle listing via operator-supplied project ID

THE Artifact_TUI SHALL allow an operator to list the bundles of a project they name directly, by accepting a project identifier as input and issuing the project-scoped Metadata_Index query (`query_by_project`) for that identifier. This listing SHALL NOT depend on the project having a live ECR repository: it SHALL surface the project's Metadata_Index records even when ECR enumeration would not discover the project (for example when the project's ECR images were lifecycle-expired or deleted while its Metadata_Index records persist). The operator-supplied identifier and all returned values SHALL pass through the terminal-render-safety rule (see *Terminal-render safety for untrusted stored values*) before display, and the identifier SHALL be used only as a parameterized key-condition value. THE Artifact_TUI SHALL NOT issue an unscoped DynamoDB `Scan` to satisfy this listing. WHEN the project-scoped query raises an AWS client or transport error (for example `ResourceNotFoundException` against a missing or unreadable Metadata_Index table), THE Artifact_TUI SHALL surface a non-fatal, render-safe error indication in the project view and remain interactive rather than terminating the session; this error indication SHALL be distinguishable from an empty (zero-record) result.

#### Scenario: [TUI-011.01] Operator lists a known project directly

- **WHEN** an operator enters a project identifier into the project-listing input
- **THEN** the Artifact_TUI issues the project-scoped Metadata_Index query for that identifier and lists the returned bundles, without enumerating ECR repositories to discover the project

#### Scenario: [TUI-011.02] Project is reachable despite no live ECR repository

- **WHEN** an operator enters a project identifier whose ECR repository has no images (or no longer exists) but whose Metadata_Index records persist
- **THEN** the Artifact_TUI lists those Metadata_Index records, so the bundles are reachable even though the ECR-derived all-bundles list would omit the project

#### Scenario: [TUI-011.03] Project listing rides only sanctioned access paths

- **WHEN** the Artifact_TUI lists a project by operator-supplied identifier whose result set spans more than one Metadata_Index page
- **THEN** it issues only the project-scoped key-condition query (no unscoped `dynamodb:Scan`) and retrieves further pages via the pagination token rather than silently truncating the displayed set

#### Scenario: [TUI-011.04] Sanctioned read error surfaces without terminating the session

- **WHEN** an operator enters a project identifier and the project-scoped Metadata_Index query raises an AWS client or transport error (for example `ResourceNotFoundException` against a missing or unreadable table)
- **THEN** the Artifact_TUI keeps the session running and interactive and surfaces a non-fatal, render-safe error indication in the project view that is distinguishable from a zero-record result, rather than terminating with an unhandled exception

### Requirement: [TUI-012] OCI referrer relationship visibility and navigation

WHEN an operator selects a bundle, THE Artifact_TUI SHALL surface the OCI Referrers attached to that bundle's manifest — discovered by issuing `ecr:ListImageReferrers` against the bundle's repository and manifest digest as the subject (`subjectId`) — so the operator can see the artifacts that refer to the bundle and navigate between them. THE Artifact_TUI SHALL classify each referrer by its `artifactType`, distinguishing at least the Notation signature (`application/vnd.cncf.notary.signature`) and the WORM_Receipt referrer, and SHALL render each referrer's `artifactType`, digest, and tag. All referrer values surfaced (artifactType, digest, tag, and any displayed annotation) SHALL pass through the terminal-render-safety rule (see *Terminal-render safety for untrusted stored values*) before display.

To build or navigate the referrer relationship, THE Artifact_TUI SHALL retrieve referrers via allowlisted reads only — walking the Referrers API pagination token to exhaustion so no referrer is silently truncated — and SHALL NOT issue any mutating or message-consuming operation or any `dynamodb:Scan`. THE Artifact_TUI SHALL allow the operator to navigate from a bundle to its referrer list and from a selected referrer back to the subject bundle it refers to. WHEN a selected bundle has no referrers, or the Referrers read raises an AWS client or transport error (for example the caller's principal lacks `ecr:ListImageReferrers`), THE Artifact_TUI SHALL surface a non-fatal, render-safe indication ("no referrers" or "referrers unavailable") in the detail view and remain interactive rather than terminating the session; this indication SHALL be distinguishable from a populated referrer list. The referrer view SHALL be fetched lazily — only for the selected bundle — consistent with the lazy per-bundle detail reads, and SHALL NOT perform live cryptographic re-verification of any referrer (signature verification remains the manually-run `cairn verify` invocation).

#### Scenario: [TUI-012.01] Selecting a bundle surfaces its signature and WORM_Receipt referrers

- **WHEN** an operator selects a bundle whose manifest has a Notation signature referrer and a WORM_Receipt referrer attached in ECR
- **THEN** the Artifact_TUI issues `ecr:ListImageReferrers` against the bundle's manifest digest as subject, lists each referrer classified by `artifactType` (the Notation signature and the WORM_Receipt) with its digest and tag, and renders every referrer value through the render-safety rule

#### Scenario: [TUI-012.02] Referrer fetch rides only sanctioned read paths

- **WHEN** the Artifact_TUI lists or navigates a bundle's referrers, including a manifest whose referrer set spans more than one Referrers API page
- **THEN** it issues only `ecr:ListImageReferrers` (walking the pagination token to exhaustion) and no mutating, message-consuming, ECR tag/delete, or `dynamodb:Scan` operation, and every referrer is reachable with none silently dropped

#### Scenario: [TUI-012.03] Operator navigates from a bundle to a referrer and back to the subject

- **WHEN** an operator opens a bundle's referrer list, selects a referrer, and then returns
- **THEN** the Artifact_TUI shows the selected referrer's identity (artifactType, digest, tag) and the subject bundle it refers back to, and the operator can navigate back to the bundle without leaving the detail flow

#### Scenario: [TUI-012.04] Hostile referrer field renders as inert text

- **WHEN** a referrer's `artifactType`, tag, or annotation contains ANSI/OSC/DCS escape sequences or C0/C1 control bytes
- **THEN** the Artifact_TUI renders the referrer entry as inert, visible text with the control and escape bytes stripped, and the operator's terminal state is not altered by the referrer content

#### Scenario: [TUI-012.05] Absent or unreadable referrers degrade without terminating the session

- **WHEN** a selected bundle has no referrers attached, or the `ecr:ListImageReferrers` read raises an AWS client or transport error (for example the caller's principal lacks the permission)
- **THEN** the Artifact_TUI surfaces a non-fatal, render-safe "no referrers" or "referrers unavailable" indication that is distinguishable from a populated list, keeps the session interactive, and issues no mutating or message-consuming call

### Requirement: [TUI-013] Referrer grouping under the primary artifact in the all-bundles list

THE Artifact_TUI SHALL present, in its all-bundles list, each pipeline-minted OCI referrer (a Notation signature, a WORM_Receipt, or a resource-reference referrer) as a collapsible CHILD of the primary artifact it refers to rather than as a flat sibling row, grouping a referrer under a listed primary WHEN the referrer's manifest `subject` digest equals that primary's `manifestDigest`.

THE Artifact_TUI SHALL derive the `subject` digest from the same per-image OCI manifest it already reads to classify the unindexed entry (see *Surface ECR-present bundles absent from the Metadata_Index*), issuing no additional AWS operation and remaining within the read-only allowlist. To build this grouping it SHALL NOT issue a per-bundle `ecr:ListImageReferrers` from the list path, and SHALL NOT issue a `dynamodb:Scan`.

THE Artifact_TUI SHALL allow the operator to expand and collapse a primary's referrer children. A primary artifact with no referrers SHALL render as a normal leaf row with no empty child group and no expand affordance. A referrer whose `subject` digest matches no listed primary (an orphaned/unresolved subject — for example a primary whose ECR image was lifecycle-expired while the referrer persists) SHALL be surfaced in a clearly-labelled "unattached" group rather than silently dropped. Every value surfaced for a grouped or unattached referrer (classification label, repository name, tag, digest) SHALL pass through the terminal-render-safety rule (see *Terminal-render safety for untrusted stored values*) before display.

#### Scenario: [TUI-013.01] A bundle's referrers render as collapsible children of the primary

- **WHEN** an operator opens the all-bundles view and an artifact repository holds a primary bundle together with its Notation signature, WORM_Receipt, and resource-reference referrers, each whose manifest `subject` digest equals the primary's `manifestDigest`
- **THEN** the Artifact_TUI renders the three referrers as collapsible children nested under the primary's row rather than as flat sibling rows, deriving each child's `subject` digest from the manifest already read to classify it and issuing no additional AWS operation

#### Scenario: [TUI-013.02] A referrer-less primary renders as a leaf with no expand affordance

- **WHEN** the all-bundles view includes a primary artifact that has no referrers grouped under it
- **THEN** the Artifact_TUI renders it as a normal leaf row with no empty child group and no expand/collapse affordance

#### Scenario: [TUI-013.03] An orphaned referrer surfaces in the unattached group

- **WHEN** the all-bundles view includes a referrer whose manifest `subject` digest matches no listed primary (for example the subject bundle's ECR image was lifecycle-expired while the referrer persists)
- **THEN** the Artifact_TUI surfaces that referrer in a clearly-labelled "unattached" group, showing its classification, tag, and digest, rather than dropping it silently

#### Scenario: [TUI-013.04] Grouping rides only sanctioned read paths

- **WHEN** the Artifact_TUI builds the grouped all-bundles list, including resolving each referrer's subject
- **THEN** it derives the grouping from the per-image manifest reads (`ecr:BatchGetImage`) and per-project Metadata_Index key-condition queries it already issues, and issues no per-bundle `ecr:ListImageReferrers`, no `dynamodb:Scan`, and no mutating or message-consuming operation

#### Scenario: [TUI-013.05] Grouped referrer fields render as inert text

- **WHEN** a grouped or unattached referrer's classification label, tag, or digest would contain ANSI/OSC/DCS escape sequences or C0/C1 control bytes
- **THEN** the Artifact_TUI renders the nested entry as inert, visible text with the control and escape bytes stripped, and the operator's terminal state is not altered

### Requirement: [TUI-014] Image-index entries are hidden by default behind an operator toggle

OCI image-index entries (the `index` classification — registry-machinery manifests that the Cairn pipeline does not itself mint, written as a side-effect of the signing/push tooling) carry no evidence signal in the operator's all-bundles view. THE Artifact_TUI SHALL therefore EXCLUDE image-index–classified unindexed entries from the rendered all-bundles list BY DEFAULT, and SHALL provide an operator toggle that reveals them. THE Artifact_TUI SHALL surface a persistent, render-safe indication of how many image-index entries are currently hidden (so the omission is never silent) and how to reveal them; when none are hidden the indication SHALL be empty. The toggle SHALL re-render from the already-fetched record set without issuing any additional AWS read, and the operator's show/hide choice SHALL persist across a watch-tick rebuild. Hiding is a presentation filter only: it SHALL NOT drop image-index entries from the underlying fetched set, alter any other classification, or affect the grouping of referrer children under their primary.

#### Scenario: [TUI-014.01] Image-index entries are hidden by default with a count indication

- **WHEN** an operator opens the all-bundles view and the fetched set includes one or more image-index–classified entries
- **THEN** those entries are excluded from the rendered list by default, and a persistent render-safe indication reports how many are hidden and that pressing the toggle reveals them

#### Scenario: [TUI-014.02] Toggle reveals and re-hides image-index entries without a re-fetch

- **WHEN** the operator activates the show/hide-indexes toggle
- **THEN** the Artifact_TUI re-renders the list including (then again excluding) the image-index entries from the already-fetched record set, issuing no additional AWS read, and surfaces the resulting state

#### Scenario: [TUI-014.03] Hidden-index choice persists across a watch-tick rebuild and affects nothing else

- **WHEN** a watch-tick (or on-demand refresh) rebuilds the all-bundles list while indexes are hidden
- **THEN** the image-index entries remain hidden (the choice persists), non-index entries and the nesting of referrer children under their primaries are unaffected, and no image-index entry is dropped from the underlying fetched set

### Requirement: [TUI-015] Create-date populated for every all-bundles row

THE Artifact_TUI SHALL render a value in the create-date column for EVERY row of the all-bundles list — Metadata_Index–backed primaries, unindexed entries (unvalidated bundles and unclassified entries), pipeline-minted referrer children, unattached orphan referrers, and any synthetic group-header row. No rendered row SHALL emit fewer cells than the table's column count.

For a Metadata_Index–backed row THE Artifact_TUI SHALL display the stored `createdAt` (set by the ingestion pipeline from the AWS `ecrPushedAt` event field). For an unindexed entry — including a pipeline-minted referrer surfaced as a child or in the unattached group — THE Artifact_TUI SHALL source the create-date from the AWS-set ECR push timestamp (`imagePushedAt`) carried in the `ecr:DescribeImages` response it already issues, and SHALL NOT derive the displayed create-date from any producer-supplied OCI manifest annotation (such as a `created` / `org.opencontainers.image.created` annotation). The displayed create-date then carries the same not-a-producer-claim trust property as the indexed rows. THE Artifact_TUI SHALL NOT issue any AWS operation outside the existing read allowlist (no additional `BatchGetImage`, no `dynamodb:Scan`, no mutating call) to obtain the create-date; `imagePushedAt` is already present in the response it reads.

WHEN the AWS push timestamp is absent for a row (for example a delete race or a `DescribeImages` page that omits `imagePushedAt`), THE Artifact_TUI SHALL render the same placeholder it uses for any absent value and remain interactive. A synthetic group-header row, which backs no record, SHALL render the placeholder in its create-date column so the row is column-complete and the table stays aligned. The create-date value SHALL pass through the terminal-render-safety rule (see *Terminal-render safety for untrusted stored values*) like every other cell.

#### Scenario: [TUI-015.01] Indexed primary shows its stored create-date

- **WHEN** the all-bundles list includes a Metadata_Index–backed primary whose record carries a `createdAt`
- **THEN** the Artifact_TUI renders that `createdAt` value in the primary's create-date column

#### Scenario: [TUI-015.02] Unindexed referrer child shows the AWS push timestamp

- **WHEN** the all-bundles list includes a pipeline-minted referrer (signature, WORM_Receipt, or resource-reference) nested as a child of its primary, and that referrer has no Metadata_Index record
- **THEN** the Artifact_TUI renders the child's create-date column from the AWS `imagePushedAt` of the same image's `ecr:DescribeImages` entry, not a placeholder and not a value taken from the manifest's own annotations

#### Scenario: [TUI-015.03] Unattached orphan referrer shows the AWS push timestamp

- **WHEN** the all-bundles list includes a referrer whose subject digest matches no listed primary and which is surfaced in the unattached group
- **THEN** the Artifact_TUI renders its create-date column from the AWS `imagePushedAt`, and the orphan is not dropped or left blank

#### Scenario: [TUI-015.04] Synthetic group-header is column-complete

- **WHEN** the all-bundles list renders the synthetic "unattached" group-header row, which backs no record
- **THEN** the Artifact_TUI emits a create-date cell containing the placeholder so the header row carries the full column count and the rows beneath it stay column-aligned

#### Scenario: [TUI-015.05] Producer-supplied manifest annotation never becomes the displayed create-date

- **WHEN** an unindexed entry's OCI manifest carries a producer-supplied `created` (or `org.opencontainers.image.created`) annotation that differs from the AWS `imagePushedAt`
- **THEN** the Artifact_TUI displays the AWS `imagePushedAt` value in the create-date column and never the producer-supplied annotation value

#### Scenario: [TUI-015.06] Create-date is obtained without any extra AWS read

- **WHEN** the Artifact_TUI builds or refreshes the all-bundles list and populates the create-date column for unindexed rows
- **THEN** it derives every non-indexed create-date from the `imagePushedAt` already present in the `ecr:DescribeImages` response, issuing no additional `ecr:BatchGetImage`, no `dynamodb:Scan`, and no other operation outside the read-only allowlist


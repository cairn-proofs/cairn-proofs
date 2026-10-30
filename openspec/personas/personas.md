# Cairn Proofs — User Personas

> Canonical reference for the human actors the [capabilities](../specs/) specify. Lives under `openspec/` (the governed tree) so it stays current with the system it describes, but is a **living document edited directly** — not through `/opsx`. OpenSpec has no persona-delta machinery, and forcing personas through the requirement/scenario schema would contort them. This is a deliberate, narrow carve-out to the "no direct edits under `openspec/`" rule.
>
> A sibling of `specs/` and `changes/`, this file is inert to `openspec validate --specs` (it carries no `### Requirement:` blocks). A real access path grounds each persona: an IAM-scoped operation, a `cairn` CLI verb, or a deployment-mode responsibility. When the specs and a persona disagree on behavior, the specs win — update this file, don't bend the spec.

Cairn is tamper-evident storage for audit artifacts (NIST 800-53 AU family, ISO 27002 §5.x). OSCAL is a first-class native format. ALCOA+ is the governing data-integrity thesis.

## The four actor axes

Per [ADR-0019](../../docs/adr/0019-actor-model-four-axes-integrity-independent-of-operator.md), every actor sits on one of four orthogonal axes. The personas below map to three of them:

1. **Production** — authors and pushes artifacts (Priya, Sam, Nadia). It MAY run anywhere — multi-cloud, on-prem, or hybrid. Any ORAS or Cairn SDK/CLI client is a producer.
2. **Consumption** — reads and *evaluates* artifacts (Dana and Marcus, plus Quinn's narrow sign-off). Also location-agnostic.
3. **Operations** — deploys and runs the store (Alex, Robin). The operator is **untrusted for integrity by construction**. It keeps the system available but cannot forge or tamper with an artifact undetectably. The Manifest_Digest, signature, and WORM_Receipt anchor integrity. Who operates is a delivery choice, not a trust decision.
4. **Custody** — the customer org that *owns the AWS account* the artifacts live in. This is an ownership axis, not a persona. In Customer_Hosted the custodian and the operator MAY be the same org (Alex's), but they are distinct roles. Custody never leaves the customer's AWS accounts.

The three persona bands below correspond to Consumption (**Stakeholders**), Production (**Producers**), and Operations (**Operators**):

- **Stakeholders** who care that the artifacts exist and hold up — GRC owners and external auditors. They live mostly on the read/query path. One exception signs. The **attesting reviewer** (an internal quality/compliance approver) adds a human sign-off before artifacts go to an external auditor. So they hold a narrow sign capability the read-only auditors deliberately do not.
- **Producers** who feed artifacts in — DevSecOps engineers, GRC engineers, and standards authors who publish authoritative catalogs/profiles. They live on the push path and the bundle contract. Standards authors push via a technical proxy, near-term.
- **Operators** who stand up and run the store. The deployment owner is an in-house platform team. The service operator is an MSP / vendor-managed offering running customer-custodied deployments.

## Persona ↔ system mapping

| Persona | Axis | Primary IAM scope | Cairn surface | Capabilities they live in |
|---|---|---|---|---|
| [Dana — GRC Program Manager](#1-dana--grc-program-manager-the-sponsor) | Consumption | Read-only auditor (account scope) | `cairn query`, `cairn verify` | metadata-index, oscal-interop, retention-lifecycle, worm-archive |
| [Marcus — External Auditor / Assessor](#2-marcus--external-auditor--independent-assessor) | Consumption | Read-only auditor (project scope) | `cairn pull`, `cairn verify`, `cairn query` | access-control, signing, worm-archive, metadata-index |
| [Quinn — Attesting Reviewer (internal sign-off)](#6-quinn--attesting-reviewer-internal-sign-off) | Consumption (+ sign-off) | Sign-capable on the `{appName}-artifact-reviewer-signoff` profile (project scope); read elsewhere | `cairn sign`, `cairn verify`, `cairn pull` | signing, artifact-verifiability, worm-archive, metadata-index |
| [Priya — DevSecOps / Artifact Producer](#3-priya--devsecops--artifact-producer-engineer) | Production | Artifact_Producer (project-scoped push) | `cairn push` from CI/CD | artifact-ingestion, signing, processing-pipeline |
| [Sam — GRC Engineer](#4-sam--grc-engineer) | Production | Artifact_Producer (OSCAL bundles) | `cairn push`, `cairn pull`, `cairn query` | oscal-interop, artifact-ingestion, metadata-index |
| [Nadia — Standards Author / Framework Maintainer](#5-nadia--standards-author--framework-maintainer) | Production | Content authority (proxied Artifact_Producer, near-term) | `cairn push` of catalogs/profiles + `cairn alias`, via proxy | oscal-interop, artifact-ingestion, metadata-index |
| [Alex — Platform / Security Engineer (deployment owner)](#7-alex--platform--security-engineer-deployment-owner) | Operations (+ in-house custodian) | Operator/administrator | CDK deploy, AWS console, alarms | encryption, observability, retention-lifecycle, platform-constraints |
| [Robin — Service Operator / SRE (MSP/vendor)](#8-robin--service-operator--sre-mspvendor-runs-a-fleet) | Operations | Operator across a fleet of customer-custodied deployments | onboarding automation, IAM/KMS | deployment-modes, access-control, runtime-slos |

---

## 1. Dana — GRC Program Manager (the sponsor)

**Axis.** Consumption.

**Archetype.** Owns the compliance program at a mid-to-large regulated enterprise (FinServ / health / gov-adjacent). Answers to the CISO, the audit committee, and ultimately external regulators. Champions and funds Cairn — the "why this exists" persona.

**Context.** Lives in control frameworks, not AWS. Comfortable with NIST 800-53, ISO 27002, and SOC 2. Increasingly comfortable too with AI-governance obligations (e.g., GDPR Article 22's "meaningful explanation of automated decision logic"). These obligations require keeping the *whole* chain of automated-workflow artifacts, not just the final attestation.

**Goals.**
- Walk into an audit able to produce signed, timestamped, immutable artifacts for any control on demand — "show me everything backing AU-6 this quarter."
- Prove chain of custody and attributability without manually curating folders.
- Set retention once (up to 99 years) and trust the system to honor it.

**How they use Cairn.** Runs control-coverage queries against the Metadata_Index (the `{catalogOrProfileUuid}#{controlId}` control-lookup path), spot-checks that bundles verify, and reviews retention/WORM posture. Rarely touches the push side.

**What Cairn solves for them.** Replaces shared drives and screenshot folders with an ALCOA+ store. The store is attributable (AWS-verified push principal), contemporaneous (`ecrPushedAt` set by AWS), original (SHA-256 content-addressing), and enduring (WORM + lifecycle tiering). No silent-degrade on OSCAL.

**Success looks like.** Audit prep drops from weeks to hours. Zero "we can't find / can't prove" findings. Key metric: control-coverage completeness and audit-prep time (the weeks-to-hours drop above).

**Frustrations Cairn must not reintroduce.** These are the frustrations to avoid:
- Artifacts that someone can edit after the fact.
- Gaps where a producer only asserts an identity rather than proving it.
- Having to learn ECR/IAM to answer an auditor.

---

## 2. Marcus — External Auditor / Independent Assessor

**Axis.** Consumption. (May sit anywhere — engagements run from the assessor's own environment, on-prem or cloud, via the Cairn CLI/SDK.)

**Archetype.** Third-party assessor or regulator-side reviewer granted scoped, time-boxed read access during an engagement. Skeptical by training — assumes nothing until it verifies.

**Context.** Bound to the **read-only auditor role**, often scoped to a single `{tenant-id}/{project-id}`. Must be able to do their job without any push or sign capability, and without custom per-engagement policy authoring.

**Goals.**
- Independently verify cryptographic integrity and signature status — trust the math, not the vendor's word.
- Sample artifacts by control, date, or assessment ID and confirm they're complete and untampered.
- Confirm the store itself enforces immutability (Object Lock / deny-delete).

**How they use Cairn.** `cairn pull` to retrieve, `cairn verify` to check Notation/AWS Signer signatures and the `WORM_Receipt`, `cairn query` to navigate by control. Confirms project-scoped isolation actually holds (cannot see other projects/tenants).

**What Cairn solves for them.** A verifiable chain they can validate end-to-end: signed manifests, timestamped signatures, content-addressed blobs, CloudTrail access logs. Read-only by construction — they cannot alter what they assess.

**Success looks like.** Every sampled artifact verifies. Isolation holds under probing. Key metric: signature-verification pass rate, zero cross-scope leakage.

**Frustrations Cairn must not reintroduce.** These are the frustrations to avoid:
- Exports they cannot independently verify.
- Over-broad access that compromises their independence.
- Having to trust an operator's assertion of immutability.

---

## 3. Priya — DevSecOps / Artifact Producer Engineer

**Axis.** Production. (Pipelines may run in any AWS account, another cloud, or on-prem. Off-AWS producers federate in via IAM Roles Anywhere or OIDC web-identity.)

**Archetype.** Platform/DevSecOps engineer who builds the automated pipelines that *emit* artifacts — vuln scans, compliance scans, config snapshots, attestations, log extracts. The pipelines push them into Cairn from CI/CD.

**Context.** Holds an **Artifact_Producer** principal scoped to a specific `{tenant-id}/{project-id}` ECR namespace. Thinks in pipelines, exit codes, and artifact contracts, not compliance frameworks.

**Goals.**
- Push an artifact as a reliable, idempotent CI step that fails loud, not silent.
- Conform to the bundle contract (OCI manifest, required annotations, Config_Blob) without re-reading the spec each time.
- Never be the reason an audit has a gap — fire-and-forget that actually lands.

**How they use Cairn.** `cairn push` of an Artifact_Bundle at the end of a pipeline run. Relies on the validate → sign → archive → metadata pipeline to do the rest. Watches for permanent-failure surfacing (validation failures are terminal, not retried into the void).

**What Cairn solves for them.** A clear producer contract with verified identity captured automatically — they don't manage signing keys or timestamps. Tag immutability and content-addressing mean a re-push can't silently overwrite.

**Success looks like.** Push is a one-liner in the pipeline. Failures are actionable at the CLI. Key metric: push success rate, mean-time-to-diagnose a rejected bundle.

**Frustrations Cairn must not reintroduce.** These are the frustrations to avoid:
- Opaque rejections ("invalid bundle" with no reason).
- Silent degradation when OSCAL or annotations are off.
- Needing to understand the whole archive pipeline to push one artifact.

---

## 4. Sam — GRC Engineer

**Axis.** Production.

**Archetype.** A GRC engineer who models his firm's systems and controls in OSCAL. He also integrates externally-produced OSCAL into the firm's compliance workflow. He writes code, not spreadsheets. He generates OSCAL from engineering inputs. He merges authoritative content from outside producers into a coherent, queryable whole.

**Context.** Sits at the engineering seam of the compliance pipeline, between Priya (DevSecOps) and Dana (GRC). He takes Priya's infrastructure-as-code artifacts and generates the OSCAL that documents the system (SSPs, component definitions, assessment results). He also merges in content from external producers — vendor Component Definitions, standards-developer Control Catalogs and profiles (Nadia's output). **His output artifacts are Dana's inputs.** The control-lookup query path Dana and Marcus depend on is only as good as Sam's modeling and extraction.

**Goals.**
- Generate OSCAL from engineering inputs (Priya's IaC) and merge external OSCAL (vendor component definitions, authored catalogs/profiles) into his firm's bundles. Then push them so the system extracts every control reference into the queryable Metadata_Index + Controls relationship table, with none dropped.
- Catch schema drift early. Cairn cross-checks the producer-declared OSCAL version against the document's embedded version. It validates the bundle-type / Config_Blob / ECR-path triple.
- Trust that OSCAL extraction failure is a *hard* failure, not a degrade.

**How they use Cairn.** `cairn pull` / `cairn query` retrieves the external and upstream OSCAL he integrates (vendor component definitions, Nadia's catalogs/profiles, Priya's artifacts). `cairn push` sends the OSCAL bundles he produces. `cairn query` confirms controls resolved as expected (`{catalogOrProfileUuid}#{controlId}` keys). He consumes the generated artifact references.

**What Cairn solves for them.** OSCAL is a native, non-negotiable format with no silent-degrade path. Cairn enforces extraction fidelity rather than treating it as best-effort. So the models he builds and the content he merges stay faithful end to end.

**Success looks like.** Every control in a pushed SSP/assessment is queryable. Cairn rejects version mismatches at ingest, not at audit. Key metric: extraction completeness, schema-cross-check catch rate.

**Frustrations Cairn must not reintroduce.** These are the frustrations to avoid:
- OSCAL flattened to opaque blobs.
- Token-id name clashes between catalogs and profiles.
- Extraction that "mostly" works and leaves silent holes.

---

## 5. Nadia — Standards Author / Framework Maintainer

**Axis.** Production (content authority, pushes via a technical proxy, near-term).

**Archetype.** A security/compliance domain expert who authors and maintains an authoritative control framework or benchmark. Think a NIST 800-53 maintainer, a PCI-DSS contributor, or a CIS Benchmark editor. Deep on controls, light on tooling. She works in spreadsheets, Word, and PDF and publishes "digital paper" today. She recognizes that paper-bound standards are the root of the interoperability tax Cairn exists to remove. She wants out — but is not a coder and may not know git.

**Context.** Owns the *content authority* for a framework: what the controls say, which revision is current, how versions supersede. Near-term she has already-authored, authoritative OSCAL catalogs and profiles (or can have them produced). She needs them published in Cairn as first-class, addressable reference artifacts **now**. The handles stay stable and human-meaningful, so consumers track "the current NIST 800-53 catalog" or pin a specific revision. She is not, today, an AWS or CLI operator. Her near-term publishing runs through a technical proxy (Sam/Priya). Nadia stays the content authority who decides what a channel like `latest` points at.

**Goals.**
- Publish existing authoritative OSCAL catalogs and profiles into Cairn as immutable, signed reference artifacts. A stable handle addresses each one — a `latest` channel that follows the current revision, plus named version aliases (e.g. `nist-800-53-r5`). Consumers track neither digests nor revision tags.
- Move a channel (`latest` → a new revision) when she releases an update, without disturbing the immutable identity of any prior revision.
- *(Forward-looking, not yet explored)* Transform "digital paper" (spreadsheets, Word, PDF) into OSCAL through low-friction, phased, non-coder tooling. Eventually publish self-service without a technical proxy.

**How they use Cairn.** Near-term, via a technical proxy. `cairn push` sends authored OSCAL catalogs/profiles (reference artifacts). `cairn alias set/move` publishes and advances channel/version handles. `cairn query`/`pull` confirms what a handle resolves to. Direct, self-service use is future scope.

**What Cairn solves for them.** Their framework stops being a PDF. It becomes a signed, immutable, content-addressed artifact consumers can verify and track by a stable name. This is the first step out of digital-paper workflows, before any authoring tooling exists.

**Success looks like.** An authoritative catalog is publishable the day it lands. Consumers can reference `nist-800-53-latest` and pin `nist-800-53-r5`. Advancing `latest` to a new revision is a single, auditable operation. Key near-term metric: time-to-publish an authoritative catalog and the stability of its consumer-facing handle.

**Frustrations Cairn must not reintroduce.** These are the frustrations to avoid:
- Standards trapped in formats no tool can consume.
- Needing to be a coder (or learn git/IAM) to publish a catalog.
- A "current version" handle consumers can't trust or that silently breaks a prior revision's identity.

---

## 6. Quinn — Attesting Reviewer (internal sign-off)

**Axis.** Consumption, plus a narrow attesting sign-off (the only write-shaped capability on the read side).

**Archetype.** An internal quality, compliance, or controls reviewer at the producing organization who **attests** to a bundle before it goes to an external auditor. This is the "second set of eyes" that turns "the pipeline vouches for this" into "a responsible human also vouches for this." Think internal-audit lead, QA/compliance approver, or control owner signing off on a quarter's artifacts. **Distinct from the read-only auditors.** Marcus and Dana can only read and verify. Quinn can additionally *sign*.

**Context.** Holds a **sign-capable** principal scoped to the `{appName}-artifact-reviewer-signoff` AWS Signer profile. IAM separates it from both the read-only auditor role (which cannot sign) and the automated pipeline profile (`{appName}-artifact-pipeline-signing`). The sign grant is the only elevated capability they hold — everywhere else they are a reader. Works in control frameworks and internal-approval workflows, not AWS internals. The signing key never leaves AWS Signer / KMS, so Quinn manages an approval decision, not key material.

**Goals.**
- Add an independent, human attestation to a bundle. An external auditor can verify it alone — "an internal reviewer, not just the automation, stands behind this."
- Sign off using stock tooling, so the resulting attestation is just another Notation signature referrer. It stays index-free verifiable, with no Cairn-proprietary attestation format an auditor has to learn or trust.
- Never gain write/delete reach over the artifacts themselves. Attesting must not mean the ability to alter or remove the attested artifact.

**How they use Cairn.** `cairn sign` attaches a sign-off referrer to a bundle's manifest digest. It is a thin convenience over stock `notation sign` against the `{appName}-artifact-reviewer-signoff` profile. First, `cairn pull` / `cairn verify` review the bundle and confirm the pipeline signature and `WORM_Receipt`. Their sign-off coexists with the pipeline signature as a second, independently verifiable signature on the same content-addressed artifact.

**What Cairn solves for them.** A first-class multi-signature path. The attestation is a standard OCI signature, not a bolt-on. The producing org can demonstrate dual attestation (automated + human) on a control's artifacts without inventing an out-of-band sign-off record. Every sign-off is verifiable by the same index-free, stock-tooling chain an external auditor already trusts.

**Success looks like.** Bundles destined for an external engagement carry both the pipeline signature and an attesting-reviewer sign-off. `cairn verify` reports `signed` across both profiles. The sign grant never widens into write/delete reach. Key metric: sign-off coverage on auditor-bound artifacts, and zero capability bleed from "can attest" into "can alter."

**Frustrations Cairn must not reintroduce.** These are the frustrations to avoid:
- A bespoke attestation artifact an auditor can't verify with stock Notation.
- A sign capability that quietly also grants the ability to edit or delete artifacts.
- Having to manage signing keys rather than just making an approval decision.

---

## 7. Alex — Platform / Security Engineer (deployment owner)

**Axis.** Operations (and, for an in-house deployment, the org Alex works for is the Custody owner — distinct roles that happen to coincide).

**Archetype.** The engineer who stands up and runs a Cairn deployment inside their organization's own AWS account. Owns the stack, the keys, and the on-call pager. This is the primary in-house operator persona for the MVP slice.

**Context.** Fluent in AWS, CDK, IAM, KMS, CloudWatch. Owns the irreversible decisions: region pinning via `{regionCode}-{stage}`, and the one-way `WORM_Enabled` choice (must be `true` in production, with explicit confirmation required at provisioning). Operates the store but is **untrusted for integrity by construction**. Full deploy/admin rights do not let Alex forge or tamper with an artifact undetectably. The digest, signature, and WORM_Receipt anchor integrity.

**Goals.**
- Deploy reproducibly (`cdk/scripts/deploy.sh`) — Notation layer, Lambda bundle, CloudTrail preflight, `cdk deploy --all` — and bring the pipeline up clean.
- Keep the pipeline healthy. Watch alarms, DLQs, and integrity-mismatch signals. Resolve permanent failures.
- Get encryption, signing, retention, and access scoping right the first time — several choices can't be undone post-provisioning.

**How they use Cairn.** CDK deploy and synth. AWS console / CLI for KMS CMK, AWS Signer profile, IAM role scoping, and retention/lifecycle config. CloudWatch for metrics, alarms, and SNS notifications. DLQ inspection when archive fails.

**What Cairn solves for them.** A bounded, AWS-only architecture (ECR, S3 Object Lock, DynamoDB, Lambda, EventBridge, Signer, KMS) with constraints encoded as spec. The constraints: no internet egress, regional residency, audit-coverage preflight.

**Success looks like.** Green deploy, empty DLQs, alarms quiet, WORM/retention provably correct. Key metric: pipeline availability (99.9% SLO), DLQ depth, integrity-mismatch count = 0.

**Frustrations Cairn must not reintroduce.** These are the frustrations to avoid:
- Irreversible config set by accident (WORM, region) with no guardrail.
- Failures that vanish instead of paging.
- Docker-in-the-toolchain or unverified layer builds.

---

## 8. Robin — Service Operator / SRE (MSP/vendor; runs a fleet)

**Axis.** Operations.

**Archetype.** Runs Cairn as a managed service — an MSP, systems integrator, or vendor-managed offering operating a **fleet** of deployments on customers' behalf. Critically, the deployments Robin operates are **customer-custodied**. Each runs in the customer's own AWS accounts, and the artifacts never move into vendor-owned infrastructure. Robin operates. The customer custodies. (See [ADR-0019](../../docs/adr/0019-actor-model-four-axes-integrity-independent-of-operator.md).)

**Context.** Operates many deployments across a customer's estate, sharded by sector / regime / business unit / stage. May instead operate across several customers, each custodied separately. **Untrusted for integrity by construction**, like any operator. Operational reach does not confer the ability to forge or tamper. Accountable for isolation at two levels (per ADR-0019). The **hard boundary** is the deployment — separate accounts, KMS keys, and operator credentials. For example, a defense workload is a separate deployment from a commercial one. The **soft boundary** within a deployment is the IAM-prefix-scoped path hierarchy `{org-id}/{tenant-id}/{project-id}`. Cairn defers fleet-wide onboarding and cross-account producer push — see backlog `customer-hosted-multi-account-topology`.

**Goals.**
- Stand up and onboard a new deployment (or a new tenant within one) fast and safely, with rollback if provisioning fails partway.
- Set up cross-account producer push via repository policies with **explicit** principal allowlisting — never account/principal wildcards.
- Honor Customer_CMK grants for tenants who bring their own key, and hold SLOs across the whole fleet.

**How they use Cairn.** Onboarding automation. ECR repository policy and IAM scope management. KMS grant setup for Customer_CMK. Runtime SLO and multi-region posture monitoring. Verifies tenant A cannot read tenant B at every layer — and that no operator action can compromise artifact integrity.

**What Cairn solves for them.** Isolation specified and testable at the data layer. Region pinning per deployment. An onboarding contract that's automatable rather than bespoke per customer. Custody stays with the customer and integrity stays independent of the operator.

**Success looks like.** New deployments/tenants live quickly with zero cross-tenant leakage. SLOs held fleet-wide. Key metric: onboarding time, isolation-probe pass rate, per-tenant SLO attainment.

**Frustrations Cairn must not reintroduce.** These are the frustrations to avoid:
- Wildcard cross-account trust.
- Isolation that depends on a single IAM statement being correct.
- Manual, non-rollback-able onboarding.
- Any path by which operating a deployment could alter the artifacts it holds.

---

## Adjacent stakeholders (not direct users)

- **The regulator / inspector.** The ultimate downstream audience. Never touches the system directly — receives verifiable artifacts *via* Dana and Marcus. The whole ALCOA+ thesis exists to satisfy this reader at one remove.
- **The CISO / audit committee.** Consumes Dana's reporting. Cares about residual risk and audit outcomes, not the console.

## Notes on scope

The MVP slice (`mvp-evidence-store`) is single-account, one region, `WORM_Enabled=false`, operated in-house. That makes **Alex**, **Priya**, **Sam**, and the read-side of **Dana**/**Marcus** the live personas today. **Robin** (the vendor-managed fleet operator) and the deferred work behind it — multi-account fan-in, Customer_CMK, onboarding automation — are Post-MVP. The specs describe the intended steady state and the persona set should too.

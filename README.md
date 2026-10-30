# Cairn Proofs

**Keep the whole body of artifacts that govern and document your security posture in a form no one can quietly alter, lose, or dispute. An outside auditor can verify it without taking your word for it.**

A compliance program runs on far more than evidence. It runs on a whole body of artifacts:

- the authoritative standards you're held to (control catalogs and profiles),
- the descriptions of your systems against them (system documentation, infrastructure-as-code, system security plans, component definitions),
- the plans and results of assessing them,
- the attestations that sign them off,
- and the evidence backing all of it — scan reports, configuration snapshots, log extracts, screenshots.

Today those artifacts sit scattered across shared drives and folders. Anyone can edit them after the fact. "Who produced this and when" is a claim rather than a fact. Proving no one tampered with them means trusting whoever controls the folder. When an auditor or regulator asks *"how do you know this is the real, unaltered document from that date?"*, "because it's in our system" is not a satisfying answer.

Cairn Proofs is a place to put all of it so the answer becomes **"here is independent cryptographic proof."** On arrival, Cairn seals every artifact — a published standard, a system description, an assessment, a sign-off, or a piece of raw evidence. It stamps each one with a trustworthy timestamp and a verified producer identity. It then stores the artifact so no one can alter or delete it out from under you. Anyone you share it with can verify it is authentic and untouched. They use standard, off-the-shelf tools — without access to your systems and without trusting you. Cairn handles OSCAL, the open standard for machine-readable compliance documents, as a first-class native format.

## What you get

- **Artifacts that no one can silently alter.** Cairn content-seals each artifact. In production, it locks the artifact under write-once storage with a retention period you set (up to 99 years). To alter or delete it is not a matter of policy or permissions. The platform prevents it, and makes any tampering detectable.

- **Trustworthy attribution and timing.** The platform itself records who submitted an artifact, and when — the submitter does not assert it. No one can forge the producer's claimed identity into the record.

- **Independent verification.** An auditor can take a single artifact and verify, on their own, that it is authentic and unaltered. They use standard tooling, their own list of trusted signers, and nothing from your database. The proof travels with the artifact.

- **A second human sign-off when you need it.** Beyond the automated seal, an internal reviewer can add their own attestation to an artifact. They do this before it goes to an external auditor — a verifiable "a responsible person also stands behind this."

- **A living, addressable reference library.** Cairn does not just store authoritative standards and system documents. It publishes them under stable, human-meaningful handles. Consumers can track "the current NIST 800-53 catalog" or pin a specific revision, and each version keeps its own immutable identity.

- **Findability without compromising trust.** You can search your artifacts by control, date, or project to assemble what an audit needs. That search is a convenience layer. The *proof* never depends on it.

- **Your data stays yours.** Cairn runs inside your own cloud account. There is no vendor-held copy. Your team, or a managed-service provider in your account, can operate the system. By design, that operator still cannot forge or alter what it holds.

## The integrity standard it's built to meet

Regulated industries judge data integrity by the **ALCOA+** principles. Cairn aims to make every artifact it holds satisfy them:

| Principle | What it means for your artifacts |
|---|---|
| **Attributable** | The platform records a verified submitter identity — not one the submitter can fake. |
| **Legible** | Artifacts are retrievable and readable with standard tools, now and decades from now. |
| **Contemporaneous** | The arrival timestamp is set by the platform, not the submitter, and can't be back-dated. |
| **Original** | Each artifact is sealed to its exact content. The stored copy is provably the real one. |
| **Accurate** | The platform tests submissions for consistency. It rejects malformed or mismatched content, rather than quietly accepting it. |
| **Complete** | The full chain of workflow artifacts is kept, not just a final summary. |
| **Consistent** | Records can't race or contradict each other, and the search index is always rebuildable from the sealed artifacts. |
| **Enduring** | Configurable retention up to 99 years, on storage built to survive. |
| **Available** | Artifacts stay retrievable when you need them, with a high-availability service target. |

## Why you don't have to trust the vendor

The heart of Cairn is a deliberate design choice: **verifying an artifact never consults Cairn's own database.**

When an auditor verifies an artifact, Cairn re-derives the verdict — is this authentic, unaltered, and under its retention lock? — from the sealed artifact itself and the signatures attached to it, using standard tooling. Cairn never reads its own internal index. This matters because:

- **Trust the math, not the operator.** A verdict that required querying the operator's database is one the operator could fake. A proof re-derived from signatures over fixed content cannot be.
- **It outlives any single system.** An artifact may need to hold up for decades — longer than any particular database, or the company running it. The proof relies only on open, standard formats and tooling, so it survives the system that created it.
- **The database is disposable.** Cairn's search index can be rebuilt at any time from the sealed artifacts. Something you can regenerate can't also be the root of trust — that would be circular.
- **Audit independence.** "The system says it's valid" is not audit evidence. An independently re-derivable cryptographic proof is. A skeptical auditor can even supply their own list of trusted signers and reach the same verdict without trusting anything you hand them.

The honest boundary: you use Cairn's index to *find* the right artifact. You need nothing from Cairn to *prove* it.

## How it runs

Cairn runs inside your own cloud account, pinned to a single region. Custody of your artifacts never leaves your control. Your own team or a managed-service provider can operate it. Either way, the operator runs the system but, by design, cannot tamper with what it holds. Production deployments turn on write-once, locked storage (an irreversible, deliberate choice made at setup). Non-production environments run the same workflow without the lock, so you can clean up test data.

## Learn more

- **What the system guarantees, in precise detail** — the behavioral specifications live in [`openspec/specs/`](openspec/specs/). Start with any capability's `## Purpose`. The independent-verification guarantees are in [`artifact-verifiability`](openspec/specs/artifact-verifiability/spec.md).
- **How it's architected and built** — see [`docs/`](docs/) (start at [`docs/index.md`](docs/index.md)) and [`docs/architecture-as-deployed.md`](docs/architecture-as-deployed.md).
- **Building, testing, and deploying it** — see **[DEVELOPING.md](DEVELOPING.md)**.
- **What's planned** — tracked as [GitHub Issues](../../issues). In-flight work lives under [`openspec/changes/`](openspec/changes/).

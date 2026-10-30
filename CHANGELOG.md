# Changelog

This file documents all notable changes to Cairn Proofs.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The `openspec/` directory holds the requirements as the source of truth. This log records the user- and operator-visible changes that land in the codebase.

## [Unreleased]

The project is pre-1.0 and has not yet cut a tagged release.

### Added

- **Initial public release.** Cairn Proofs is a tamper-evident artifact store. Producers push artifacts in OCI bundles to Amazon ECR. An EventBridge-driven, five-stage Lambda pipeline then processes each bundle. It validates the bundle, signs it, and archives it to S3 (with optional WORM Object Lock). It then indexes the bundle in DynamoDB and mints a canonical ECR tag. The data plane anchors artifact integrity — content digest, Notation signature, and WORM receipt. Anyone can verify integrity independently of whoever operates the substrate. Verification never reads the index.

  This release comprises:
  - The five-stage processing pipeline (validate → sign → archive → index → tag) as AWS Lambda functions, driven by EventBridge.
  - The `cairn` CLI — `push`, `pull`, `verify`, `query`, `sign`, `alias`, and `config` — plus a read-only operator TUI (`cairn tui`).
  - A producer compliance claim carried as a signed in-toto Statement in a Producer_Attestation_Referrer. Multi-signature verification (pipeline + auditor sign-off) re-derives its verdict from the ECR artifact, its OCI referrers, and a Notation trust policy alone.
  - OSCAL as the first native interop module over a format-agnostic artifact foundation. Every bundle carries a universal, digest-derived artifact reference as an OCI Referrer.
  - A rebuildable DynamoDB metadata projection (never the root of trust), tenant-partitioned for isolation.
  - The AWS CDK application (one stack) that provisions the pipeline, storage, encryption, IAM, and observability, deployable to any registered region.
  - The mise-managed toolchain and the `mise run check` pre-merge gate (lint, tests, OpenSpec validation, CDK typecheck + tests, SAST, secret scanning).

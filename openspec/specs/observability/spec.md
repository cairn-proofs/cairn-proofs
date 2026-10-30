# observability Specification

## Purpose
Distributed-trace correlation and structured logging across every pipeline stage, plus the CloudWatch metric and alarm surface. Every Lambda invocation extracts W3C `traceparent` / `baggage` context (falling back to the active ADOT span) and opens a scope that injects `traceId` and `sessionId` into every structured JSON log line. Per-invocation ambient state lives in OpenTelemetry baggage (`opentelemetry.baggage`) — ContextVar-backed, async-safe, and W3C-propagated. Metrics emit via EMF (Embedded Metric Format via `aws_lambda_powertools.metrics`) under the `CairnProofs/{regionCode}-{stage}` namespace, with an unconditional `TenantId` dimension on per-bundle metrics. The provisioned alarms are the DLQ-depth alarm and the per-Lambda `DeadLetterErrors` alarms, with SNS actions gated by `isProd OR alarmActionsEnabled` (production forces them enabled); no alarm is provisioned on the per-metric signing-failure, archive-failure, or integrity-mismatch metrics.

*Domain terms (`Capitalized_Underscore`) used below are defined in [GLOSSARY.md](../../GLOSSARY.md), which names the governing requirement for each.*
## Requirements
### Requirement: [OBS-001] OpenTelemetry as Standard Observability Infrastructure

THE Provenance_System SHALL use OpenTelemetry via the AWS Distro for OpenTelemetry (ADOT) as the standard observability infrastructure for all Lambda pipeline functions and any service-layer components.

ADOT provides the SDK, exporters, and Lambda layer every pipeline function uses, so traces, metrics, and logs share one instrumentation model and downstream collector configuration. No alternative tracing framework (X-Ray SDK alone, custom propagation, Datadog APM, etc.) is permitted for in-system components.

#### Scenario: [OBS-001.01] Lambda pipeline function instrumented with ADOT

- **WHEN** any Processing_Pipeline Lambda function (validation, signing, archive, metadata-update) is deployed
- **THEN** the function is configured with the ADOT Lambda layer and uses the OpenTelemetry SDK for span creation, context propagation, and metric emission

#### Scenario: [OBS-001.02] Service-layer component uses OpenTelemetry

- **WHEN** a service-layer component (e.g., an API Gateway-fronted handler or background worker) is deployed as part of the Provenance_System
- **THEN** the component is instrumented with the OpenTelemetry SDK via ADOT for the same trace, metric, and log emission semantics as the Lambda pipeline

### Requirement: [OBS-002] W3C Trace Context Extraction with Span Fallback

WHEN an Processing_Pipeline Lambda function is invoked, the Provenance_System SHALL extract W3C `traceparent` and `baggage` headers (or the equivalent propagated context from EventBridge event metadata) to obtain the `traceId` and `sessionId` for the invocation. IF the headers are absent, the Provenance_System SHALL fall back to the active AWS ADOT (OpenTelemetry) span via `Span.current()` as defined by `TraceContext`'s resolution order.

This guarantees every invocation has a `traceId` and `sessionId` for log correlation, even when an upstream producer or event source omits the W3C headers.

#### Scenario: [OBS-002.01] Traceparent and baggage headers present on invocation

- **WHEN** an Processing_Pipeline Lambda is invoked with W3C `traceparent` and `baggage` headers (or equivalent fields carried in the EventBridge event payload metadata)
- **THEN** the Provenance_System parses those headers to obtain the `traceId` and `sessionId` and uses them for the invocation's span and log context

#### Scenario: [OBS-002.02] Headers absent — fall back to active span

- **WHEN** an Processing_Pipeline Lambda is invoked with no `traceparent` or `baggage` headers and no equivalent EventBridge metadata
- **THEN** the Provenance_System resolves the active OpenTelemetry span via `Span.current()` per `TraceContext`'s resolution order and uses that span's `traceId` and `sessionId` for the invocation

### Requirement: [OBS-003] Per-Invocation Span with Deterministic Scope Closure

THE Processing_Pipeline SHALL create a Span for every Lambda invocation, ensuring that `traceId` and `sessionId` are automatically injected into every log line for the duration of that invocation. The scope SHALL be closed deterministically at invocation exit — whether normal or exceptional — to prevent MDC state leakage across reused Lambda execution environments.

Lambda execution environments are reused across invocations; without deterministic scope closure, a prior invocation's trace context could leak into a later invocation's logs and corrupt correlation. Scope closure is mandatory on both the success and failure paths.

#### Scenario: [OBS-003.01] Span created at invocation entry and logs carry traceId/sessionId

- **WHEN** an Processing_Pipeline Lambda invocation begins
- **THEN** a Span is created for the invocation and every log line emitted during the invocation includes the invocation's `traceId` and `sessionId` automatically

#### Scenario: [OBS-003.02] Scope closed on normal exit

- **WHEN** an Processing_Pipeline Lambda invocation completes successfully
- **THEN** the invocation's Span scope is closed deterministically before the handler returns, leaving no MDC state in the reused execution environment

#### Scenario: [OBS-003.03] Scope closed on exceptional exit

- **WHEN** an Processing_Pipeline Lambda invocation throws an exception or otherwise terminates abnormally
- **THEN** the invocation's Span scope is still closed deterministically (e.g., via a `finally` block or equivalent) so that no MDC state from the failed invocation contaminates a subsequent invocation in the same execution environment

### Requirement: [OBS-004] Pipeline-Specific Field Attachment with Standard Key Names

WHEN additional fields beyond `traceId` and `sessionId` are needed for a pipeline stage, the Processing_Pipeline SHALL attach them — for example, `manifestDigest`, `projectId`, `eventType` — using the key names defined in `ObservabilityKeys` for standard fields and consistent camelCase naming for pipeline-specific fields.

A central `ObservabilityKeys` constant set defines the canonical key names for fields that recur across stages, so searches and dashboards operate on a stable vocabulary. Pipeline-specific fields (those not in `ObservabilityKeys`) follow camelCase to keep the structured log schema uniform.

#### Scenario: [OBS-004.01] Standard field uses ObservabilityKeys constant

- **WHEN** an Processing_Pipeline stage attaches the manifest digest, project identifier, or event type to its log context
- **THEN** the attached key name matches the corresponding constant in `ObservabilityKeys` (`manifestDigest`, `projectId`, `eventType`) rather than an ad-hoc variant such as `manifest_digest` or `project-id`

#### Scenario: [OBS-004.02] Pipeline-specific field uses camelCase

- **WHEN** an Processing_Pipeline stage attaches a field that is not in `ObservabilityKeys` (e.g., a stage-local diagnostic such as `signerInvocationId`)
- **THEN** the field is attached using consistent camelCase naming rather than snake_case or kebab-case

### Requirement: [OBS-005] Trace Correlation Preserved Across Async Boundaries

WHEN pipeline work is handed off asynchronously (e.g., passing context to a thread pool executor or an async callback), the Provenance_System SHALL preserve trace correlation across the async boundary.

Without explicit propagation, the OpenTelemetry context is lost when work crosses a thread boundary or is scheduled on a different executor, breaking distributed-trace stitching for any operation that fans out asynchronously. The pipeline must capture the current context and re-attach it on the receiving side of every async hand-off.

#### Scenario: [OBS-005.01] Context propagates through a thread pool executor

- **WHEN** an Processing_Pipeline stage submits work to a thread pool executor or equivalent async runtime
- **THEN** the current OpenTelemetry context (including `traceId` and `sessionId`) is captured at submission and re-attached on the worker thread, so logs and spans emitted by the worker are correlated to the originating invocation

#### Scenario: [OBS-005.02] Context propagates through an async callback

- **WHEN** an Processing_Pipeline stage registers an asynchronous callback (e.g., a future completion handler)
- **THEN** the OpenTelemetry context is preserved into the callback's execution so that the callback's log and span output carries the same `traceId` and `sessionId` as the originating stage

### Requirement: [OBS-006] Per-Invocation Ambient State Uses OpenTelemetry Baggage

WHEN any Processing_Pipeline stage attaches per-invocation ambient state — logging context fields such as `traceId`, `sessionId`, `projectId`, `manifestDigest`, `eventType`, or any pipeline-specific field defined via `ObservabilityKeys` or camelCase convention — the Provenance_System SHALL use OpenTelemetry baggage (`opentelemetry.baggage`) as the underlying storage primitive. Baggage entries are stored in the active OpenTelemetry context (itself `contextvars.ContextVar`-backed), are inherited by spawned tasks, threads, and asyncio coroutines, and propagate across W3C-compliant service boundaries via the standard `Baggage` HTTP header (or equivalent transport-specific carriers) when emitted by the OTEL `BaggagePropagator`.

The Provenance_System SHALL NOT use module-level mutable state, `threading.local`, or a project-local MDC dictionary for per-invocation ambient state — those substrates leak across tests under parallel execution, break under async event-loop concurrency, and stop at process boundaries, forfeiting the cross-service propagation W3C baggage provides. This Requirement aligns with the `toolchain` capability's *Tests Safe Under Parallel Execution* Requirement, which states the same rule in terms of test-suite invariants.

#### Scenario: [OBS-006.01] set_baggage attaches a field that downstream logs and spans inherit

- **WHEN** an Processing_Pipeline stage calls `opentelemetry.baggage.set_baggage("projectId", project_id)` at handler entry
- **THEN** every subsequent log line emitted within the same invocation (from the handler, from `libs/*` code, from any nested function) carries `projectId` as a field in the structured JSON output, and every span created during the invocation inherits the baggage entry automatically — without the field being explicitly threaded as a parameter through every function call

#### Scenario: [OBS-006.02] Baggage propagates across asyncio task spawns within the same invocation

- **WHEN** an Processing_Pipeline stage spawns work via `asyncio.create_task()` or `asyncio.gather()` after setting baggage entries on the current context
- **THEN** the spawned tasks inherit a copy of the parent's baggage, can read the inherited entries via `baggage.get_all()`, and any modifications they make to their own baggage do NOT propagate back into the parent task — providing structural per-task isolation without explicit cleanup

#### Scenario: [OBS-006.03] Project-local MDC dictionary is prohibited

- **WHEN** any reviewer or static-analysis pass encounters a module-level mutable dict, list, or attribute being used as a per-invocation context store (an "MDC"-style module-global context store)
- **THEN** the construct is rejected; the substrate is OpenTelemetry baggage. Module-level globals are permitted only for read-only configuration captured at process start (e.g., `STAGE = os.environ.get("STAGE")`, `_LOG = logging.getLogger(__name__)`)

#### Scenario: [OBS-006.04] Baggage keys flowing into structured log output are restricted to an allow-list

- **WHEN** the Provenance_System extracts W3C `baggage` headers from a producer-asserted EventBridge event payload via `W3CBaggagePropagator`, OR a caller passes `**extra_fields` to `observability_scope(...)`
- **THEN** only baggage keys present in the closed `ALLOWED_BAGGAGE_KEYS` allow-list (defined alongside `ObservabilityKeys` — currently the `sessionId`, `requestId`, `eventType`, `projectId`, `manifestDigest`, `artifactKind`, `tenantId`, `ecrRepository`, and `signingProfileArn` constants) are serialized into structured log records as top-level fields; baggage entries with keys outside the allow-list are silently dropped at formatter time and disallowed `extra_fields` are silently dropped at scope entry. This prevents an untrusted producer from injecting arbitrary log fields (shadowing operator-meaningful keys, polluting dashboards, or accidentally capturing upstream secrets from producer CI pipelines)

### Requirement: [OBS-007] Structured JSON Formatter with Guaranteed Base Fields

ALL structured log output from the Processing_Pipeline SHALL be formatted using `CairnLogLayout`, producing a record per log line that contains at minimum the four guaranteed fields: `level`, `message`, `traceId`, and `sessionId`. Pipeline-specific fields SHALL also appear in the structured log output.

The four guaranteed fields set the floor of every log record, so operators can rely on their presence in dashboards, alarms, and ad-hoc CloudWatch Logs Insights queries regardless of which stage emitted the line. Any additional context attached via `ObservabilityKeys` or pipeline-specific camelCase keys MUST also be serialized into the same record.

#### Scenario: [OBS-007.01] Log record contains all four base fields

- **WHEN** any Processing_Pipeline component emits a structured log line
- **THEN** the resulting JSON record produced by `CairnLogLayout` contains at minimum the fields `level`, `message`, `traceId`, and `sessionId`

#### Scenario: [OBS-007.02] Pipeline-specific fields serialized alongside base fields

- **WHEN** an Processing_Pipeline stage has attached pipeline-specific fields (e.g., `manifestDigest`, `projectId`) to its log context
- **THEN** those fields appear in the structured log record produced by `CairnLogLayout` alongside the four guaranteed base fields

### Requirement: [OBS-008] Structured JSON Log Per Pipeline Stage Invocation

THE Processing_Pipeline SHALL emit a structured JSON log entry for every pipeline stage invocation using the `CairnLogLayout` format, including the following fields beyond the four base fields: `requestId` (Lambda invocation ID), `eventType` (one of: `validated`, `signed`, `archived`, `metadata-updated`, `validation-failed`, `signing-failed`, `archive-failed`), `projectId`, `manifestDigest`, `durationMs` (pipeline stage wall-clock duration in milliseconds), and `status` (one of: `success`, `failure`, `error`). WHEN the stage invocation is a failure (status `failure` or `error`), the structured log record SHALL ADDITIONALLY include a `failedCriteria` field enumerating each contributing failure as an object carrying its `criterion`, `field`, and `message`, so the cause of the failure is diagnosable from CloudWatch Logs alone without reconstructing the corresponding `artifact.validation-failed` (or other failure) EventBridge event. The `failedCriteria` detail SHALL be emitted to the log at the single point that composes the failure event, so it accompanies every permanent-failure path exactly once.

This is the pipeline's canonical per-stage log line — what dashboards, alarms, and post-incident forensics index against. Every stage MUST emit exactly one such record per invocation, with `eventType` and `status` drawn from the closed vocabularies above. Carrying the failing criteria in the log record (not only in the EventBridge event) lets an operator reading the log group determine *why* a bundle failed without replaying the event bus.

#### Scenario: [OBS-008.01] Successful stage invocation log

- **WHEN** an Processing_Pipeline stage completes a successful invocation (e.g., validation, signing, archive, or metadata update succeeds for a bundle)
- **THEN** a structured JSON log entry is emitted via `CairnLogLayout` containing `level`, `message`, `traceId`, `sessionId`, `requestId`, `eventType` (one of `validated`, `signed`, `archived`, `metadata-updated`), `projectId`, `manifestDigest`, `durationMs`, and `status` set to `success`

#### Scenario: [OBS-008.02] Failed stage invocation log

- **WHEN** an Processing_Pipeline stage fails an invocation (e.g., validation, signing, or archive fails for a bundle)
- **THEN** a structured JSON log entry is emitted via `CairnLogLayout` with `eventType` set to the corresponding failure value (`validation-failed`, `signing-failed`, or `archive-failed`), `status` set to `failure` or `error`, all other required fields (`requestId`, `projectId`, `manifestDigest`, `durationMs`, `traceId`, `sessionId`, `level`, `message`) populated, and a `failedCriteria` field enumerating each contributing failure as a `{criterion, field, message}` object

#### Scenario: [OBS-008.03] Validation failure reason is diagnosable from CloudWatch Logs alone

- **WHEN** the validate stage records a permanent validation failure (for example a Config_Blob that violates one or more validation criteria) and emits both the `artifact.validation-failed` EventBridge event and its structured failure log record
- **THEN** the CloudWatch log record carries the same per-criterion detail (`criterion`, `field`, `message` for each failure) that the EventBridge event carries, so an operator can determine which criterion failed by reading the log group without reconstructing the event, rather than seeing only a generic summary or a failure count

### Requirement: [OBS-009] CloudWatch Metric Namespace, Names, and Dimensions

THE Provenance_System SHALL emit CloudWatch metrics under the `CairnProofs/{regionCode}-{stage}` namespace for each pipeline operation: `BundleIngested` (dimensioned by ProjectId and ArtifactKind), `ValidationSuccess`/`ValidationFailure` (dimensioned by ProjectId and ArtifactKind), `ValidationSkipped` (dimensioned by SkipReason — the pipeline-internal artifact kind that caused the skip), `SigningSuccess`/`SigningFailure` (dimensioned by ProjectId), `ArchiveSuccess`/`ArchiveFailure` (dimensioned by ProjectId), `MetadataUpdateSuccess`/`MetadataUpdateFailure` (dimensioned by EventType), and `QueryLatency` (in milliseconds, dimensioned by ProjectId). All per-bundle metrics (`BundleIngested`, `ValidationSuccess`/`Failure`, `SigningSuccess`/`Failure`, `ArchiveSuccess`/`Failure`, `QueryLatency`) SHALL additionally carry a `TenantId` dimension so per-tenant observability and alarms are possible without cross-tenant aggregation. `ValidationSkipped` is NOT a per-bundle metric — a skipped artifact is by definition not an Artifact_Bundle, so it carries no ProjectId/ArtifactKind and no TenantId dimension; it is dimensioned only by SkipReason. The `ArtifactKind` dimension value (and the corresponding `artifactKind` baggage key, where attached) SHALL be the validated `artifactKind` resolved from the Producer_Attestation_Referrer claim — artifact-kind does not reside in the content manifest (see artifact-ingestion *Producer Claim Carried as Signed Attestation Referrer*), so the dimension is populated from the claim, never from a content-manifest annotation or Config_Blob field.

The `{regionCode}-{stage}` Deployment_Qualifier suffix in the namespace prevents naming collisions between deployment environments (e.g., `CairnProofs/pdx-prod` vs `CairnProofs/iad-gamma`). The `TenantId` dimension makes per-tenant dashboards and alarms tractable; without it, metrics from all tenants would aggregate into a single time series. `ValidationSkipped` makes pipeline-internal artifacts the validate stage filters out (Notation signature referrers, OCI empty/referrer configs, image indexes — see the artifact-ingestion *Pipeline-Internal Artifact Filtering Before Bundle Validation* Requirement) observable as a distinct, expected, non-failure outcome that does not contaminate the `ValidationFailure` series or trip failure alarms.

#### Scenario: [OBS-009.01] Metric emitted under qualified namespace with required dimensions

- **WHEN** the Processing_Pipeline completes a stage that emits a metric (e.g., `SigningSuccess` for a bundle)
- **THEN** the metric is published to CloudWatch under the namespace `CairnProofs/{regionCode}-{stage}` with the dimensions specified for that metric (e.g., `ProjectId` for `SigningSuccess`)

#### Scenario: [OBS-009.02] TenantId dimension carried on per-bundle metrics

- **WHEN** the Provenance_System emits any per-bundle metric (`BundleIngested`, `ValidationSuccess`/`Failure`, `SigningSuccess`/`Failure`, `ArchiveSuccess`/`Failure`, or `QueryLatency`)
- **THEN** the emitted metric carries a `TenantId` dimension identifying the tenant the bundle belongs to, so per-tenant observability and alarms operate without cross-tenant aggregation

#### Scenario: [OBS-009.03] ArtifactKind dimension is sourced from the validated claim

- **WHEN** a per-bundle metric carrying an `ArtifactKind` dimension (e.g. `BundleIngested` or `ValidationSuccess`) is emitted for a validated submission
- **THEN** the `ArtifactKind` dimension value is the `artifactKind` resolved from the Producer_Attestation_Referrer claim, not a value read from the content manifest's annotations or Config_Blob

#### Scenario: [OBS-009.04] QueryLatency reported in milliseconds

- **WHEN** the Metadata_Index serves a query
- **THEN** a `QueryLatency` metric is emitted under `CairnProofs/{regionCode}-{stage}` with a value in milliseconds and a `ProjectId` dimension (and a `TenantId` dimension)

#### Scenario: [OBS-009.05] ValidationSkipped emitted for a pipeline-internal artifact

- **WHEN** the validate stage classifies a pushed artifact as pipeline-internal (e.g. a Notation signature referrer) and skips it
- **THEN** a `ValidationSkipped` metric is emitted under `CairnProofs/{regionCode}-{stage}` dimensioned by `SkipReason`, and no `ValidationFailure` metric is emitted for that artifact

### Requirement: [OBS-010] Metric Emission via EMF (Embedded Metric Format)

THE Provenance_System SHALL emit CloudWatch metrics via EMF (Embedded Metric Format) — Lambda handlers print specially-formatted JSON log lines to stdout that CloudWatch Logs auto-parses into CloudWatch Metrics — rather than via synchronous `cloudwatch:PutMetricData` API calls. The recommended implementation is `aws_lambda_powertools.metrics.Metrics`, which handles the EMF JSON schema, the namespace composition (`Artifact/{regionCode}-{stage}` per the existing *CloudWatch Metric Namespace, Names, and Dimensions* Requirement), dimension management, and the flush-at-handler-exit lifecycle via the `@metrics.log_metrics` decorator on the Lambda handler.

Lambda execution roles SHALL NOT be granted `cloudwatch:PutMetricData`. The EMF path uses the function's existing CloudWatch Logs write permission (granted by the AWS-managed basic Lambda execution role), so no metric-specific IAM permission is required. Granting `PutMetricData` is unnecessary and expands the role's blast radius without operational benefit.

#### Scenario: [OBS-010.01] Lambda handler emits a metric via EMF, no PutMetricData IAM permission needed

- **WHEN** an Processing_Pipeline Lambda is constructed with `metrics = Metrics(namespace="Artifact/cmh-alpha", service="validate-bundle")`, calls `metrics.add_dimension("ProjectId", project_id)` + `metrics.add_metric("ValidationSuccess", "Count", 1)`, and is decorated with `@metrics.log_metrics`
- **THEN** at handler exit the EMF JSON line is printed to stdout, CloudWatch Logs ingests it via the function's existing log group, the metric appears in CloudWatch Metrics under `Artifact/cmh-alpha` with the correct dimensions, and the function's IAM role has no `cloudwatch:PutMetricData` permission

#### Scenario: [OBS-010.02] Lambda execution role explicitly excludes cloudwatch:PutMetricData

- **WHEN** the CDK Lambda construct (`cdk/lib/constructs/lambda-functions.ts`) provisions an execution role for any of the four pipeline Lambdas
- **THEN** the role's policy statements contain no `cloudwatch:PutMetricData` action; the only CloudWatch-related grant is the AWS-managed `logs:CreateLogStream` / `logs:PutLogEvents` from the basic Lambda execution role, sufficient for EMF emission

#### Scenario: [OBS-010.03] Handler missing the log_metrics decorator is rejected in review

- **WHEN** a new Lambda handler is authored that constructs a `Metrics` instance and calls `add_metric(...)` but is NOT decorated with `@metrics.log_metrics`
- **THEN** code review rejects the handler — without the decorator, EMF lines are never flushed and metric emissions are silently dropped. The decorator pattern is the contract; absence is a bug

### Requirement: [OBS-011] CloudWatch Alarms for Pipeline Failures

THE Provenance_System SHALL configure a CloudWatch alarm that triggers when dead letter queue depth exceeds 0 within a 5-minute evaluation period, and SHALL configure a CloudWatch alarm on each pipeline Lambda's `DeadLetterErrors` metric (the AWS-managed Lambda metric that counts failed writes to the function's dead letter queue) that triggers when that metric exceeds 0 within a 5-minute evaluation window. The `DeadLetterErrors` alarm SHALL be provisioned for every pipeline Lambda.

The SNS actions of these alarms SHALL be enabled WHEN the deployment is production (`isProd`, which `WORM_Enabled=true` implies). WHEN the deployment is not production, the actions SHALL be enabled only if the `alarmActionsEnabled` deployment parameter is true. The effective action posture SHALL be `isProd OR alarmActionsEnabled`, and `isProd` SHALL force the actions enabled regardless of `alarmActionsEnabled`. An alarm whose actions are disabled SHALL continue to evaluate and remain visible in the CloudWatch console but SHALL publish no SNS notification.

A DLQ message means a pipeline event has exhausted its retry budget and needs human attention; the 5-minute evaluation window surfaces incidents promptly while absorbing transient retries. The `DeadLetterErrors` alarm covers a distinct failure: when a Lambda cannot write a failed async-invoke payload to its dead letter queue (KMS denial, missing IAM permission, queue removed, queue-policy denial), the payload is dropped and the only AWS-emitted signal is the `DeadLetterErrors` metric — a failure the DLQ-depth alarm cannot observe, because depth stays at 0 when the write itself fails.

THE Provenance_System SHALL provision pipeline-failure alarms only on the dead letter queue depth metric, the per-Lambda `DeadLetterErrors` metric, and the `archive-to-s3` duration envelope. It SHALL NOT provision an alarm on a `SigningFailure`, `ArchiveFailure`, or `IntegrityMismatch` metric, and SHALL NOT provision a validation skip-rate dashboard.

#### Scenario: [OBS-011.01] DLQ depth above zero trips alarm

- **WHEN** the dead letter queue depth metric records a value greater than 0 within any 5-minute evaluation window
- **THEN** the configured CloudWatch alarm transitions to ALARM state

#### Scenario: [OBS-011.02] Non-production deployment without opt-in disables alarm actions

- **WHEN** the Provenance_System stack is synthesized with `isProd=false` and `alarmActionsEnabled` unset or false
- **THEN** each pipeline-failure alarm (the DLQ-depth alarm and each per-Lambda `DeadLetterErrors` alarm) is provisioned with its SNS action wired but disabled (`ActionsEnabled=false`), remaining visible in the CloudWatch console and publishing no notification

#### Scenario: [OBS-011.03] No failure-count alarms or skip-rate dashboard are provisioned

- **WHEN** the Provenance_System stack is synthesized
- **THEN** no CloudWatch alarm is provisioned on the `SigningFailure`, `ArchiveFailure`, or `IntegrityMismatch` metrics, and no validation skip-rate dashboard is provisioned

#### Scenario: [OBS-011.04] Production deployment enables alarm actions

- **WHEN** the Provenance_System stack is synthesized with `isProd=true` (as `WORM_Enabled=true` requires), regardless of the `alarmActionsEnabled` value
- **THEN** each pipeline-failure alarm (the DLQ-depth alarm and each per-Lambda `DeadLetterErrors` alarm) is provisioned with its SNS action enabled (`ActionsEnabled=true`)

#### Scenario: [OBS-011.05] Non-production opt-in enables alarm actions

- **WHEN** the Provenance_System stack is synthesized with `isProd=false` and `alarmActionsEnabled=true`
- **THEN** each pipeline-failure alarm (the DLQ-depth alarm and each per-Lambda `DeadLetterErrors` alarm) is provisioned with its SNS action enabled (`ActionsEnabled=true`)

#### Scenario: [OBS-011.06] DeadLetterErrors alarm exists for every pipeline Lambda

- **WHEN** the Provenance_System stack is synthesized
- **THEN** one `DeadLetterErrors` CloudWatch alarm is provisioned per pipeline Lambda, each with a threshold of 0 and a 5-minute evaluation window

#### Scenario: [OBS-011.07] DLQ write failure is visible but does not page while actions are disabled

- **WHEN** any pipeline Lambda's `DeadLetterErrors` metric records a count greater than 0 within any 5-minute evaluation window in a deployment where alarm actions are disabled
- **THEN** the configured CloudWatch alarm transitions to ALARM state and is visible in the CloudWatch console, but publishes no SNS notification because its actions are disabled

### Requirement: [OBS-013] SNS Notification on Alarm State Transition

WHEN a CloudWatch alarm whose actions are enabled transitions to ALARM state, the Provenance_System SHALL publish a notification to a configured SNS topic. The SNS topic and its subscriptions SHALL remain provisioned even when every Provenance_System alarm has its actions disabled, so enabling an alarm's actions restores notification without re-subscribing consumers.

SNS is the fan-out point for operator notification — subscribers (PagerDuty, email, Slack-bridge, downstream Lambda) consume from the topic. The Provenance_System does not prescribe which subscribers attach, only that an action-enabled alarm publishes to the configured topic on every ALARM-state transition. WHEN a deployment has its alarm actions disabled, no alarm publishes to the topic; the topic remains provisioned and dormant until actions are enabled — by deploying as `isProd` or setting `alarmActionsEnabled`.

#### Scenario: [OBS-013.01] Action-enabled alarm transition publishes to SNS

- **WHEN** an Provenance_System CloudWatch alarm whose actions are enabled transitions to ALARM state
- **THEN** a notification is published to the configured SNS topic for downstream subscribers to consume

#### Scenario: [OBS-013.02] No notification while alarm actions are disabled

- **WHEN** a pipeline alarm transitions to ALARM state in a deployment where its alarm actions are disabled
- **THEN** no notification is published, and the configured SNS topic and its subscriptions remain provisioned for later enablement

### Requirement: [OBS-014] Producer-Side W3C Trace Context Injection on Emitted Pipeline Events

WHEN an Processing_Pipeline Lambda function emits a pipeline EventBridge event (`artifact.validated`, `artifact.signed`, `artifact.archived`, `artifact.tagged`, and their failure counterparts), the Provenance_System SHALL inject the active invocation's W3C `traceparent` and allow-listed `baggage` into the event `Detail` so the downstream stage's `extract_trace_context()` resolves `source="headers"` and continues the same trace rather than starting a fresh root span. Injection SHALL use the same composite propagator and the same baggage allow-list (`ALLOWED_BAGGAGE_KEYS`) as the *W3C Trace Context Extraction with Span Fallback* path, so the producer-written carrier and the consumer-probed carrier cannot drift. IF no valid span is active when an event is emitted, the injected carrier SHALL be absent or empty rather than malformed, and the downstream stage SHALL fall back to its existing resolution order unchanged.

This is the symmetric producer counterpart to the *W3C Trace Context Extraction with Span Fallback* Requirement: producer-side injection and consumer-side extraction together make a single logical bundle push share one distributed trace end-to-end.

#### Scenario: [OBS-014.01] Emitted event carries the active trace context

- **WHEN** a pipeline stage with an active OpenTelemetry span emits a pipeline EventBridge event via its `emit(...)` helper
- **THEN** the event `Detail` carries a `traceparent` (and allow-listed `baggage`) serialized from the active context, such that a consumer calling `extract_trace_context()` on that event resolves the same `traceId` with `source="headers"`

#### Scenario: [OBS-014.02] Injected baggage is restricted to the allow-list

- **WHEN** a stage injects trace context and the active baggage contains keys outside `ALLOWED_BAGGAGE_KEYS`
- **THEN** only allow-listed baggage keys are serialized into the emitted `Detail`, and non-allow-listed keys are dropped — matching the extraction-side and formatter-side allow-list trust model

#### Scenario: [OBS-014.03] No active span — carrier absent, downstream falls back

- **WHEN** a stage emits an event while no valid OpenTelemetry span is active
- **THEN** the emitted `Detail` contains no malformed `traceparent`, and the downstream stage resolves trace context via its existing fallback order (active span, then empty) without error

#### Scenario: [OBS-014.04] Re-injection preserves span parentage across hops

- **WHEN** a stage that continued an upstream trace emits its own downstream event
- **THEN** the injected `traceparent` reflects that stage's current span (not a verbatim echo of the upstream header), so the downstream stage's parent-span id points at the emitting stage and the full push forms a correct parent chain under one trace id

#### Scenario: [OBS-014.05] Full pipeline push shares one root trace

- **WHEN** a bundle push flows validate → sign → archive → update-metadata → mint-tag, each stage injecting context on emit and extracting it on entry
- **THEN** all stage invocations appear in X-Ray under one root trace identity for that logical push rather than as separate root traces

### Requirement: [OBS-015] Pipeline-Internal Artifact Invocations Linked to Originating Bundle Trace

WHEN a pipeline-internal artifact `PutImage` event (Notation signature referrer, OCI image index, OCI empty config, or mint-tag re-tag) triggers a `ValidationSkipped` validate-bundle invocation, the Provenance_System SHALL relate that invocation to the originating bundle's trace via an OpenTelemetry **span link** rather than leaving it as a disconnected root trace. These events are authored by ECR and carry no `traceparent`, so the relationship SHALL be recovered (not injected) from context available on the skip path. The link SHALL be best-effort: IF the originating bundle's trace context cannot be recovered, the invocation SHALL proceed as a standalone `ValidationSkipped`, and the absence of the link SHALL NOT gate or fail the skip path.

A span link (rather than a forced shared trace id) is the correct representation because these are separate root invocations delivered independently by EventBridge; the link makes their causal relationship to the bundle navigable in X-Ray without misrepresenting them as one synchronous trace.

#### Scenario: [OBS-015.01] Skip invocation links to the originating bundle trace

- **WHEN** a pipeline-internal artifact `PutImage` triggers a `ValidationSkipped` invocation and the originating bundle's trace context is recoverable
- **THEN** the skip invocation's span records a link to that bundle's trace context, so the relationship is navigable in X-Ray, while the invocation remains a distinct expected non-failure outcome

#### Scenario: [OBS-015.02] Originating context unavailable — skip proceeds unlinked

- **WHEN** a pipeline-internal artifact triggers a `ValidationSkipped` invocation but the originating bundle's trace context cannot be recovered
- **THEN** the invocation proceeds and emits `ValidationSkipped`, with no span link and no error on the skip path

#### Scenario: [OBS-015.03] Linking does not contaminate the failure series

- **WHEN** a skip invocation links to an originating bundle trace
- **THEN** the invocation still emits `ValidationSkipped` (dimensioned by `SkipReason`) and emits no `ValidationFailure` metric, preserving the metric semantics for pipeline-internal artifacts

### Requirement: [OBS-016] AWS SDK Calls Instrumented as Trace Subsegments

WHEN an Processing_Pipeline Lambda function makes an AWS SDK (boto3 / botocore) call — DynamoDB, S3, KMS, EventBridge, or any other AWS service — during an instrumented invocation, the Provenance_System SHALL emit a child span (X-Ray subsegment) for that call nested under the invocation's active handler span, identifying the AWS service and operation. Instrumentation SHALL be installed once per process via `BotocoreInstrumentor().instrument()` against the SDK `TracerProvider` that `configure_tracing()` installs, idempotently and parallel-safely under the same global-provider guard, so it applies to every boto3 client the stages construct without per-handler wiring. Instrumentation SHALL be the manual `BotocoreInstrumentor`, NOT the ADOT auto-instrument wrapper (`AWS_LAMBDA_EXEC_WRAPPER`), consistent with the collector-only-layer architecture that keeps the bundled OpenTelemetry distribution the single provider.

This completes the per-dependency visibility the handler-level trace leaves open: an operator can see which AWS dependency a stage spent its time on, and a failed dependency call is attributable to the exact service/operation rather than to the stage as a whole.

#### Scenario: [OBS-016.01] AWS SDK call emits a child subsegment under the handler span

- **WHEN** a pipeline stage with an active handler span makes a boto3 call (e.g. a DynamoDB `PutItem`, an S3 `PutObject`, a KMS `Decrypt`, or an EventBridge `PutEvents`)
- **THEN** a child span is recorded under that stage's handler span identifying the AWS service and operation, appearing as a subsegment of the stage in X-Ray for that invocation's trace

#### Scenario: [OBS-016.02] Faulting AWS call marks the subsegment with error/fault status

- **WHEN** a downstream AWS SDK call fails or throttles mid-invocation (e.g. a `ThrottlingException`, `AccessDenied`, or service error)
- **THEN** the corresponding child span is marked with error/fault status carrying the error detail, nested under the originating stage's handler span, so the failure is attributable to that specific call and stage without log spelunking

#### Scenario: [OBS-016.03] Instrumentation installed once per process, idempotent

- **WHEN** `configure_tracing()` runs at module-init and again on a reused Lambda execution environment
- **THEN** `BotocoreInstrumentor` is instrumented exactly once against the SDK `TracerProvider` and a second call is a no-op, registering no duplicate instrumentation and producing no duplicate subsegments

#### Scenario: [OBS-016.04] No recording span active — no error, no orphan subsegment

- **WHEN** a boto3 call runs with no recording handler span active (e.g. a unit test under the default no-op provider, or module-init AWS calls before a scope opens)
- **THEN** botocore instrumentation produces a non-recording child span without raising, leaving the call's behavior unchanged and emitting no orphan root subsegment

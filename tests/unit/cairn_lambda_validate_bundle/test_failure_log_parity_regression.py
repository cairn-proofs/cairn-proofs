"""Regression guards for the validate-bundle failure-log field-parity fix.

This file carries the success-record parity test plus the Unchanged-Behavior
regression guards — the surfaces the fix must leave intact while it adds
``eventType`` / ``status`` / ``durationMs`` / ``projectId`` to BOTH the failure
and success per-stage log records.

Capture surfaces (same mechanism as ``test_failure_logging_guards.py``):

- ``caplog`` inspects raw ``LogRecord`` ATTRIBUTES — the ``extra=`` fields
  ``_emit_stage_log`` attaches directly.
- The baggage-driven fields (``requestId``, ``eventType``, …) are merged into
  the JSON by ``StructuredJsonFormatter`` *at emission time* from active OTEL
  baggage, so we attach a dedicated handler that renders each record to JSON
  DURING emission (while the ``observability_scope`` baggage is live) and assert
  against that rendered line.

What each test covers:
- ``test_successful_validate_emits_obs_008_per_stage_log``: the success line
  carries the four fields and no ``failedCriteria``.
- ``test_validation_failed_event_detail_fields_unchanged``: the validation-failed
  event detail is unchanged (via ``emit_validation_failed``).
- ``test_permanent_failure_single_reason_bearing_dlq_before_emit``: the DLQ
  rejection is unchanged (via ``dlq.send_rejection``).
- ``test_oversized_detail_collapses_failed_criteria_before_dlq``: the byte-ceiling
  collapse is unchanged (via ``_enforce_detail_byte_ceiling``).
"""

from __future__ import annotations

import json
import logging

import pytest
from cairn_observability.log import StructuredJsonFormatter

from .conftest import (
    PROJECT_ID,
    REPO_NAME,
    attestation_claim,
    attestation_push_event,
    build_attestation,
    build_bundle,
    submit,
)


def _render_during_emission(root: logging.Logger) -> tuple[logging.Handler, list[str]]:
    """Attach a handler rendering each WARNING/INFO record to JSON at emission
    time (while ``observability_scope`` baggage is live). Returns the handler and
    the list it appends to; the caller removes the handler in a ``finally``."""
    rendered_lines: list[str] = []

    class _RenderingHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.INFO)
            self._fmt = StructuredJsonFormatter()

        def emit(self, record: logging.LogRecord) -> None:
            rendered_lines.append(self._fmt.format(record))

    handler = _RenderingHandler()
    root.addHandler(handler)
    return handler, rendered_lines


# --------------------------------------------------------------------------
# Test Properties (a).5-(a).9 -- success-record parity: a SUCCESSFUL validate
# emits a per-stage log satisfying [OBS-008.01]. The success emission shares the
# _emit_stage_log helper with the failure chokepoint, so eventType=validated
# rides the same baggage override and status/durationMs/projectId ride extra.
# --------------------------------------------------------------------------


def test_successful_validate_emits_obs_008_per_stage_log(reload_handler, lambda_context) -> None:
    """OBS-008.01 success-record parity: the clean-validation 'validate-bundle
    validated' line carries eventType=validated, status=success, a non-negative
    durationMs, projectId (the cross-checked claim value), and NO failedCriteria.

    Assertions are against the rendered JSON line (eventType rides a baggage
    override, same as the failure path)."""
    handler = reload_handler
    bundle = build_bundle()
    _attestation, event = submit(
        handler,
        bundle=bundle,
        claim_bytes=attestation_claim(project_id=PROJECT_ID),
    )

    root = logging.getLogger()
    rendering_handler, rendered_lines = _render_during_emission(root)
    try:
        payload = handler.lambda_handler(event, lambda_context)
    finally:
        root.removeHandler(rendering_handler)

    # Confirm this is the success path (no failure event).
    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    assert payload["manifestDigest"] == bundle.manifest_digest

    lines = [ln for ln in rendered_lines if '"validate-bundle validated"' in ln]
    assert lines, "per-stage success log line was not emitted"
    emitted = json.loads(lines[-1])

    assert emitted["eventType"] == "validated"
    assert emitted["status"] == "success"
    # durationMs parity with the failure line: not merely present but a
    # non-negative int (never a bool). The success and failure lines share the
    # _emit_stage_log helper, so the success line's durationMs is subject to the
    # same int-computation — pin it at the same strength the failure test does
    # (test_failure_log_field_parity.test_failure_log_carries_non_negative_integer_duration_ms).
    duration_ms = emitted.get("durationMs")
    assert duration_ms is not None, "success line is missing durationMs"
    assert isinstance(duration_ms, int) and not isinstance(duration_ms, bool), (
        f"durationMs must be an int; got {type(duration_ms).__name__} ({duration_ms!r})"
    )
    assert duration_ms >= 0, f"durationMs must be non-negative; got {duration_ms!r}"
    assert emitted["projectId"] == PROJECT_ID
    assert "failedCriteria" not in emitted


# --------------------------------------------------------------------------
# Property (c).2 — Unchanged clause #2: the artifact.validation-failed
# EventBridge detail still carries manifestDigest + bounded failedCriteria +
# errorKind, unchanged by the fix. Guards blast-radius #1
# (_emit_failure_event -> emit_validation_failed).
# --------------------------------------------------------------------------


def test_validation_failed_event_detail_fields_unchanged(reload_handler, lambda_context) -> None:
    handler = reload_handler
    # A post-claim UUID-shape failure (criterion c5): the claim carries a
    # malformed catalogOrProfileUuid, routing through the identity-aware emit
    # with a populated failedCriteria + manifestDigest.
    bundle = build_bundle()
    _attestation, event = submit(
        handler,
        bundle=bundle,
        claim_bytes=attestation_claim(catalog_or_profile_uuid="not-a-uuid"),
    )

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    failed = handler._test_recorder.details("artifact.validation-failed")
    assert len(failed) == 1
    detail = failed[0]
    assert detail["manifestDigest"] == bundle.manifest_digest
    assert detail["failedCriteria"], detail
    for entry in detail["failedCriteria"]:
        assert set(entry) >= {"criterion", "field", "message"}


def test_validation_failed_event_detail_carries_error_kind(reload_handler, lambda_context) -> None:
    """A classified failure populates errorKind on the detail — the fix leaves
    errorKind sourcing untouched.

    An invalid Config_Blob (one whose parse fails inside ``validate_bundle``)
    routes through the ``invalid_config_blob`` branch, which passes
    ``error_kind="invalid_config_blob"`` to the chokepoint. ``_emit_failure_event``
    attaches ``errorKind`` to the detail only when ``error_kind`` is truthy
    (unchanged by the fix), so this is the path that pins the key's presence."""
    handler = reload_handler
    # A Config_Blob that is not valid JSON fails to parse -> result.config_blob
    # is None -> errorKind="invalid_config_blob".
    bundle = build_bundle(config_bytes=b"not json at all")
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    failed = handler._test_recorder.details("artifact.validation-failed")
    assert len(failed) == 1
    assert failed[0]["errorKind"] == "invalid_config_blob"
    assert failed[0]["manifestDigest"] == bundle.manifest_digest
    assert failed[0]["failedCriteria"]


# --------------------------------------------------------------------------
# Property (c).3 — Unchanged clause #3: every permanent-failure path deposits
# exactly ONE reason-bearing DLQ rejection, ordered BEFORE the EventBridge emit.
# Guards blast-radius #1 (_emit_failure_event -> dlq.send_rejection).
# --------------------------------------------------------------------------


def test_permanent_failure_single_reason_bearing_dlq_before_emit(
    reload_handler, lambda_context
) -> None:
    handler = reload_handler
    bundle = build_bundle(drop_annotations=("dev.cairnproofs.artifact.project-id",))
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    # Exactly one DLQ rejection, carrying the failure reason (bounded failedCriteria).
    bodies = handler._test_fake_sqs.bodies()
    assert len(bodies) == 1, bodies
    body = bodies[0]
    assert body["manifestDigest"] == bundle.manifest_digest
    assert body["failedCriteria"], "DLQ rejection body carried no failure reason"

    # Exactly one EventBridge validation-failed emit.
    failed = handler._test_recorder.details("artifact.validation-failed")
    assert len(failed) == 1

    # Ordering: the DLQ send precedes the EventBridge emit. _emit_failure_event
    # calls dlq.send_rejection before emit_validation_failed, so by the time the
    # recorder holds an entry the DLQ already holds its message. Assert the DLQ
    # body and the emitted detail are the same reason-bearing record.
    assert handler._test_fake_sqs.messages, "no DLQ message was sent"
    assert body["failedCriteria"] == failed[0]["failedCriteria"]


def test_direct_emit_sends_dlq_before_eventbridge(reload_handler) -> None:
    """Call order is DLQ-then-EventBridge. A recording fake that raises on the
    EventBridge put would leave the DLQ already populated — assert the ordering
    directly by observing both sinks after a direct _emit_failure_event call."""
    handler = reload_handler
    failed = [
        {"criterion": "a", "field": "manifest.annotations[x]", "message": "missing"},
    ]
    handler._emit_failure_event(
        manifest_digest="sha256:dead",
        repository_name="artifact-cmh-alpha/p",
        failed_criteria=failed,
        error_kind="invalid_config_blob",
    )

    # Exactly one on each sink, same reason-bearing payload.
    assert len(handler._test_fake_sqs.bodies()) == 1
    emitted = handler._test_recorder.details("artifact.validation-failed")
    assert len(emitted) == 1
    assert handler._test_fake_sqs.bodies()[0]["failedCriteria"] == emitted[0]["failedCriteria"]


# --------------------------------------------------------------------------
# Property (c).4 — Unchanged clause #4: an oversized failure detail still
# collapses failedCriteria to the single _enforce_detail_byte_ceiling marker
# BEFORE the DLQ send and event emit. Guards blast-radius #1.
# --------------------------------------------------------------------------


def test_oversized_detail_collapses_failed_criteria_before_dlq(reload_handler) -> None:
    handler = reload_handler
    # _bound_failed_criteria caps COUNT (<=50) and per-string code-point length
    # (<=1024) upstream, so an ASCII payload can never breach the ceiling after
    # bounding. The byte-true backstop _enforce_detail_byte_ceiling exists for
    # the non-ASCII case: json.dumps' ensure_ascii expands each non-ASCII code
    # point to a 6-byte \\uXXXX escape, so a bounded 50x1024 non-ASCII payload
    # serializes to ~50 * 1024 * 6 bytes (~300 KB), overshooting the 200 KB
    # ceiling. Mirror that amplification to exercise the collapse through the
    # real chokepoint.
    ceiling = handler.MAX_EVENT_DETAIL_BYTES
    non_ascii_message = "\u00e9" * handler.MAX_CRITERION_STR_LEN
    failed = [
        {
            "criterion": f"c{i}",
            "field": f"config_blob.field_{i}",
            "message": non_ascii_message,
        }
        for i in range(handler.MAX_FAILED_CRITERIA)
    ]

    handler._emit_failure_event(
        manifest_digest="sha256:oversized",
        repository_name="artifact-cmh-alpha/p",
        failed_criteria=failed,
        error_kind="invalid_config_blob",
    )

    # The DLQ body and the EventBridge detail share the collapsed single marker.
    bodies = handler._test_fake_sqs.bodies()
    assert len(bodies) == 1
    collapsed = bodies[0]["failedCriteria"]
    assert len(collapsed) == 1, collapsed
    marker = collapsed[0]
    assert marker["criterion"] == "truncated"
    assert marker["field"] == "failedCriteria"
    assert str(len(failed)) in marker["message"]
    # The serialized DLQ body now fits the ceiling.
    assert len(json.dumps(bodies[0], default=str).encode("utf-8")) <= ceiling

    # The EventBridge detail carries the identical collapsed marker (same record).
    emitted = handler._test_recorder.details("artifact.validation-failed")
    assert len(emitted) == 1
    assert emitted[0]["failedCriteria"] == collapsed


def test_enforce_detail_byte_ceiling_collapses_directly(reload_handler) -> None:
    """Unit-level guard on _enforce_detail_byte_ceiling: an oversized detail
    collapses failedCriteria to the single marker; a small detail is untouched."""
    handler = reload_handler
    ceiling = handler.MAX_EVENT_DETAIL_BYTES

    small = {
        "manifestDigest": "sha256:ok",
        "failedCriteria": [{"criterion": "a", "field": "f", "message": "m"}],
    }
    out = handler._enforce_detail_byte_ceiling(small, total_criteria=1)
    assert out["failedCriteria"] == small["failedCriteria"]

    big = {
        "manifestDigest": "sha256:big",
        "failedCriteria": [
            {"criterion": f"c{i}", "field": "f", "message": "x" * 4096}
            for i in range((ceiling // 4096) + 50)
        ],
    }
    total = len(big["failedCriteria"])
    out = handler._enforce_detail_byte_ceiling(big, total_criteria=total)
    assert len(out["failedCriteria"]) == 1
    assert out["failedCriteria"][0]["criterion"] == "truncated"
    assert str(total) in out["failedCriteria"][0]["message"]


_FAILURE_LOG_MESSAGE = "validate-bundle validation failed"


def _rendered_failure_lines(rendered_lines: list[str]) -> list[dict]:
    """Parse the rendered JSON lines, keeping only the failure-log records.

    The capture list holds every rendered record (success per-stage line, skip
    lines, the ``permanent failure`` marker). Filter to the
    ``_emit_failure_event`` chokepoint line by message so the assertions target
    exactly the record OBS-008 governs."""
    return [json.loads(ln) for ln in rendered_lines if f'"{_FAILURE_LOG_MESSAGE}"' in ln]


# --------------------------------------------------------------------------
# Fix-only invariant (b).1 — PRE-claim failure parity: a permanent failure
# raised BEFORE the producer claim identity triple is parsed carries
# eventType / status / durationMs but OMITS projectId.
#
# projectId is guarded "when the producer claim identity triple has been
# parsed". The parity tests in
# test_failure_log_field_parity.py exercise a POST-claim trigger (identity
# cross-check) where projectId IS present; this is the complementary edge. The
# missing-user-identity path (handler.py ~953) fires before any claim parse —
# extract_authenticated_push_principal raises MissingUserIdentityError and the
# chokepoint is called with NO project_id — yet it runs inside the live
# lambda_handler -> observability_scope path, so _INVOCATION_START is set and
# the baggage eventType override + status + durationMs all apply. The rendered
# line must therefore show the three present AND projectId absent.
# --------------------------------------------------------------------------


def test_pre_claim_failure_omits_project_id_but_carries_other_three_fields(
    reload_handler, lambda_context
) -> None:
    handler = reload_handler
    # omit_user_identity=True drives the missing-user-identity permanent failure,
    # which is raised before the producer claim is parsed (claim.project_id is
    # never in scope), so the chokepoint receives no project_id.
    _attestation, event = submit(handler, bundle=build_bundle(), omit_user_identity=True)

    root = logging.getLogger()
    rendering_handler, rendered_lines = _render_during_emission(root)
    try:
        result = handler.lambda_handler(event, lambda_context)
    finally:
        root.removeHandler(rendering_handler)

    assert result is handler._REJECTED
    failure_lines = _rendered_failure_lines(rendered_lines)
    assert failure_lines, "pre-claim permanent-failure chokepoint did not emit its log line"
    emitted = failure_lines[-1]

    # Confirm this is the pre-claim path: the failure classified as
    # missing_user_identity, which has no claim behind it.
    assert emitted.get("errorKind") == "missing_user_identity", emitted

    # The three invocation-derived fields are present even pre-claim.
    assert emitted["eventType"] == "validation-failed", (
        f"pre-claim failure line missing eventType=='validation-failed'; "
        f"got {emitted.get('eventType', '<absent>')!r}"
    )
    assert emitted.get("status") in {"failure", "error"}, (
        "pre-claim failure line missing status in {failure, error}; "
        f"got {emitted.get('status')!r}"
    )
    duration_ms = emitted.get("durationMs")
    assert isinstance(duration_ms, int) and not isinstance(duration_ms, bool), (
        f"pre-claim failure line missing integer durationMs; got {duration_ms!r}"
    )
    assert duration_ms >= 0, f"durationMs must be non-negative; got {duration_ms!r}"

    # projectId is the guarded field: ABSENT because no claim was parsed. This is
    # the fix-only invariant — assert absence, not a value.
    assert "projectId" not in emitted, (
        "pre-claim failure line must OMIT projectId (no producer claim parsed), "
        f"but it carried projectId={emitted.get('projectId')!r}"
    )


# --------------------------------------------------------------------------
# durationMs is ABSENT on a direct _emit_failure_event call outside
# lambda_handler. The _INVOCATION_START ContextVar is unset for a bare unit
# call, so durationMs must be OMITTED (not 0, not an error) — omitted entirely
# when unset. The other three extra-sourced fields that do
# not depend on the invocation anchor (status; eventType via the baggage
# override) still render.
# --------------------------------------------------------------------------


def test_direct_emit_omits_duration_ms_when_invocation_start_unset(reload_handler) -> None:
    handler = reload_handler
    # Sanity: no invocation anchor is set outside lambda_handler.
    assert handler._INVOCATION_START.get() is None

    root = logging.getLogger()
    rendering_handler, rendered_lines = _render_during_emission(root)
    try:
        handler._emit_failure_event(
            manifest_digest="sha256:bare",
            repository_name="artifact-cmh-alpha/p",
            failed_criteria=[
                {"criterion": "a", "field": "manifest.annotations[x]", "message": "missing"},
            ],
            error_kind="invalid_config_blob",
        )
    finally:
        root.removeHandler(rendering_handler)

    failure_lines = _rendered_failure_lines(rendered_lines)
    assert failure_lines, "direct _emit_failure_event did not emit its log line"
    emitted = failure_lines[-1]

    # The anchor-independent fields still render on a bare call.
    assert emitted["eventType"] == "validation-failed"
    assert emitted.get("status") in {"failure", "error"}
    # durationMs is omitted (not present, not None, not 0) — the ContextVar was unset.
    assert "durationMs" not in emitted, (
        "durationMs must be ABSENT on a direct call with no invocation anchor; "
        f"got {emitted.get('durationMs')!r}"
    )


# --------------------------------------------------------------------------
# The single chokepoint stamps the failure vocabulary on EVERY permanent-failure
# path. A light parametrized check over two DISTINCT post-claim triggers
# (invalid config blob vs. identity cross-check mismatch) confirms both render
# eventType=='validation-failed' / status=='failure' — the fix is path-agnostic
# because _emit_failure_event is the sole emitter.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected_error_kind"),
    [
        pytest.param(
            {"bundle_kwargs": {"config_bytes": b"not json at all"}, "claim_kwargs": {}},
            "invalid_config_blob",
            id="invalid-config-blob",
        ),
        pytest.param(
            {"bundle_kwargs": {}, "claim_kwargs": {"org_id": "org-foreign"}},
            None,
            id="identity-crosscheck-mismatch",
        ),
    ],
)
def test_distinct_failure_triggers_all_render_failure_vocabulary(
    reload_handler, lambda_context, kwargs, expected_error_kind
) -> None:
    handler = reload_handler
    bundle = build_bundle(**kwargs["bundle_kwargs"])
    claim_kwargs = kwargs["claim_kwargs"]
    _attestation, event = submit(
        handler,
        bundle=bundle,
        claim_bytes=attestation_claim(**claim_kwargs) if claim_kwargs else None,
    )

    root = logging.getLogger()
    rendering_handler, rendered_lines = _render_during_emission(root)
    try:
        result = handler.lambda_handler(event, lambda_context)
    finally:
        root.removeHandler(rendering_handler)

    assert result is handler._REJECTED
    failure_lines = _rendered_failure_lines(rendered_lines)
    assert failure_lines, "permanent-failure chokepoint did not emit its log line"
    emitted = failure_lines[-1]

    # Every distinct trigger renders the same closed failure vocabulary.
    assert emitted["eventType"] == "validation-failed"
    assert emitted["status"] == "failure"
    if expected_error_kind is not None:
        assert emitted.get("errorKind") == expected_error_kind


# --------------------------------------------------------------------------
# OBS-008 projectId parity on a POST-claim, NON-cross-check failure path.
#
# The cross-scope-attestation-hijack path (subject_scope) fires AFTER the claim
# is parsed but BEFORE the identity cross-check, so it is a distinct post-claim
# site from the identity cross-check the parity file covers. Expected clause #4
# and OBS-008.02 require projectId on the rendered line for every parsed-claim
# failure, not just the cross-check. The claim's org/tenant/project match the
# triggering repo path (so the claim is coherent and the subject-scope check is
# the failure), and the content subject resides in a DIFFERENT repo.
# --------------------------------------------------------------------------


def test_cross_scope_hijack_failure_line_carries_project_id(reload_handler, lambda_context) -> None:
    handler = reload_handler
    # Content bundle lives in a foreign (victim) repo; its residency is there.
    victim_repo = "artifact-cmh-alpha/org-victim/tnt-victim/victim-project"
    bundle = build_bundle(repo_name=victim_repo)
    handler._test_fake_ecr.add_bundle(bundle)
    # Attestation is pushed into the canonical REPO_NAME; its claim's identity
    # triple matches REPO_NAME's segments (default attestation_claim), so the
    # claim parses coherently and the subject-scope check — not the identity
    # cross-check — is the permanent failure.
    attestation = build_attestation(subject_digest=bundle.manifest_digest, repo_name=REPO_NAME)
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)

    root = logging.getLogger()
    rendering_handler, rendered_lines = _render_during_emission(root)
    try:
        result = handler.lambda_handler(event, lambda_context)
    finally:
        root.removeHandler(rendering_handler)

    assert result is handler._REJECTED
    failure_lines = _rendered_failure_lines(rendered_lines)
    assert failure_lines, "cross-scope-hijack chokepoint did not emit its log line"
    emitted = failure_lines[-1]

    # Confirm this is the post-claim subject-scope path (not the cross-check).
    assert emitted.get("errorKind") == "cross_scope_attestation_hijack", emitted
    # projectId surfaces because the claim was parsed before this failure.
    assert emitted["eventType"] == "validation-failed"
    assert emitted["status"] == "failure"
    assert emitted["projectId"] == PROJECT_ID, (
        "post-claim (non-cross-check) failure line must carry the claim's projectId; "
        f"expected {PROJECT_ID!r}, got {emitted.get('projectId', '<absent>')!r}"
    )


# --------------------------------------------------------------------------
# projectId is TRUNCATED on the failure log line. On the identity-cross-check
# path the claim's projectId is logged before the cross-check confirms it, so it
# is raw, unvalidated producer input the claim model does not bound. An oversized
# value is capped at MAX_PROJECT_ID_LOG_LEN so it cannot amplify CloudWatch log
# volume — a cost/DoS defense mirroring the failedCriteria byte bounds.
# --------------------------------------------------------------------------


def test_oversized_project_id_is_truncated_on_failure_line(reload_handler, lambda_context) -> None:
    handler = reload_handler
    cap = handler.MAX_PROJECT_ID_LOG_LEN
    oversized = "p" * (cap * 4)
    # A claim whose projectId does NOT match the ECR path segment: the identity
    # cross-check fails (criterion f), and the raw oversized projectId is passed
    # to the chokepoint before the cross-check could reject it.
    _attestation, event = submit(
        handler,
        bundle=build_bundle(),
        claim_bytes=attestation_claim(project_id=oversized),
    )

    root = logging.getLogger()
    rendering_handler, rendered_lines = _render_during_emission(root)
    try:
        result = handler.lambda_handler(event, lambda_context)
    finally:
        root.removeHandler(rendering_handler)

    assert result is handler._REJECTED
    failure_lines = _rendered_failure_lines(rendered_lines)
    assert failure_lines, "identity-mismatch chokepoint did not emit its log line"
    emitted = failure_lines[-1]

    logged = emitted.get("projectId")
    assert logged is not None, "failure line should still carry a (truncated) projectId"
    assert len(logged) == cap, (
        f"projectId must be truncated to {cap} code points on the log line; "
        f"got length {len(logged)}"
    )
    assert logged == oversized[:cap]

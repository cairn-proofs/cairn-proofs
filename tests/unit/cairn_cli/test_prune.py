"""`cairn prune` ([RETAIN-002], [RETAIN-005], [RETAIN-010], [RETAIN-011], [ACCESS-007]).

The command runs against the in-memory boto3-boundary fakes in ``prune_fakes``;
each test drives ``prune._run`` with an injected session and clock.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

import pytest
from cairn_cli import main as cli_main
from cairn_cli import prune

from tests.unit.cairn_cli.prune_fakes import (
    CONTROLS_TABLE,
    NETWORK_ERROR,
    NOW,
    ORG,
    PROJECT,
    PRUNER_ROLE_ARN,
    PRUNER_SESSION_ARN,
    TENANT,
    FakeDdb,
    FakeLogs,
    FakeS3,
    FakeSession,
    World,
    client_error,
    digest_of,
)

TARGET = f"{ORG}/{TENANT}/{PROJECT}"


def _args(*digests: str) -> argparse.Namespace:
    return argparse.Namespace(
        target=TARGET,
        org_id=None,
        tenant_id=None,
        project_id=None,
        digest=list(digests),
        stage="alpha",
        region_code="cmh",
        app_name=None,
        mfa_serial=None,
    )


def _no_prompt(_: str) -> str:
    raise AssertionError("an existing pruner session must not prompt for an MFA code")


def _run(w: World, *digests: str, **session_kw) -> int:
    return prune._run(
        _args(*digests), session=w.session(**session_kw), now=lambda: NOW, prompt=_no_prompt
    )


def _three_candidates(w: World) -> tuple[str, str, str]:
    w.add_other_project_row()
    return w.add_bundle("a"), w.add_bundle("b"), w.add_image("junk")


# ── [RETAIN-005] operator-initiated prune ────────────────────────────────────


def test_report_lists_every_candidate_and_deletes_nothing(capsys) -> None:
    """[RETAIN-005.01] / [RETAIN-002.02]"""
    w = World()
    a, b, junk = _three_candidates(w)
    assert _run(w) == prune.EXIT_OK
    out = capsys.readouterr().out
    for d in (a, b, junk):
        assert d in out
    assert "expired-bundle" in out and "unarchived-manifest" in out
    assert "cairn prune --target" in out
    assert w.deletes() == []
    assert w.audit == []
    assert w.row_present(a) and w.row_present(b)


def test_report_names_failed_checks_and_exits_non_zero(capsys) -> None:
    """[RETAIN-008.05] the report names the failed check."""
    w = World()
    w.add_other_project_row()
    junk = w.add_image("junk")
    w.fail("list_image_referrers", junk, "ThrottlingException")
    assert _run(w) == prune.EXIT_ITEM_FAILED
    out = capsys.readouterr().out
    assert "Checks that failed" in out
    assert "ECR ListImageReferrers" in out and "ThrottlingException" in out
    assert w.deletes() == []


def test_report_needs_no_pruner_session() -> None:
    w = World(caller_arn="arn:aws:sts::111111111111:assumed-role/auditor/x")
    _three_candidates(w)
    assert _run(w) == prune.EXIT_OK


def test_only_the_named_candidate_is_deleted() -> None:
    """[RETAIN-005.02]"""
    w = World()
    a, b, junk = _three_candidates(w)
    assert _run(w, a) == prune.EXIT_OK
    assert not w.row_present(a)
    assert w.prefix_versions(a) == []
    assert w.row_present(b) and w.prefix_versions(b)
    assert junk in w.images and b in w.images
    deleted = {d for kind, d in w.deletes() if kind == "delete:ecr"}
    assert a in deleted and b not in deleted and junk not in deleted


def test_eligibility_is_rechecked_at_delete_time() -> None:
    """[RETAIN-005.03] a legal hold placed after the report refuses the item."""
    w = World(worm=True)
    w.add_other_project_row()
    d = w.add_bundle()
    w.prefix_versions(d)[0].legal_hold = True
    assert _run(w, d) == prune.EXIT_ITEM_FAILED
    assert w.deletes() == []
    refused = [r for r in w.audit if r["outcome"] == "refused"]
    assert len(refused) == 1 and "legal hold" in refused[0]["reason"]


@pytest.mark.parametrize(
    "selector",
    ["*", "sha256:*", "sha256:abc*", "expired-bundle", "unarchived-manifest", "SHA256:" + "a" * 64],
)
def test_wildcard_or_class_selector_is_a_usage_error(selector: str, capsys) -> None:
    """[RETAIN-005.04]"""
    w = World()
    w.add_bundle()
    session = w.session()
    rc = prune._run(_args(selector), session=session, now=lambda: NOW)
    assert rc == prune.EXIT_USAGE
    assert "wildcards and class-wide selectors are not accepted" in capsys.readouterr().err
    assert session.clients_requested == []
    assert w.deletes() == []


def test_class_wide_flag_is_rejected_by_the_parser() -> None:
    with pytest.raises(SystemExit) as e:
        cli_main.main(["prune", "--target", TARGET, "--all"])
    assert e.value.code == 2


def test_prune_is_registered_with_help(capsys) -> None:
    with pytest.raises(SystemExit):
        cli_main.main(["prune", "--help"])
    out = capsys.readouterr().out
    assert "--digest" in out and "pruner" in out


# ── [ACCESS-007] pruner session gate ─────────────────────────────────────────


@pytest.mark.parametrize(
    "caller",
    [
        "arn:aws:sts::111111111111:assumed-role/cairnproofs4af-operator/x",
        "arn:aws:sts::222222222222:assumed-role/cairnproofs4af-pruner-cmh-alpha/x",
        "arn:aws:sts::111111111111:assumed-role/cairnproofs4af-pruner-cmh-alpha-evil/x",
        "arn:aws:sts::111111111111:federated-user/dana",
    ],
)
def test_delete_refused_unless_caller_is_a_pruner_session_or_iam_user(caller: str, capsys) -> None:
    w = World(caller_arn=caller)
    w.add_other_project_row()
    d = w.add_bundle()
    assert _run(w, d) == prune.EXIT_NOT_PRUNER
    err = capsys.readouterr().err
    assert "not a session of the pruner role" in err and "not an IAM user" in err
    assert w.assume_calls == []
    assert w.deletes() == []
    assert w.audit == []


# ── [ACCESS-007] the CLI assumes the pruner role for an IAM user ─────────────

IAM_USER_ARN = "arn:aws:iam::111111111111:user/ops/dana"


@pytest.fixture
def pruner_session_factory(monkeypatch):
    """`_session_from_credentials` returns a session whose caller is the pruner role."""
    made: list[dict] = []

    def install(w: World, caller_arn: str = PRUNER_SESSION_ARN) -> list[dict]:
        def factory(base, creds):
            made.append(creds)
            return FakeSession(w, None, caller_arn=caller_arn)

        monkeypatch.setattr(prune, "_session_from_credentials", factory)
        return made

    return install


def _run_as_user(w: World, *digests: str, code: str = "123456", mfa_serial: str | None = None):
    args = _args(*digests)
    args.mfa_serial = mfa_serial
    prompts: list[str] = []

    def prompt(text: str) -> str:
        prompts.append(text)
        return code

    rc = prune._run(args, session=w.session(), now=lambda: NOW, prompt=prompt)
    return rc, prompts


def test_iam_user_assumes_the_pruner_role_with_mfa_and_source_identity(
    pruner_session_factory,
) -> None:
    """[ACCESS-007.03] the CLI sends SerialNumber, TokenCode, and SourceIdentity itself."""
    w = World(caller_arn=IAM_USER_ARN)
    made = pruner_session_factory(w)
    w.add_other_project_row()
    d = w.add_bundle()
    rc, prompts = _run_as_user(w, d)
    assert rc == prune.EXIT_OK
    assert w.assume_calls == [
        {
            "RoleArn": PRUNER_ROLE_ARN,
            "RoleSessionName": "dana",
            "SourceIdentity": "dana",
            "SerialNumber": "arn:aws:iam::111111111111:mfa/dana",
            "TokenCode": "123456",
            "DurationSeconds": 3600,
        }
    ]
    assert prompts == ["MFA code for arn:aws:iam::111111111111:mfa/dana: "]
    assert len(made) == 1
    assert not w.row_present(d)
    # The audit names the pruner session, not the base IAM user.
    assert {r["callerArn"] for r in w.audit} == {PRUNER_SESSION_ARN}


def test_mfa_serial_override_is_sent(pruner_session_factory) -> None:
    w = World(caller_arn=IAM_USER_ARN)
    pruner_session_factory(w)
    w.add_other_project_row()
    d = w.add_bundle()
    serial = "arn:aws:iam::111111111111:mfa/dana-yubikey"
    rc, _ = _run_as_user(w, d, mfa_serial=serial)
    assert rc == prune.EXIT_OK
    assert w.assume_calls[0]["SerialNumber"] == serial


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef"])
def test_malformed_mfa_code_refuses_before_assume_role(
    code: str, pruner_session_factory, capsys
) -> None:
    w = World(caller_arn=IAM_USER_ARN)
    pruner_session_factory(w)
    w.add_other_project_row()
    d = w.add_bundle()
    rc, _ = _run_as_user(w, d, code=code)
    assert rc == prune.EXIT_NOT_PRUNER
    assert "six digits" in capsys.readouterr().err
    assert w.assume_calls == [] and w.deletes() == [] and w.audit == []


def test_denied_assume_role_refuses_delete(pruner_session_factory, capsys) -> None:
    w = World(caller_arn=IAM_USER_ARN)
    pruner_session_factory(w)
    w.add_other_project_row()
    d = w.add_bundle()
    w.fail("assume_role", PRUNER_ROLE_ARN, "AccessDenied")
    rc, _ = _run_as_user(w, d)
    assert rc == prune.EXIT_NOT_PRUNER
    assert "cannot assume the pruner role" in capsys.readouterr().err
    assert w.deletes() == [] and w.audit == []


def test_assumed_session_that_is_not_the_pruner_role_refuses(
    pruner_session_factory, capsys
) -> None:
    w = World(caller_arn=IAM_USER_ARN)
    pruner_session_factory(w, caller_arn="arn:aws:sts::111111111111:assumed-role/other/dana")
    w.add_other_project_row()
    d = w.add_bundle()
    rc, _ = _run_as_user(w, d)
    assert rc == prune.EXIT_NOT_PRUNER
    assert "is not a session of the pruner role" in capsys.readouterr().err
    assert w.deletes() == []


def test_assumed_session_expiry_comes_from_sts(pruner_session_factory) -> None:
    """M35 reads the AssumeRole Expiration, not the static credentials of the new session."""
    w = World(caller_arn=IAM_USER_ARN)
    pruner_session_factory(w)
    w.add_other_project_row()
    d = w.add_bundle()
    # Fresh one-hour credentials (Expiration NOW + 3600 s): eligible to start.
    rc, _ = _run_as_user(w, d)
    assert rc == prune.EXIT_OK

    # Two minutes before that Expiration, the item is refused at preflight.
    w2 = World(caller_arn=IAM_USER_ARN)
    pruner_session_factory(w2)
    w2.add_other_project_row()
    d2 = w2.add_bundle()
    late = NOW + timedelta(minutes=58)
    rc = prune._run(_args(d2), session=w2.session(), now=lambda: late, prompt=lambda _: "123456")
    assert rc == prune.EXIT_ITEM_FAILED
    refused = [r for r in w2.audit if r["step"] == "preflight"]
    assert refused and "expires at" in refused[0]["reason"]
    assert w2.deletes() == []


def test_session_from_credentials_builds_a_regional_boto3_session() -> None:
    class Base:
        region_name = "us-east-2"

    s = prune._session_from_credentials(
        Base(), {"AccessKeyId": "AKIA", "SecretAccessKey": "s", "SessionToken": "t"}
    )
    assert s.region_name == "us-east-2"
    creds = s.get_credentials()
    assert (creds.access_key, creds.secret_key, creds.token) == ("AKIA", "s", "t")


@pytest.mark.parametrize("expiry", [NOW + timedelta(minutes=2), None])
def test_short_or_unknown_session_lifetime_refuses_before_any_delete(expiry) -> None:
    """M35"""
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    assert _run(w, d, expiry=expiry) == prune.EXIT_ITEM_FAILED
    assert w.deletes() == []
    assert w.audit[-1]["step"] == "preflight" and w.audit[-1]["outcome"] == "refused"


def test_is_pruner_session_matches_exact_role_name() -> None:
    role = "arn:aws:iam::111111111111:role/path/cairnproofs4af-pruner-cmh-alpha"
    assert prune._is_pruner_session(PRUNER_SESSION_ARN, role)
    assert not prune._is_pruner_session(
        "arn:aws:sts::111111111111:assumed-role/cairnproofs4af-pruner-cmh-alpha/", role
    )
    assert not prune._is_pruner_session(PRUNER_SESSION_ARN, "not-an-arn")


# ── [RETAIN-010] ordered deletion ────────────────────────────────────────────


def test_bundle_deletion_follows_the_fixed_order() -> None:
    """[RETAIN-010.01]"""
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    referrers = set(w.images) - {d}
    assert _run(w, d) == prune.EXIT_OK
    kinds = [kind for kind, _ in w.deletes()]
    ecr_targets = [t for kind, t in w.deletes() if kind == "delete:ecr"]
    assert ecr_targets[-1] == d  # manifest after every referrer
    assert set(ecr_targets[:-1]) <= referrers
    last_ecr = max(i for i, k in enumerate(kinds) if k == "delete:ecr")
    first_s3 = kinds.index("delete:s3")
    last_s3 = max(i for i, k in enumerate(kinds) if k == "delete:s3")
    first_claims = kinds.index("delete:claims")
    assert last_ecr < first_s3 < last_s3 < first_claims < kinds.index("delete:index")
    assert kinds[-1] == "delete:index"
    # The S3 receipt copy goes last within the S3 step (resume anchor).
    s3_targets = [t for kind, t in w.deletes() if kind == "delete:s3"]
    assert "worm-receipt.json" in s3_targets[-1]
    # Only this bundle's ControlClaims rows.
    claims = [t for kind, t in w.deletes() if kind == "delete:claims"]
    assert len(claims) == 2 and all(t.endswith(d) for t in claims)


def test_failed_s3_step_halts_and_the_next_run_resumes(capsys) -> None:
    """[RETAIN-010.02] / [RETAIN-002.01]"""
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    blob_key = next(v.key for v in w.prefix_versions(d) if "blobs" in v.key)
    w.fail("delete_object", blob_key, "SlowDown")

    assert _run(w, d) == prune.EXIT_ITEM_FAILED
    assert "step s3-versions" in capsys.readouterr().out
    assert d not in w.images  # ECR parts already gone
    assert w.row_present(d)  # index record kept until every S3 version is gone
    assert any(v.key.endswith("worm-receipt.json") for v in w.prefix_versions(d))
    assert [r["outcome"] for r in w.audit if r["step"] == "s3-versions"] == ["starting", "failed"]

    w.errors.clear()
    assert _run(w, d) == prune.EXIT_OK
    assert w.prefix_versions(d) == []
    assert not w.row_present(d)
    second = [r for r in w.audit if r["step"] == "item"]
    assert second[-1]["outcome"] == "deleted"


def test_ecr_copy_kept_while_any_s3_version_is_retained() -> None:
    """[RETAIN-010.03]"""
    w = World(worm=True)
    w.add_other_project_row()
    d = w.add_bundle()
    w.prefix_versions(d)[1].retain_until = NOW + timedelta(days=30)
    assert _run(w, d) == prune.EXIT_ITEM_FAILED
    assert w.deletes() == []
    assert d in w.images


def test_unarchived_manifest_deletes_only_that_image() -> None:
    w = World()
    w.add_other_project_row()
    junk = w.add_image("junk")
    keep = w.add_bundle()
    assert _run(w, junk) == prune.EXIT_OK
    assert w.deletes() == [("delete:ecr", junk)]
    assert keep in w.images


def test_failed_ecr_delete_skips_every_later_step() -> None:
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    w.fail("batch_delete_image", d, "AccessDeniedException")
    assert _run(w, d) == prune.EXIT_ITEM_FAILED
    assert not any(kind == "delete:s3" for kind, _ in w.deletes())
    assert w.row_present(d)


@pytest.mark.parametrize(
    ("method", "step"),
    [
        ("batch_delete_image", "ecr-manifest"),
        ("delete_object", "s3-versions"),
        ("query", "control-claims"),
        ("delete_item", "index-record"),
    ],
)
def test_network_error_in_a_delete_step_is_audited_and_reported(
    method: str, step: str, capsys
) -> None:
    """[RETAIN-010] / [RETAIN-011] a BotoCoreError fails the step; it never escapes."""
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    key = {
        "batch_delete_image": d,
        "delete_object": f"{ORG}/{TENANT}/{PROJECT}/{d}/manifest.json",
        "query": CONTROLS_TABLE,
        "delete_item": f"{PROJECT}#{d}",
    }[method]
    w.fail(method, key, NETWORK_ERROR)
    assert _run(w, d) == prune.EXIT_ITEM_FAILED
    failed = [r for r in w.audit if r["outcome"] == "failed"]
    assert [r["step"] for r in failed] == [step]
    assert "Could not connect to the endpoint URL" in failed[0]["detail"]
    assert f"step {step}" in capsys.readouterr().out
    assert not any(r["step"] == "item" for r in w.audit)


def test_network_error_on_an_audit_write_stops_deletion(capsys) -> None:
    """[RETAIN-011.02] a BotoCoreError on the audit write is an audit-write failure."""
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    w.fail("put_log_events", "ecr-referrers/starting", NETWORK_ERROR)
    assert _run(w, d) == prune.EXIT_ITEM_FAILED
    assert w.deletes() == []
    assert "no further delete" in capsys.readouterr().err


# ── [RETAIN-011] audit ───────────────────────────────────────────────────────


def test_every_named_item_is_audited_with_the_sts_caller() -> None:
    """[RETAIN-011.01] one deleted, one refused, one failed."""
    w = World()
    w.add_other_project_row()
    ok = w.add_bundle("ok")
    retained = w.add_bundle("retained", archived_at=NOW - timedelta(days=5))
    broken = w.add_bundle("broken")
    w.fail("batch_delete_image", broken, "InternalError")

    assert _run(w, ok, retained, broken) == prune.EXIT_ITEM_FAILED

    final = {}
    for r in w.audit:
        assert r["callerArn"] == PRUNER_SESSION_ARN
        assert r["reason"]
        final[r["digest"]] = r
    assert final[ok]["outcome"] == "deleted" and final[ok]["class"] == "expired-bundle"
    assert final[retained]["outcome"] == "refused"
    assert final[broken]["outcome"] == "failed" and "InternalError" in final[broken]["detail"]


def test_audit_precedes_every_delete_step() -> None:
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    assert _run(w, d) == prune.EXIT_OK
    started = set()
    for kind, target in w.ops:
        if kind == "audit" and target.endswith(":starting"):
            started.add(target.split(":")[2])
        if kind.startswith("delete:"):
            step = {
                "delete:s3": "s3-versions",
                "delete:claims": "control-claims",
                "delete:index": "index-record",
            }.get(kind)
            if step:
                assert step in started


def test_audit_names_every_deleted_referrer_version_and_key() -> None:
    """[RETAIN-011] the audit trail shows exactly what each step removed."""
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    referrers = set(w.images) - {d}  # the bundle's referrers and its tag-schema index
    versions = {f"{v.key}?versionId={v.version_id}" for v in w.prefix_versions(d)}
    assert _run(w, d) == prune.EXIT_OK

    done = {r["step"]: r["deleted"] for r in w.audit if r["outcome"] == "done"}
    assert set(done["ecr-referrers"]) == referrers
    assert done["ecr-manifest"] == [d]
    assert set(done["s3-versions"]) == versions
    assert len(done["control-claims"]) == 2
    assert done["index-record"] == [f"{TENANT}/{PROJECT}#{d}"]
    # Every deleted ECR image and S3 version appears in some audit record.
    deleted = {x for names in done.values() for x in names}
    for kind, target in w.deletes():
        if kind == "delete:ecr":
            assert target in deleted
        if kind == "delete:s3":
            key, vid = target.split("?")
            assert f"{key}?versionId={vid}" in deleted


def test_failed_step_audits_what_it_deleted_before_failing() -> None:
    """[RETAIN-011] a partial S3 step names the versions it removed."""
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    w.fail("delete_object", f"{ORG}/{TENANT}/{PROJECT}/{d}/worm-receipt.json", "SlowDown")
    assert _run(w, d) == prune.EXIT_ITEM_FAILED
    failed = next(r for r in w.audit if r["outcome"] == "failed")
    assert failed["step"] == "s3-versions"
    assert len(failed["deleted"]) == 3  # every non-receipt version; the receipt is last
    assert all("worm-receipt.json" not in x for x in failed["deleted"])


def test_audit_write_failure_stops_the_delete_step(capsys) -> None:
    """[RETAIN-011.02]"""
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    w.fail("put_log_events", "s3-versions/starting", "ServiceUnavailableException")
    assert _run(w, d) == prune.EXIT_ITEM_FAILED
    assert "audit write failed" in capsys.readouterr().err
    assert not any(kind == "delete:s3" for kind, _ in w.deletes())
    assert w.row_present(d)


def test_audit_failure_stops_the_remaining_items_too() -> None:
    w = World()
    w.add_other_project_row()
    first, second = w.add_bundle("first"), w.add_bundle("second")
    w.fail("put_log_events", "*", "ServiceUnavailableException")
    assert _run(w, first, second) == prune.EXIT_ITEM_FAILED
    assert w.deletes() == []


def test_audit_stream_creation_failure_deletes_nothing() -> None:
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    w.fail("create_log_stream", "*", "AccessDeniedException")
    assert _run(w, d) == prune.EXIT_ITEM_FAILED
    assert w.deletes() == []


# ── discovery and output safety ──────────────────────────────────────────────


def test_missing_retention_setting_is_a_discovery_error(capsys) -> None:
    w = World()
    w.params.pop("/cairnproofs4af/alpha/retention/period-days")
    assert _run(w) == prune.EXIT_DISCOVERY
    assert "retention/period-days" in capsys.readouterr().err


def test_duplicate_digests_are_pruned_once() -> None:
    w = World()
    w.add_other_project_row()
    junk = w.add_image("junk")
    assert _run(w, junk, junk) == prune.EXIT_OK
    assert w.deletes() == [("delete:ecr", junk)]


def test_output_escapes_control_characters() -> None:
    assert prune._safe("ok\x1b[31mred\x07") == "ok\\x1b[31mred\\x07"


def test_unknown_digest_is_refused_not_failed() -> None:
    w = World()
    w.add_other_project_row()
    assert _run(w, digest_of("nope")) == prune.EXIT_ITEM_FAILED
    assert w.audit[-1]["outcome"] == "refused"


# ── pagination, failed steps, discovery, and session (tester gap fill) ────────


class _PagedQueryDdb(FakeDdb):
    """ControlClaims Query returns one row per page (LastEvaluatedKey)."""

    def query(self, *, TableName: str, ExpressionAttributeValues: dict, **kw):
        resp = super().query(
            TableName=TableName,
            ExpressionAttributeValues=ExpressionAttributeValues,
            **{k: v for k, v in kw.items() if k != "ExclusiveStartKey"},
        )
        if TableName != CONTROLS_TABLE:
            return resp
        i = int(kw.get("ExclusiveStartKey", {}).get("i", 0))
        page = {"Items": resp["Items"][i : i + 1], "Count": 1}
        if i + 1 < len(resp["Items"]):
            page["LastEvaluatedKey"] = {"i": str(i + 1)}
        return page


def _session_with(w: World, **overrides):
    session = w.session()
    real = session.client

    def client(name: str):
        if name in overrides:
            session.clients_requested.append(name)
            return overrides[name](w)
        return real(name)

    session.client = client  # type: ignore[method-assign]
    return session


def test_control_claims_on_later_query_pages_are_all_deleted() -> None:
    """[RETAIN-010.01] every ControlClaims row goes, not only the first page."""
    w = World()
    w.add_other_project_row()
    d = w.add_bundle(controls=("ac-2", "au-4", "si-7"))
    session = _session_with(w, dynamodb=_PagedQueryDdb)
    assert prune._run(_args(d), session=session, now=lambda: NOW) == prune.EXIT_OK
    claims = [t for kind, t in w.deletes() if kind == "delete:claims"]
    assert len(claims) == 3
    assert not any(t == CONTROLS_TABLE and d in s for (t, _p, s) in w.items)


class _StickyS3(FakeS3):
    """DeleteObject reports success but leaves the version in place."""

    def delete_object(self, *, Bucket: str, Key: str, VersionId: str) -> dict:
        self.w.ops.append(("delete:s3", f"{Key}?{VersionId}"))
        return {}


def test_versions_remaining_after_delete_halt_before_index_rows() -> None:
    """[RETAIN-010.03] the index record stays until S3 reports zero versions."""
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    session = _session_with(w, s3=_StickyS3)
    assert prune._run(_args(d), session=session, now=lambda: NOW) == prune.EXIT_ITEM_FAILED
    failed = [r for r in w.audit if r["outcome"] == "failed"]
    assert failed and failed[-1]["step"] == "s3-versions"
    assert "remain under" in failed[-1]["detail"]
    assert w.row_present(d)
    assert not any(kind in {"delete:claims", "delete:index"} for kind, _ in w.deletes())


def test_ecr_failure_entry_in_a_successful_response_halts() -> None:
    """BatchDeleteImage returns failures in-band; anything but ImageNotFound is a failure."""
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    session = w.session()
    real = session.client

    def client(name: str):
        c = real(name)
        if name == "ecr":
            c.batch_delete_image = lambda **kw: {  # type: ignore[method-assign]
                "imageIds": [],
                "failures": [
                    {
                        "imageId": kw["imageIds"][0],
                        "failureCode": "ImageReferencedByManifestList",
                        "failureReason": "still referenced",
                    }
                ],
            }
        return c

    session.client = client  # type: ignore[method-assign]
    assert prune._run(_args(d), session=session, now=lambda: NOW) == prune.EXIT_ITEM_FAILED
    failed = [r for r in w.audit if r["outcome"] == "failed"]
    assert failed and "ImageReferencedByManifestList" in failed[0]["detail"]
    assert w.deletes() == []
    assert w.row_present(d) and w.prefix_versions(d)


class _RejectingLogs(FakeLogs):
    def put_log_events(self, **kw) -> dict:
        super().put_log_events(**kw)
        return {"rejectedLogEventsInfo": {"tooOldLogEventEndIndex": 0}}


def test_rejected_audit_event_stops_every_delete(capsys) -> None:
    """[RETAIN-011.02] an in-band rejection is an audit-write failure."""
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    session = _session_with(w, logs=_RejectingLogs)
    assert prune._run(_args(d), session=session, now=lambda: NOW) == prune.EXIT_ITEM_FAILED
    assert "audit write rejected" in capsys.readouterr().err
    assert w.deletes() == []


def test_resume_after_control_claims_failure_finishes_the_item() -> None:
    """[RETAIN-010.02] ECR and S3 gone, claims and index left: the re-run completes."""
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    first_claim = next(s for (t, _p, s) in w.items if t == CONTROLS_TABLE)
    w.fail("delete_item", first_claim, "ProvisionedThroughputExceededException")
    assert _run(w, d) == prune.EXIT_ITEM_FAILED
    assert w.row_present(d)
    assert d not in w.images and w.prefix_versions(d) == []

    w.errors.clear()
    assert _run(w, d) == prune.EXIT_OK
    assert not w.row_present(d)
    assert not any(t == CONTROLS_TABLE for (t, _p, _s) in w.items)


class _FailingSts:
    def __init__(self, w: World) -> None:
        pass

    def get_caller_identity(self) -> dict:
        raise client_error("ExpiredToken", "GetCallerIdentity")


def test_sts_error_refuses_delete(capsys) -> None:
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    session = _session_with(w, sts=_FailingSts)
    assert prune._run(_args(d), session=session, now=lambda: NOW) == prune.EXIT_NOT_PRUNER
    assert "STS caller identity" in capsys.readouterr().err
    assert w.deletes() == [] and w.audit == []


@pytest.mark.parametrize("suffix", ["prune/pruner-role-arn", "prune/audit-log-group"])
def test_missing_prune_parameter_is_a_discovery_error(suffix: str, capsys) -> None:
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    w.params.pop(f"/cairnproofs4af/alpha/{suffix}")
    assert _run(w, d) == prune.EXIT_DISCOVERY
    assert suffix in capsys.readouterr().err
    assert w.deletes() == []


@pytest.mark.parametrize("value", ["thirty", "-1", "", "2.5"])
def test_non_integer_window_setting_is_a_discovery_error(value: str) -> None:
    w = World()
    w.params["/cairnproofs4af/alpha/retention/unarchived-manifest-window-days"] = value
    assert _run(w) == prune.EXIT_DISCOVERY


class _LockConfigDeniedS3(FakeS3):
    def get_object_lock_configuration(self, *, Bucket: str) -> dict:
        raise client_error("AccessDenied", "GetObjectLockConfiguration")


def test_unreadable_object_lock_configuration_is_a_discovery_error(capsys) -> None:
    """WORM_Enabled is never assumed false on a read error."""
    w = World(worm=True)
    w.add_other_project_row()
    session = _session_with(w, s3=_LockConfigDeniedS3)
    assert prune._run(_args(), session=session, now=lambda: NOW) == prune.EXIT_DISCOVERY
    assert "Object Lock configuration" in capsys.readouterr().err


def test_report_enumeration_error_exits_non_zero(capsys) -> None:
    w = World()
    w.add_other_project_row()
    w.add_bundle()
    session = w.session()
    real = session.client

    def client(name: str):
        c = real(name)
        if name == "ecr":

            def boom(_name: str):
                raise client_error("AccessDeniedException", "ListImages")

            c.get_paginator = boom  # type: ignore[method-assign]
        return c

    session.client = client  # type: ignore[method-assign]
    assert prune._run(_args(), session=session, now=lambda: NOW) == prune.EXIT_ITEM_FAILED
    assert "cannot enumerate the project" in capsys.readouterr().err


def test_session_lifetime_is_rechecked_before_each_item() -> None:
    """M35: a later item is refused once the session nears expiry; earlier ones are done."""
    w = World()
    w.add_other_project_row()
    first, second = w.add_bundle("first"), w.add_bundle("second")
    # Every clock read (preflight and each audit record) advances two minutes,
    # so the first item's ~13 audit records consume the session's lifetime.
    ticks = iter([NOW + timedelta(minutes=2 * m) for m in range(0, 1000)])
    current = {"t": NOW}

    def clock():
        current["t"] = next(ticks)
        return current["t"]

    session = w.session(expiry=NOW + timedelta(minutes=20))
    assert prune._run(_args(first, second), session=session, now=clock) == prune.EXIT_ITEM_FAILED
    final = {r["digest"]: r for r in w.audit}
    assert final[first]["outcome"] == "deleted"
    assert final[second]["step"] == "preflight" and final[second]["outcome"] == "refused"
    assert w.row_present(second) and second in w.images


def test_naive_credential_expiry_is_read_as_utc() -> None:
    w = World()
    naive = datetime(2034, 1, 1, 0, 30)
    session = w.session(expiry=naive)
    assert prune._session_expiry(session) == naive.replace(tzinfo=UTC)
    assert prune._session_expiry(None) is None


def test_deletion_uses_only_derived_coordinates() -> None:
    """[RETAIN-012.01] every S3 and index delete targets the derived prefix and key."""
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    assert _run(w, d) == prune.EXIT_OK
    for kind, target in w.deletes():
        if kind == "delete:s3":
            assert target.startswith(f"{ORG}/{TENANT}/{PROJECT}/{d}/")
        if kind == "delete:index":
            assert target == f"{PROJECT}#{d}"


@pytest.mark.parametrize(
    "caller",
    [
        # Same role name, another account.
        "arn:aws:sts::222222222222:assumed-role/cairnproofs4af-pruner-cmh-alpha/dana",
        # Role name that only starts with the pruner role name.
        "arn:aws:sts::111111111111:assumed-role/cairnproofs4af-pruner-cmh-alpha-evil/dana",
        # Another partition.
        "arn:aws-us-gov:sts::111111111111:assumed-role/cairnproofs4af-pruner-cmh-alpha/dana",
        # The role ARN itself, or an IAM user of that name, is not a session.
        PRUNER_ROLE_ARN,
        "arn:aws:iam::111111111111:user/cairnproofs4af-pruner-cmh-alpha",
    ],
)
def test_is_pruner_session_rejects_near_misses(caller: str) -> None:
    """[ACCESS-007] only an STS session of exactly the pruner role, in its account."""
    assert not prune._is_pruner_session(caller, PRUNER_ROLE_ARN)


def test_near_miss_session_deletes_nothing_and_never_prompts(capsys) -> None:
    """A role-name-prefix collision is neither a pruner session nor an IAM user."""
    w = World(
        caller_arn="arn:aws:sts::111111111111:assumed-role/cairnproofs4af-pruner-cmh-alpha-evil/x"
    )
    w.add_other_project_row()
    d = w.add_bundle()
    assert _run(w, d) == prune.EXIT_NOT_PRUNER
    assert w.deletes() == []
    assert w.assume_calls == []
    assert w.audit == []


@pytest.mark.parametrize("value", ["\u00b2", "1\u00b2", "\u0661"])
def test_unicode_digit_setting_is_a_discovery_error(value: str) -> None:
    """[RETAIN-007] ASCII digits only; a Unicode digit is a discovery error, not a crash."""
    # Covers: RETAIN-007.04
    w = World()
    w.params["/cairnproofs4af/alpha/retention/period-days"] = value
    assert _run(w) == prune.EXIT_DISCOVERY


@pytest.mark.parametrize(
    ("suffix", "value"),
    [
        ("retention/period-days", "0"),
        ("retention/period-days", "36136"),
        ("retention/unarchived-manifest-window-days", "0"),
        ("retention/unarchived-manifest-window-days", "366"),
    ],
)
def test_out_of_bounds_setting_is_a_discovery_error(suffix: str, value: str, capsys) -> None:
    """[RETAIN-007] a window of 0 would make freshly pushed manifests candidates."""
    # Covers: RETAIN-007.04
    w = World()
    w.add_other_project_row()
    fresh = w.add_image("fresh", pushed_at=NOW)
    w.params[f"/cairnproofs4af/alpha/{suffix}"] = value
    assert _run(w, fresh) == prune.EXIT_DISCOVERY
    assert suffix in capsys.readouterr().err
    assert w.deletes() == []
    assert w.audit == []


@pytest.mark.parametrize(
    "suffix", ["retention/period-days", "retention/unarchived-manifest-window-days"]
)
def test_missing_setting_is_a_discovery_error(suffix: str, capsys) -> None:
    # Covers: RETAIN-007.04
    w = World()
    w.add_other_project_row()
    junk = w.add_image("junk")
    del w.params[f"/cairnproofs4af/alpha/{suffix}"]
    assert _run(w, junk) == prune.EXIT_DISCOVERY
    assert suffix in capsys.readouterr().err
    assert w.deletes() == []


def test_index_remnant_deletes_claims_then_index_with_audit() -> None:
    """ECR and S3 gone after an interrupted prune; the re-run removes the rows."""
    # Covers: RETAIN-010.04
    w = World()
    w.add_other_project_row()
    d = w.add_bundle()
    for img in [i for i in w.images.values() if i.digest != d]:
        del w.images[img.digest]
    del w.images[d]
    w.versions = [v for v in w.versions if v not in w.prefix_versions(d)]
    assert _run(w, d) == prune.EXIT_OK
    kinds = [kind for kind, _ in w.deletes()]
    assert kinds == ["delete:claims", "delete:claims", "delete:index"]
    assert not w.row_present(d)
    steps = [(r["step"], r["outcome"]) for r in w.audit if r["digest"] == d]
    assert steps == [
        ("s3-versions", "starting"),
        ("s3-versions", "done"),
        ("control-claims", "starting"),
        ("control-claims", "done"),
        ("index-record", "starting"),
        ("index-record", "done"),
        ("item", "deleted"),
    ]


def test_index_remnant_under_retention_is_refused_and_kept() -> None:
    # Covers: RETAIN-010.04
    w = World()
    w.add_other_project_row()
    d = w.add_bundle(archived_at=NOW - timedelta(days=5))
    w.images.clear()
    w.versions = [v for v in w.versions if v not in w.prefix_versions(d)]
    assert _run(w, d) == prune.EXIT_ITEM_FAILED
    assert w.deletes() == []
    assert w.row_present(d)


def test_bundle_with_an_archived_referrer_is_refused_and_nothing_is_deleted(capsys) -> None:
    """A producer-forged subject cannot make prune delete another bundle's parts."""
    # Covers: RETAIN-010.05
    w = World()
    w.add_other_project_row()
    a = w.add_bundle("a")  # expired
    b = w.add_bundle("b", archived_at=NOW - timedelta(days=5))  # under retention
    w.images[b].manifest["subject"] = {
        "digest": a,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "size": 1,
    }
    images_before = set(w.images)
    versions_before = len(w.versions)
    assert _run(w, a) == prune.EXIT_ITEM_FAILED
    assert b in capsys.readouterr().out
    assert w.deletes() == []
    assert set(w.images) == images_before
    assert len(w.versions) == versions_before
    assert w.row_present(a) and w.row_present(b)
    refused = next(r for r in w.audit if r["digest"] == a)
    assert refused["outcome"] == "refused" and b in refused["reason"]


@pytest.mark.parametrize(
    ("suffix", "value"),
    [
        ("retention/period-days", "1"),
        ("retention/period-days", "36135"),
        ("retention/unarchived-manifest-window-days", "1"),
        ("retention/unarchived-manifest-window-days", "365"),
    ],
)
def test_in_bounds_setting_edges_are_accepted(suffix: str, value: str) -> None:
    w = World()
    w.add_other_project_row()
    w.params[f"/cairnproofs4af/alpha/{suffix}"] = value
    assert _run(w) == prune.EXIT_OK


# ── aggregate-gate tester gap fill ───────────────────────────────────────────


@pytest.mark.parametrize("expiration", [None, "2034-01-01T01:00:00Z"])
def test_assume_role_without_a_datetime_expiry_refuses(
    expiration, pruner_session_factory, monkeypatch, capsys
) -> None:
    """M35: an expiry that cannot be read is never assumed to be long enough."""
    from tests.unit.cairn_cli import prune_fakes

    w = World(caller_arn=IAM_USER_ARN)
    made = pruner_session_factory(w)
    w.add_other_project_row()
    d = w.add_bundle()
    real = prune_fakes.FakeSts.assume_role

    def assume_role(self, **kw):
        resp = real(self, **kw)
        resp["Credentials"]["Expiration"] = expiration
        return resp

    monkeypatch.setattr(prune_fakes.FakeSts, "assume_role", assume_role)
    rc, _ = _run_as_user(w, d)
    assert rc == prune.EXIT_NOT_PRUNER
    assert "no credential expiry" in capsys.readouterr().err
    assert made == []  # no session was built over the credentials
    assert w.deletes() == [] and w.audit == []


def test_report_omits_items_that_are_plainly_ineligible(capsys) -> None:
    """[RETAIN-005.01] a bundle under retention is neither a candidate nor a failed check."""
    w = World()
    w.add_other_project_row()
    expired = w.add_bundle("expired")
    retained = w.add_bundle("retained", archived_at=NOW - timedelta(days=5))
    assert _run(w) == prune.EXIT_OK
    out = capsys.readouterr().out
    assert expired in out
    assert retained not in out
    assert "Checks that failed" not in out
    assert w.deletes() == [] and w.audit == []


def test_report_under_unproven_coordinates_lists_no_unarchived_candidate(capsys) -> None:
    """M11 / M32: with no index row in the partition, absences are not trusted."""
    w = World()
    # An S3 object under the project prefix but no Metadata_Index row: unproven.
    w.put_version(f"{ORG}/{TENANT}/{PROJECT}/sha256:{'f' * 64}/manifest.json")
    junk = w.add_image("junk")
    assert _run(w) == prune.EXIT_ITEM_FAILED
    out = capsys.readouterr().out
    assert "Prune_Candidates in" in out and ": 0" in out.splitlines()[0]
    assert "Checks that failed" in out
    assert junk in out and "coordinates unproven" in out
    assert w.deletes() == []


def test_unproven_coordinates_refuse_an_unarchived_delete() -> None:
    """M11 / M32 at delete time: the re-classification refuses and deletes nothing."""
    w = World()
    w.put_version(f"{ORG}/{TENANT}/{PROJECT}/sha256:{'f' * 64}/manifest.json")
    junk = w.add_image("junk")
    assert _run(w, junk) == prune.EXIT_ITEM_FAILED
    assert junk in w.images
    assert w.deletes() == []
    refused = [r for r in w.audit if r["outcome"] == "refused"]
    assert len(refused) == 1 and "coordinates unproven" in refused[0]["reason"]


def test_refused_item_does_not_stop_a_later_candidate() -> None:
    """[RETAIN-005] item 3: refuse and report one item; the other named items continue."""
    w = World()
    w.add_other_project_row()
    retained = w.add_bundle("retained", archived_at=NOW - timedelta(days=5))
    junk = w.add_image("junk")
    assert _run(w, retained, junk) == prune.EXIT_ITEM_FAILED
    assert w.deletes() == [("delete:ecr", junk)]
    assert retained in w.images and w.row_present(retained)
    outcomes = {r["digest"]: r["outcome"] for r in w.audit if r["step"] in {"classify", "item"}}
    assert outcomes == {retained: "refused", junk: "deleted"}

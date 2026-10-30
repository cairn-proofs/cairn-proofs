"""Tests for cairn_cli.verify — trust policy pre-flight + verify_artifact."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from cairn_cli import main as main_module
from cairn_cli import verify as verify_module

from tests.unit.cairn_cli.conftest import subparser_help


def _make_args(reference: str) -> argparse.Namespace:
    return argparse.Namespace(reference=reference, stage="alpha", region_code="cmh", app_name=None)


class _FakeOras:
    """Minimal oras stand-in so verify's ECR login does not shell out."""

    class OrasCommandError(RuntimeError):
        def __init__(self, *, returncode: int, stderr: str, command: list[str]) -> None:
            super().__init__(stderr)
            self.returncode = returncode
            self.stderr = stderr
            self.command = command

    def __init__(self, *, login_should_fail: str | None = None) -> None:
        self._login_should_fail = login_should_fail
        self.login_calls: list[dict[str, str]] = []

    def login(self, *, registry: str, username: str, password: str) -> None:
        if self._login_should_fail is not None:
            raise self.OrasCommandError(returncode=1, stderr=self._login_should_fail, command=[])
        self.login_calls.append({"registry": registry, "username": username})


def test_verify_help_has_usage_examples_and_concrete_example() -> None:
    help_text = subparser_help(main_module.build_parser(), "verify")
    assert "usage" in help_text.lower()
    assert "Examples:" in help_text
    assert "cairn verify alpha-bank/assessment-results:2026-q1" in help_text


def _verify_ok(_reference: str) -> None:
    return None


def _verify_fail(_reference: str) -> None:
    from cairn_signing import NotationCommandError

    raise NotationCommandError(returncode=1, stderr="strict verification failed", command=[])


def test_missing_trust_policy_emits_install_hint(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "no-such" / "trustpolicy.json"
    args = _make_args("alpha-bank/assessment-results:2026-q1--ac-2--1")
    rc = verify_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        verify_fn=_verify_ok,
        trust_policy_path=missing,
    )
    assert rc == 4
    err = capsys.readouterr().err
    assert "trust policy" in err.lower()
    assert "/cairnproofs4af/alpha/notation/trust-policy" in err


def test_verify_success_path(aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    policy = tmp_path / "trustpolicy.json"
    policy.write_text("{}")
    args = _make_args("alpha-bank/assessment-results:2026-q1--ac-2--1")
    fake_oras = _FakeOras()
    rc = verify_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        oras_module=fake_oras,
        verify_fn=_verify_ok,
        trust_policy_path=policy,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "verified:" in out
    # verify authenticated the credential store itself (no ambient docker login).
    assert len(fake_oras.login_calls) == 1


def test_verify_failure_surfaces_notation_error(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy = tmp_path / "trustpolicy.json"
    policy.write_text("{}")
    args = _make_args("alpha-bank/assessment-results:2026-q1--ac-2--1")
    rc = verify_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        oras_module=_FakeOras(),
        verify_fn=_verify_fail,
        trust_policy_path=policy,
    )
    assert rc == 4
    err = capsys.readouterr().err
    assert "strict verification failed" in err


def test_verify_invalid_reference_returns_2(aws_stack, tmp_path: Path) -> None:
    policy = tmp_path / "trustpolicy.json"
    policy.write_text("{}")
    args = _make_args("totally-bogus")
    rc = verify_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        verify_fn=_verify_ok,
        trust_policy_path=policy,
    )
    assert rc == 2


def test_verify_login_failure_surfaces_exit_4(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy = tmp_path / "trustpolicy.json"
    policy.write_text("{}")
    args = _make_args("alpha-bank/assessment-results:2026-q1--ac-2--1")
    rc = verify_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        oras_module=_FakeOras(login_should_fail="denied: not authorized"),
        verify_fn=_verify_ok,
        trust_policy_path=policy,
    )
    assert rc == 4
    assert "ECR login failed" in capsys.readouterr().err


# --- Multi-signature aggregation rendering (task 3.2) -----------------------

_BY_DIGEST_REF = "alpha-bank/assessment-results@sha256:" + "a" * 64


def _classify_returning(results):
    """Build an injectable classifier that ignores its inputs and returns ``results``."""

    def classify(_ecr, **_kwargs):
        return results

    return classify


def _verify_with_signatures(results, tmp_path: Path, aws_stack):
    from cairn_signing import SignatureResult  # noqa: F401 — imported for type clarity

    policy = tmp_path / "trustpolicy.json"
    policy.write_text("{}")
    args = _make_args(_BY_DIGEST_REF)
    return verify_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        oras_module=_FakeOras(),
        verify_fn=_verify_ok,
        trust_policy_path=policy,
        classify_signatures_fn=_classify_returning(results),
    )


def _sig(profile_name: str, status):
    from cairn_signing import SignatureResult

    return SignatureResult(
        profile_arn=f"sha256:{profile_name}",
        profile_name=profile_name,
        status=status,
        signing_timestamp="2026-06-05T00:00:00Z",
    )


def test_verify_renders_signed_pipeline_only(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cairn_signing import PIPELINE_PROFILE_NAME, SignatureStatus

    rc = _verify_with_signatures(
        [_sig(PIPELINE_PROFILE_NAME, SignatureStatus.VALID)], tmp_path, aws_stack
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "verified:" in out
    assert "signingStatus: signed" in out
    assert f"signatureDetails: {PIPELINE_PROFILE_NAME} [unknown] valid" in out


def test_verify_renders_signed_pipeline_plus_signoff(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cairn_signing import PIPELINE_PROFILE_NAME, REVIEWER_PROFILE_NAME, SignatureStatus

    rc = _verify_with_signatures(
        [
            _sig(PIPELINE_PROFILE_NAME, SignatureStatus.VALID),
            _sig(REVIEWER_PROFILE_NAME, SignatureStatus.VALID),
        ],
        tmp_path,
        aws_stack,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "signingStatus: signed" in out
    assert f"signatureDetails: {PIPELINE_PROFILE_NAME} [unknown] valid" in out
    assert f"signatureDetails: {REVIEWER_PROFILE_NAME} [unknown] valid" in out


def test_verify_renders_partial_when_one_revoked(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cairn_signing import PIPELINE_PROFILE_NAME, REVIEWER_PROFILE_NAME, SignatureStatus

    rc = _verify_with_signatures(
        [
            _sig(PIPELINE_PROFILE_NAME, SignatureStatus.VALID),
            _sig(REVIEWER_PROFILE_NAME, SignatureStatus.REVOKED),
        ],
        tmp_path,
        aws_stack,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "signingStatus: partial" in out
    assert f"signatureDetails: {REVIEWER_PROFILE_NAME} [unknown] revoked" in out


def test_verify_renders_revoked_when_all_revoked(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cairn_signing import PIPELINE_PROFILE_NAME, SignatureStatus

    rc = _verify_with_signatures(
        [_sig(PIPELINE_PROFILE_NAME, SignatureStatus.REVOKED)], tmp_path, aws_stack
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "signingStatus: revoked" in out


def test_verify_default_classifier_enumerates_referrers(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The PRODUCTION classifier walks list_image_referrers (no injection)."""

    import base64

    class _StubEcr:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def get_authorization_token(self):
            token = base64.b64encode(b"AWS:pw").decode()
            return {"authorizationData": [{"authorizationToken": token}]}

        def list_image_referrers(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "referrers": [
                    {
                        "artifactType": verify_module.NOTARY_SIGNATURE_ARTIFACT_TYPE,
                        "imageDigest": "sha256:" + "b" * 64,
                    },
                    {"artifactType": "application/vnd.oci.empty.v1+json"},  # ignored
                ]
            }

    stub_ecr = _StubEcr()
    policy = tmp_path / "trustpolicy.json"
    policy.write_text("{}")
    args = _make_args(_BY_DIGEST_REF)
    rc = verify_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=stub_ecr,
        oras_module=_FakeOras(),
        verify_fn=_verify_ok,
        trust_policy_path=policy,
        # No classify_signatures_fn → exercises the real _classify_signatures.
    )
    assert rc == 0
    out = capsys.readouterr().out
    # Exactly one notary.signature referrer counted → signed.
    assert "signingStatus: signed" in out
    # Enumeration was keyed on the resolved subject digest, not the index.
    assert stub_ecr.calls
    assert stub_ecr.calls[0]["subjectId"]["imageDigest"] == "sha256:" + "a" * 64


def test_classify_signatures_walks_nexttoken_pagination(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real classifier paginates list_image_referrers via nextToken."""

    import base64

    class _PagedEcr:
        def __init__(self) -> None:
            self.tokens_seen: list[str | None] = []

        def get_authorization_token(self):
            token = base64.b64encode(b"AWS:pw").decode()
            return {"authorizationData": [{"authorizationToken": token}]}

        def list_image_referrers(self, **kwargs):
            self.tokens_seen.append(kwargs.get("nextToken"))
            sig = {
                "artifactType": verify_module.NOTARY_SIGNATURE_ARTIFACT_TYPE,
                "imageDigest": "sha256:" + "d" * 64,
            }
            if "nextToken" not in kwargs:
                # First page: one signature + a continuation token.
                return {"referrers": [sig], "nextToken": "page-2"}
            # Second (last) page: one more signature, no token.
            return {"referrers": [{**sig, "imageDigest": "sha256:" + "e" * 64}]}

    stub_ecr = _PagedEcr()
    policy = tmp_path / "trustpolicy.json"
    policy.write_text("{}")
    args = _make_args(_BY_DIGEST_REF)
    rc = verify_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=stub_ecr,
        oras_module=_FakeOras(),
        verify_fn=_verify_ok,
        trust_policy_path=policy,
    )
    assert rc == 0
    out = capsys.readouterr().out
    # Both pages' signatures were enumerated (still all-valid → signed) and the
    # walk visited the continuation token (so paginated registries aren't
    # silently truncated to the first page).
    assert "signingStatus: signed" in out
    assert stub_ecr.tokens_seen == [None, "page-2"]
    # Two signatureDetails rendered, one per page.
    assert out.count("signatureDetails:") == 2


def test_classify_signatures_reads_reviewer_profile_from_annotation(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A referrer annotated with the reviewer profile renders as the reviewer profile."""
    import base64

    from cairn_signing import PIPELINE_PROFILE_NAME, REVIEWER_PROFILE_NAME

    class _AnnotatedEcr:
        def get_authorization_token(self):
            token = base64.b64encode(b"AWS:pw").decode()
            return {"authorizationData": [{"authorizationToken": token}]}

        def list_image_referrers(self, **kwargs):
            return {
                "referrers": [
                    {
                        "artifactType": verify_module.NOTARY_SIGNATURE_ARTIFACT_TYPE,
                        "imageDigest": "sha256:" + "f" * 64,
                        "annotations": {"com.cairn.profile": REVIEWER_PROFILE_NAME},
                    },
                    {
                        "artifactType": verify_module.NOTARY_SIGNATURE_ARTIFACT_TYPE,
                        "imageDigest": "sha256:" + "0" * 64,
                    },
                ]
            }

    policy = tmp_path / "trustpolicy.json"
    policy.write_text("{}")
    args = _make_args(_BY_DIGEST_REF)
    rc = verify_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=_AnnotatedEcr(),
        oras_module=_FakeOras(),
        verify_fn=_verify_ok,
        trust_policy_path=policy,
    )
    assert rc == 0
    out = capsys.readouterr().out
    # Annotated referrer → reviewer; un-annotated → pipeline fallback. Both sign
    # the content manifest (no attestation referrer present) → role=content.
    assert f"signatureDetails: {REVIEWER_PROFILE_NAME} [content] valid" in out
    assert f"signatureDetails: {PIPELINE_PROFILE_NAME} [content] valid" in out


def test_verify_degraded_referrer_enumeration_keeps_success_verdict(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A referrer-enumeration error never turns a SUCCESSFUL verify into a failure."""

    class _BrokenEcr:
        def get_authorization_token(self):
            import base64

            token = base64.b64encode(b"AWS:pw").decode()
            return {"authorizationData": [{"authorizationToken": token}]}

        def list_image_referrers(self, **kwargs):
            raise RuntimeError("registry hiccup during referrer enumeration")

    policy = tmp_path / "trustpolicy.json"
    policy.write_text("{}")
    args = _make_args(_BY_DIGEST_REF)
    rc = verify_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=_BrokenEcr(),
        oras_module=_FakeOras(),
        verify_fn=_verify_ok,
        trust_policy_path=policy,
    )
    # The pass/fail verdict stands on its own; the roll-up degrades silently.
    assert rc == 0
    out = capsys.readouterr().out
    assert "verified:" in out
    assert "signingStatus:" not in out


# --- Two-signature trust verdict on the verify path --------------------------


def _roled_sig(profile_name: str, status, role):
    from cairn_signing import SignatureResult

    return SignatureResult(
        profile_arn=None,
        profile_name=profile_name,
        status=status,
        signing_timestamp="2026-06-05T00:00:00Z",
        role=role,
    )


def test_verify_renders_fully_trusted_when_content_and_attestation_signed(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cairn_signing import PIPELINE_PROFILE_NAME, SignatureRole, SignatureStatus

    rc = _verify_with_signatures(
        [
            _roled_sig(PIPELINE_PROFILE_NAME, SignatureStatus.VALID, SignatureRole.CONTENT),
            _roled_sig(PIPELINE_PROFILE_NAME, SignatureStatus.VALID, SignatureRole.ATTESTATION),
        ],
        tmp_path,
        aws_stack,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "trust: fully-trusted" in out
    assert "missing" not in out


def test_verify_reports_partial_when_only_content_signed(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Content-signed-but-attestation-unsigned is NEVER fully trusted.
    from cairn_signing import PIPELINE_PROFILE_NAME, SignatureRole, SignatureStatus

    rc = _verify_with_signatures(
        [_roled_sig(PIPELINE_PROFILE_NAME, SignatureStatus.VALID, SignatureRole.CONTENT)],
        tmp_path,
        aws_stack,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "trust: partial" in out
    assert "missing attestation signature" in out
    assert "trust: fully-trusted" not in out


def test_verify_reports_partial_when_only_attestation_signed(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cairn_signing import PIPELINE_PROFILE_NAME, SignatureRole, SignatureStatus

    rc = _verify_with_signatures(
        [_roled_sig(PIPELINE_PROFILE_NAME, SignatureStatus.VALID, SignatureRole.ATTESTATION)],
        tmp_path,
        aws_stack,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "trust: partial" in out
    assert "missing content signature" in out


def test_verify_reports_unsigned_when_no_signatures(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _verify_with_signatures([], tmp_path, aws_stack)
    assert rc == 0
    out = capsys.readouterr().out
    assert "trust: unsigned" in out


def test_real_classifier_assigns_content_and_attestation_roles_via_two_hops(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The production classifier walks BOTH the content and attestation subjects.

    The content digest carries a content signature + an Attestation referrer;
    the attestation digest carries the attestation signature. The two-hop walk
    must surface both roles → fully trusted.
    """
    import base64

    from cairn_oci import PRODUCER_ATTESTATION_ARTIFACT_TYPE

    content_digest = "sha256:" + "a" * 64
    attestation_digest = "sha256:" + "c" * 64

    class _TwoHopEcr:
        def __init__(self) -> None:
            self.subjects_seen: list[str] = []

        def get_authorization_token(self):
            token = base64.b64encode(b"AWS:pw").decode()
            return {"authorizationData": [{"authorizationToken": token}]}

        def list_image_referrers(self, **kwargs):
            subject = kwargs["subjectId"]["imageDigest"]
            self.subjects_seen.append(subject)
            if subject == content_digest:
                return {
                    "referrers": [
                        {
                            "artifactType": verify_module.NOTARY_SIGNATURE_ARTIFACT_TYPE,
                            "imageDigest": "sha256:" + "b" * 64,
                        },
                        {
                            "artifactType": PRODUCER_ATTESTATION_ARTIFACT_TYPE,
                            "imageDigest": attestation_digest,
                        },
                    ]
                }
            if subject == attestation_digest:
                return {
                    "referrers": [
                        {
                            "artifactType": verify_module.NOTARY_SIGNATURE_ARTIFACT_TYPE,
                            "imageDigest": "sha256:" + "d" * 64,
                        }
                    ]
                }
            return {"referrers": []}

    stub_ecr = _TwoHopEcr()
    policy = tmp_path / "trustpolicy.json"
    policy.write_text("{}")
    args = _make_args(_BY_DIGEST_REF)
    rc = verify_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=stub_ecr,
        oras_module=_FakeOras(),
        verify_fn=_verify_ok,
        trust_policy_path=policy,
    )
    assert rc == 0
    out = capsys.readouterr().out
    # Both subjects were walked (content first, then the attestation referrer).
    assert content_digest in stub_ecr.subjects_seen
    assert attestation_digest in stub_ecr.subjects_seen
    # One content-role signature + one attestation-role signature → fully trusted.
    assert "trust: fully-trusted" in out
    assert "[content] valid" in out
    assert "[attestation] valid" in out


def test_real_classifier_reports_partial_when_attestation_signature_invalid(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A PRESENT attestation signature that fails its own verify → partial, not fully trusted.

    Load-bearing T-3/T-4 detective control: the attestation referrer carries a
    Notation signature (so it is present), but that signature does NOT verify
    cryptographically. The top-level content verify passed, yet the bundle must
    be reported ``partial`` — a present-but-unverified second signature is never
    fully trusted. The attestation referrer is independently verified by re-
    running verify_fn against its by-digest reference; a NotationCommandError
    there marks the attestation REVOKED.
    """
    import base64

    from cairn_oci import PRODUCER_ATTESTATION_ARTIFACT_TYPE
    from cairn_signing import NotationCommandError

    content_digest = "sha256:" + "a" * 64
    attestation_digest = "sha256:" + "c" * 64
    att_ref_suffix = "@" + attestation_digest

    def _verify_fail_on_attestation(reference: str) -> None:
        # The content verify (by tag/full content ref) passes; the independent
        # attestation verify (by the attestation's own digest) fails strictly.
        if reference.endswith(att_ref_suffix):
            raise NotationCommandError(
                returncode=1, stderr="strict verification failed", command=[]
            )
        return None

    class _PresentButInvalidAttestationEcr:
        def __init__(self) -> None:
            self.subjects_seen: list[str] = []

        def get_authorization_token(self):
            token = base64.b64encode(b"AWS:pw").decode()
            return {"authorizationData": [{"authorizationToken": token}]}

        def list_image_referrers(self, **kwargs):
            subject = kwargs["subjectId"]["imageDigest"]
            self.subjects_seen.append(subject)
            if subject == content_digest:
                return {
                    "referrers": [
                        {
                            "artifactType": verify_module.NOTARY_SIGNATURE_ARTIFACT_TYPE,
                            "imageDigest": "sha256:" + "b" * 64,
                        },
                        {
                            "artifactType": PRODUCER_ATTESTATION_ARTIFACT_TYPE,
                            "imageDigest": attestation_digest,
                        },
                    ]
                }
            if subject == attestation_digest:
                # The attestation referrer DOES carry a Notation signature — it
                # is present, but it will fail its independent verify above.
                return {
                    "referrers": [
                        {
                            "artifactType": verify_module.NOTARY_SIGNATURE_ARTIFACT_TYPE,
                            "imageDigest": "sha256:" + "d" * 64,
                        }
                    ]
                }
            return {"referrers": []}

    stub_ecr = _PresentButInvalidAttestationEcr()
    policy = tmp_path / "trustpolicy.json"
    policy.write_text("{}")
    args = _make_args(_BY_DIGEST_REF)
    rc = verify_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=stub_ecr,
        oras_module=_FakeOras(),
        verify_fn=_verify_fail_on_attestation,
        trust_policy_path=policy,
    )
    # The content verify passed → the overall verdict succeeds (rc 0), but the
    # attestation signature did not verify → never fully trusted.
    assert rc == 0
    out = capsys.readouterr().out
    assert "verified:" in out
    assert attestation_digest in stub_ecr.subjects_seen
    assert "trust: partial" in out
    assert "missing attestation signature" in out
    assert "trust: fully-trusted" not in out
    # The present-but-invalid attestation signature is reported REVOKED, never VALID.
    assert "[attestation] revoked" in out


def test_real_classifier_reports_fully_trusted_when_both_signatures_verify(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Content verified AND the attestation independently verifies → fully trusted.

    Companion to the present-but-invalid case: when verify_fn succeeds for BOTH
    the content ref and the attestation's by-digest ref, both roles are VALID
    and the bundle is fully trusted.
    """
    import base64

    from cairn_oci import PRODUCER_ATTESTATION_ARTIFACT_TYPE

    content_digest = "sha256:" + "a" * 64
    attestation_digest = "sha256:" + "c" * 64

    verified_refs: list[str] = []

    def _verify_records(reference: str) -> None:
        verified_refs.append(reference)
        return None

    class _BothValidEcr:
        def get_authorization_token(self):
            token = base64.b64encode(b"AWS:pw").decode()
            return {"authorizationData": [{"authorizationToken": token}]}

        def list_image_referrers(self, **kwargs):
            subject = kwargs["subjectId"]["imageDigest"]
            if subject == content_digest:
                return {
                    "referrers": [
                        {
                            "artifactType": verify_module.NOTARY_SIGNATURE_ARTIFACT_TYPE,
                            "imageDigest": "sha256:" + "b" * 64,
                        },
                        {
                            "artifactType": PRODUCER_ATTESTATION_ARTIFACT_TYPE,
                            "imageDigest": attestation_digest,
                        },
                    ]
                }
            if subject == attestation_digest:
                return {
                    "referrers": [
                        {
                            "artifactType": verify_module.NOTARY_SIGNATURE_ARTIFACT_TYPE,
                            "imageDigest": "sha256:" + "d" * 64,
                        }
                    ]
                }
            return {"referrers": []}

    policy = tmp_path / "trustpolicy.json"
    policy.write_text("{}")
    args = _make_args(_BY_DIGEST_REF)
    rc = verify_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=_BothValidEcr(),
        oras_module=_FakeOras(),
        verify_fn=_verify_records,
        trust_policy_path=policy,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "trust: fully-trusted" in out
    assert "[content] valid" in out
    assert "[attestation] valid" in out
    # The attestation was independently verified by its OWN by-digest reference.
    assert any(r.endswith("@" + attestation_digest) for r in verified_refs)


def test_real_classifier_reports_partial_when_attestation_unsigned(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Content signed, an Attestation referrer present but UNSIGNED → partial."""
    import base64

    from cairn_oci import PRODUCER_ATTESTATION_ARTIFACT_TYPE

    content_digest = "sha256:" + "a" * 64
    attestation_digest = "sha256:" + "c" * 64

    class _NoAttestationSigEcr:
        def get_authorization_token(self):
            token = base64.b64encode(b"AWS:pw").decode()
            return {"authorizationData": [{"authorizationToken": token}]}

        def list_image_referrers(self, **kwargs):
            subject = kwargs["subjectId"]["imageDigest"]
            if subject == content_digest:
                return {
                    "referrers": [
                        {
                            "artifactType": verify_module.NOTARY_SIGNATURE_ARTIFACT_TYPE,
                            "imageDigest": "sha256:" + "b" * 64,
                        },
                        {
                            "artifactType": PRODUCER_ATTESTATION_ARTIFACT_TYPE,
                            "imageDigest": attestation_digest,
                        },
                    ]
                }
            # The attestation referrer carries no Notation signature.
            return {"referrers": []}

    policy = tmp_path / "trustpolicy.json"
    policy.write_text("{}")
    args = _make_args(_BY_DIGEST_REF)
    rc = verify_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=_NoAttestationSigEcr(),
        oras_module=_FakeOras(),
        verify_fn=_verify_ok,
        trust_policy_path=policy,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "trust: partial" in out
    assert "missing attestation signature" in out
    assert "trust: fully-trusted" not in out

"""Tests for cairn_cli.push — content manifest + Producer_Attestation_Referrer.

``cairn push`` emits TWO OCI
objects — a pure-content manifest (content-descriptive Config_Blob + layers, NO
producer claim, NO ``org.opencontainers.image.created``) followed by an
Producer_Attestation_Referrer (subject = content digest) carrying the producer's claim.

Invariants pinned here:
  * content manifest is pushed FIRST, attestation referrer SECOND;
  * the content Manifest_Digest is reproducible (pure function of content bytes —
    no ``created``/ambient state), so two pushes of byte-identical content yield
    the identical digest;
  * the attestation referrer's ``subject`` is the content digest and it carries
    the claim fields;
  * the content digest is printed ONLY after the attestation push succeeds.

The client still admits both objects BY DIGEST (untagged); the pipeline mints the
canonical tag server-side (eventually consistent).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest
from cairn_cli import config as config_module
from cairn_cli import push as push_module
from cairn_oci import (
    INTOTO_STATEMENT_MEDIA_TYPE,
    PINNED_CREATED,
    PRODUCER_ATTESTATION_ARTIFACT_TYPE,
    PRODUCER_PREDICATE_TYPE,
    parse_digest,
)

from tests.unit.cairn_cli.conftest import subparser_help


def _push_parser() -> argparse.ArgumentParser:
    """Build a parser carrying ONLY the ``push`` subcommand.

    Decoupled from ``cairn_cli.main.build_parser`` so these push/parser tests do
    not import the whole CLI tree (``main`` also wires the ``query``/``alias``
    read paths, whose tenant-PK metadata-facade migration is a separate, still
    in-flight change). The ``push`` subparser registers its own
    ``formatter_class`` in ``add_subparser``, so help rendering is identical to
    the real CLI.
    """
    parser = argparse.ArgumentParser(prog="cairn")
    subparsers = parser.add_subparsers(dest="command")
    push_module.add_subparser(subparsers)
    return parser


def _statement(attach_call: dict[str, Any]) -> dict[str, Any]:
    """Parse the attached referrer layer as an in-toto Statement.

    The Producer_Attestation_Referrer's layer is a standard in-toto Statement,
    not a flat ``ProducerAttestationReferrer`` JSON object (layer ``mediaType``
    ``application/vnd.in-toto+json``); the flat claim fields now live under the
    Statement's ``predicate``.
    """
    return json.loads(attach_call["claim_bytes"])


def _predicate(attach_call: dict[str, Any]) -> dict[str, Any]:
    """The flat claim fields, read from the Statement ``predicate``."""
    return _statement(attach_call)["predicate"]


class _FakeOras:
    """Captures push + attach invocations so tests can inspect args.

    The fake records call ORDER across both ``push`` and ``attach`` in
    ``call_log`` so tests can assert the content-then-attestation sequence.
    ``push`` returns a digest computed from the content member set it is handed
    (config bytes + layer member tuples) so the reproducible-digest property is
    exercised through the fake rather than asserted on a constant.
    """

    class OrasCommandError(RuntimeError):
        def __init__(self, *, returncode: int, stderr: str, command: list[str]) -> None:
            super().__init__(f"oras exit {returncode}: {stderr}")
            self.returncode = returncode
            self.stderr = stderr
            self.command = command

    def __init__(
        self, *, push_should_fail: str | None = None, attach_should_fail: str | None = None
    ) -> None:
        self.login_calls: list[dict[str, Any]] = []
        self.push_calls: list[dict[str, Any]] = []
        self.attach_calls: list[dict[str, Any]] = []
        self.call_log: list[str] = []
        self._push_should_fail = push_should_fail
        self._attach_should_fail = attach_should_fail

    def login(self, *, registry: str, username: str, password: str) -> None:
        self.login_calls.append({"registry": registry, "username": username})

    def _content_digest(self, *, config_bytes: bytes, files: list[tuple[Path, str, str]]) -> str:
        # A deterministic function of the content member set ONLY (config bytes +
        # each layer's bytes + media type + title). No wall-clock / ambient state,
        # mirroring the pure-content-identity guarantee.
        from cairn_oci import compute_sha256_digest

        material = bytearray(config_bytes)
        for path, media_type, title in files:
            material += path.read_bytes()
            material += media_type.encode()
            material += title.encode()
        return compute_sha256_digest(bytes(material))

    def push(
        self,
        *,
        reference: str,
        config_file: Path,
        config_media_type: str,
        files: list[tuple[Path, str, str]],
        annotations: dict[str, str],
        export_manifest_to: Path,
    ):
        self.call_log.append("push")
        if self._push_should_fail is not None:
            raise self.OrasCommandError(
                returncode=1, stderr=self._push_should_fail, command=["oras", "push"]
            )
        export_manifest_to.write_text(json.dumps({"layers": []}))
        config_bytes = config_file.read_bytes()
        digest = self._content_digest(config_bytes=config_bytes, files=files)
        self.push_calls.append(
            {
                "reference": reference,
                "config_bytes": config_bytes,
                "config_media_type": config_media_type,
                "files": files,
                "annotations": annotations,
                "manifest_digest": digest,
            }
        )

        class _R:
            manifest_digest = digest

        return _R()

    def attach(
        self,
        *,
        subject_reference: str,
        artifact_type: str,
        file: Path,
        file_media_type: str,
        annotations: dict[str, str],
        export_manifest_to: Path,
    ):
        self.call_log.append("attach")
        if self._attach_should_fail is not None:
            raise self.OrasCommandError(
                returncode=1, stderr=self._attach_should_fail, command=["oras", "attach"]
            )
        export_manifest_to.write_text(json.dumps({"layers": []}))
        self.attach_calls.append(
            {
                "subject_reference": subject_reference,
                "artifact_type": artifact_type,
                "claim_bytes": file.read_bytes(),
                "file_media_type": file_media_type,
                "annotations": annotations,
            }
        )

        class _R:
            manifest_digest = "sha256:" + "a" * 64

        return _R()


_UNSET = object()


def _make_args(
    files: list[Path],
    *,
    artifact_kind: str = "assessment-results",
    catalog_uuid: str | None = "11111111-1111-4111-8111-111111111111",
    controls: list[str] | object | None = _UNSET,
    assessment_period: str | None = "2026-q1",
    media_type_overrides: list[str] | None = None,
    org_id: str | None = "acme-co",
    tenant_id: str | None = "acme",
    project_id: str | None = "alpha-bank",
    target: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        org_id=org_id,
        tenant_id=tenant_id,
        project_id=project_id,
        target=target,
        assessment_period=assessment_period,
        files=files,
        artifact_kind=artifact_kind,
        catalog_or_profile_uuid=catalog_uuid,
        control=["ac-2"] if controls is _UNSET else controls,
        media_type=media_type_overrides or [],
        source_tool=None,
        assessor=None,
        assessment_id=None,
        supersedes=None,
        stage="alpha",
        region_code="cmh",
        app_name=None,
    )


@pytest.fixture
def evidence_file(tmp_path: Path) -> Path:
    p = tmp_path / "report.pdf"
    p.write_bytes(b"%PDF-1.7\n%% body")
    return p


def _ecr_clients(aws_stack):
    """Wire ECR responses into the moto stack used by tests."""
    ecr = aws_stack["ecr"]
    return aws_stack["ssm"], ecr


# --------------------------------------------------------------------------
# Two-object emission: content manifest then attestation referrer.
# --------------------------------------------------------------------------


def test_push_emits_content_manifest_then_attestation_referrer(
    aws_stack, evidence_file: Path
) -> None:
    """``cairn push`` emits exactly two OCI objects in order: the content
    manifest first (``push``) then the Producer_Attestation_Referrer (``attach``)."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file])
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    assert len(fake_oras.push_calls) == 1
    assert len(fake_oras.attach_calls) == 1
    # ORDER: content manifest push strictly precedes the attestation attach.
    assert fake_oras.call_log == ["push", "attach"]


def test_attestation_referrer_subject_is_content_digest_and_carries_claim(
    aws_stack, evidence_file: Path
) -> None:
    """The attestation referrer's ``subject`` resolves to the content digest and
    its ``artifactType`` + payload carry the producer claim.

    The OCI envelope ``artifactType`` stays ``PRODUCER_ATTESTATION_ARTIFACT_TYPE`` (the
    role label / validate-trigger discriminator, UNCHANGED), while the layer
    ``mediaType`` is now the in-toto Statement type (the format announcement)."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file])
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0

    content_digest = fake_oras.push_calls[0]["manifest_digest"]
    attach = fake_oras.attach_calls[0]
    # subject_reference is the content reference pinned by the content digest.
    assert attach["subject_reference"].endswith(f"@{content_digest}")
    # Envelope artifactType UNCHANGED; layer mediaType flipped to in-toto.
    assert attach["artifact_type"] == PRODUCER_ATTESTATION_ARTIFACT_TYPE
    assert attach["file_media_type"] == INTOTO_STATEMENT_MEDIA_TYPE

    claim = _predicate(attach)
    assert claim["orgId"] == "acme-co"
    assert claim["tenantId"] == "acme"
    assert claim["projectId"] == "alpha-bank"
    assert claim["artifactKind"] == "assessment-results"
    assert claim["controls"] == ["ac-2"]
    assert claim["catalogOrProfileUuid"] == "11111111-1111-4111-8111-111111111111"
    assert claim["assessmentPeriod"] == "2026-q1"


def test_attested_layer_is_a_valid_intoto_statement(aws_stack, evidence_file: Path) -> None:
    """The attached referrer layer
    is a valid in-toto Statement — ``_type`` = the in-toto Statement type, a
    SINGLE-element ``subject`` whose ``sha256`` DigestSet entry equals the content
    Manifest_Digest (bare hex of the same value the envelope subject points at),
    ``predicateType`` = the Cairn URI, and ``predicate`` = the flat claim fields.
    The OCI envelope ``artifactType`` is UNCHANGED."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file])
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0

    content_digest = fake_oras.push_calls[0]["manifest_digest"]
    attach = fake_oras.attach_calls[0]

    # Envelope artifactType unchanged; only the layer payload + mediaType changed.
    assert attach["artifact_type"] == PRODUCER_ATTESTATION_ARTIFACT_TYPE
    assert attach["file_media_type"] == INTOTO_STATEMENT_MEDIA_TYPE

    statement = _statement(attach)
    assert statement["_type"] == "https://in-toto.io/Statement/v1"
    assert statement["predicateType"] == PRODUCER_PREDICATE_TYPE

    # subject is a single-element array binding the content Manifest_Digest. The
    # in-toto DigestSet is bare hex (algo-keyed); the envelope/subject_reference is
    # the OCI prefixed (``sha256:<hex>``) form of the SAME digest.
    subject = statement["subject"]
    assert isinstance(subject, list)
    assert len(subject) == 1
    assert subject[0]["digest"]["sha256"] == parse_digest(content_digest)
    assert content_digest.startswith("sha256:")
    assert content_digest.endswith(subject[0]["digest"]["sha256"])

    # predicate carries exactly the flat claim fields.
    predicate = statement["predicate"]
    assert predicate["orgId"] == "acme-co"
    assert predicate["tenantId"] == "acme"
    assert predicate["projectId"] == "alpha-bank"
    assert predicate["artifactKind"] == "assessment-results"
    assert predicate["controls"] == ["ac-2"]
    assert predicate["catalogOrProfileUuid"] == "11111111-1111-4111-8111-111111111111"
    assert predicate["assessmentPeriod"] == "2026-q1"

    # The referrer manifest's created is deliberately NOT pinned: a deterministic
    # referrer would collide across identical re-pushes (ECR no-op → no validate
    # re-trigger; both subjects already-signed → sign short-circuits). It stays
    # unique per push. The wrapper still overrides the claim layer title to the
    # basename, keeping the ephemeral temp path out of the referrer.
    assert attach["annotations"] == {}


def test_content_manifest_created_is_pinned_and_deterministic(
    aws_stack, evidence_file: Path
) -> None:
    """``org.opencontainers.image.created`` is pinned to a fixed epoch and is
    byte-stable across pushes — the client never folds wall-clock ``now()`` into
    the manifest annotations, so oras (which would otherwise auto-inject ``now()``)
    receives a deterministic value and the content digest stays reproducible
    (artifact-ingestion *Manifest Digest Computation*). Regression guard for the
    dedup bug where identical content re-pushed minted a new bundle."""
    ssm, ecr = _ecr_clients(aws_stack)

    first = _FakeOras()
    assert (
        push_module._run(
            _make_args([evidence_file]), ssm_client=ssm, ecr_client=ecr, oras_module=first
        )
        == 0
    )
    second = _FakeOras()
    assert (
        push_module._run(
            _make_args([evidence_file]), ssm_client=ssm, ecr_client=ecr, oras_module=second
        )
        == 0
    )

    created_first = first.push_calls[0]["annotations"]["org.opencontainers.image.created"]
    assert created_first == PINNED_CREATED
    # A pinned constant, NOT a wall-clock timestamp: identical across pushes.
    assert second.push_calls[0]["annotations"]["org.opencontainers.image.created"] == created_first
    # The full manifest-annotation set is identical across the two pushes (the
    # client folds in no per-push or ambient state at all).
    assert first.push_calls[0]["annotations"] == second.push_calls[0]["annotations"]


def test_claim_fields_not_in_content_manifest(aws_stack, evidence_file: Path) -> None:
    """The content manifest carries ONLY the identity floor (project-id) plus a
    PINNED ``created``; NO artifact-kind annotation (kind is claim-only), NO
    claim annotations, and the Config_Blob carries no claim fields and no
    bundleType."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file], controls=["ac-2", "au-6"])
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    call = fake_oras.push_calls[0]
    annos = call["annotations"]

    # Identity floor present.
    assert annos["dev.cairnproofs.artifact.project-id"] == "alpha-bank"

    # artifact-kind is not a content-manifest annotation — kind is carried
    # solely in the Producer_Attestation_Referrer claim, so it does not perturb the content
    # Manifest_Digest.
    assert "dev.cairnproofs.artifact.artifact-kind" not in annos

    # The creation timestamp is PINNED to a fixed epoch (not wall-clock now()) so
    # oras cannot auto-inject a per-push timestamp that would pollute content
    # identity — present but deterministic.
    assert annos["org.opencontainers.image.created"] == PINNED_CREATED

    # No claim annotations on the content manifest.
    assert "dev.cairnproofs.artifact.controls" not in annos
    assert "dev.cairnproofs.artifact.assessment-period" not in annos
    assert "dev.cairnproofs.artifact.assessor" not in annos
    assert "dev.cairnproofs.artifact.assessment-id" not in annos
    assert "dev.cairnproofs.artifact.supersedes" not in annos
    assert "dev.cairnproofs.artifact.catalog-or-profile-uuid" not in annos

    # Config_Blob is content-descriptive only — no claim fields and no bundleType
    # (kind left the content manifest).
    blob = json.loads(call["config_bytes"])
    assert blob["projectId"] == "alpha-bank"
    assert "bundleType" not in blob
    for claim_field in (
        "controls",
        "catalogOrProfileUuid",
        "assessmentPeriod",
        "assessor",
        "assessmentId",
        "supersedes",
        "accountId",
        "controlFamily",
    ):
        assert claim_field not in blob


# --------------------------------------------------------------------------
# Reproducible content identity (no `created`, no ambient state).
# --------------------------------------------------------------------------


def test_content_digest_reproducible_across_two_pushes(aws_stack, tmp_path: Path) -> None:
    """Byte-identical content pushed twice yields the IDENTICAL content digest —
    no ``created``/ambient state is folded into the content manifest, so identity
    is a pure function of content bytes (Bundle Identity Is Pure Content
    Addressing)."""
    ssm, ecr = _ecr_clients(aws_stack)

    def _push_once() -> str:
        f = tmp_path / "report.pdf"
        f.write_bytes(b"%PDF-1.7\n%% identical body")
        fake = _FakeOras()
        args = _make_args([f])
        rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake)
        assert rc == 0
        return fake.push_calls[0]["manifest_digest"]

    first = _push_once()
    second = _push_once()
    assert first == second


def test_claim_does_not_perturb_content_digest(aws_stack, tmp_path: Path) -> None:
    """Byte-identical content pushed with DIFFERENT producer claims yields the
    IDENTICAL content digest. The compliance claim (controls, assessmentPeriod,
    catalog uuid, assessor, …) lives in the separately-signed Attestation
    referrer and is NOT part of the content manifest, so it cannot perturb
    bundle-level identity (artifact-ingestion *Content-Addressable Deduplication*
    — "the producer's compliance claim does not perturb this bundle-level
    identity")."""
    ssm, ecr = _ecr_clients(aws_stack)

    def _push_with_claim(**claim_overrides: Any) -> str:
        f = tmp_path / "report.pdf"
        f.write_bytes(b"%PDF-1.7\n%% identical body across claims")
        fake = _FakeOras()
        args = _make_args([f], **claim_overrides)
        rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake)
        assert rc == 0
        return fake.push_calls[0]["manifest_digest"]

    first = _push_with_claim(
        controls=["ac-2"],
        assessment_period="2026-q1",
        catalog_uuid="11111111-1111-4111-8111-111111111111",
    )
    second = _push_with_claim(
        controls=["au-6", "si-4", "ac-2"],
        assessment_period="2027-q4",
        catalog_uuid="22222222-2222-4222-8222-222222222222",
    )
    assert first == second


def test_distinct_content_yields_distinct_digest(aws_stack, tmp_path: Path) -> None:
    """One byte changed in a content layer yields a distinct content digest."""
    ssm, ecr = _ecr_clients(aws_stack)

    def _push(body: bytes) -> str:
        f = tmp_path / "report.pdf"
        f.write_bytes(body)
        fake = _FakeOras()
        args = _make_args([f])
        rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake)
        assert rc == 0
        return fake.push_calls[0]["manifest_digest"]

    assert _push(b"%PDF-1.7\nA") != _push(b"%PDF-1.7\nB")


# --------------------------------------------------------------------------
# Digest is reported ONLY after the attestation push succeeds.
# --------------------------------------------------------------------------


def test_content_digest_printed_only_after_attestation_push(
    aws_stack, evidence_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """On the happy path the content digest is printed to stdout — and it is the
    digest the attestation was attached against (so it is reported only because
    the attestation push succeeded)."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file])
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    out = capsys.readouterr().out
    content_digest = fake_oras.push_calls[0]["manifest_digest"]
    assert content_digest in out
    # The printed digest is exactly the attestation's subject digest.
    assert fake_oras.attach_calls[0]["subject_reference"].endswith(f"@{content_digest}")


def test_attestation_push_failure_suppresses_digest_report(
    aws_stack, evidence_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the attestation attach FAILS, the content manifest is an incomplete
    submission: the CLI returns non-zero and does NOT print the content digest as
    if the submission were complete."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras(attach_should_fail="referrers api unavailable")
    args = _make_args([evidence_file])
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 5
    captured = capsys.readouterr()
    # The content manifest WAS pushed, but its digest is NOT reported on stdout.
    content_digest = fake_oras.push_calls[0]["manifest_digest"]
    assert content_digest not in captured.out
    assert "referrers api unavailable" in captured.err
    assert "incomplete submission" in captured.err


# --------------------------------------------------------------------------
# Untagged admission by digest (unchanged from prior wave).
# --------------------------------------------------------------------------


def test_push_admits_untagged_manifest_by_digest(aws_stack, evidence_file: Path) -> None:
    """The content push reference carries NO canonical tag — admitted by digest
    (a tagless ``registry/repo`` reference)."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file])
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    reference = fake_oras.push_calls[0]["reference"]
    assert reference.endswith("/cairnproofs4af-artifact-cmh-alpha/acme-co/acme/alpha-bank")
    repo_portion = reference.split("/", 1)[1]
    assert ":" not in repo_portion
    assert "@" not in reference


def test_push_surfaces_tag_pending_message(
    aws_stack, evidence_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 'tag pending / eventually consistent' notice is surfaced to the user."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file])
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert push_module.TAG_PENDING_MESSAGE in combined
    assert "tag pending" in push_module.TAG_PENDING_MESSAGE
    assert "eventually consistent" in push_module.TAG_PENDING_MESSAGE


def test_push_makes_no_client_side_tag_composition(
    aws_stack, evidence_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """push must NOT call ArtifactLocator tag-minting methods. Repo-name
    derivation (``repo_name``) is still allowed; only tag composition is
    forbidden."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()

    def _boom(*_a, **_k):
        raise AssertionError("push must not compose a canonical tag client-side")

    monkeypatch.setattr(push_module.ArtifactLocator, "build_tag", _boom, raising=False)
    monkeypatch.setattr(push_module.ArtifactLocator, "locate", _boom, raising=False)
    monkeypatch.setattr(push_module.ArtifactLocator, "build_ref_tag", _boom, raising=False)

    args = _make_args([evidence_file])
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    assert ":" not in fake_oras.push_calls[0]["reference"].split("/", 1)[1]


def test_tag_sequence_flag_no_longer_accepted() -> None:
    """The --tag-sequence flag is removed; the parser rejects it (SystemExit 2)."""
    parser = _push_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(
            [
                "push",
                "--project-id",
                "alpha-bank",
                "--assessment-period",
                "2026-q1",
                "report.pdf",
                "--tag-sequence",
                "7",
            ]
        )
    assert excinfo.value.code == 2


# --------------------------------------------------------------------------
# Bundle assembly: content layers, media types, OSCAL schema-version omission.
# --------------------------------------------------------------------------


def test_happy_path_content_manifest_and_claim(aws_stack, evidence_file: Path) -> None:
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file])
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    call = fake_oras.push_calls[0]
    # Identity floor annotations.
    annos = call["annotations"]
    assert annos["dev.cairnproofs.artifact.project-id"] == "alpha-bank"
    # artifact-kind is claim-only — not a content-manifest annotation.
    assert "dev.cairnproofs.artifact.artifact-kind" not in annos
    # Content-descriptive Config_Blob — project-id only, no bundleType.
    blob = json.loads(call["config_bytes"])
    assert blob["projectId"] == "alpha-bank"
    assert "bundleType" not in blob
    # Per-file media type and layer title plumbed through to the content layer.
    files = call["files"]
    assert files[0][1] == "application/pdf"
    assert files[0][2] == "report.pdf"
    # Claim travels in the referrer — kind lives ONLY here now.
    claim = _predicate(fake_oras.attach_calls[0])
    assert claim["artifactKind"] == "assessment-results"
    assert claim["catalogOrProfileUuid"] == "11111111-1111-4111-8111-111111111111"


def test_oscal_schema_version_flag_no_longer_accepted() -> None:
    """The --oscal-schema-version flag is removed; the parser rejects it."""
    parser = _push_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(
            [
                "push",
                "--project-id",
                "alpha-bank",
                "--assessment-period",
                "2026-q1",
                "report.pdf",
                "--oscal-schema-version",
                "1.1.2",
            ]
        )
    assert excinfo.value.code == 2


def test_oscal_bundle_does_not_emit_schema_version(aws_stack, tmp_path: Path) -> None:
    ssm, ecr = _ecr_clients(aws_stack)
    oscal_file = tmp_path / "ar.json"
    oscal_file.write_text(json.dumps({"assessment-results": {"uuid": "x"}}))
    fake_oras = _FakeOras()
    args = _make_args([oscal_file])
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    blob = json.loads(fake_oras.push_calls[0]["config_bytes"])
    assert "oscalSchemaVersion" not in blob


def test_missing_file_fails_fast_before_network(aws_stack, tmp_path: Path) -> None:
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([tmp_path / "does-not-exist.pdf"])
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 2
    assert fake_oras.login_calls == []
    assert fake_oras.push_calls == []
    assert fake_oras.attach_calls == []


def test_media_type_override_applied(aws_stack, tmp_path: Path) -> None:
    ssm, ecr = _ecr_clients(aws_stack)
    bin_file = tmp_path / "weird.bin"
    bin_file.write_bytes(b"data")
    fake_oras = _FakeOras()
    args = _make_args([bin_file], media_type_overrides=[f"{bin_file.name}=application/x-custom"])
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    assert fake_oras.push_calls[0]["files"][0][1] == "application/x-custom"


def test_invalid_catalog_uuid_fails_preflight(aws_stack, evidence_file: Path) -> None:
    """A malformed catalog/profile UUID is rejected client-side before the
    network round-trip (exit 3), and nothing is pushed."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file], catalog_uuid="not-a-uuid")
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 3
    assert fake_oras.push_calls == []
    assert fake_oras.attach_calls == []


def test_content_push_failure_surfaces_error(
    aws_stack, evidence_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing content-manifest push surfaces exit 5 with the underlying stderr
    and never attempts the attestation attach."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras(push_should_fail="some registry error")
    args = _make_args([evidence_file])
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 5
    assert "some registry error" in capsys.readouterr().err
    assert fake_oras.attach_calls == []


def test_media_type_override_missing_equals_rejected(aws_stack, evidence_file: Path) -> None:
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file], media_type_overrides=["bogus_no_equals"])
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 2


def test_supersedes_claim_in_referrer(aws_stack, evidence_file: Path) -> None:
    """``supersedes`` is a CLAIM field carried in the Producer_Attestation_Referrer, not a
    content-manifest annotation."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file])
    args.supersedes = "tag-a,tag-b"
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    # Not on the content manifest.
    assert "dev.cairnproofs.artifact.supersedes" not in fake_oras.push_calls[0]["annotations"]
    # In the claim.
    claim = _predicate(fake_oras.attach_calls[0])
    assert claim["supersedes"] == "tag-a,tag-b"


def test_assessor_and_assessment_id_in_referrer(aws_stack, evidence_file: Path) -> None:
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file])
    args.assessor = "Marcus"
    args.assessment_id = "assess-2026-q1"
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    claim = _predicate(fake_oras.attach_calls[0])
    assert claim["assessor"] == "Marcus"
    assert claim["assessmentId"] == "assess-2026-q1"
    # The producing identity is not a claim field; the tenant-id identity
    # coordinate carries it.
    assert "accountId" not in claim
    # None of these leak onto the content manifest.
    annos = fake_oras.push_calls[0]["annotations"]
    assert "dev.cairnproofs.artifact.assessor" not in annos
    assert "dev.cairnproofs.artifact.assessment-id" not in annos


# --------------------------------------------------------------------------
# optional-control-metadata: control linkage + period are optional CLAIM fields.
# --------------------------------------------------------------------------


def test_identity_only_push_defaults_type_and_period(aws_stack, evidence_file: Path) -> None:
    """An identity-only push (no controls/catalog/period) builds a content
    manifest with only the identity floor and a claim with the defaulted
    artifact-kind + a resolved (ingestion-date) period."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args(
        [evidence_file],
        artifact_kind=push_module._DEFAULT_ARTIFACT_KIND,
        catalog_uuid=None,
        controls=None,
        assessment_period=None,
    )
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    call = fake_oras.push_calls[0]
    annos = call["annotations"]

    assert push_module._DEFAULT_ARTIFACT_KIND == "other"
    assert annos["dev.cairnproofs.artifact.project-id"] == "alpha-bank"
    # artifact-kind is claim-only — not a content-manifest annotation.
    assert "dev.cairnproofs.artifact.artifact-kind" not in annos
    assert annos["org.opencontainers.image.created"] == PINNED_CREATED

    # No control linkage / period on the content manifest at all.
    assert "dev.cairnproofs.artifact.controls" not in annos
    assert "dev.cairnproofs.artifact.assessment-period" not in annos
    assert not any("control-family" in k for k in annos)

    blob = json.loads(call["config_bytes"])
    assert "controlFamily" not in blob

    # The claim carries the defaulted artifact-kind, resolved period, empty controls.
    claim = _predicate(fake_oras.attach_calls[0])
    assert claim["artifactKind"] == "other"
    assert claim["assessmentPeriod"]  # a real resolved (ingestion-date) value
    assert claim["controls"] == []
    assert "catalogOrProfileUuid" not in claim  # exclude_none


def test_resolved_period_in_claim_only(aws_stack, evidence_file: Path) -> None:
    """The resolved period lands in the Producer_Attestation_Referrer claim and NOWHERE on
    the content manifest (no period annotation, no Config_Blob field)."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file], assessment_period=None)
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    claim = _predicate(fake_oras.attach_calls[0])
    assert claim["assessmentPeriod"]
    annos = fake_oras.push_calls[0]["annotations"]
    assert "dev.cairnproofs.artifact.assessment-period" not in annos
    blob = json.loads(fake_oras.push_calls[0]["config_bytes"])
    assert "assessmentPeriod" not in blob


def test_controls_in_claim_not_content(aws_stack, evidence_file: Path) -> None:
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file], controls=["ac-2", "au-6"])
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    claim = _predicate(fake_oras.attach_calls[0])
    assert claim["controls"] == ["ac-2", "au-6"]
    annos = fake_oras.push_calls[0]["annotations"]
    assert "dev.cairnproofs.artifact.controls" not in annos
    blob = json.loads(fake_oras.push_calls[0]["config_bytes"])
    assert "controls" not in blob


def test_control_flags_optional_no_controls_no_catalog(aws_stack, evidence_file: Path) -> None:
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file], controls=None, catalog_uuid=None)
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0


@pytest.mark.parametrize("bad_control", ["ac#2", "ac-2\n", "AC-2", "", "ac/2"])
def test_malformed_control_rejected_client_side(
    aws_stack, evidence_file: Path, bad_control: str
) -> None:
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file], controls=["ac-2", bad_control])
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 2
    assert fake_oras.login_calls == []
    assert fake_oras.push_calls == []
    assert fake_oras.attach_calls == []


# --------------------------------------------------------------------------
# artifact-kind vocabulary handling.
# --------------------------------------------------------------------------


def test_default_artifact_kind_is_a_canonical_value() -> None:
    from cairn_oci import ARTIFACT_KINDS

    assert push_module._DEFAULT_ARTIFACT_KIND == "other"
    assert push_module._DEFAULT_ARTIFACT_KIND in ARTIFACT_KINDS


def _push_subparser_help() -> str:
    return subparser_help(_push_parser(), "push")


def test_help_lists_all_canonical_choices() -> None:
    from cairn_oci import ARTIFACT_KINDS

    sub_help = _push_subparser_help()
    collapsed = "".join(sub_help.split())
    for kind in ARTIFACT_KINDS:
        flat = "".join(kind.split("-"))
        assert flat in collapsed.replace("-", ""), (
            f"canonical kind {kind!r} missing from --artifact-kind help"
        )
    assert "Canonicalvalues:" in collapsed


def test_bundle_type_alias_accepted_and_preserved_verbatim(aws_stack, evidence_file: Path) -> None:
    """An alias like 'attestations' is accepted client-side and the SUPPLIED
    spelling is preserved verbatim in the claim's artifactKind. Kind is claim-only
    now — it is NOT carried as a content-manifest annotation."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file], artifact_kind="attestations")
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    call = fake_oras.push_calls[0]
    assert "dev.cairnproofs.artifact.artifact-kind" not in call["annotations"]
    claim = _predicate(fake_oras.attach_calls[0])
    assert claim["artifactKind"] == "attestations"


def test_unknown_bundle_type_rejected_with_canonical_values(
    aws_stack, evidence_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cairn_oci import ARTIFACT_KINDS

    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file], artifact_kind="screenshot")
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 2
    assert fake_oras.login_calls == []
    assert fake_oras.push_calls == []
    err = capsys.readouterr().err
    assert "screenshot" in err
    for kind in ARTIFACT_KINDS:
        assert kind in err
    assert "general" not in err


def test_mixed_case_bundle_type_rejected_before_network(
    aws_stack, evidence_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file], artifact_kind="Attestations")
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 2
    assert fake_oras.login_calls == []
    assert fake_oras.push_calls == []
    assert "Attestations" in capsys.readouterr().err


def test_bundle_type_alias_parses_without_argparse_choices_rejection() -> None:
    parser = _push_parser()
    ns = parser.parse_args(
        [
            "push",
            "--project-id",
            "alpha-bank",
            "--assessment-period",
            "2026-q1",
            "report.pdf",
            "--artifact-kind",
            "baseline",
        ]
    )
    assert ns.artifact_kind == "baseline"


# --------------------------------------------------------------------------
# identity/metadata are named switches; ``files`` is
# the SOLE positional.
# --------------------------------------------------------------------------


def test_omitted_identity_parses_as_files_not_coordinates() -> None:
    parser = _push_parser()
    # Identity coordinates are now optional switches (resolved from args/env/config),
    # so omitting them does NOT raise at parse time and never turns a file token
    # into an identity coordinate — every token is a file. Runtime fail-loud on an
    # unresolved coordinate is covered by test_push_unresolved_coordinate_fails_loud.
    ns = parser.parse_args(["push", "report.json", "evidence.pdf"])
    assert ns.org_id is None
    assert ns.tenant_id is None
    assert ns.project_id is None
    assert [p.name for p in ns.files] == ["report.json", "evidence.pdf"]


def test_files_are_the_sole_positional_no_interleave() -> None:
    parser = _push_parser()
    ns = parser.parse_args(["push", "--project-id", "acme", "report.json", "evidence.pdf"])
    assert ns.project_id == "acme"
    assert ns.assessment_period is None
    assert [p.name for p in ns.files] == ["report.json", "evidence.pdf"]


@pytest.mark.parametrize("n_files", [2, 3])
def test_n_files_produce_n_content_layers(aws_stack, tmp_path: Path, n_files: int) -> None:
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    files = []
    for i in range(n_files):
        f = tmp_path / f"evidence-{i}.pdf"
        f.write_bytes(b"%PDF-1.7\n%% body " + str(i).encode())
        files.append(f)
    args = _make_args(files)
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    assert len(fake_oras.push_calls[0]["files"]) == n_files


def test_push_help_is_self_documenting() -> None:
    from cairn_cli._help import EXAMPLES_HEADING

    sub_help = _push_subparser_help()
    assert "usage" in sub_help
    assert "--project-id" in sub_help
    assert EXAMPLES_HEADING in sub_help


# --------------------------------------------------------------------------
# HARD-BREAK regression guards (old positional form).
# --------------------------------------------------------------------------


def test_old_positional_syntax_rejected(
    aws_stack, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The old positional form ``push acme 2026-q2 report.json`` never resolves
    identity from a file token.

    Identity coordinates are now named switches / env / config only, so the bare
    positional tokens parse as FILES (one content layer each), and with no
    coordinate resolvable the run fails loud ([INGEST-032.02/.03]) — ``acme`` /
    ``2026-q2`` are never silently consumed as an org/tenant/project id.
    """
    _clear_identity_sources(monkeypatch)
    parser = _push_parser()
    ns = parser.parse_args(["push", "acme", "2026-q2", "report.json"])
    assert [p.name for p in ns.files] == ["acme", "2026-q2", "report.json"]

    fake_oras = _FakeOras()
    rc = push_module._run(
        ns, ssm_client=aws_stack["ssm"], ecr_client=aws_stack["ecr"], oras_module=fake_oras
    )
    assert rc == 2
    assert fake_oras.push_calls == []
    err = capsys.readouterr().err
    assert "could not resolve identity coordinate" in err


def test_old_control_scope_positional_no_longer_exists() -> None:
    parser = _push_parser()
    ns = parser.parse_args(["push", "--project-id", "acme", "in-scope-systems", "report.json"])
    assert not hasattr(ns, "control_scope")
    assert [p.name for p in ns.files] == ["in-scope-systems", "report.json"]


# --------------------------------------------------------------------------
# Identity-coordinate resolution on push ([INGEST-032]).
# --------------------------------------------------------------------------


def _clear_identity_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate resolution: clear CAIRN_* env and stub both persisted configs empty.

    Without this, a developer's real ``.cairn.toml`` / per-user config could
    satisfy a coordinate the test means to leave unresolved.
    """
    for env_var in ("CAIRN_ORG_ID", "CAIRN_TENANT_ID", "CAIRN_PROJECT_ID"):
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setattr(config_module, "load_project_config", lambda cwd=None: {})
    monkeypatch.setattr(config_module, "load_user_config", lambda environ=None: {})


def test_push_target_populates_all_three_coordinates(aws_stack, evidence_file: Path) -> None:
    """[INGEST-032.05] A single ``--target O/T/P`` populates the full identity tuple."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args(
        [evidence_file], org_id=None, tenant_id=None, project_id=None, target="org2/tnt2/proj2"
    )
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    claim = _predicate(fake_oras.attach_calls[0])
    assert (claim["orgId"], claim["tenantId"], claim["projectId"]) == ("org2", "tnt2", "proj2")
    # The repo path threads all three segments under the appName namespace.
    assert fake_oras.push_calls[0]["reference"].endswith(
        "cairnproofs4af-artifact-cmh-alpha/org2/tnt2/proj2"
    )


def test_push_individual_arg_overrides_target_segment(aws_stack, evidence_file: Path) -> None:
    """[INGEST-032.07] An individual ``--project-id`` overrides the ``--target`` project."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args(
        [evidence_file], org_id=None, tenant_id=None, project_id="override-proj", target="o/t/p"
    )
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    claim = _predicate(fake_oras.attach_calls[0])
    assert (claim["orgId"], claim["tenantId"], claim["projectId"]) == ("o", "t", "override-proj")


def test_push_resolves_coordinates_from_environment(
    aws_stack, evidence_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[INGEST-032.06] Coordinates resolve from CAIRN_* env when no args are given."""
    _clear_identity_sources(monkeypatch)
    monkeypatch.setenv("CAIRN_ORG_ID", "env-org")
    monkeypatch.setenv("CAIRN_TENANT_ID", "env-tnt")
    monkeypatch.setenv("CAIRN_PROJECT_ID", "env-proj")
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file], org_id=None, tenant_id=None, project_id=None)
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    claim = _predicate(fake_oras.attach_calls[0])
    assert (claim["orgId"], claim["tenantId"], claim["projectId"]) == (
        "env-org",
        "env-tnt",
        "env-proj",
    )


def test_push_explicit_arg_overrides_environment(
    aws_stack, evidence_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[INGEST-032.07] Explicit args take precedence over the environment."""
    _clear_identity_sources(monkeypatch)
    monkeypatch.setenv("CAIRN_ORG_ID", "env-org")
    monkeypatch.setenv("CAIRN_TENANT_ID", "env-tnt")
    monkeypatch.setenv("CAIRN_PROJECT_ID", "env-proj")
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file], org_id="arg-org", tenant_id="arg-tnt", project_id="arg-proj")
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    claim = _predicate(fake_oras.attach_calls[0])
    assert (claim["orgId"], claim["tenantId"], claim["projectId"]) == (
        "arg-org",
        "arg-tnt",
        "arg-proj",
    )


def test_push_unresolved_coordinate_fails_loud(
    aws_stack,
    evidence_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[INGEST-032.02] An unresolved coordinate fails loud and pushes nothing."""
    _clear_identity_sources(monkeypatch)
    monkeypatch.setenv("CAIRN_ORG_ID", "env-org")
    monkeypatch.setenv("CAIRN_TENANT_ID", "env-tnt")
    # CAIRN_PROJECT_ID intentionally unset and no --project-id / config.
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file], org_id=None, tenant_id=None, project_id=None)
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 2
    assert fake_oras.push_calls == []
    err = capsys.readouterr().err
    assert "project-id" in err


def test_push_malformed_coordinate_fails_loud(aws_stack, evidence_file: Path) -> None:
    """A coordinate that violates the opaque-id grammar fails loud before push."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    args = _make_args([evidence_file], org_id="Bad_Org", tenant_id="acme", project_id="alpha-bank")
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 2
    assert fake_oras.push_calls == []


def test_footgun_two_files_yield_two_layers_period_not_eaten(aws_stack, tmp_path: Path) -> None:
    """Regression for the cairn-push positional footgun: two files (with
    --project-id) produce TWO content layers AND the period claim is untouched
    (defaulted, not stolen from a filename)."""
    ssm, ecr = _ecr_clients(aws_stack)
    parser = _push_parser()
    file_a = tmp_path / "fileA.pdf"
    file_b = tmp_path / "fileB.pdf"
    file_a.write_bytes(b"%PDF-1.7\nA")
    file_b.write_bytes(b"%PDF-1.7\nB")
    ns = parser.parse_args(
        [
            "push",
            "--org-id",
            "acme-co",
            "--tenant-id",
            "acme",
            "--project-id",
            "acme",
            str(file_a),
            str(file_b),
        ]
    )
    assert ns.assessment_period is None
    assert [p.name for p in ns.files] == ["fileA.pdf", "fileB.pdf"]

    fake_oras = _FakeOras()
    rc = push_module._run(ns, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    call = fake_oras.push_calls[0]
    assert len(call["files"]) == 2
    assert [title for _p, _mt, title in call["files"]] == ["fileA.pdf", "fileB.pdf"]
    period = _predicate(fake_oras.attach_calls[0])["assessmentPeriod"]
    assert period
    assert period not in {"fileA.pdf", "fileA", file_a.name, str(file_a)}


def test_omitted_period_defaults_to_ingestion_date_via_parser(
    aws_stack, evidence_file: Path
) -> None:
    """--assessment-period omitted: the parser yields ``None`` and the resolver
    fills the ingestion-date default into the claim."""
    ssm, ecr = _ecr_clients(aws_stack)
    parser = _push_parser()
    ns = parser.parse_args(
        [
            "push",
            "--org-id",
            "acme-co",
            "--tenant-id",
            "acme",
            "--project-id",
            "acme",
            str(evidence_file),
        ]
    )
    assert ns.assessment_period is None
    fake_oras = _FakeOras()
    rc = push_module._run(ns, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    claim = _predicate(fake_oras.attach_calls[0])
    assert claim["assessmentPeriod"]


def test_push_help_documents_identity_resolution() -> None:
    sub_help = _push_subparser_help()
    # The help documents the three identity-coordinate switches, the --target
    # shorthand, and the resolution chain (args > env > config).
    assert "--org-id" in sub_help
    assert "--tenant-id" in sub_help
    assert "--project-id" in sub_help
    assert "--target" in sub_help
    assert "CAIRN_ORG_ID" in sub_help
    assert "Identity coordinates" in sub_help


# --------------------------------------------------------------------------
# The client-side pre-flight rejects an
# out-of-bounds content-layer count LOCALLY (before any login / blob upload),
# surfacing the which-constraint code (i1 too-few, i2 too-many).
# --------------------------------------------------------------------------


def test_zero_layer_bundle_rejected_preflight_i1(
    aws_stack, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bundle with NO content layers is rejected by the client-side pre-flight
    with criterion ``i1`` (too-few) at exit 3 — before any registry login or blob
    upload. This is the clean which-constraint path (``validate_bundle`` runs and
    surfaces ``i1``)."""
    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    # ``files`` is empty: zero content layers reach validate_bundle (the parser's
    # nargs="+" is bypassed here since we build the Namespace directly, exercising
    # the pre-flight bound rather than the argparse arity).
    args = _make_args([])
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 3
    # Rejected locally: nothing logged in / pushed / attached.
    assert fake_oras.login_calls == []
    assert fake_oras.push_calls == []
    assert fake_oras.attach_calls == []
    err = capsys.readouterr().err
    assert "[i1]" in err
    assert "at least 1" in err


def test_too_many_layers_bundle_rejected_locally_before_upload(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bundle exceeding ``MAX_CONTENT_LAYERS`` (300) is rejected LOCALLY before
    any blob upload.

    Path taken: ``OciManifest`` enforces ``max_length=MAX_CONTENT_LAYERS`` at the
    Pydantic layer, so a 301-layer manifest raises a ``ValidationError`` at
    manifest construction — BEFORE ``validate_bundle`` can surface criterion
    ``i2``. push.py catches that and translates it into the same exit-3 pre-flight
    rejection, naming the too-many constraint (``i2``). Either way the user gets a
    clean local rejection before login/upload — no traceback, no network."""
    from cairn_oci.manifest import MAX_CONTENT_LAYERS

    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    n = MAX_CONTENT_LAYERS + 1  # 301
    files = []
    for i in range(n):
        f = tmp_path / f"evidence-{i}.bin"
        # Distinct bytes per file so descriptors are distinct (not load-bearing
        # for the count bound, but mirrors a realistic many-layer bundle).
        f.write_bytes(b"x" + str(i).encode())
        files.append(f)
    args = _make_args(files)
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 3
    # Rejected locally before any network activity.
    assert fake_oras.login_calls == []
    assert fake_oras.push_calls == []
    assert fake_oras.attach_calls == []
    err = capsys.readouterr().err
    assert "[i2]" in err
    assert str(MAX_CONTENT_LAYERS) in err
    assert str(n) in err


def test_exactly_max_layers_passes_preflight(aws_stack, tmp_path: Path) -> None:
    """GAP / off-by-one guard: a bundle with EXACTLY ``MAX_CONTENT_LAYERS`` (300)
    content layers is the inclusive max — it must PASS the pre-flight (the
    pre-flight bound is ``> 300``, not ``>= 300``) and reach the push. Pins the
    accept side of the i2 boundary so a regression to ``>= 300`` is caught."""
    from cairn_oci.manifest import MAX_CONTENT_LAYERS

    ssm, ecr = _ecr_clients(aws_stack)
    fake_oras = _FakeOras()
    files = []
    for i in range(MAX_CONTENT_LAYERS):  # exactly 300
        f = tmp_path / f"evidence-{i}.bin"
        f.write_bytes(b"x" + str(i).encode())
        files.append(f)
    args = _make_args(files)
    rc = push_module._run(args, ssm_client=ssm, ecr_client=ecr, oras_module=fake_oras)
    assert rc == 0
    # Reached the push with all 300 layers — no false pre-flight rejection.
    assert len(fake_oras.push_calls[0]["files"]) == MAX_CONTENT_LAYERS

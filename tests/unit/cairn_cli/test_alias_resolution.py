"""Tests for the read-path alias handling.

These cover the three CLI read consumers — ``pull`` / ``verify`` / ``query`` —
asserting the Marcus invariant: a mutable alias (`_als`/`_chn`) is resolved to
its underlying immutable Manifest_Digest, the verification verdict is keyed on
the DIGEST (never the alias string), and the alias surfaces only as a mutable
LABEL. A canonical `_ref` tag or a by-digest reference is untouched.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from cairn_cli import pull as pull_module
from cairn_cli import query as query_module
from cairn_cli import verify as verify_module
from cairn_cli._refs import ResolvedReference
from cairn_cli.pull import alias_tag_of, mutable_alias_label
from cairn_metadata import MetadataIndex
from cairn_oci import ArtifactLocator, compute_sha256_digest

from tests.unit.cairn_cli.conftest import METADATA_TABLE

_LOC = ArtifactLocator()
_NAMED_ALIAS = _LOC.build_named_alias(artifact_kind="catalog", name="nist-800-53-latest")
_CHANNEL = _LOC.build_channel_pointer(artifact_kind="catalog", channel="latest")
_REF_TAG = _LOC.build_ref_tag(artifact_kind="catalog", digest="a" * 12)


def _ref(selector: str) -> ResolvedReference:
    return ResolvedReference(
        registry="123456789012.dkr.ecr.us-east-2.amazonaws.com",
        repository="cairnproofs4af-artifact-cmh-alpha/alpha-bank/catalog",
        selector=selector,
    )


# --- classifier (task 4.1) ------------------------------------------------


def test_alias_tag_of_recognizes_named_alias() -> None:
    assert alias_tag_of(_ref(f":{_NAMED_ALIAS}")) == _NAMED_ALIAS


def test_alias_tag_of_recognizes_channel_pointer() -> None:
    assert alias_tag_of(_ref(f":{_CHANNEL}")) == _CHANNEL


def test_alias_tag_of_ignores_canonical_ref_tag() -> None:
    assert alias_tag_of(_ref(f":{_REF_TAG}")) is None


def test_alias_tag_of_ignores_by_digest_reference() -> None:
    assert alias_tag_of(_ref("@sha256:" + "c" * 64)) is None


def test_alias_tag_of_ignores_unparseable_tag() -> None:
    # A tag the registry-backed parser rejects is NOT an alias — fail closed.
    assert alias_tag_of(_ref(":totally-not-a-cairn-tag")) is None


def test_mutable_alias_label_points_at_digest() -> None:
    digest = "sha256:" + "b" * 64
    label = mutable_alias_label(_NAMED_ALIAS, digest)
    assert _NAMED_ALIAS in label
    assert digest in label
    assert "mutable alias" in label


# --- pull (tasks 4.1 / 4.2) -----------------------------------------------


class _FakeOras:
    class OrasCommandError(RuntimeError):
        def __init__(self, *, returncode: int, stderr: str, command: list[str]) -> None:
            super().__init__(stderr)
            self.returncode = returncode
            self.stderr = stderr
            self.command = command

    def __init__(self, *, layers: list[tuple[str, bytes]]) -> None:
        self._layers = layers
        self.manifest_bytes = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "layers": [
                    {
                        "mediaType": "application/octet-stream",
                        "digest": compute_sha256_digest(body),
                        "size": len(body),
                        "annotations": {"org.opencontainers.image.title": title},
                    }
                    for title, body in layers
                ],
            }
        ).encode("utf-8")
        self.fetch_calls: list[str] = []

    def login(self, *, registry: str, username: str, password: str) -> None:
        pass

    def fetch_manifest(self, *, reference: str) -> bytes:
        self.fetch_calls.append(reference)
        return self.manifest_bytes

    def pull(self, *, reference: str, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        for title, body in self._layers:
            (output_dir / title).write_bytes(body)


def _pull_args(reference: str, output_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        reference=reference,
        output_dir=output_dir,
        stage="alpha",
        region_code="cmh",
        app_name=None,
    )


def _verify_ok(_reference: str) -> None:
    return None


def test_pull_via_alias_labels_mutable_and_keys_verdict_on_digest(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeOras(layers=[("report.pdf", b"hello-pdf")])
    expected_digest = compute_sha256_digest(fake.manifest_bytes)
    args = _pull_args(f"alpha-bank/catalog:{_NAMED_ALIAS}", tmp_path)
    rc = pull_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        oras_module=fake,
        verify_fn=_verify_ok,
    )
    assert rc == 0
    out = capsys.readouterr().out
    # The alias surfaces only as a MUTABLE LABEL pointing at the resolved digest.
    assert "reached via mutable alias" in out
    assert _NAMED_ALIAS in out
    # The verdict is keyed on the DIGEST, never the alias string.
    assert f"verified: {expected_digest}" in out
    assert f"verified: {_NAMED_ALIAS}" not in out


def test_pull_via_channel_pointer_labels_mutable(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeOras(layers=[("report.pdf", b"hello-pdf")])
    expected_digest = compute_sha256_digest(fake.manifest_bytes)
    args = _pull_args(f"alpha-bank/catalog:{_CHANNEL}", tmp_path)
    rc = pull_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        oras_module=fake,
        verify_fn=_verify_ok,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "reached via mutable alias" in out
    assert _CHANNEL in out
    assert f"verified: {expected_digest}" in out


def test_pull_canonical_tag_unchanged_no_alias_label(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeOras(layers=[("report.pdf", b"hello-pdf")])
    args = _pull_args(f"alpha-bank/catalog:{_REF_TAG}", tmp_path)
    rc = pull_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        oras_module=fake,
        verify_fn=_verify_ok,
    )
    assert rc == 0
    out = capsys.readouterr().out
    # A canonical pull keeps presenting its resolved reference, no alias label.
    assert "mutable alias" not in out
    assert "verified:" in out


# --- verify (task 4.1) ----------------------------------------------------


class _VerifyFakeOras:
    class OrasCommandError(RuntimeError):
        def __init__(self, *, returncode: int, stderr: str, command: list[str]) -> None:
            super().__init__(stderr)
            self.returncode = returncode
            self.stderr = stderr
            self.command = command

    def __init__(self, manifest_bytes: bytes) -> None:
        self.manifest_bytes = manifest_bytes
        self.fetch_calls: list[str] = []

    def login(self, *, registry: str, username: str, password: str) -> None:
        pass

    def fetch_manifest(self, *, reference: str) -> bytes:
        self.fetch_calls.append(reference)
        return self.manifest_bytes


def _verify_args(reference: str) -> argparse.Namespace:
    return argparse.Namespace(reference=reference, stage="alpha", region_code="cmh", app_name=None)


def test_verify_via_alias_anchors_verdict_on_digest_not_alias(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy = tmp_path / "trustpolicy.json"
    policy.write_text("{}")
    manifest_bytes = json.dumps({"schemaVersion": 2, "layers": []}).encode("utf-8")
    expected_digest = compute_sha256_digest(manifest_bytes)
    fake = _VerifyFakeOras(manifest_bytes)
    args = _verify_args(f"alpha-bank/catalog:{_NAMED_ALIAS}")
    rc = verify_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        verify_fn=_verify_ok,
        ecr_client=aws_stack["ecr"],
        oras_module=fake,
        trust_policy_path=policy,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "reached via mutable alias" in out
    assert _NAMED_ALIAS in out
    # NEVER render a verdict keyed on the alias string — it is keyed on the digest.
    assert f"verified: {expected_digest}" in out
    assert f"verified: {_NAMED_ALIAS}" not in out
    # The digest was resolved via the EXISTING fetch_manifest machinery.
    assert fake.fetch_calls


def test_verify_canonical_tag_does_not_resolve_a_digest(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy = tmp_path / "trustpolicy.json"
    policy.write_text("{}")
    fake = _VerifyFakeOras(b"{}")
    args = _verify_args(f"alpha-bank/catalog:{_REF_TAG}")
    rc = verify_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        verify_fn=_verify_ok,
        ecr_client=aws_stack["ecr"],
        oras_module=fake,
        trust_policy_path=policy,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "mutable alias" not in out
    # A canonical verify keeps its resolved reference and never fetches a manifest
    # just to label (no extra ECR round-trip on the non-alias path).
    assert fake.fetch_calls == []


def test_verify_via_alias_keeps_a_real_failure_failed(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A FAILED signature verify on the alias path must STAY failed.

    The Marcus invariant cuts both ways: alias resolution/labeling must never
    convert a real PASS into a FAIL (covered below) AND must never mask a real
    FAIL into a PASS. When ``verify_fn`` raises, the verdict is the failure exit
    code, keyed on the error — the alias-labeling block is never reached, so no
    "verified:" line (keyed on alias OR digest) is ever rendered.
    """
    from cairn_signing import NotationCommandError

    policy = tmp_path / "trustpolicy.json"
    policy.write_text("{}")
    fake = _VerifyFakeOras(b"{}")

    def _verify_fail(_reference: str) -> None:
        raise NotationCommandError(
            returncode=1, stderr="signature verification failed", command=["notation", "verify"]
        )

    args = _verify_args(f"alpha-bank/catalog:{_NAMED_ALIAS}")
    rc = verify_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        verify_fn=_verify_fail,
        ecr_client=aws_stack["ecr"],
        oras_module=fake,
        trust_policy_path=policy,
    )
    assert rc == 4
    captured = capsys.readouterr()
    # No verdict rendered at all — never a PASS, never keyed on the alias label.
    assert "verified:" not in captured.out
    assert "reached via mutable alias" not in captured.out
    assert "signature verification failed" in captured.err
    # The failure short-circuited before any label-resolution fetch.
    assert fake.fetch_calls == []


def test_verify_alias_digest_resolution_failure_falls_back_safely(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A SUCCESSFUL verify must stay successful even if the label fetch fails."""
    policy = tmp_path / "trustpolicy.json"
    policy.write_text("{}")

    class _FailingFetch(_VerifyFakeOras):
        def fetch_manifest(self, *, reference: str) -> bytes:
            raise self.OrasCommandError(returncode=1, stderr="boom", command=[])

    fake = _FailingFetch(b"{}")
    args = _verify_args(f"alpha-bank/catalog:{_NAMED_ALIAS}")
    rc = verify_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        verify_fn=_verify_ok,
        ecr_client=aws_stack["ecr"],
        oras_module=fake,
        trust_policy_path=policy,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "verified:" in out


# --- query (task 4.2) -----------------------------------------------------


def _query_args(project: str) -> argparse.Namespace:
    return argparse.Namespace(
        project=project,
        tenant_id="acme",
        control=None,
        catalog_or_profile_uuid=None,
        limit=100,
        page_token=None,
        stage="alpha",
        region_code="cmh",
        app_name=None,
        date_range=None,
        date_from=None,
        date_to=None,
        assessment_id=None,
        text=None,
        digest=None,
    )


def _seed(resource, *, project_id: str, image_tag: str, idx: int) -> None:
    index = MetadataIndex(METADATA_TABLE, dynamodb_resource=resource)
    digest = f"sha256:{idx:064x}"
    index.create_on_validated(
        manifest_digest=digest,
        tenant_id="acme",
        project_id=project_id,
        record={"manifestDigest": digest, "imageTag": image_tag},
    )


def test_query_labels_alias_tagged_record(aws_stack, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(aws_stack["dynamodb"], project_id="alpha-bank", image_tag=_NAMED_ALIAS, idx=0)
    rc = query_module._run(_query_args("alpha-bank"), dynamodb_resource=aws_stack["dynamodb"])
    assert rc == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["reachedViaAlias"] is True
    assert record["aliasTag"] == _NAMED_ALIAS
    # The digest stays the identity, untouched.
    assert record["manifestDigest"] == f"sha256:{0:064x}"


def test_query_canonical_record_not_labeled(aws_stack, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(aws_stack["dynamodb"], project_id="alpha-bank", image_tag=_REF_TAG, idx=1)
    rc = query_module._run(_query_args("alpha-bank"), dynamodb_resource=aws_stack["dynamodb"])
    assert rc == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    record = json.loads(lines[0])
    assert "reachedViaAlias" not in record
    assert "aliasTag" not in record

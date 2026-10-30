"""Tests for cairn_cli.alias — set/move alias tags over already-filed digests.

The wrapper enforces three invariants: grammar, the filed-digest precondition,
and legible AccessDenied surfacing (IAM is the load-bearing control). A move
emits a structured audit event.

ECR is faked (no live AWS, no subprocess): the fake models just the four verbs
the wrapper calls — ``describe_images``, ``batch_get_image``, ``put_image`` —
plus an injectable ``AccessDenied``. SSM + DynamoDB come from the moto-backed
``aws_stack`` fixture.
"""

from __future__ import annotations

import argparse
import logging

import pytest
from botocore.exceptions import ClientError
from cairn_cli import alias as alias_module
from cairn_cli import main as main_module
from cairn_metadata import MetadataIndex
from cairn_oci import ArtifactLocator

from tests.unit.cairn_cli.conftest import METADATA_TABLE, subparser_help

_LOCATOR = ArtifactLocator()
ORG = "acme-co"
TENANT = "acme"
PROJECT = "alpha-bank"
KIND = "catalog"
FILED_DIGEST = "sha256:" + "a" * 64
OTHER_FILED_DIGEST = "sha256:" + "b" * 64
CANONICAL_TAG = _LOCATOR.build_ref_tag(artifact_kind=KIND, digest="cafef00dbeef")
OTHER_CANONICAL_TAG = _LOCATOR.build_ref_tag(artifact_kind=KIND, digest="deadbeef1234")
#: A literal 4-segment ``{artifact-kind}--_ref--{name}--{digest}`` string.
#: ``build_ref_tag`` does not mint a ``name`` segment, so this is constructed as a
#: literal; ``parse_tag`` raises on it, so a digest in this form reads as NOT filed
#: (conservative).
LEGACY_REF_TAG = f"{KIND}--_ref--nist-800-53--cafef00dbeef"


class _ImageNotFound(Exception):
    pass


class _FakeEcr:
    """In-memory ECR modeling describe_images / batch_get_image / put_image.

    State is two dicts: digest -> set(tags) and tag -> digest. ``access_denied``
    forces every mutating/inspecting call to raise an ECR AccessDenied so the
    authz-surfacing branch can be exercised without IAM.
    """

    def __init__(self, *, access_denied: bool = False) -> None:
        self.digest_tags: dict[str, set[str]] = {}
        self.tag_digest: dict[str, str] = {}
        self.access_denied = access_denied
        self.put_calls: list[dict[str, str]] = []

        class _Exceptions:
            ImageNotFoundException = _ImageNotFound

        self.exceptions = _Exceptions()

    # -- seeding helpers --------------------------------------------------
    def add_image(self, digest: str, *tags: str) -> None:
        self.digest_tags.setdefault(digest, set()).update(tags)
        for tag in tags:
            self.tag_digest[tag] = digest

    # -- modeled ECR verbs ------------------------------------------------
    def _maybe_deny(self) -> None:
        if self.access_denied:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
                "PutImage",
            )

    def describe_images(self, *, repositoryName, imageIds):
        self._maybe_deny()
        image_id = imageIds[0]
        if "imageDigest" in image_id:
            digest = image_id["imageDigest"]
            if digest not in self.digest_tags:
                raise _ImageNotFound()
            return {
                "imageDetails": [
                    {"imageDigest": digest, "imageTags": sorted(self.digest_tags[digest])}
                ]
            }
        tag = image_id["imageTag"]
        if tag not in self.tag_digest:
            raise _ImageNotFound()
        return {"imageDetails": [{"imageDigest": self.tag_digest[tag], "imageTags": [tag]}]}

    def batch_get_image(self, *, repositoryName, imageIds, acceptedMediaTypes):
        self._maybe_deny()
        digest = imageIds[0]["imageDigest"]
        if digest not in self.digest_tags:
            return {
                "images": [],
                "failures": [{"imageId": imageIds[0], "failureCode": "ImageNotFound"}],
            }
        return {
            "images": [
                {
                    "imageManifest": '{"mediaType":"application/vnd.oci.image.manifest.v1+json"}',
                    "imageManifestMediaType": "application/vnd.oci.image.manifest.v1+json",
                    "imageId": {"imageDigest": digest},
                }
            ]
        }

    def put_image(self, *, repositoryName, imageManifest, imageManifestMediaType, imageTag):
        self._maybe_deny()
        target = self._pending_digest
        self.put_calls.append({"tag": imageTag, "digest": target})
        # Atomic repoint: drop the tag from any prior digest, point it at the
        # target. The target digest already exists (batch_get_image succeeded).
        prior = self.tag_digest.get(imageTag)
        if prior is not None and prior in self.digest_tags:
            self.digest_tags[prior].discard(imageTag)
        self.digest_tags.setdefault(target, set()).add(imageTag)
        self.tag_digest[imageTag] = target
        return {"image": {"imageId": {"imageDigest": target, "imageTag": imageTag}}}

    # The digest the next put_image lands the tag on; the test harness sets it
    # to args.digest before invoking the wrapper.
    _pending_digest = FILED_DIGEST


class _FakeSts:
    def __init__(self, arn: str = "arn:aws:iam::123456789012:role/alias-mover") -> None:
        self._arn = arn

    def get_caller_identity(self):
        return {"Arn": self._arn, "Account": "123456789012", "UserId": "AIDA"}


def _make_args(
    verb: str, *, name=None, channel=None, digest=FILED_DIGEST, kind=KIND
) -> argparse.Namespace:
    return argparse.Namespace(
        alias_command=verb,
        org_id=ORG,
        tenant_id=TENANT,
        project_id=PROJECT,
        artifact_kind=kind,
        digest=digest,
        name=name,
        channel=channel,
        stage="alpha",
        region_code="cmh",
        app_name=None,
        handler=alias_module.run,
    )


def _seed_filed(ecr: _FakeEcr, resource, *, digest=FILED_DIGEST, canonical=CANONICAL_TAG) -> None:
    """Make ``digest`` filed: canonical _ref tag in ECR + a Metadata_Index row."""
    ecr.add_image(digest, canonical)
    idx = MetadataIndex(METADATA_TABLE, dynamodb_resource=resource)
    idx.create_on_validated(
        manifest_digest=digest,
        tenant_id=TENANT,
        project_id=PROJECT,
        record={"manifestDigest": digest, "validateStatus": "validated"},
    )


def _invoke(args, ecr, resource, sts=None) -> int:
    ecr._pending_digest = args.digest
    return alias_module._run(
        args,
        ecr_client=ecr,
        dynamodb_resource=resource,
        sts_client=sts or _FakeSts(),
    )


# --------------------------------------------------------------------------
# Grammar (well-formed vs malformed alias)
# --------------------------------------------------------------------------


def test_set_named_alias_to_filed_digest_succeeds(aws_stack, capsys) -> None:
    ecr = _FakeEcr()
    _seed_filed(ecr, aws_stack["dynamodb"])
    rc = _invoke(_make_args("set", name="nist-800-53-latest"), ecr, aws_stack["dynamodb"])
    assert rc == 0
    expected_tag = _LOCATOR.build_named_alias(artifact_kind=KIND, name="nist-800-53-latest")
    assert ecr.tag_digest[expected_tag] == FILED_DIGEST
    assert expected_tag in capsys.readouterr().out


def test_set_channel_pointer_to_filed_digest_succeeds(aws_stack, capsys) -> None:
    ecr = _FakeEcr()
    _seed_filed(ecr, aws_stack["dynamodb"])
    rc = _invoke(_make_args("set", channel="latest"), ecr, aws_stack["dynamodb"])
    assert rc == 0
    expected_tag = _LOCATOR.build_channel_pointer(artifact_kind=KIND, channel="latest")
    assert ecr.tag_digest[expected_tag] == FILED_DIGEST


def test_malformed_alias_slug_rejected(aws_stack, capsys) -> None:
    ecr = _FakeEcr()
    _seed_filed(ecr, aws_stack["dynamodb"])
    # Uppercase / underscore are not render-safe slug chars → build_* raises.
    rc = _invoke(_make_args("set", name="Bad_Slug!"), ecr, aws_stack["dynamodb"])
    assert rc == 2
    assert "malformed alias" in capsys.readouterr().err
    assert ecr.put_calls == []


def test_malformed_digest_rejected(aws_stack, capsys) -> None:
    ecr = _FakeEcr()
    rc = _invoke(_make_args("set", name="x", digest="not-a-digest"), ecr, aws_stack["dynamodb"])
    assert rc == 2
    assert "Manifest_Digest" in capsys.readouterr().err


def test_non_hex_digest_rejected(aws_stack, capsys) -> None:
    """The substitution target is the most security-relevant input — a sha256:
    prefix with a correct-length but NON-hex tail must reject before any AWS
    call, not be passed through to ECR."""
    ecr = _FakeEcr()
    bad = "sha256:" + "g" * 64  # right length, 'g' is not a hex digit
    rc = _invoke(_make_args("set", name="x", digest=bad), ecr, aws_stack["dynamodb"])
    assert rc == 2
    assert "Manifest_Digest" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Filed-digest precondition: never-pushed vs pushed-not-filed
# --------------------------------------------------------------------------


def test_never_pushed_digest_rejected(aws_stack, capsys) -> None:
    ecr = _FakeEcr()  # digest absent entirely
    rc = _invoke(_make_args("set", name="x"), ecr, aws_stack["dynamodb"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "never pushed" in err
    assert ecr.put_calls == []


def test_pushed_but_no_canonical_tag_rejected(aws_stack, capsys) -> None:
    ecr = _FakeEcr()
    ecr.add_image(FILED_DIGEST)  # present, but NO canonical _ref tag, NO index row
    rc = _invoke(_make_args("set", name="x"), ecr, aws_stack["dynamodb"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not yet filed" in err
    assert "no canonical _ref tag" in err
    assert "no Metadata_Index row" in err
    assert ecr.put_calls == []


def test_legacy_four_segment_ref_tag_only_reads_as_not_filed(aws_stack, capsys) -> None:
    # A digest carrying ONLY the 4-segment `_ref` tag is NOT filed. The grammar
    # authority raises
    # InvalidTagError on the legacy form, so `_has_canonical_ref_tag` treats it
    # exactly like an unparseable tag — conservative: legacy-only = not filed.
    # An index row is seeded too, isolating the failure to the missing canonical
    # tag (so the error names the tag, not the row).
    ecr = _FakeEcr()
    ecr.add_image(FILED_DIGEST, LEGACY_REF_TAG)
    idx = MetadataIndex(METADATA_TABLE, dynamodb_resource=aws_stack["dynamodb"])
    idx.create_on_validated(
        manifest_digest=FILED_DIGEST, tenant_id=TENANT, project_id=PROJECT, record={"x": 1}
    )
    rc = _invoke(_make_args("set", name="x"), ecr, aws_stack["dynamodb"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not yet filed" in err
    assert "no canonical _ref tag" in err
    assert "no Metadata_Index row" not in err  # the row exists; only the tag is legacy
    assert ecr.put_calls == []


def test_has_canonical_ref_tag_three_segment_true_legacy_false() -> None:
    # Unit-level guard on the predicate itself (no AWS): the new 3-segment
    # canonical `_ref` tag of the right kind reads as canonical; a digest set
    # carrying ONLY the legacy 4-segment form, or ONLY alias (`_als`/`_chn`)
    # tags, or a `_ref` for a different kind, does not.
    named = _LOCATOR.build_named_alias(artifact_kind=KIND, name="nist-800-53-latest")
    channel = _LOCATOR.build_channel_pointer(artifact_kind=KIND, channel="latest")
    other_kind_ref = _LOCATOR.build_ref_tag(artifact_kind="poam", digest="cafef00dbeef")
    assert alias_module._has_canonical_ref_tag([CANONICAL_TAG], artifact_kind=KIND) is True
    assert alias_module._has_canonical_ref_tag([LEGACY_REF_TAG], artifact_kind=KIND) is False
    assert alias_module._has_canonical_ref_tag([named, channel], artifact_kind=KIND) is False
    assert alias_module._has_canonical_ref_tag([other_kind_ref], artifact_kind=KIND) is False
    # And it still finds the canonical tag amid alias noise.
    assert (
        alias_module._has_canonical_ref_tag([named, CANONICAL_TAG, channel], artifact_kind=KIND)
        is True
    )


def test_pushed_canonical_tag_but_no_index_row_rejected(aws_stack, capsys) -> None:
    ecr = _FakeEcr()
    ecr.add_image(FILED_DIGEST, CANONICAL_TAG)  # tagged but NOT indexed
    rc = _invoke(_make_args("set", name="x"), ecr, aws_stack["dynamodb"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not yet filed" in err
    assert "no Metadata_Index row" in err
    assert "no canonical _ref tag" not in err
    assert ecr.put_calls == []


def test_canonical_tag_for_other_kind_does_not_count_as_filed(aws_stack, capsys) -> None:
    ecr = _FakeEcr()
    # The digest carries a _ref tag, but for a DIFFERENT artifact-kind.
    other_kind_tag = _LOCATOR.build_ref_tag(artifact_kind="poam", digest="cafef00dbeef")
    ecr.add_image(FILED_DIGEST, other_kind_tag)
    idx = MetadataIndex(METADATA_TABLE, dynamodb_resource=aws_stack["dynamodb"])
    idx.create_on_validated(
        manifest_digest=FILED_DIGEST, tenant_id=TENANT, project_id=PROJECT, record={"x": 1}
    )
    rc = _invoke(_make_args("set", name="x", kind=KIND), ecr, aws_stack["dynamodb"])
    assert rc == 2
    assert "no canonical _ref tag" in capsys.readouterr().err


# --------------------------------------------------------------------------
# set = create-or-fail; move = rebind-existing
# --------------------------------------------------------------------------


def test_set_refuses_when_alias_already_exists(aws_stack, capsys) -> None:
    ecr = _FakeEcr()
    _seed_filed(ecr, aws_stack["dynamodb"])
    alias_tag = _LOCATOR.build_named_alias(artifact_kind=KIND, name="latest-alias")
    ecr.add_image(OTHER_FILED_DIGEST, alias_tag)  # alias already bound elsewhere
    rc = _invoke(_make_args("set", name="latest-alias"), ecr, aws_stack["dynamodb"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "already exists" in err
    assert "cairn alias move" in err
    assert ecr.put_calls == []


def test_set_same_digest_is_idempotent_noop(aws_stack, capsys) -> None:
    ecr = _FakeEcr()
    _seed_filed(ecr, aws_stack["dynamodb"])
    alias_tag = _LOCATOR.build_named_alias(artifact_kind=KIND, name="latest-alias")
    ecr.add_image(FILED_DIGEST, alias_tag)  # already points at the same filed digest
    rc = _invoke(_make_args("set", name="latest-alias"), ecr, aws_stack["dynamodb"])
    assert rc == 0
    assert "no-op" in capsys.readouterr().out
    assert ecr.put_calls == []


def test_move_rebinds_existing_alias(aws_stack, capsys) -> None:
    ecr = _FakeEcr()
    resource = aws_stack["dynamodb"]
    _seed_filed(ecr, resource, digest=FILED_DIGEST, canonical=CANONICAL_TAG)
    _seed_filed(ecr, resource, digest=OTHER_FILED_DIGEST, canonical=OTHER_CANONICAL_TAG)
    alias_tag = _LOCATOR.build_named_alias(artifact_kind=KIND, name="latest-alias")
    ecr.add_image(FILED_DIGEST, alias_tag)  # currently on the first digest

    rc = _invoke(_make_args("move", name="latest-alias", digest=OTHER_FILED_DIGEST), ecr, resource)
    assert rc == 0
    assert ecr.tag_digest[alias_tag] == OTHER_FILED_DIGEST
    out = capsys.readouterr().out
    assert FILED_DIGEST in out and OTHER_FILED_DIGEST in out


def test_move_to_unfiled_digest_rejected(aws_stack, capsys) -> None:
    # The filed-digest precondition gates BOTH set and move: a move
    # onto an existing alias must still be rejected if the TARGET digest is not
    # filed (never pushed → distinct "never pushed" error), and no tag is written.
    ecr = _FakeEcr()
    resource = aws_stack["dynamodb"]
    _seed_filed(ecr, resource, digest=FILED_DIGEST, canonical=CANONICAL_TAG)
    alias_tag = _LOCATOR.build_named_alias(artifact_kind=KIND, name="latest-alias")
    ecr.add_image(FILED_DIGEST, alias_tag)  # alias exists on a filed digest …

    # … but the move TARGET (OTHER_FILED_DIGEST) was never pushed.
    rc = _invoke(_make_args("move", name="latest-alias", digest=OTHER_FILED_DIGEST), ecr, resource)
    assert rc == 2
    err = capsys.readouterr().err
    assert "never pushed" in err
    assert ecr.put_calls == []
    # The alias is left untouched on its original digest.
    assert ecr.tag_digest[alias_tag] == FILED_DIGEST


def test_move_preserves_prior_digest_canonical_ref_tag(aws_stack, capsys) -> None:
    # Spec scenario: "the previously pointed-to manifest retains its canonical
    # immutable `_ref` tag" after an alias move. The move repoints ONLY the alias
    # tag; the prior digest keeps its canonical _ref tag (immutable identity).
    ecr = _FakeEcr()
    resource = aws_stack["dynamodb"]
    _seed_filed(ecr, resource, digest=FILED_DIGEST, canonical=CANONICAL_TAG)
    _seed_filed(ecr, resource, digest=OTHER_FILED_DIGEST, canonical=OTHER_CANONICAL_TAG)
    alias_tag = _LOCATOR.build_named_alias(artifact_kind=KIND, name="latest-alias")
    ecr.add_image(FILED_DIGEST, alias_tag)

    rc = _invoke(_make_args("move", name="latest-alias", digest=OTHER_FILED_DIGEST), ecr, resource)
    assert rc == 0
    # The alias now lands on the new digest in a SINGLE put_image call (atomic).
    assert ecr.tag_digest[alias_tag] == OTHER_FILED_DIGEST
    assert len(ecr.put_calls) == 1
    # The prior digest KEEPS its canonical immutable _ref tag, and shed the alias.
    assert CANONICAL_TAG in ecr.digest_tags[FILED_DIGEST]
    assert alias_tag not in ecr.digest_tags[FILED_DIGEST]


def test_move_refuses_when_alias_absent(aws_stack, capsys) -> None:
    ecr = _FakeEcr()
    _seed_filed(ecr, aws_stack["dynamodb"])
    rc = _invoke(_make_args("move", name="never-set"), ecr, aws_stack["dynamodb"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "does not exist" in err
    assert "cairn alias set" in err
    assert ecr.put_calls == []


# --------------------------------------------------------------------------
# AccessDenied surfacing
# --------------------------------------------------------------------------


def test_access_denied_surfaced_legibly(aws_stack, capsys) -> None:
    ecr = _FakeEcr(access_denied=True)
    rc = _invoke(_make_args("set", name="x"), ecr, aws_stack["dynamodb"])
    assert rc == 4
    err = capsys.readouterr().err
    assert "access denied" in err
    assert "cairnproofs4af-alias-mover-cmh-alpha" in err


class _DenyPutImageEcr(_FakeEcr):
    """Filed-check and inspect verbs succeed; only the put_image WRITE denies.

    The base ``_FakeEcr(access_denied=True)`` denies every verb, so it can only
    exercise the AccessDenied branch at the FIRST inspect call. A move that
    clears the filed-digest precondition and the prior-binding lookup, then hits
    AccessDenied at the actual tag WRITE, is a distinct path through
    ``_maybe_access_denied`` — this fake isolates it.
    """

    def put_image(self, **kwargs):
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "PutImage",
        )


def test_move_write_access_denied_surfaced_legibly(aws_stack, capsys) -> None:
    ecr = _DenyPutImageEcr()
    resource = aws_stack["dynamodb"]
    _seed_filed(ecr, resource, digest=FILED_DIGEST, canonical=CANONICAL_TAG)
    _seed_filed(ecr, resource, digest=OTHER_FILED_DIGEST, canonical=OTHER_CANONICAL_TAG)
    alias_tag = _LOCATOR.build_named_alias(artifact_kind=KIND, name="latest-alias")
    ecr.add_image(FILED_DIGEST, alias_tag)  # alias exists → move proceeds to the write

    rc = _invoke(_make_args("move", name="latest-alias", digest=OTHER_FILED_DIGEST), ecr, resource)
    assert rc == 4
    err = capsys.readouterr().err
    assert "access denied" in err
    assert "cairnproofs4af-alias-mover-cmh-alpha" in err


# --------------------------------------------------------------------------
# Audit event on move
# --------------------------------------------------------------------------


def test_move_emits_structured_audit_event(aws_stack, caplog) -> None:
    ecr = _FakeEcr()
    resource = aws_stack["dynamodb"]
    _seed_filed(ecr, resource, digest=FILED_DIGEST, canonical=CANONICAL_TAG)
    _seed_filed(ecr, resource, digest=OTHER_FILED_DIGEST, canonical=OTHER_CANONICAL_TAG)
    alias_tag = _LOCATOR.build_named_alias(artifact_kind=KIND, name="latest-alias")
    ecr.add_image(FILED_DIGEST, alias_tag)

    with caplog.at_level(logging.INFO, logger="cairn.alias.audit"):
        rc = _invoke(
            _make_args("move", name="latest-alias", digest=OTHER_FILED_DIGEST),
            ecr,
            resource,
            sts=_FakeSts(arn="arn:aws:iam::123456789012:role/alias-mover"),
        )
    assert rc == 0
    records = [r for r in caplog.records if r.name == "cairn.alias.audit"]
    assert len(records) == 1
    rec = records[0]
    assert rec.event == "alias.move"
    assert rec.alias == alias_tag
    assert rec.priorDigest == FILED_DIGEST
    assert rec.newDigest == OTHER_FILED_DIGEST
    assert rec.principal == "arn:aws:iam::123456789012:role/alias-mover"


def test_set_does_not_emit_move_audit(aws_stack, caplog) -> None:
    ecr = _FakeEcr()
    _seed_filed(ecr, aws_stack["dynamodb"])
    with caplog.at_level(logging.INFO, logger="cairn.alias.audit"):
        rc = _invoke(_make_args("set", name="fresh"), ecr, aws_stack["dynamodb"])
    assert rc == 0
    assert [r for r in caplog.records if r.name == "cairn.alias.audit"] == []


# --------------------------------------------------------------------------
# Parser registration
# --------------------------------------------------------------------------


def test_alias_subcommands_registered() -> None:
    parser = main_module.build_parser()
    args = parser.parse_args(["alias", "set", PROJECT, KIND, FILED_DIGEST, "--name", "x"])
    assert args.command == "alias"
    assert args.alias_command == "set"
    assert args.handler is alias_module.run
    args = parser.parse_args(["alias", "move", PROJECT, KIND, FILED_DIGEST, "--channel", "latest"])
    assert args.alias_command == "move"


def test_alias_name_and_channel_mutually_exclusive() -> None:
    parser = main_module.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["alias", "set", PROJECT, KIND, FILED_DIGEST, "--name", "x", "--channel", "y"]
        )


# --------------------------------------------------------------------------
# Help text (additive — usage examples + description)
# --------------------------------------------------------------------------


def test_alias_help_has_usage_and_examples() -> None:
    help_text = subparser_help(main_module.build_parser(), "alias")
    assert "usage" in help_text
    assert "Examples:" in help_text
    assert "cairn alias set" in help_text


def test_alias_set_verb_help_has_usage_and_example() -> None:
    help_text = subparser_help(main_module.build_parser(), "alias", "set")
    assert "usage" in help_text
    assert "Examples:" in help_text
    assert "cairn alias set" in help_text

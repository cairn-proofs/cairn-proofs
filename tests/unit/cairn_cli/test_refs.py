"""Tests for cairn_cli._refs — reference parsing (long + short forms)."""

from __future__ import annotations

import pytest
from cairn_cli._aws import DeploymentTarget
from cairn_cli._refs import InvalidReferenceError, resolve_reference

TARGET = DeploymentTarget(stage="alpha", region_code="cmh", app_name="cairnproofs4af")
HOST = "123456789012.dkr.ecr.us-east-2.amazonaws.com"
DIGEST = "sha256:" + "a" * 64
# Canonical 3-segment ECR tag: {artifact-kind}--_ref--{digest}. The CLI treats a
# tag as an opaque selector — it neither parses nor builds canonical tags — so
# this is a representative literal, not a grammar dependency.
CANONICAL_TAG = "assessment-results--_ref--0123456789ab"


def test_long_form_by_tag_passes_through() -> None:
    ref = resolve_reference(
        f"{HOST}/artifact-cmh-alpha/foo/assessment-results:{CANONICAL_TAG}",
        registry_host=HOST,
        target=TARGET,
    )
    assert ref.registry == HOST
    assert ref.repository == "artifact-cmh-alpha/foo/assessment-results"
    assert ref.selector == f":{CANONICAL_TAG}"


def test_long_form_by_digest_passes_through() -> None:
    ref = resolve_reference(
        f"{HOST}/artifact-cmh-alpha/foo/assessment-results@{DIGEST}",
        registry_host=HOST,
        target=TARGET,
    )
    assert ref.registry == HOST
    assert ref.repository == "artifact-cmh-alpha/foo/assessment-results"
    assert ref.selector == f"@{DIGEST}"


# The short form expands to the appName-namespaced repo path (the deployed ECR
# namespace), while a long-form ref the user typed is echoed verbatim.
NS_REPO = "cairnproofs4af-artifact-cmh-alpha/foo/assessment-results"


def test_short_form_by_tag_expands() -> None:
    ref = resolve_reference(
        f"foo/assessment-results:{CANONICAL_TAG}",
        registry_host=HOST,
        target=TARGET,
    )
    assert ref.repository == NS_REPO
    assert ref.selector == f":{CANONICAL_TAG}"
    assert ref.full == f"{HOST}/{NS_REPO}:{CANONICAL_TAG}"


def test_short_form_by_digest_expands() -> None:
    ref = resolve_reference(
        f"foo/assessment-results@{DIGEST}",
        registry_host=HOST,
        target=TARGET,
    )
    assert ref.repository == NS_REPO
    assert ref.selector == f"@{DIGEST}"


def test_rejects_no_slash() -> None:
    with pytest.raises(InvalidReferenceError):
        resolve_reference("nope", registry_host=HOST, target=TARGET)


def test_rejects_no_selector() -> None:
    with pytest.raises(InvalidReferenceError):
        resolve_reference("foo/bar", registry_host=HOST, target=TARGET)


def test_rejects_malformed_digest() -> None:
    with pytest.raises(InvalidReferenceError):
        resolve_reference(
            "foo/assessment-results@sha256:notlongenough",
            registry_host=HOST,
            target=TARGET,
        )


def test_rejects_empty_tag() -> None:
    with pytest.raises(InvalidReferenceError):
        resolve_reference("foo/assessment-results:", registry_host=HOST, target=TARGET)


def test_short_form_with_dotted_segment_rejected() -> None:
    # Dotted first segment looks like a registry host; if it doesn't match
    # the host regex AND isn't valid short form, we reject.
    with pytest.raises(InvalidReferenceError):
        resolve_reference("FOO/bar:x", registry_host=HOST, target=TARGET)


# --- derived-ref-id-discriminator wave 5 (task 6.1/6.2) ---------------------


def test_canonical_tag_selector_preserved_verbatim() -> None:
    # The single canonical form carries two ``--`` separators inside the :tag
    # selector. The resolver splits repository from selector on the FIRST
    # selector char (``:``), so every ``--`` after it stays in the selector
    # untouched — the CLI never parses the tag into segments.
    assert CANONICAL_TAG.count("--") == 2
    ref = resolve_reference(
        f"foo/assessment-results:{CANONICAL_TAG}",
        registry_host=HOST,
        target=TARGET,
    )
    assert ref.selector == f":{CANONICAL_TAG}"
    assert ref.repository == NS_REPO
    # The full ECR-qualified reference round-trips the tag with no mangling.
    assert ref.full.endswith(f":{CANONICAL_TAG}")


def test_by_digest_resolution_unaffected_by_tag_form_change() -> None:
    # By-``@digest`` resolution is the load-bearing identity path and is wholly
    # independent of any tag form. Long and short forms both pass the digest
    # selector through verbatim.
    # A long-form ref is echoed verbatim (the user already typed the full repo
    # path); the short form expands to the appName-namespaced path.
    long_ref = resolve_reference(
        f"{HOST}/{NS_REPO}@{DIGEST}",
        registry_host=HOST,
        target=TARGET,
    )
    short_ref = resolve_reference(
        f"foo/assessment-results@{DIGEST}",
        registry_host=HOST,
        target=TARGET,
    )
    assert long_ref.selector == short_ref.selector == f"@{DIGEST}"
    assert long_ref.repository == short_ref.repository == NS_REPO

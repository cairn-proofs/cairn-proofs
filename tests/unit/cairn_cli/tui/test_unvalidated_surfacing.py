"""Failed bundles surface as unvalidated entries in the TUI bundle list.

Property: given an ECR evidence repository containing an image digest that
matches NO Metadata_Index ``manifestDigest`` for that project,
``_sources.ArtifactSources.list_all_bundles()`` SHALL return an entry for
that digest marked ``status="unvalidated"`` (the did-not-land marker).

The mechanism: an allowlisted ECR image read (``ecr:DescribeImages``) plus
``list_all_bundles`` diffing ECR image digests against the project's
Metadata_Index ``manifestDigest`` set and surfacing the unindexed digests. A
bundle that failed validation has no metadata row; without the ECR image read
it would be silently dropped.

The recording client mirrors the ReadOnlyAwsClient surface used in
``test_sources.py`` and adds a ``describe_images`` recorder for the new
allowlisted image read the fix introduces. ``scan`` / ``receive_message``
remain structurally absent so the no-Scan invariant still fails loudly.
"""

from __future__ import annotations

from typing import Any

from cairn_cli._aws import DeploymentTarget
from cairn_cli.tui._sources import ArtifactSources

REGION = "us-east-2"
ACCOUNT_ID = "123456789012"
TARGET = DeploymentTarget(stage="alpha", region_code="cmh", app_name="cairnproofs4af")
QUALIFIER = "cmh-alpha"


class _RecordingTable:
    def __init__(self, name: str, calls: list[str], pages_by_pk: dict[str, list[dict]]) -> None:
        self._name = name
        self._calls = calls
        self._pages_by_pk = pages_by_pk
        self._cursor: dict[str, int] = {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self._calls.append("query")
        pk = kwargs["ExpressionAttributeValues"][":pk"]
        pages = self._pages_by_pk.get(pk, [{"Items": []}])
        idx = 0 if "ExclusiveStartKey" not in kwargs else self._cursor.get(pk, 0)
        page = pages[idx] if idx < len(pages) else {"Items": []}
        if "LastEvaluatedKey" in page:
            self._cursor[pk] = idx + 1
        return page

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self._calls.append("get_item")
        return {}


class _RecordingResource:
    def __init__(self, calls: list[str], pages_by_pk: dict[str, list[dict]]) -> None:
        self._calls = calls
        self._pages_by_pk = pages_by_pk

    def Table(self, name: str) -> _RecordingTable:  # boto3 API name
        return _RecordingTable(name, self._calls, self._pages_by_pk)


class _RecordingClient:
    """ReadOnlyAwsClient-shaped stub recording every op, incl. describe_images.

    ``describe_images`` is the allowlisted image read for the ECR-diff path; the
    stub serves a scripted ``imageDetails`` list per repository so the fix's
    ECR-diff path has something to read. Methods outside the allowlist
    (``scan``, ``receive_message``) are structurally absent.
    """

    def __init__(
        self,
        *,
        ecr_pages: list[dict],
        pages_by_pk: dict[str, list[dict]] | None = None,
        images_by_repo: dict[str, list[dict]] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._ecr_pages = ecr_pages
        self._resource = _RecordingResource(self.calls, pages_by_pk or {})
        self._images_by_repo = images_by_repo or {}

    @property
    def dynamodb_resource(self) -> _RecordingResource:
        return self._resource

    def describe_repositories(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("describe_repositories")
        idx = int(kwargs["nextToken"]) if "nextToken" in kwargs else 0
        return self._ecr_pages[idx] if idx < len(self._ecr_pages) else {"repositories": []}

    def describe_images(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("describe_images")
        repo = kwargs.get("repositoryName")
        return {"imageDetails": list(self._images_by_repo.get(repo, []))}

    def batch_get_image(self, **kwargs: Any) -> dict[str, Any]:
        # The per-unindexed-image manifest read (ecr:BatchGetImage) the data
        # layer issues to classify each orphan. This test asserts surfacing +
        # status, not the classification value, so an empty image set (which
        # degrades to "unclassified") is a sufficient stub.
        self.calls.append("batch_get_image")
        return {"images": []}


def _repo(name: str, *, registry_id: str = ACCOUNT_ID) -> dict[str, Any]:
    return {"repositoryName": name, "registryId": registry_id}


def _record(project_id: str, digest: str, **extra: Any) -> dict[str, Any]:
    return {"projectId": project_id, "manifestDigest": digest, **extra}


def _image(digest: str, *, tag: str = "other--_ref--0123456789ab") -> dict[str, Any]:
    return {"imageDigest": digest, "imageTags": [tag]}


def _sources(client: _RecordingClient) -> ArtifactSources:
    return ArtifactSources(target=TARGET, client=client, region=REGION)


def test_ecr_only_digest_surfaces_as_unvalidated_entry() -> None:
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/mvp-tenant/mvp-tenant"
    validated_digest = "sha256:" + "a" * 64
    orphan_digest = "sha256:0e957095" + "0" * 56  # in ECR, absent from metadata

    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        # Metadata_Index has only the validated bundle for this project.
        pages_by_pk={"mvp-tenant": [{"Items": [_record("mvp-tenant", validated_digest)]}]},
        # ECR holds BOTH the validated image and the orphan that never landed.
        images_by_repo={
            repo: [_image(validated_digest), _image(orphan_digest)],
        },
    )
    sources = _sources(client)

    bundles = sources.list_all_bundles()

    by_digest = {b.get("manifestDigest"): b for b in bundles}
    assert orphan_digest in by_digest, (
        "ECR-present-but-unindexed digest was dropped from list_all_bundles — "
        f"got digests {sorted(by_digest)}"
    )
    orphan = by_digest[orphan_digest]
    assert orphan.get("status") == "unvalidated", (
        f"orphan digest not marked status='unvalidated'; entry was {orphan!r}"
    )
    # The validated bundle must still appear and must NOT be mismarked.
    assert validated_digest in by_digest
    assert by_digest[validated_digest].get("status") != "unvalidated"


def test_unvalidated_entry_carries_subject_digest_field() -> None:
    # Every synthetic unindexed entry gains a ``subjectDigest`` key (the
    # grouping pointer the TUI reads).
    # This stub's batch_get_image returns no images, so the manifest is
    # unreadable -> subjectDigest degrades to None, but the FIELD is present.
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/mvp-tenant/mvp-tenant"
    orphan_digest = "sha256:0e957095" + "0" * 56

    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        pages_by_pk={"mvp-tenant": [{"Items": []}]},
        images_by_repo={repo: [_image(orphan_digest)]},
    )
    sources = _sources(client)

    bundles = sources.list_all_bundles()

    by_digest = {b.get("manifestDigest"): b for b in bundles}
    assert orphan_digest in by_digest
    entry = by_digest[orphan_digest]
    assert "subjectDigest" in entry
    assert entry["subjectDigest"] is None

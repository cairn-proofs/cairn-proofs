"""Reference resolution for `cairn pull` / `cairn verify`.

The CLI accepts three reference shapes:

  long form, by digest:   {registry}/{repo}@{sha256:...}
  long form, by tag:      {registry}/{repo}:{tag}
  short form, by digest:  <project-id>/<artifact-kind>@{sha256:...}
  short form, by tag:     <project-id>/<artifact-kind>:{tag}

The short form is expanded to the long form by inserting the resolved
registry hostname AND prepending the Customer_Hosted repo-path prefix
(`{appName}-artifact-{regionCode}-{stage}/`) from the deployment target.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cairn_cli._aws import DeploymentTarget

# A long-form ref always contains a `/` AND either a `:` (tag) or `@` (digest)
# AFTER the registry host. The registry host itself may contain `:` for a
# port, so we test by looking for `.` or `:port` in the first path segment.
_REGISTRY_HOST_RE = re.compile(r"^[a-z0-9.\-]+(?::[0-9]+)?$", re.IGNORECASE)

# A short-form ref has no `.` or `:port` in its leading segment — only
# {project-id}/{artifact-kind}.
_SHORT_FORM_RE = re.compile(
    r"^([a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)/"  # project-id
    r"([a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)"  # artifact-kind
    r"([:@].+)$"  # :tag or @sha256:...
)


@dataclass(frozen=True)
class ResolvedReference:
    """A registry-qualified OCI reference suitable for `oras` / `notation`.

    `repository` is the path AFTER the registry host (e.g.,
    `artifact-cmh-alpha/foo/assessment-results`).
    `selector` is either `:tag` or `@digest` (carries the leading character).
    """

    registry: str
    repository: str
    selector: str

    @property
    def full(self) -> str:
        return f"{self.registry}/{self.repository}{self.selector}"


class InvalidReferenceError(ValueError):
    """Raised when the user-supplied reference can't be parsed."""


def resolve_reference(
    raw: str,
    *,
    registry_host: str,
    target: DeploymentTarget,
) -> ResolvedReference:
    """Resolve a long-form or short-form reference into a full ECR-qualified one.

    `registry_host` is the SSM-resolved ECR registry hostname (e.g.,
    `123456789012.dkr.ecr.us-east-2.amazonaws.com`). It is used both to
    detect long-form vs short-form input and to expand short-form input.
    """
    if not raw or "/" not in raw:
        raise InvalidReferenceError(
            f"reference {raw!r} is malformed — expected "
            "'<project-id>/<artifact-kind>(:tag|@digest)' or a full "
            "registry-qualified reference"
        )

    head, _, tail = raw.partition("/")
    if _REGISTRY_HOST_RE.match(head) and ("." in head or ":" in head):
        # Long form: head is the registry, tail is repo+selector.
        return _parse_long_form(head, tail, raw=raw)

    # Short form: full string must match the project/category prefix grammar.
    m = _SHORT_FORM_RE.match(raw)
    if not m:
        raise InvalidReferenceError(
            f"reference {raw!r} is not in '<project-id>/<artifact-kind>(:tag|@digest)' shape"
        )
    project_id, artifact_kind, selector = m.group(1), m.group(2), m.group(3)
    repository = target.repo_path(f"artifact-{target.qualifier}/{project_id}/{artifact_kind}")
    _validate_selector(selector, raw=raw)
    return ResolvedReference(
        registry=registry_host,
        repository=repository,
        selector=selector,
    )


def _parse_long_form(registry: str, tail: str, *, raw: str) -> ResolvedReference:
    # Tail looks like `artifact-cmh-alpha/foo/assessment-results:tag` or
    # `...@sha256:...`. The selector is the FIRST `@sha256:` or LAST `:` —
    # but ECR repos never contain `:`, so we use the first selector char.
    at_idx = tail.find("@")
    colon_idx = tail.find(":")
    if at_idx == -1 and colon_idx == -1:
        raise InvalidReferenceError(f"reference {raw!r} has no `:tag` or `@digest` selector")
    if at_idx != -1 and (colon_idx == -1 or at_idx < colon_idx):
        repository = tail[:at_idx]
        selector = tail[at_idx:]
    else:
        repository = tail[:colon_idx]
        selector = tail[colon_idx:]
    if not repository:
        raise InvalidReferenceError(f"reference {raw!r} has no repository segment")
    _validate_selector(selector, raw=raw)
    return ResolvedReference(registry=registry, repository=repository, selector=selector)


def _validate_selector(selector: str, *, raw: str) -> None:
    if selector.startswith("@"):
        digest = selector[1:]
        if not digest.startswith("sha256:") or len(digest) != len("sha256:") + 64:
            raise InvalidReferenceError(
                f"reference {raw!r} digest selector {digest!r} is not a sha256:<64-hex>"
            )
    elif selector.startswith(":"):
        if len(selector) < 2:
            raise InvalidReferenceError(f"reference {raw!r} has an empty :tag selector")
    else:
        raise InvalidReferenceError(f"reference {raw!r} has malformed selector {selector!r}")

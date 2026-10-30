"""Identity-registry display-name resolution at the TUI render boundary
([IDREG-001], [TUI-002.04], [TUI-003]; gap-fill area 5).

The control-plane Identity_Registry display name is an untrusted,
producer/operator-influenced source that the TUI renders. [TUI-003]
names it explicitly. ``ArtifactStoreApp._display_identity`` formats the opaque
``{org-id}``/``{tenant-id}``/``{project-id}`` as ``{display name} ({opaque-id})``
when a name is resolved, falls back to the bare opaque id when it is not
(degrade-don't-crash — the registry read may have degraded to ``{}``, see
``_sources.resolve_identity_names``), and the placeholder for an empty/non-str
id. Every render site wraps it in ``_safe`` (the single render-safety choke
point), so a hostile registry display name renders inert.

The source-level degrade (registry read error -> ``{}``) is covered in
``tests/unit/cairn_cli/tui/test_sources.py``; this module pins the app-level
formatting + the render-safety composition that no existing test exercised.
"""

from __future__ import annotations

import re

from cairn_cli._aws import DeploymentTarget
from cairn_cli.tui.app import ArtifactStoreApp

# Same control-byte class the render-safety corpus asserts against: C0, DEL, C1.
_CONTROL_BYTE = re.compile(r"[\x00-\x1f\x7f\x80-\x9f]")
_PLACEHOLDER = "—"

ESC = "\x1b"
BEL = "\x07"


def _app(identity_names: dict[str, str] | None = None) -> ArtifactStoreApp:
    app = ArtifactStoreApp(
        object(),  # type: ignore[arg-type]  # sources unused by these pure methods
        deployment_target=DeploymentTarget(
            stage="alpha", region_code="cmh", app_name="cairnproofs4af"
        ),
    )
    if identity_names is not None:
        app._identity_names = identity_names
    return app


def test_display_identity_formats_name_with_opaque_id_when_resolved() -> None:
    """A resolved name is shown WITHOUT losing the opaque id (the queryable key)."""
    app = _app({"org-01JABC": "Acme Federal"})
    assert app._display_identity("org-01JABC") == "Acme Federal (org-01JABC)"


def test_display_identity_falls_back_to_bare_opaque_id_when_unresolved() -> None:
    """Degrade-don't-crash: an id absent from the join (unknown, or the registry
    read degraded to {}) renders as the bare opaque id ([IDREG-001.03])."""
    app = _app({})  # empty join, as if the registry read degraded
    assert app._display_identity("tnt-01JXYZ") == "tnt-01JXYZ"


def test_display_identity_empty_and_non_str_render_placeholder() -> None:
    app = _app({})
    assert app._display_identity("") == _PLACEHOLDER
    assert app._display_identity(None) == _PLACEHOLDER
    assert app._display_identity(12345) == _PLACEHOLDER


def test_hostile_registry_display_name_renders_inert_through_safe() -> None:
    """A crafted Identity_Registry display name is a [TUI-003] untrusted source:
    routed through the render-call-site composition ``_safe(_display_identity(...))``
    it emits no active control/escape bytes, and the opaque id (the trustworthy
    queryable key) survives intact."""
    hostile = f"{ESC}]0;PWNED{BEL}{ESC}[31mEvil Corp{ESC}[0m\x00"
    app = _app({"org-evil": hostile})

    rendered = app._safe(app._display_identity("org-evil"))

    # No control byte of any class survives.
    assert _CONTROL_BYTE.search(rendered) is None, f"control byte leaked: {rendered!r}"
    # The legitimate printable name text and the opaque id both survive.
    assert "Evil Corp" in rendered
    assert "(org-evil)" in rendered


def test_hostile_opaque_id_fallback_renders_inert_through_safe() -> None:
    """Defense in depth: even on the bare-opaque-id fallback path (no resolved
    name), the value still passes through ``_safe`` inert — an opaque id that
    somehow carried control bytes cannot reach the terminal active."""
    app = _app({})  # nothing resolved -> bare-id fallback path
    hostile_id = f"prj-{ESC}[2J01JBAD"

    rendered = app._safe(app._display_identity(hostile_id))

    assert _CONTROL_BYTE.search(rendered) is None, f"control byte leaked: {rendered!r}"
    assert "prj-" in rendered and "01JBAD" in rendered

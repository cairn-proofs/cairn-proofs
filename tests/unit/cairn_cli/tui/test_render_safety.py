"""Escape-sequence corpus for the terminal-render-safety sanitizer.

Each hostile input is asserted to render INERT: no byte in the C0 range
(0x00-0x1f), no DEL (0x7f), no C1 range (0x80-0x9f) survives, while the
legitimate printable substring is preserved verbatim. Benign inputs pass
through unchanged.
"""

import re

import pytest
from cairn_cli.tui._render_safety import render_safe

# A control byte is anything the terminal could interpret as a command
# rather than as printable text: C0, DEL, or C1.
_CONTROL_BYTE = re.compile(r"[\x00-\x1f\x7f\x80-\x9f]")

ESC = "\x1b"
BEL = "\x07"
ST = "\x1b\\"  # 7-bit string terminator

# (id, hostile_input, expected_surviving_printable_substring)
HOSTILE_CORPUS = [
    # --- CSI / ANSI ---
    ("csi_sgr_color", f"{ESC}[31mRED{ESC}[0m", "RED"),
    ("csi_cursor_move", f"before{ESC}[2;5Hafter", "beforeafter"),
    ("csi_erase_display", f"keep{ESC}[2Jkeep2", "keepkeep2"),
    ("csi_scroll_region", f"a{ESC}[1;10rb", "ab"),
    # --- OSC: window title, clipboard, BEL- and ST-terminated ---
    ("osc_window_title_bel", f"{ESC}]0;evil-title{BEL}ok", "ok"),
    ("osc_clipboard_bel", f"{ESC}]52;c;ZXZpbA=={BEL}safe", "safe"),
    ("osc_terminated_by_st", f"{ESC}]0;evil{ST}tail", "tail"),
    ("osc_set_color", f"x{ESC}]4;1;rgb:ff/00/00{BEL}y", "xy"),
    # --- DCS / APC / PM / SOS string sequences (ST-terminated) ---
    ("dcs_sequence", f"lead{ESC}P0;1|payload{ST}trail", "leadtrail"),
    ("apc_sequence", f"a{ESC}_application-cmd{ST}b", "ab"),
    ("pm_sequence", f"a{ESC}^privacy-msg{ST}b", "ab"),
    ("sos_sequence", f"a{ESC}Xstart-of-string{ST}b", "ab"),
    # --- Raw C0 bytes ---
    ("c0_bel", f"ring{BEL}ing", "ringing"),
    ("c0_backspace", "ab\x08c", "abc"),
    ("c0_nul", "a\x00b", "ab"),
    # Lone ESC: the ESC introducer is stripped; the following printable byte
    # is inert text and survives (we do not greedily consume a real char).
    ("c0_lone_esc", f"a{ESC}b", "ab"),
    ("c0_lone_esc_at_end", f"keep{ESC}", "keep"),
    ("c0_tab_newline_cr", "a\tb\nc\rd", "abcd"),
    # --- DEL ---
    ("del_byte", "a\x7fb", "ab"),
    # --- Raw C1 bytes (8-bit single-byte introducers) ---
    # \x9b == 8-bit CSI introducer; sequence is "\x9b31m", so the "Zb" after
    # the final byte "m" is post-sequence printable text and survives.
    ("c1_csi_8bit", "a\x9b31mZb", "aZb"),
    ("c1_osc_8bit", f"a\x9d0;title{BEL}b", "ab"),  # \x9d == 8-bit OSC
    ("c1_dcs_8bit", f"a\x90payload{ST}b", "ab"),  # \x90 == 8-bit DCS
    ("c1_lone_byte", "a\x84b", "ab"),  # bare C1 with no payload
    # --- Combinations ---
    ("nested_csi_osc", f"{ESC}[1m{ESC}]0;t{BEL}KEEP{ESC}[0m", "KEEP"),
]


@pytest.mark.parametrize(
    ("hostile", "expected"),
    [pytest.param(h, e, id=i) for (i, h, e) in HOSTILE_CORPUS],
)
def test_hostile_input_renders_inert(hostile: str, expected: str) -> None:
    out = render_safe(hostile)
    # No control byte of any class survives.
    assert _CONTROL_BYTE.search(out) is None, f"control byte survived: {out!r}"
    # The legitimate printable text is preserved exactly.
    assert out == expected


BENIGN_CORPUS = [
    ("plain_ascii", "artifact-cmh-alpha/proj-1/oscal", "artifact-cmh-alpha/proj-1/oscal"),
    ("unicode_letters", "café-Über-naïve", "café-Über-naïve"),
    ("emoji", "build ✅ shipped 🚀", "build ✅ shipped 🚀"),
    ("digest", "sha256:abc123def456", "sha256:abc123def456"),
    (
        "arn",
        "arn:aws:signer:us-east-2:1:/signing-profiles/p",
        "arn:aws:signer:us-east-2:1:/signing-profiles/p",
    ),
    ("empty", "", ""),
    ("printable_brackets", "value[42] = {ok}", "value[42] = {ok}"),
]


@pytest.mark.parametrize(
    ("benign", "expected"),
    [pytest.param(b, e, id=i) for (i, b, e) in BENIGN_CORPUS],
)
def test_benign_input_passes_through_unchanged(benign: str, expected: str) -> None:
    assert render_safe(benign) == expected


def test_none_maps_to_empty_string() -> None:
    assert render_safe(None) == ""


def test_non_str_is_coerced() -> None:
    assert render_safe(42) == "42"
    assert render_safe(["a", "b"]) == "['a', 'b']"


def test_csi_payload_bytes_do_not_leak_as_text() -> None:
    # The SGR parameters/intermediate bytes (e.g. "31") must be consumed as
    # part of the sequence, not left behind as visible digits.
    assert render_safe(f"{ESC}[38;5;201mX") == "X"


def test_sanitizer_is_idempotent() -> None:
    # Output is already inert, so a second pass must be a no-op. Defends the
    # invariant that re-rendering a stored value can never resurrect control
    # bytes (no byte in the output can introduce a new escape).
    dirty = f"{ESC}[31m{ESC}]0;t{BEL}val\x9bXmore{ST}\x00"
    once = render_safe(dirty)
    assert render_safe(once) == once


def test_unterminated_osc_cannot_emit_control_bytes() -> None:
    # An OSC introducer with no BEL/ST terminator: the sequence regex won't
    # match, but the bare introducer and any control bytes still get swept,
    # so no active control byte can reach the terminal. Payload text may show
    # as inert characters, which is the safe outcome.
    out = render_safe(f"{ESC}]0;unterminated-evil-payload")
    assert _CONTROL_BYTE.search(out) is None


# --------------------------------------------------------------------------
# Raw-manifest drill-down whole-blob neutralization.
# The ENTIRE pretty-printed manifest blob — not selected fields — passes through
# the single _safe choke point, so escape/control bytes embedded in manifest
# KEYS, VALUES, and STRUCTURE alike render inert. Covers BOTH the bundle and the
# focused-referrer raw-manifest render paths (both call render_raw_manifest_lines).
# --------------------------------------------------------------------------

# A control-byte corpus mixing 7-bit OSC/CSI/DCS, 8-bit C1, and raw C0/DEL bytes
# — the same shapes the app's hostile tests use, embedded in JSON positions.
_HOSTILE_BYTES = (
    "\x1b]0;PWNED\x07"  # OSC window title
    "\x1b[31mRED\x1b[0m"  # CSI SGR
    "\x1bPq\x1b\\"  # DCS
    "\x9b1m"  # 8-bit C1 CSI
    "\x00\x07\x1f\x7f\x80\x9f"  # raw C0 / DEL / C1
)


def _hostile_manifest() -> dict:
    """A manifest with hostile bytes in KEYS, VALUES, and nested STRUCTURE."""
    return {
        f"schema{_HOSTILE_BYTES}Version": 2,  # hostile KEY
        "mediaType": f"application/vnd.oci{_HOSTILE_BYTES}",  # hostile VALUE
        "config": {  # hostile bytes deep in nested STRUCTURE
            f"media{_HOSTILE_BYTES}Type": f"app{_HOSTILE_BYTES}",
            "nested": [f"layer{_HOSTILE_BYTES}", {f"k{_HOSTILE_BYTES}": f"v{_HOSTILE_BYTES}"}],
        },
        "annotations": {f"org.cairn{_HOSTILE_BYTES}": f"note{_HOSTILE_BYTES}"},
    }


def _app_with_target():
    from cairn_cli._aws import DeploymentTarget
    from cairn_cli.tui.app import ArtifactStoreApp

    return ArtifactStoreApp(
        object(),  # type: ignore[arg-type]  # sources unused by the pure render method
        deployment_target=DeploymentTarget(
            stage="alpha", region_code="cmh", app_name="cairnproofs4af"
        ),
    )


def test_raw_manifest_blob_renders_inert_with_hostile_keys_values_structure() -> None:
    # The pretty-printed blob is neutralized line-by-line: no control byte of any
    # class survives in any line, regardless of which JSON position it was in.
    app = _app_with_target()
    for line in app.render_raw_manifest_lines(_hostile_manifest()):
        assert _CONTROL_BYTE.search(line) is None, f"control byte leaked: {line!r}"
    # The blob is still the manifest (pretty-printed): a fully-benign key (no
    # embedded hostile bytes) survives verbatim. Note json.dumps's default
    # ensure_ascii=True ALREADY escapes a hostile byte into printable "\\uXXXX"
    # text — so a key that itself carried hostile bytes is split by those escapes
    # and is NOT expected to survive as a contiguous substring; that is the safe
    # outcome (defense in depth ahead of _safe's sweep).
    blob = "\n".join(app.render_raw_manifest_lines(_hostile_manifest()))
    assert "Raw OCI manifest" in blob
    assert "mediaType" in blob  # a benign key survives the pretty-print verbatim


def test_raw_manifest_is_pretty_printed_multiline() -> None:
    # Regression: render_safe strips C0 control bytes incl. newline (0x0a), so a
    # single-pass self._safe(blob) would COLLAPSE the json.dumps(indent=2) output
    # to one line. The per-line sanitize must preserve the structural newlines so
    # the operator sees an actually pretty-printed (indented, multi-line) manifest.
    app = _app_with_target()
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.empty.v1+json"},
        "layers": [{"digest": "sha256:" + "a" * 64}],
    }
    rendered = "\n".join(app.render_raw_manifest_lines(manifest))
    # Multi-line structure survives (not collapsed): several JSON lines present.
    assert rendered.count("\n") >= 5, rendered
    # Indentation survives (json.dumps indent=2 leaves leading spaces, which are
    # printable and not stripped by render_safe).
    assert any(line.startswith("  ") for line in rendered.split("\n")), rendered
    # Each structural field appears on its own line, not run together.
    assert '"schemaVersion": 2' in rendered
    assert any(line.strip() == '"config": {' for line in rendered.split("\n")), rendered


def test_raw_manifest_focused_referrer_path_renders_inert() -> None:
    # The focused-referrer raw manifest rides the SAME render_raw_manifest_lines
    # choke point, so a hostile referrer manifest is equally inert (covers the
    # focused-referrer path called out in task 3.3).
    app = _app_with_target()
    referrer_manifest = {
        f"artifact{_HOSTILE_BYTES}Type": f"application/vnd{_HOSTILE_BYTES}",
        "subject": {"digest": f"sha256:{_HOSTILE_BYTES}" + "a" * 30},
    }
    for line in app.render_raw_manifest_lines(referrer_manifest):
        assert _CONTROL_BYTE.search(line) is None, f"control byte leaked: {line!r}"


def test_raw_manifest_unavailable_message_is_inert() -> None:
    # The None/unavailable degradation message is a fixed render-safe string.
    app = _app_with_target()
    for line in app.render_raw_manifest_lines(None):
        assert _CONTROL_BYTE.search(line) is None
    assert any("unavailable" in line.lower() for line in app.render_raw_manifest_lines(None))

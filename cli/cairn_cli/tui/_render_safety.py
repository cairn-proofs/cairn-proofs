"""Single shared terminal-render-safety sanitizer for the Artifact_TUI.

WHY: the TUI is the first component to render stored,
producer-controlled values — ECR repo names, OSCAL annotations/identifiers,
Metadata_Index free-text — into an interactive TTY. Producers are UNTRUSTED
for content. A crafted value can carry ANSI/OSC/DCS escape sequences that
forge the session, rewrite the window title, write the clipboard (OSC 52),
move the cursor, repaint colors, or (in vulnerable emulators) inject
commands. Every producer-influenced string in every view (list, detail,
control, the surfaced ``cairn verify`` command) MUST be routed through
``render_safe`` before any terminal write so it renders as inert, visible
text. Favor a conservative "strip anything that could move the terminal out
of plain-text mode" stance over cleverness — this is security-critical.
"""

import re
from typing import Final

# Escape *sequences* — introducer + payload + terminator — matched as whole
# units so the payload bytes (which may themselves look like control bytes)
# are removed along with the introducer. Ordered most-specific first.
#
# Both 7-bit (ESC-prefixed) and 8-bit (C1 single-byte introducer) forms are
# covered. C1 introducers: CSI=0x9b, OSC=0x9d, DCS=0x90, APC=0x9f, PM=0x9e,
# SOS=0x98. The string terminator (ST) is ESC-backslash (0x1b 0x5c) or the
# 8-bit C1 ST (0x9c); OSC also accepts BEL (0x07) as a terminator.
_STRING_TERMINATOR: Final = r"(?:\x1b\\|\x9c)"

_ESCAPE_SEQUENCES: Final = re.compile(
    "|".join(
        (
            # OSC: window title (ESC]0;), clipboard (ESC]52;), etc. Payload is
            # any non-terminator byte; ends at BEL, 7-bit ST, or 8-bit ST.
            rf"(?:\x1b\]|\x9d).*?(?:\x07|{_STRING_TERMINATOR})",
            # DCS / APC / PM / SOS string sequences — payload ends at ST.
            rf"(?:\x1b[PX^_]|[\x90\x98\x9e\x9f]).*?{_STRING_TERMINATOR}",
            # CSI: ESC[ ... <final byte 0x40-0x7e> (SGR color, cursor moves,
            # scroll regions, etc.). Parameter/intermediate bytes are
            # 0x20-0x3f; final byte is 0x40-0x7e.
            r"(?:\x1b\[|\x9b)[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]",
        ),
    ),
    re.DOTALL,
)

# Any remaining stray control bytes after sequences are removed: C0
# (0x00-0x1f) incl. a lone ESC, DEL (0x7f), and C1 (0x80-0x9f) incl. any
# unmatched single-byte C1 introducer. These are stripped wholesale.
#
# Deliberately we do NOT greedily eat the byte following a lone ESC (e.g.
# the "c" of a two-byte ESC c reset). Stripping the ESC introducer alone
# already neutralizes the escape — the terminal needs the ESC byte to enter
# escape mode — so the trailing byte is left as inert printable text. This
# preserves visible content (a stray ESC inside a digest must not swallow a
# real character) while still removing all terminal-control capability.
_STRAY_CONTROL_BYTES: Final = re.compile(r"[\x00-\x1f\x7f\x80-\x9f]")


def render_safe(value: object) -> str:
    """Return an inert, visible-text version of ``value`` safe for a TTY.

    Coerces non-``str`` to ``str`` (``None`` maps to ``""``), strips ANSI/
    CSI/OSC/DCS/APC/PM/SOS escape sequences (7-bit and 8-bit C1 forms), then
    strips any remaining C0/C1 control bytes and DEL. Printable characters
    of the input survive unchanged.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    # Strip whole escape sequences first so their payloads go with them,
    # then sweep up any orphaned control bytes (lone ESC, BEL, NUL, C1, ...).
    text = _ESCAPE_SEQUENCES.sub("", text)
    return _STRAY_CONTROL_BYTES.sub("", text)

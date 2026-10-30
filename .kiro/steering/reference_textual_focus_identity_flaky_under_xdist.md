---
inclusion: fileMatch
fileMatchPattern: ["cli/cairn_cli/tui/**", "tests/unit/cairn_cli/tui/**"]
name: reference-textual-focus-identity-flaky-under-xdist
description: Textual TUI tests must not assert app.focused identity — flaky under pytest-xdist; assert the active tab / visible state instead.
---

In `cli/cairn_cli/tui/` tests (Textual `App.run_test()` pilot), asserting keyboard-**focus identity** — e.g. `app.focused is app.query_one("#detail-pane", VerticalScroll)` — passes under `pytest -n 0` but fails **intermittently under `mise run check`'s `-n auto` (xdist)**. Focus settles asynchronously (often via `call_after_refresh`) and is environment/timing-sensitive in headless Textual, so a focus-identity assertion is the "passes serially, fails in parallel" test bug the toolchain spec forbids.

**How to apply:** assert the DETERMINISTIC, user-visible outcome instead — the active tab (`query_one(TabbedContent).active`, set synchronously), row counts, or rendered cell text. When you add or change a TUI test, run it under `-n auto` (not just `-n 0`) before considering it done — the per-step `-n 0` runs I used all session hid an `app.focused` flake that only the final xdist gate caught. The `_safe`/render-safety and grouping outcomes are all assertable via cell text + active-tab, which are stable. See [[feedback_capture_full_output_to_file]] for the gate-run habit.

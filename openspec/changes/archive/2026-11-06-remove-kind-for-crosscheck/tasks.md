## 1. Remove `kind_for_crosscheck`

- [x] 1.1 Remove the `ArtifactLocator.kind_for_crosscheck` method from `libs/oci/cairn_oci/categories.py`. In the same file, remove `kind_for_crosscheck` from the list of read sites in the `normalize_artifact_kind` docstring. Keep `parse_tag` and its helpers unchanged ([design Decision 1](design.md#decisions)).
- [x] 1.2 Remove the `kind_for_crosscheck` tests ([design Decision 3](design.md#decisions)):
  1. In `tests/unit/cairn_oci/test_categories.py`, remove `test_kind_for_crosscheck_reads_canonical_form` and its section comment.
  2. In `tests/unit/cairn_oci/test_locator.py`, remove `test_kind_for_crosscheck_reads_tag`, `test_kind_for_crosscheck_fail_closed_on_bad_tag`, `test_kind_for_crosscheck_takes_no_repo_name_argument`, and their section comment. Remove any import that becomes unused.
- [x] 1.3 Run `uv run ruff check --fix` and `uv run ruff format` on the three edited files. Run `uv run pytest tests/unit/cairn_oci/` and make sure it passes.
- [x] 1.4 Search the repository for `kind_for_crosscheck`. Make sure that the only matches are under `openspec/changes/` (this change and the archive).

"""Shared OSCAL fixtures for cairn_oscal tests.

Inline JSON-string fixtures (mirroring the upstream `oscal-bindings`
test_document.py pattern) for the document types that can be expressed
without a deeply nested required graph (Catalog, AP, AR).

For SystemSecurityPlan (which has multiple deeply nested required
fields), tests construct via `model_construct()` and wrap with
`OscalDoc` directly — see `_build_ssp_doc()` in `test_oscal_extraction.py`.
"""

from __future__ import annotations

import json

OSCAL_VERSION = "1.2.1"
TEST_UUID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"


DOC_VERSION = "1.0"


LAST_MODIFIED = "2026-01-01T00:00:00Z"
PUBLISHED = "2025-12-15T00:00:00Z"


def metadata_block(
    title: str,
    oscal_version: str = OSCAL_VERSION,
    doc_version: str = DOC_VERSION,
    published: str | None = None,
) -> dict:
    block = {
        "title": title,
        "last-modified": LAST_MODIFIED,
        "version": doc_version,
        "oscal-version": oscal_version,
    }
    if published is not None:
        block["published"] = published
    return block


def minimal_catalog_json(
    oscal_version: str = OSCAL_VERSION,
    doc_version: str = DOC_VERSION,
    published: str | None = None,
) -> str:
    return json.dumps(
        {
            "catalog": {
                "uuid": TEST_UUID,
                "metadata": metadata_block("Test Catalog", oscal_version, doc_version, published),
            }
        }
    )


def minimal_assessment_plan_json() -> str:
    return json.dumps(
        {
            "assessment-plan": {
                "uuid": TEST_UUID,
                "metadata": metadata_block("Test AP"),
                "import-ssp": {"href": "https://example.com/ssp.json"},
                "reviewed-controls": {"control-selections": [{"include-all": {}}]},
                "tasks": [
                    {
                        "uuid": TEST_UUID,
                        "type": "action",
                        "title": "Window",
                        "timing": {
                            "within-date-range": {
                                "start": "2025-02-01T00:00:00Z",
                                "end": "2025-02-15T00:00:00Z",
                            }
                        },
                    }
                ],
            }
        }
    )


def ar_with_include_all_only_json() -> str:
    """AR with a single result whose control-selections is only `include-all`.

    Exercises the `ControlSelections` branch (variant without
    `include-controls`) — `extract_oscal_metadata` should yield an empty
    `control_references` list because the dispatcher cannot expand
    `include-all` without resolving the imported SSP/Profile.
    """
    return json.dumps(
        {
            "assessment-results": {
                "uuid": TEST_UUID,
                "metadata": metadata_block("Test AR"),
                "import-ap": {"href": "https://example.com/ap.json"},
                "results": [
                    {
                        "uuid": TEST_UUID,
                        "title": "R1",
                        "description": "first",
                        "start": "2025-02-01T00:00:00Z",
                        "end": "2025-02-15T00:00:00Z",
                        "reviewed-controls": {"control-selections": [{"include-all": {}}]},
                    }
                ],
            }
        }
    )


def ar_with_mixed_selections_json() -> str:
    """AR with one result that mixes `include-controls` AND `include-all`.

    The two selections coexist on the same result's `control-selections[]`,
    so the extractor must (a) populate `control_references` from the
    explicit selection AND (b) set `control_references_include_all=True`
    because at least one selection used the `include-all` variant. The
    sticky-bool semantics matter for the metadata writer — see [OSCAL-003.01]
    rationale in `extraction.py`."""
    return json.dumps(
        {
            "assessment-results": {
                "uuid": TEST_UUID,
                "metadata": metadata_block("Test AR"),
                "import-ap": {"href": "https://example.com/ap.json"},
                "results": [
                    {
                        "uuid": TEST_UUID,
                        "title": "R1",
                        "description": "mixed",
                        "start": "2025-02-01T00:00:00Z",
                        "end": "2025-02-15T00:00:00Z",
                        "reviewed-controls": {
                            "control-selections": [
                                {
                                    "include-controls": [
                                        {"control-id": "ac-2"},
                                        {"control-id": "ac-1"},
                                    ]
                                },
                                {"include-all": {}},
                            ]
                        },
                    }
                ],
            }
        }
    )


def ar_with_include_controls_json() -> str:
    """AR with results having explicit `include-controls` plus a duplicate.

    Across the two results, control IDs `["ac-1", "ac-2", "ac-1", "ac-3"]`
    appear; expected deduped + sorted output is `["ac-1", "ac-2", "ac-3"]`.
    """
    u2 = "00000000-0000-4000-8000-000000000002"
    return json.dumps(
        {
            "assessment-results": {
                "uuid": TEST_UUID,
                "metadata": metadata_block("Test AR"),
                "import-ap": {"href": "https://example.com/ap.json"},
                "results": [
                    {
                        "uuid": TEST_UUID,
                        "title": "R1",
                        "description": "first",
                        "start": "2025-02-01T00:00:00Z",
                        "end": "2025-02-15T00:00:00Z",
                        "reviewed-controls": {
                            "control-selections": [
                                {
                                    "include-controls": [
                                        {"control-id": "ac-2"},
                                        {"control-id": "ac-1"},
                                    ]
                                }
                            ]
                        },
                    },
                    {
                        "uuid": u2,
                        "title": "R2",
                        "description": "second",
                        "start": "2025-01-20T00:00:00Z",
                        "end": "2025-03-01T00:00:00Z",
                        "reviewed-controls": {
                            "control-selections": [
                                {
                                    "include-controls": [
                                        {"control-id": "ac-1"},
                                        {"control-id": "ac-3"},
                                    ]
                                }
                            ]
                        },
                    },
                ],
            }
        }
    )

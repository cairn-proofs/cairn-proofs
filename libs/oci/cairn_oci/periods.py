"""Assessment-period resolver (minting-time, evidence-only).

``assessment-period`` is optional metadata at the validation gate, but it remains
one of the coordinates that *address* an evidence bundle: the ECR tag grammar
``{evidence-category}--{assessment-period}--{control-scope}--{sequence}`` ([INGEST-021])
requires it, the per-``(repo, category, period, scope)`` sequence counter is scoped
by it, and the S3 WORM key prefix mirrors it. With the Config_Blob field now
omittable, the period-consuming mint path would write a literal ``None`` segment
unless it resolves the period through one shared primitive. This module is that
primitive.

The period is resolved **at minting time**, as a pure function of the durable
evidence record, in precedence order (highest first):

1. ``config_period`` — a present, non-empty Config_Blob ``assessmentPeriod``.
2. ``date(created)`` — the date portion (``YYYY-MM-DD``) of the always-required
   RFC 3339 creation timestamp, the last-resort fallback.

The tag is **not** a source. The tag is the *output* of
minting, not an input — reading a period back from it would be circular, so the
tag is not consulted as a period source.

Only the **evidence** form carries a period coordinate. The **reference** form
has none, so callers signal ``is_reference=True`` to bypass resolution entirely;
the resolver short-circuits to ``None`` before any source is
consulted.

The resolver is pure (no AWS, no I/O) and total/defensive: a ``None`` or empty
individual input never raises — it simply falls through to the next source. A
``ValueError`` is raised ONLY when no source is usable on the evidence path,
which signals a bug upstream of the always-required creation timestamp.
"""

from __future__ import annotations

__all__ = ["resolve_assessment_period"]


def _date_of(created: str | None) -> str | None:
    """Return the ``YYYY-MM-DD`` date portion of an RFC 3339 timestamp.

    Splits on ``"T"`` and takes the first segment, so both a full timestamp
    (``"2026-05-30T14:22:00Z"`` -> ``"2026-05-30"``) and a bare date
    (``"2026-05-30"`` -> ``"2026-05-30"``) resolve. Returns ``None`` for a
    ``None``/empty input or when the date portion is empty (e.g. a leading
    ``"T"``), so the resolver can distinguish "unusable" from a real date.
    """
    if not created:
        return None
    date_part = created.split("T", 1)[0]
    return date_part or None


def resolve_assessment_period(
    config_period: str | None,
    created: str | None,
    *,
    is_reference: bool = False,
) -> str | None:
    """Resolve an evidence bundle's assessment period at minting time.

    This is a pure function of the durable evidence record — the Config_Blob's
    ``assessmentPeriod`` plus the ``created`` (``createdAt``) timestamp — so its
    output can be computed once and recorded for downstream consumers, never
    re-derived.

    Sources are consulted in precedence order: ``config_period`` when it is a
    non-empty string, else the date portion (``YYYY-MM-DD``) of the RFC 3339
    ``created`` timestamp (split on ``"T"``, first segment). The tag is
    deliberately NOT a source: the tag is an output of minting, so reading a
    period back from it would be circular.

    Reference-form artifacts have no period coordinate, so ``is_reference=True``
    short-circuits to ``None`` before any source is consulted.

    Total/defensive on the evidence path: a ``None``/empty individual input never
    raises; it falls through to the next source. Raises :class:`ValueError` ONLY
    when ``is_reference`` is false and neither a usable ``config_period`` nor a
    parseable ``created`` is available — a state that indicates a bug upstream of
    the always-required creation timestamp.
    """
    # Reference form has no period coordinate — bypass resolution entirely.
    if is_reference:
        return None
    if config_period:
        return config_period
    created_date = _date_of(created)
    if created_date:
        return created_date
    raise ValueError(
        "cannot resolve assessment period: config_period is empty and "
        f"created={created!r} has no parseable date portion"
    )

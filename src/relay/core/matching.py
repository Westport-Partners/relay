"""Alarm-name prefix suggestion — pure domain (no AWS, no I/O).

When an operator ignores an alarm, they almost never mean "just this one".
CloudWatch alarm names are generated from a stable prefix plus a per-resource
suffix, so one noisy alarm usually arrives with a crowd of near-identical
siblings::

    ABCDE-ABCDE-123131231
    ABCDE-ABCDE-982340234e
    TargetTracking-table/relay-prod-AlarmHigh-abc123
    TargetTracking-table/relay-prod-AlarmHigh-9z8y

Given the alarm in front of the operator and the alarm names Relay has already
seen, this module derives the *longest separator-aligned prefix* that still
catches at least one sibling — the tightest ignore rule that is still broad
enough to be worth writing. Alignment to a separator matters: a mid-token cut
like ``ABCDE-ABCDE-9`` would silently match on a digit of the resource id.

Used by the Hub's ignore-rule forms to pre-fill the prefix field, and kept pure
so the suggestion is testable without a store, a request, or an AWS call.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

# Characters that mark token boundaries in alarm names.  A valid group prefix
# must end at one of these characters so we never cut mid-token.
SEPARATORS: str = "-_/.:"


# ---------------------------------------------------------------------------
# Primitive helpers
# ---------------------------------------------------------------------------


def longest_common_prefix(values: Sequence[str]) -> str:
    """Return the longest string that is a prefix of every value in *values*.

    Args:
        values: A sequence of strings to examine.

    Returns:
        The longest common prefix, or ``""`` when *values* is empty or the
        strings share no common leading character.
    """
    if not values:
        return ""
    # Use the shortest value as the upper bound for the scan.
    first = values[0]
    limit = min(len(v) for v in values)
    idx = 0
    while idx < limit and all(v[idx] == first[idx] for v in values):
        idx += 1
    return first[:idx]


def trim_to_separator(prefix: str) -> str:
    """Truncate *prefix* to and including its last separator character.

    A "group prefix" must end at a token boundary so callers can use it safely
    as a ``startswith`` key without accidentally matching unrelated names.

    Args:
        prefix: An arbitrary string, typically the raw output of
            :func:`longest_common_prefix`.

    Returns:
        *prefix* up to and including the last separator, or ``""`` if
        *prefix* contains no separator at all.  When *prefix* already ends
        with a separator it is returned unchanged.

    Examples::

        trim_to_separator("ABCDE-ABCDE-98")   # → "ABCDE-ABCDE-"
        trim_to_separator("ABCDE-ABCDE-")     # → "ABCDE-ABCDE-"
        trim_to_separator("NoSeparatorHere")   # → ""
    """
    # Walk backwards to find the last separator.
    for i in range(len(prefix) - 1, -1, -1):
        if prefix[i] in SEPARATORS:
            return prefix[: i + 1]
    return ""


def separator_aligned_prefixes(name: str) -> list[str]:
    """Return every prefix of *name* that ends at a separator boundary.

    Each entry corresponds to one position ``i`` where ``name[i]`` is a
    separator, producing the slice ``name[: i + 1]``.  Results are ordered
    **longest first** so the caller can iterate and stop at the first
    qualifying prefix (maximally specific).

    Args:
        name: An alarm name or similar token-separated string.

    Returns:
        A list of separator-terminated prefixes, longest first.  The full
        *name* is included only if it itself ends with a separator.  Returns
        an empty list when *name* contains no separators.

    Examples::

        separator_aligned_prefixes("A-B-C")   # → ["A-B-", "A-"]
        separator_aligned_prefixes("A-B-C-")  # → ["A-B-C-", "A-B-", "A-"]
    """
    positions = [i for i, ch in enumerate(name) if ch in SEPARATORS]
    # longest first: reverse the list of boundary positions
    return [name[: pos + 1] for pos in reversed(positions)]


# ---------------------------------------------------------------------------
# Top-level suggestion
# ---------------------------------------------------------------------------


def suggest_alarm_prefix(
    target: str,
    candidates: Iterable[str],
    *,
    min_length: int = 3,
    min_group_size: int = 2,
) -> str | None:
    """Return the best separator-aligned prefix for grouping *target* with siblings.

    "Best" means the *longest* prefix of *target* that:

    * has ``len(prefix) >= min_length``, AND
    * is a prefix of at least ``min_group_size - 1`` *distinct* names drawn
      from *candidates*, excluding any name exactly equal to *target*.

    Iterates longest-first so the first qualifying prefix is maximally
    specific — the tightest grouping that still captures the required number
    of siblings.

    Args:
        target: The alarm name whose group prefix is being determined.
        candidates: All known alarm names in the same scope (may include
            *target* itself; exact duplicates are de-duplicated away).
        min_length: Minimum acceptable prefix length (default 3).  Shorter
            prefixes are too generic to be useful.
        min_group_size: The minimum total group size including *target*
            (default 2).  A value of 2 means at least one sibling is
            required; 3 means at least two.

    Returns:
        The longest qualifying prefix, or ``None`` when no prefix meets the
        criteria.

    Examples::

        suggest_alarm_prefix(
            "ABCDE-ABCDE-123131231",
            ["ABCDE-ABCDE-982340234e"],
        )
        # → "ABCDE-ABCDE-"
    """
    # De-duplicate candidates and exclude names identical to target.
    others = {c for c in candidates if c != target}

    required_siblings = min_group_size - 1

    for prefix in separator_aligned_prefixes(target):
        if len(prefix) < min_length:
            continue
        sibling_count = sum(1 for o in others if o.startswith(prefix))
        if sibling_count >= required_siblings:
            return prefix

    return None


__all__ = [
    "SEPARATORS",
    "longest_common_prefix",
    "trim_to_separator",
    "separator_aligned_prefixes",
    "suggest_alarm_prefix",
]

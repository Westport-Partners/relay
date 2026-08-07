"""Tests for the alarm-name prefix suggestion helpers (relay.core.matching)."""

from __future__ import annotations

from relay.core.matching import (
    SEPARATORS,
    longest_common_prefix,
    separator_aligned_prefixes,
    suggest_alarm_prefix,
    trim_to_separator,
)

# ---------------------------------------------------------------------------
# longest_common_prefix
# ---------------------------------------------------------------------------


def test_lcp_empty_sequence_returns_empty_string():
    assert longest_common_prefix([]) == ""


def test_lcp_single_value_returns_that_value():
    assert longest_common_prefix(["hello"]) == "hello"


def test_lcp_identical_values():
    assert longest_common_prefix(["foo", "foo", "foo"]) == "foo"


def test_lcp_no_common_prefix():
    assert longest_common_prefix(["abc", "xyz"]) == ""


def test_lcp_partial_common_prefix():
    assert longest_common_prefix(["ABCDE-ABCDE-123", "ABCDE-ABCDE-456"]) == "ABCDE-ABCDE-"


def test_lcp_different_lengths_stops_at_shortest():
    assert longest_common_prefix(["abcdef", "abc"]) == "abc"


def test_lcp_single_char_common():
    assert longest_common_prefix(["apple", "ant"]) == "a"


# ---------------------------------------------------------------------------
# trim_to_separator
# ---------------------------------------------------------------------------


def test_trim_no_separator_returns_empty():
    assert trim_to_separator("NoSeparatorHere") == ""


def test_trim_already_ends_with_separator_unchanged():
    assert trim_to_separator("ABCDE-ABCDE-") == "ABCDE-ABCDE-"


def test_trim_mid_token_cut():
    assert trim_to_separator("ABCDE-ABCDE-98") == "ABCDE-ABCDE-"


def test_trim_empty_string_returns_empty():
    assert trim_to_separator("") == ""


def test_trim_single_separator_at_start():
    # The only separator is position 0 — slice includes it.
    assert trim_to_separator("-abc") == "-"


def test_trim_all_separator_chars_recognised():
    """Each character in SEPARATORS must work as a recognised boundary."""
    for sep in SEPARATORS:
        result = trim_to_separator(f"prefix{sep}suffix")
        assert result == f"prefix{sep}", f"separator {sep!r} not trimmed correctly"


def test_trim_forward_slash_separator():
    assert trim_to_separator("TargetTracking-table/relay-prod-AlarmHigh-9z8y") == (
        "TargetTracking-table/relay-prod-AlarmHigh-"
    )


# ---------------------------------------------------------------------------
# separator_aligned_prefixes
# ---------------------------------------------------------------------------


def test_sep_aligned_no_separators_returns_empty_list():
    assert separator_aligned_prefixes("CpuHighNoSep") == []


def test_sep_aligned_two_separators_longest_first():
    result = separator_aligned_prefixes("A-B-C")
    assert result == ["A-B-", "A-"]


def test_sep_aligned_name_ending_in_separator_included_as_full_name():
    result = separator_aligned_prefixes("A-B-C-")
    assert result == ["A-B-C-", "A-B-", "A-"]


def test_sep_aligned_explicit_longest_first_ordering():
    """Lengths must be strictly decreasing (longest first guarantee)."""
    result = separator_aligned_prefixes("a-b/c_d:e.f")
    for i in range(len(result) - 1):
        assert len(result[i]) > len(result[i + 1]), (
            f"Not longest-first at index {i}: {result[i]!r} vs {result[i + 1]!r}"
        )


def test_sep_aligned_mixed_separators():
    result = separator_aligned_prefixes("a/b_c-d")
    # Positions: '/' at 1, '_' at 3, '-' at 5
    assert "a/b_c-" in result
    assert "a/b_" in result
    assert "a/" in result
    # Verify longest first
    assert result[0] == "a/b_c-"
    assert result[-1] == "a/"


def test_sep_aligned_single_separator_one_entry():
    assert separator_aligned_prefixes("foo-bar") == ["foo-"]


# ---------------------------------------------------------------------------
# suggest_alarm_prefix — headline scenario
# ---------------------------------------------------------------------------


def test_headline_scenario_two_token_prefix():
    """Core requirement: ABCDE-ABCDE-* matches on the second-level prefix."""
    result = suggest_alarm_prefix(
        "ABCDE-ABCDE-123131231",
        ["ABCDE-ABCDE-982340234e"],
    )
    assert result == "ABCDE-ABCDE-"


def test_headline_not_short_prefix():
    """Must NOT return the shorter first-level prefix when longer qualifies."""
    result = suggest_alarm_prefix(
        "ABCDE-ABCDE-123131231",
        ["ABCDE-ABCDE-982340234e"],
    )
    assert result != "ABCDE-"


def test_headline_not_mid_token_cut():
    """Result must end with a separator — never a mid-token substring."""
    result = suggest_alarm_prefix(
        "ABCDE-ABCDE-123131231",
        ["ABCDE-ABCDE-982340234e"],
    )
    assert result is not None
    assert result[-1] in SEPARATORS


# ---------------------------------------------------------------------------
# suggest_alarm_prefix — CloudWatch realistic shapes
# ---------------------------------------------------------------------------


def test_target_tracking_alarm_with_slash():
    """TargetTracking names use '/' as well as '-'; prefix must align to a separator."""
    target = "TargetTracking-table/relay-prod-AlarmHigh-abc123"
    sibling = "TargetTracking-table/relay-prod-AlarmHigh-9z8y"
    result = suggest_alarm_prefix(target, [sibling])
    assert result == "TargetTracking-table/relay-prod-AlarmHigh-"


def test_myapp_cpu_high_prefix():
    result = suggest_alarm_prefix(
        "MyApp-prod-Cpu-High-1a2b",
        ["MyApp-prod-Cpu-High-9z8y"],
    )
    assert result == "MyApp-prod-Cpu-High-"


# ---------------------------------------------------------------------------
# suggest_alarm_prefix — None cases
# ---------------------------------------------------------------------------


def test_no_candidates_returns_none():
    assert suggest_alarm_prefix("ABCDE-ABCDE-123", []) is None


def test_only_exact_copies_of_target_returns_none():
    """Exact duplicates of target are filtered out; no sibling → None."""
    assert suggest_alarm_prefix("ABCDE-ABCDE-123", ["ABCDE-ABCDE-123", "ABCDE-ABCDE-123"]) is None


def test_target_with_no_separator_returns_none():
    assert suggest_alarm_prefix("CpuHigh", ["CpuLow"]) is None


def test_unrelated_candidates_returns_none():
    """Candidates that share no prefix with target yield None."""
    assert suggest_alarm_prefix("MyApp-prod-1", ["Unrelated-prod-1"]) is None


# ---------------------------------------------------------------------------
# suggest_alarm_prefix — min_length rejection
# ---------------------------------------------------------------------------


def test_min_length_default_rejects_short_prefix():
    """Prefix 'a-' is length 2 < default min_length=3 → None."""
    assert suggest_alarm_prefix("a-bbbb", ["a-cccc"]) is None


def test_min_length_explicit_three_rejects_short_prefix():
    """Explicit min_length=3 still rejects length-2 prefix."""
    assert suggest_alarm_prefix("a-bbbb", ["a-cccc"], min_length=3) is None


def test_min_length_two_accepts_short_prefix():
    """min_length=2 allows the 'a-' prefix (length 2)."""
    result = suggest_alarm_prefix("a-bbbb", ["a-cccc"], min_length=2)
    assert result == "a-"


def test_min_length_exact_boundary():
    """Prefix of exactly min_length characters should be accepted."""
    # 'ab-' is length 3; min_length=3 → should qualify.
    result = suggest_alarm_prefix("ab-xxxx", ["ab-yyyy"], min_length=3)
    assert result == "ab-"


# ---------------------------------------------------------------------------
# suggest_alarm_prefix — min_group_size
# ---------------------------------------------------------------------------


def test_min_group_size_2_one_sibling_qualifies():
    """Default min_group_size=2 requires exactly one sibling."""
    result = suggest_alarm_prefix("Alpha-Beta-1", ["Alpha-Beta-2"])
    assert result == "Alpha-Beta-"


def test_min_group_size_3_two_siblings_qualifies():
    """min_group_size=3 requires two siblings; two present → result."""
    result = suggest_alarm_prefix(
        "Alpha-Beta-1",
        ["Alpha-Beta-2", "Alpha-Beta-3"],
        min_group_size=3,
    )
    assert result == "Alpha-Beta-"


def test_min_group_size_3_one_sibling_returns_none():
    """min_group_size=3 with only one sibling → None."""
    result = suggest_alarm_prefix(
        "Alpha-Beta-1",
        ["Alpha-Beta-2"],
        min_group_size=3,
    )
    assert result is None


def test_min_group_size_2_at_min_group_size_3_returns_none():
    """
    Two-sibling candidate list.  With min_group_size=2 a prefix is found;
    with min_group_size=3 the *same* sibling list must also qualify because
    there are exactly two siblings.
    """
    siblings = ["Alpha-Beta-2", "Alpha-Beta-3"]
    assert suggest_alarm_prefix("Alpha-Beta-1", siblings, min_group_size=2) is not None
    assert suggest_alarm_prefix("Alpha-Beta-1", siblings, min_group_size=3) is not None


def test_min_group_size_4_only_two_siblings_returns_none():
    """min_group_size=4 needs three siblings; only two → None."""
    result = suggest_alarm_prefix(
        "Alpha-Beta-1",
        ["Alpha-Beta-2", "Alpha-Beta-3"],
        min_group_size=4,
    )
    assert result is None


# ---------------------------------------------------------------------------
# suggest_alarm_prefix — longest-first tie-breaking
# ---------------------------------------------------------------------------


def test_longest_prefix_wins_over_shorter():
    """
    Both 'Alpha-Beta-' and 'Alpha-' would match sibling 'Alpha-Beta-2'.
    The function must return the LONGER prefix.
    """
    result = suggest_alarm_prefix("Alpha-Beta-1", ["Alpha-Beta-2"])
    assert result == "Alpha-Beta-"
    assert result != "Alpha-"


def test_shorter_prefix_returned_when_longer_has_insufficient_siblings():
    """
    Sibling only matches the first-level prefix, not the second-level.
    The shorter prefix must be chosen as the fallback.
    """
    # 'Alpha-Gamma-2' starts with 'Alpha-' but NOT 'Alpha-Beta-'
    result = suggest_alarm_prefix("Alpha-Beta-1", ["Alpha-Gamma-2"], min_length=2)
    assert result == "Alpha-"


# ---------------------------------------------------------------------------
# suggest_alarm_prefix — generator / iterator input
# ---------------------------------------------------------------------------


def test_accepts_generator_for_candidates():
    """candidates may be a one-shot generator; must not fail or return None."""
    def _gen():
        yield "ABCDE-ABCDE-982340234e"

    result = suggest_alarm_prefix("ABCDE-ABCDE-123131231", _gen())
    assert result == "ABCDE-ABCDE-"


def test_accepts_iterator_for_candidates():
    """candidates may be any iterator (iter() wrapping a list)."""
    candidates = iter(["MyApp-prod-Cpu-High-9z8y"])
    result = suggest_alarm_prefix("MyApp-prod-Cpu-High-1a2b", candidates)
    assert result == "MyApp-prod-Cpu-High-"


def test_generator_consumed_only_once():
    """
    Verify that even an exhaustible generator works correctly.
    If the module were to iterate candidates twice the second pass would
    yield nothing and the result would be None.
    """
    seen: list[str] = []

    def _tracking_gen():
        for name in ["ABCDE-ABCDE-aaa", "ABCDE-ABCDE-bbb"]:
            seen.append(name)
            yield name

    result = suggest_alarm_prefix("ABCDE-ABCDE-zzz", _tracking_gen())
    # Generator must have been iterated (not skipped entirely)
    assert len(seen) == 2
    assert result == "ABCDE-ABCDE-"


# ---------------------------------------------------------------------------
# Edge / stress cases
# ---------------------------------------------------------------------------


def test_unicode_names_do_not_blow_up():
    target = "Αlpha-Βeta-γamma-1"
    sibling = "Αlpha-Βeta-γamma-2"
    # Should not raise; result may or may not be None depending on separator positions.
    result = suggest_alarm_prefix(target, [sibling])
    # Both names share "Αlpha-Βeta-γamma-" as common prefix ending in '-'
    assert result == "Αlpha-Βeta-γamma-"


def test_very_long_name_does_not_blow_up():
    base = "A-" * 5000  # 10 000 chars, lots of separators
    target = base + "111"
    sibling = base + "222"
    result = suggest_alarm_prefix(target, [sibling])
    assert result is not None
    assert result[-1] in SEPARATORS


def test_lcp_on_very_long_strings():
    long = "x" * 10_000
    assert longest_common_prefix([long, long]) == long


def test_separator_aligned_prefixes_very_long_name():
    name = "a-" * 5000  # many separator positions
    result = separator_aligned_prefixes(name)
    # Must not raise; first entry is longest
    assert len(result) == 5000
    assert len(result[0]) > len(result[1])


def test_trim_to_separator_only_separator_chars():
    # A string consisting entirely of separators — last char is a separator.
    assert trim_to_separator("---") == "---"


def test_suggest_alarm_prefix_target_in_candidates_excluded():
    """Target name appearing in candidates must not count as its own sibling."""
    # Only non-target sibling is the second element.
    result = suggest_alarm_prefix(
        "MyApp-prod-1",
        ["MyApp-prod-1", "MyApp-prod-2"],  # first is a copy of target
    )
    assert result == "MyApp-prod-"

"""Unit tests for text normalization and evidence-span mapping."""

import pytest

from sentineliq.utils import (
    NormalizedText,
    find_evidence_span,
    normalize_for_matching,
    word_count,
)


def test_whitespace_runs_collapse_to_single_space():
    result = normalize_for_matching("data    retention\n\n  policy")
    assert result.text == "data retention policy"


def test_leading_and_trailing_whitespace_removed():
    result = normalize_for_matching("\n\n  Governing Law   \n ")
    assert result.text == "Governing Law"


def test_curly_quotes_and_dashes_unified():
    result = normalize_for_matching("the “Agreement” — party’s term")
    assert result.text == 'the "Agreement" - party\'s term'


def test_ligatures_expand_to_multiple_characters():
    result = normalize_for_matching("oﬃce of the aﬀiliate")
    assert result.text == "office of the affiliate"


def test_zero_width_characters_dropped():
    result = normalize_for_matching("con​fidential")
    assert result.text == "confidential"


def test_hyphenation_across_line_break_rejoined():
    result = normalize_for_matching("data reten-\ntion exceeds policy")
    assert result.text == "data retention exceeds policy"


def test_hyphen_before_capital_is_not_a_line_break_join():
    """A real hyphenated compound must survive, not be silently merged."""
    result = normalize_for_matching("Third-\nParty Beneficiary")
    assert "Third-" in result.text


def test_genuine_hyphen_preserved():
    result = normalize_for_matching("a non-exclusive licence")
    assert result.text == "a non-exclusive licence"


def test_case_is_preserved_by_default():
    result = normalize_for_matching("Governing Law")
    assert result.text == "Governing Law"


def test_fold_case_lowercases():
    result = normalize_for_matching("Governing Law", fold_case=True)
    assert result.text == "governing law"


def test_empty_input_produces_empty_result():
    result = normalize_for_matching("")
    assert result.text == ""
    assert result.source_offsets == ()


def test_whitespace_only_input_produces_empty_result():
    result = normalize_for_matching("   \n\t  ")
    assert result.text == ""


# --- offset map integrity -------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "data    retention\n\npolicy",
        "the “Agreement” — term",
        "data reten-\ntion clause",
        "oﬃce of the aﬀiliate",
        "  4.2 Data Retention.  Vendor shall retain data for 7 years.  ",
    ],
)
def test_offset_map_has_one_entry_per_normalized_character(source):
    result = normalize_for_matching(source)
    assert len(result.source_offsets) == len(result.text)


@pytest.mark.parametrize(
    "source",
    [
        "data    retention\n\npolicy",
        "the “Agreement” — term",
        "  4.2 Data Retention.  Vendor shall retain data for 7 years.  ",
    ],
)
def test_offsets_are_within_source_and_non_decreasing(source):
    result = normalize_for_matching(source)
    assert all(0 <= offset < len(source) for offset in result.source_offsets)
    offsets = result.source_offsets
    assert all(a <= b for a, b in zip(offsets, offsets[1:], strict=False))


def test_source_span_round_trips_to_original_text():
    """The whole point: a normalized match must cite exact source characters."""
    source = "Section 4.2  Data    Retention.  Vendor shall retain records."
    result = normalize_for_matching(source)
    position = result.text.index("Data Retention")
    start, end = result.source_span(position, position + len("Data Retention"))
    assert source[start:end] == "Data    Retention"


def test_source_offset_rejects_out_of_range_index():
    result = normalize_for_matching("abc")
    with pytest.raises(IndexError):
        result.source_offset(99)


def test_source_span_rejects_empty_span():
    result = normalize_for_matching("abc")
    with pytest.raises(ValueError):
        result.source_span(2, 2)


# --- evidence span lookup -------------------------------------------------


def test_find_evidence_span_returns_source_coordinates():
    source = "Clause 1.  The  Vendor shall  indemnify the Company."
    haystack = normalize_for_matching(source)
    span = find_evidence_span(haystack, "Vendor shall indemnify")
    assert span is not None
    start, end = span
    assert source[start:end] == "Vendor shall  indemnify"


def test_find_evidence_span_returns_none_when_absent():
    haystack = normalize_for_matching("nothing relevant here")
    assert find_evidence_span(haystack, "termination for convenience") is None


def test_find_evidence_span_returns_none_for_empty_needle():
    haystack = normalize_for_matching("some text")
    assert find_evidence_span(haystack, "   ") is None


def test_expected_start_disambiguates_repeated_text():
    """18.6% of CUAD answers repeat; first-match would cite the wrong one."""
    source = "Company shall pay. " * 3 + "END"
    haystack = normalize_for_matching(source)
    third_occurrence = source.rindex("Company")

    naive = find_evidence_span(haystack, "Company shall pay")
    guided = find_evidence_span(
        haystack, "Company shall pay", expected_source_start=third_occurrence
    )

    assert naive is not None and guided is not None
    assert naive[0] == source.index("Company")
    assert guided[0] == third_occurrence
    assert naive[0] != guided[0]


def test_unique_match_ignores_expected_start():
    source = "The termination notice period is thirty days."
    haystack = normalize_for_matching(source)
    span = find_evidence_span(haystack, "thirty days", expected_source_start=0)
    assert span is not None
    assert source[span[0] : span[1]] == "thirty days"


def test_normalized_text_length_matches_text():
    result: NormalizedText = normalize_for_matching("a  b")
    assert len(result) == len(result.text) == 3


def test_word_count_counts_word_tokens():
    assert word_count("4.2 Data Retention, per policy.") == 6

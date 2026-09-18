"""Offline tests for the scoring logic. No network, no model."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measure_extraction import classify, member_to_readable, normalize  # noqa: E402

# Real figures from BWXT's FY2025 10-K.
GOV_TOTAL = 2_350_090_000
GOV_US_ONLY = 2_323_608_000          # 1.1% away, a real row
GOV_PRODUCT_LINE = 147_138_000       # CommercialOperationsMember inside Gov
ALTERNATIVES = [
    (GOV_TOTAL, "segment total"),
    (GOV_US_ONLY, "US only"),
    (GOV_PRODUCT_LINE, "product line inside the segment"),
    (853_070_000, "Commercial segment"),
]


def test_exact_match():
    bucket, _, _ = classify(GOV_TOTAL, GOV_TOTAL, ALTERNATIVES)
    assert bucket == "exact"


def test_near_match_within_tolerance():
    bucket, detail, _ = classify(GOV_090 := 2_350_000_000, GOV_TOTAL, ALTERNATIVES)
    assert bucket == "near"
    assert "%" in detail


def test_us_only_figure_is_wrong_level_not_hallucination():
    """2,323,608,000 is real, correctly reported, and 1.1% from the answer.
    Scoring it as a hallucination would hide the interesting failure; scoring
    it as near would hide that the model read the wrong row."""
    bucket, _, matched = classify(GOV_US_ONLY, GOV_TOTAL, ALTERNATIVES)
    assert bucket == "wrong_level"
    assert matched == "US only"


def test_product_line_inside_the_segment_is_wrong_level():
    """BWXT tags a CommercialOperationsMember product line inside Government
    Operations. Name matching returns 147M where the answer is 853M."""
    bucket, _, matched = classify(GOV_PRODUCT_LINE, 853_070_000, ALTERNATIVES)
    assert bucket == "wrong_level"
    assert "product line" in matched


def test_invented_figure_is_hallucinated():
    bucket, detail, matched = classify(1_111_111_111, GOV_TOTAL, ALTERNATIVES)
    assert bucket == "hallucinated"
    assert matched == ""
    assert "matches no figure" in detail


def test_declining_to_answer_is_its_own_bucket():
    """A refusal is not a wrong answer and must not be counted as one."""
    bucket, _, _ = classify(None, GOV_TOTAL, ALTERNATIVES)
    assert bucket == "missing"


def test_no_ground_truth_is_not_scored_as_correct():
    bucket, _, _ = classify(GOV_TOTAL, None, ALTERNATIVES)
    assert bucket == "no_ground_truth"


def test_wrong_level_is_checked_before_near():
    """The US-only figure is 1.1% away, inside the near tolerance. Checking
    near first scored the most interesting failure as a rounding difference."""
    bucket, _, _ = classify(GOV_US_ONLY, GOV_TOTAL, ALTERNATIVES)
    assert bucket == "wrong_level"


def test_rounding_of_the_right_answer_is_near_not_wrong_level():
    """The ground-truth value is excluded from the alternatives, so a slightly
    rounded version of the correct figure does not match 'another row'."""
    bucket, _, _ = classify(GOV_TOTAL + 1000, GOV_TOTAL, ALTERNATIVES)
    assert bucket == "near"


def test_member_to_readable():
    assert member_to_readable("GovernmentOperationsSegmentMember") == "Government Operations"
    assert member_to_readable("McLaneCompanyMember") == "Mc Lane Company"


def test_normalize_matches_across_punctuation_and_casing():
    """Sterling writes 'E-Infrastructure Solutions'; the XBRL member renders as
    'EInfrastructure Solutions'. Exact string matching found neither."""
    assert normalize("E-Infrastructure Solutions") == normalize(
        member_to_readable("EInfrastructureSolutionsSegmentMember")
    )
    assert normalize("Government Operations") == normalize("government  operations")


# -- regressions from the first live run ---------------------------------


def test_truth_is_keyed_by_period_not_by_proximity():
    """The first run picked whichever tagged figure was closest to the model's
    answer. That fits the answer key to the guess: a correct reading of the
    wrong row was scored as a hallucination against a figure that was not the
    quarter's revenue either."""
    from measure_extraction import period_days

    class P:
        def __init__(self, start, end, instant=None):
            self.start, self.end, self.instant = start, end, instant

    assert period_days(P("2026-04-01", "2026-06-30")) == 90
    assert period_days(P("2025-01-01", "2025-12-31")) == 364
    assert period_days(P(None, None, "2026-06-30")) is None


def test_operating_income_is_wrong_level_against_revenue():
    """The model returned 105,700,000 for Government Operations: real, correct
    arithmetic, and operating income rather than revenue. The prompt never
    said which measure to take."""
    alternatives = [
        (595_000_000, "Gov revenue, quarter"),
        (105_700_000, "Gov operating income, quarter"),
    ]
    bucket, _, matched = classify(105_700_000, 595_000_000, alternatives)
    assert bucket == "wrong_level"
    assert "operating income" in matched


def test_q4_release_compares_against_the_quarter_not_the_year():
    """BWXT's February release reports Q4, and Q4 shares its period end with
    the full year. Picking the annual figure scored six exact Q4 readings as
    hallucinations: the model said 589,147,000 for Q4 2025 Government
    Operations, against a full-year figure of 2,350,090,000."""
    from measure_extraction import pick_period

    truth = {
        ("GovernmentOperationsSegmentMember", "2025-12-31", "annual"): (
            2_350_090_000,
            "acc-a",
        ),
        ("GovernmentOperationsSegmentMember", "2025-12-31", "quarter"): (
            589_148_000,
            "acc-q",
        ),
    }
    value, label = pick_period(truth, "GovernmentOperationsSegmentMember", "2026-02-23")
    assert value == 589_148_000
    assert "quarter" in label


def test_later_period_still_wins_over_bucket_preference():
    from measure_extraction import pick_period

    truth = {
        ("Seg", "2026-03-31", "quarter"): (100.0, "a"),
        ("Seg", "2025-12-31", "quarter"): (90.0, "b"),
    }
    value, _ = pick_period(truth, "Seg", "2026-05-05")
    assert value == 100.0


def test_periods_after_the_filing_date_are_ignored():
    from measure_extraction import pick_period

    truth = {("Seg", "2026-09-30", "quarter"): (100.0, "a")}
    assert pick_period(truth, "Seg", "2026-05-05") == (None, "")


def test_fourth_quarter_is_derived_because_nothing_tags_it():
    """No company files a Q4 10-Q; the 10-K reports the full year. So the
    quarter a February earnings release discusses has no tagged counterpart.
    Comparing the model's Q4 reading against the full year scored six exact
    answers as hallucinations."""
    from measure_extraction import derive_fourth_quarters

    member = "GovernmentOperationsSegmentMember"
    truth = {
        (member, "2025-03-31", "quarter"): (555_286_000, "q1"),
        (member, "2025-06-30", "quarter"): (588_959_000, "q2"),
        (member, "2025-09-30", "quarter"): (616_697_000, "q3"),
        (member, "2025-12-31", "annual"): (2_350_090_000, "10-K"),
    }
    derived = derive_fourth_quarters(truth)
    value, source = derived[(member, "2025-12-31", "quarter")]
    assert value == 589_148_000  # the model read 589,147,000
    assert "derived" in source


def test_derivation_needs_all_three_quarters():
    from measure_extraction import derive_fourth_quarters

    truth = {
        ("Seg", "2025-03-31", "quarter"): (10.0, "q1"),
        ("Seg", "2025-12-31", "annual"): (100.0, "10-K"),
    }
    assert derive_fourth_quarters(truth) == {}


def test_derivation_never_overwrites_a_tagged_figure():
    from measure_extraction import derive_fourth_quarters

    truth = {
        ("Seg", "2025-03-31", "quarter"): (10.0, "q1"),
        ("Seg", "2025-06-30", "quarter"): (20.0, "q2"),
        ("Seg", "2025-09-30", "quarter"): (30.0, "q3"),
        ("Seg", "2025-12-31", "quarter"): (55.0, "tagged"),
        ("Seg", "2025-12-31", "annual"): (100.0, "10-K"),
    }
    assert derive_fourth_quarters(truth) == {}


def test_thousandfold_error_is_units_not_hallucination():
    """Sterling's Q2 2025 E-Infrastructure revenue came back as 310,406
    against a tagged 310,406,000. Identical digits, 1000x apart: the units
    were never resolved. Scoring that as a hallucination overstates how often
    the model reads the wrong row."""
    bucket, detail, _ = classify(310_406, 310_406_000, ALTERNATIVES)
    assert bucket == "scale_error"
    assert "1000x" in detail


def test_scale_error_detected_in_both_directions():
    assert classify(310_406_000_000, 310_406_000, [])[0] == "scale_error"
    assert classify(310_406, 310_406_000, [])[0] == "scale_error"


def test_a_genuinely_different_number_is_still_hallucinated():
    bucket, _, _ = classify(311_000, 310_406_000, [])
    assert bucket == "hallucinated"

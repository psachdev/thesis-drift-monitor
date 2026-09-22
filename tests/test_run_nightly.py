"""Offline tests for the nightly loop's logic, on real BWXT and Sterling figures."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_nightly import (  # noqa: E402
    Point,
    add_point,
    bucket_for,
    claim_year,
    criterion_for,
    derive_fourth_quarters,
    evaluate,
    growth_observations,
    implied_for_annual,
    one_year_earlier,
    ratio_observations,
    subtract_histories,
)

GOV = "GovernmentOperationsSegmentMember"
COM = "CommercialOperationsSegmentMember"
EINFRA = "EInfrastructureSolutionsSegmentMember"
WRITTEN = "2026-08-18"  # when the Module 1 claims were written


def q(member, end, value, filed, start=None, bucket="quarter", acc="acc"):
    return member, Point(value, start, end, bucket, acc, filed)


def bwxt_history():
    """Real BWXT segment revenue, in dollars, with real filing dates."""
    h = {}
    rows = [
        # 2025 quarters and full year
        q(GOV, "2025-03-31", 555_286_000, "2025-05-06", "2025-01-01"),
        q(GOV, "2025-06-30", 588_959_000, "2025-08-06", "2025-04-01"),
        q(GOV, "2025-09-30", 616_697_000, "2025-11-04", "2025-07-01"),
        q(GOV, "2025-12-31", 2_350_090_000, "2026-02-23", "2025-01-01", "annual"),
        q(COM, "2025-03-31", 128_310_000, "2025-05-06", "2025-01-01"),
        q(COM, "2025-06-30", 176_139_000, "2025-08-06", "2025-04-01"),
        q(COM, "2025-09-30", 250_971_000, "2025-11-04", "2025-07-01"),
        q(COM, "2025-12-31", 853_070_000, "2026-02-23", "2025-01-01", "annual"),
        # 2026 quarters -- both filed BEFORE the claims were written
        q(GOV, "2026-03-31", 577_901_000, "2026-05-04", "2026-01-01"),
        q(GOV, "2026-06-30", 601_291_000, "2026-08-05", "2026-04-01"),
        q(COM, "2026-03-31", 283_645_000, "2026-05-04", "2026-01-01"),
        q(COM, "2026-06-30", 302_512_000, "2026-08-05", "2026-04-01"),
    ]
    for member, point in rows:
        add_point(h, member, point)
    h.update(derive_fourth_quarters(h))
    return h


B3 = {
    "measure_id": "B3-government-revenue",
    "thesis_id": "bwxt-government-growth-fy2026",
    "member": GOV,
    "comparison": "yoy_growth",
    "direction": "at_least",
    "threshold": 0.07,
    "period_type": "annual",
    "periods_required": 1,
    "fires_when": "any",
    "resolves_by": "2027-02-28",
}
B1 = dict(B3, measure_id="B1-commercial-revenue",
          thesis_id="bwxt-commercial-growth-fy2026", member=COM, threshold=0.45)


# -- small helpers -------------------------------------------------------


def test_bucket_for():
    assert bucket_for("2026-04-01", "2026-06-30") == "quarter"
    assert bucket_for("2025-01-01", "2025-12-31") == "annual"
    assert bucket_for("2026-01-01", "2026-06-30") == "ytd"
    assert bucket_for(None, "2026-06-30") == "instant"


def test_one_year_earlier():
    assert one_year_earlier("2026-06-30") == "2025-06-30"


def test_claim_year_read_from_the_thesis_id():
    assert claim_year(B3) == "2026"


def test_criterion_maps_the_address_fields():
    c = criterion_for(dict(B3, periods_required=2, fires_when="all"))
    assert c.periods_required == 2
    assert c.fires_when.value == "all"


# -- the start line --------------------------------------------------------


def test_figures_public_before_the_claim_are_baseline_not_evidence():
    """Q2 2026 was filed on 5 August; the claims were written on 18 August.
    A claim cannot survive on data its author already had."""
    obs = growth_observations(bwxt_history(), GOV, "quarter", WRITTEN)
    assert obs == []


def test_a_figure_filed_after_the_claim_counts():
    h = bwxt_history()
    add_point(h, GOV, Point(640_000_000, "2026-07-01", "2026-09-30", "quarter", "q3", "2026-11-03"))
    obs = growth_observations(h, GOV, "quarter", WRITTEN)
    assert [o.period_end for o in obs] == ["2026-09-30"]
    assert obs[0].prior_value == 616_697_000


def test_every_claim_is_open_today():
    """Nothing has been filed since 18 August that tests an FY2026 claim."""
    resolution, _, _, _ = evaluate(B3, bwxt_history(), WRITTEN)
    assert resolution.verdict.value == "open"


# -- fourth quarter ------------------------------------------------------


def test_fourth_quarter_is_derived_and_dated_to_the_10k():
    h = bwxt_history()
    q4 = h[(GOV, "2025-12-31", "quarter")]
    assert q4.value == 589_148_000
    assert q4.filing_date == "2026-02-23"


# -- what the rest of the year must deliver -------------------------------


def test_b3_second_half_needs_roughly_eleven_percent():
    """Real figures. First half of 2026 grew about 3%; to reach 7% for the
    year, the second half must grow about 10.7% over the second half of 2025."""
    required, detail, _ = implied_for_annual(bwxt_history(), GOV, 0.07, "2026")
    assert round(required * 100, 1) == 10.7
    assert "2 of 4 quarters reported" in detail
    assert "+3.1%" in detail or "+3.0%" in detail


def test_b1_second_half_needs_much_less():
    """Commercial grew about 92% in the first half; clearing 45% for the year
    needs only about 18.6% from the second half."""
    required, _, _ = implied_for_annual(bwxt_history(), COM, 0.45, "2026")
    assert round(required * 100, 1) == 18.6


def test_implied_needs_a_prior_full_year():
    required, detail, _ = implied_for_annual({}, GOV, 0.07, "2026")
    assert required is None
    assert "prior full-year" in detail


# -- margin -------------------------------------------------------------


def test_margin_observation_uses_matching_segment_and_quarter():
    """Sterling E-Infrastructure Q2: 23.3% against 27.0% a year earlier."""
    income, revenue = {}, {}
    add_point(income, EINFRA, Point(83_767_000, "2025-04-01", "2025-06-30", "quarter", "a", "2025-08-05"))
    add_point(revenue, EINFRA, Point(310_406_000, "2025-04-01", "2025-06-30", "quarter", "a", "2025-08-05"))
    add_point(income, EINFRA, Point(210_849_000, "2026-04-01", "2026-06-30", "quarter", "b", "2026-08-04"))
    add_point(revenue, EINFRA, Point(905_001_000, "2026-04-01", "2026-06-30", "quarter", "b", "2026-08-04"))

    # Filed 4 August, before the claim: baseline, not evidence.
    assert ratio_observations(income, revenue, EINFRA, WRITTEN) == []

    obs = ratio_observations(income, revenue, EINFRA, "2026-07-01")
    assert len(obs) == 1
    assert round(obs[0].value * 100, 1) == 23.3
    assert round(obs[0].prior_value * 100, 1) == 27.0


# -- free cash flow -----------------------------------------------------


def test_free_cash_flow_is_subtracted_period_by_period():
    ocf, capex = {}, {}
    add_point(ocf, "", Point(479_848_000, "2025-01-01", "2025-12-31", "annual", "k", "2026-02-23"))
    add_point(capex, "", Point(184_557_000, "2025-01-01", "2025-12-31", "annual", "k", "2026-02-23"))
    fcf = subtract_histories(ocf, capex)
    assert fcf[("", "2025-12-31", "annual")].value == 295_291_000

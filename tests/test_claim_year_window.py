from resolver import Verdict
from run_nightly import Point, evaluate

WRITTEN = "2026-11-09"  # when the FY2027 claims were written
MEASURE = {
    "measure_id": "B4-government-revenue-fy2027",
    "thesis_id": "bwxt-government-growth-fy2027",
    "member": "Gov", "comparison": "yoy_growth", "direction": "at_least",
    "threshold": 0.07, "period_type": "annual",
    "periods_required": 1, "fires_when": "any",
}


def history_through_fy2026():
    # FY2026 grows only 3%: a fail if it were wrongly used for the FY2027 claim
    return {
        ("Gov", "2025-12-31", "annual"): Point(100.0, "2025-01-01", "2025-12-31", "annual", "k25", "2026-02-20"),
        ("Gov", "2026-12-31", "annual"): Point(103.0, "2026-01-01", "2026-12-31", "annual", "k26", "2027-02-20"),
    }


def test_fy2026_report_does_not_settle_an_fy2027_claim():
    resolution, *_ = evaluate(MEASURE, history_through_fy2026(), WRITTEN)
    assert resolution.verdict is Verdict.OPEN


def test_fy2027_report_does_settle_it():
    h = history_through_fy2026()
    # FY2027 grows 4.9% over FY2026: below 7%, so the claim fails
    h[("Gov", "2027-12-31", "annual")] = Point(108.0, "2027-01-01", "2027-12-31", "annual", "k27", "2028-02-20")
    resolution, *_ = evaluate(MEASURE, h, WRITTEN)
    assert resolution.verdict is Verdict.FIRED

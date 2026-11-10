from types import SimpleNamespace as P

from run_nightly import counts_as_evidence, latest_period_by_filing

WRITTEN = "2026-08-18"  # when the Sterling claims were written
Q3 = "0000874238-26-000125"  # Sterling third-quarter 2026 report


def sterling_q3_history():
    # The third-quarter report carries the new quarter-end AND last December's
    # backlog, re-printed for comparison under the same filing number.
    return {
        ("", "2026-09-30", "instant"): P(accession=Q3, filing_date="2026-10-29"),
        ("", "2025-12-31", "instant"): P(accession=Q3, filing_date="2026-10-29"),
    }


def test_new_period_in_new_filing_counts():
    h = sterling_q3_history()
    point = h[("", "2026-09-30", "instant")]
    assert counts_as_evidence(point, "2026-09-30", "instant", WRITTEN, latest_period_by_filing(h))


def test_comparison_column_reprinted_in_new_filing_is_ignored():
    h = sterling_q3_history()
    point = h[("", "2025-12-31", "instant")]
    assert not counts_as_evidence(point, "2025-12-31", "instant", WRITTEN, latest_period_by_filing(h))


def test_published_before_claim_is_ignored():
    point = P(accession="x", filing_date="2026-08-05")
    h = {("", "2026-06-30", "quarter"): point}
    assert not counts_as_evidence(point, "2026-06-30", "quarter", WRITTEN, latest_period_by_filing(h))


def test_quarter_ended_before_claim_but_reported_after_counts():
    point = P(accession="y", filing_date="2026-08-04")
    h = {("", "2026-06-30", "quarter"): point}
    assert counts_as_evidence(point, "2026-06-30", "quarter", "2026-07-01", latest_period_by_filing(h))

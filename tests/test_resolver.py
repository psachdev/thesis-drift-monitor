"""Resolver tests. No network; the numbers are real ones already verified."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from resolver import (  # noqa: E402
    Criterion,
    Direction,
    FiresWhen,
    Observation,
    Verdict,
    claim_holds,
    implied_required_growth,
    is_implausible,
    measured_value,
    resolve,
)


def obs(end, value, prior=None, start=None, source="acc-1", prior_end=None):
    return Observation(
        period_start=start,
        period_end=end,
        value=value,
        prior_value=prior,
        prior_period_end=prior_end,
        source=source,
    )


# -- polarity ------------------------------------------------------------


def test_claim_holds_at_least():
    assert claim_holds(0.0765, Direction.AT_LEAST, 0.07)
    assert not claim_holds(0.02, Direction.AT_LEAST, 0.07)


def test_claim_holds_at_most():
    assert claim_holds(0.05, Direction.AT_MOST, 0.07)
    assert not claim_holds(0.09, Direction.AT_MOST, 0.07)


def test_boundary_is_inclusive():
    """'At least 7%' includes exactly 7%. An exclusive test would kill a
    thesis that precisely met its own bar."""
    assert claim_holds(0.07, Direction.AT_LEAST, 0.07)


# -- B3: BWXT Government Operations, annual growth >= 7% ----------------


B3 = Criterion(
    measure_id="B3-government-revenue",
    comparison="yoy_growth",
    direction=Direction.AT_LEAST,
    threshold=0.07,
    periods_required=1,
    resolves_by="2027-02-28",
)


def test_b3_survives_on_fy2025_actuals():
    """FY2025 Government Ops grew 7.65% over FY2024. Real figures."""
    result = resolve(B3, [obs("2025-12-31", 2_350_090_000, prior=2_183_040_000)])
    assert result.verdict is Verdict.SURVIVED
    assert round(result.actual * 100, 2) == 7.65


def test_b3_fires_below_threshold():
    result = resolve(B3, [obs("2026-12-31", 2_300_000_000, prior=2_350_090_000)])
    assert result.verdict is Verdict.FIRED
    assert "threshold" in result.detail


def test_b3_open_without_a_prior_period():
    result = resolve(B3, [obs("2026-12-31", 2_500_000_000)])
    assert result.verdict is Verdict.UNMEASURABLE


def test_no_observations_is_open_not_survived():
    """Silence must never be read as the claim holding."""
    assert resolve(B3, []).verdict is Verdict.OPEN


# -- B1: BWXT Commercial, annual growth >= 45% --------------------------


def test_b1_survives_on_fy2025_actuals():
    criterion = Criterion(
        measure_id="B1",
        comparison="yoy_growth",
        direction=Direction.AT_LEAST,
        threshold=0.45,
    )
    result = resolve(criterion, [obs("2025-12-31", 853_070_000, prior=523_972_000)])
    assert result.verdict is Verdict.SURVIVED
    assert round(result.actual * 100, 1) == 62.8


# -- B2 free cash flow: absolute threshold -------------------------------


def test_b2_fcf_fires_below_floor():
    """FY2025 FCF was 295.3M against a 345M floor for FY2026."""
    criterion = Criterion(
        measure_id="B2-free-cash-flow",
        comparison="absolute_value",
        direction=Direction.AT_LEAST,
        threshold=345_000_000,
    )
    result = resolve(criterion, [obs("2026-12-31", 295_291_000)])
    assert result.verdict is Verdict.FIRED


# -- S1: fires only if BOTH quarters breach ------------------------------


S1 = Criterion(
    measure_id="S1-einfra-revenue",
    comparison="yoy_growth",
    direction=Direction.AT_LEAST,
    threshold=0.50,
    periods_required=2,
    fires_when=FiresWhen.ALL,
)


def test_s1_one_soft_quarter_does_not_fire():
    """Revenue is lumpy by project timing, so one soft quarter is tolerated
    deliberately. Sterling's E-Infrastructure fell 1.5% in FY2024 and then
    grew 58.8% in FY2025 -- firing on one period would have killed a thesis
    that went on to do exactly what it predicted."""
    result = resolve(
        S1,
        [
            obs("2026-09-30", 400_000_000, prior=350_000_000),  # +14%, soft
            obs("2026-12-31", 900_000_000, prior=500_000_000),  # +80%, strong
        ],
    )
    assert result.verdict is Verdict.SURVIVED
    assert "requires all of them" in result.detail


def test_s1_both_soft_quarters_fire():
    result = resolve(
        S1,
        [
            obs("2026-09-30", 400_000_000, prior=350_000_000),
            obs("2026-12-31", 420_000_000, prior=390_000_000),
        ],
    )
    assert result.verdict is Verdict.FIRED


def test_s1_stays_open_with_one_quarter_reported():
    """One breach is not enough to fire and not enough to survive."""
    result = resolve(S1, [obs("2026-09-30", 400_000_000, prior=350_000_000)])
    assert result.verdict is Verdict.OPEN
    assert "1 of 2" in result.detail


def test_s1_q2_2026_actuals_pass_easily():
    result = resolve(
        Criterion(
            measure_id="S1",
            comparison="yoy_growth",
            direction=Direction.AT_LEAST,
            threshold=0.50,
        ),
        [obs("2026-06-30", 905_001_000, prior=310_406_000)],
    )
    assert result.verdict is Verdict.SURVIVED
    assert round(result.actual * 100) == 192


# -- S2: fires if EITHER quarter falls ------------------------------------


S2 = Criterion(
    measure_id="S2-einfra-operating-margin",
    comparison="yoy_ratio_change",
    direction=Direction.AT_LEAST,
    threshold=0.0,
    periods_required=2,
    fires_when=FiresWhen.ANY,
)


def test_s2_single_decline_fires():
    """The deliberate asymmetry with S1: margin is a rate, and a single
    year-over-year decline is exactly the dilution signal being watched for."""
    result = resolve(
        S2,
        [
            obs("2026-09-30", 0.233, prior=0.270),
            obs("2026-12-31", 0.280, prior=0.260),
        ],
    )
    assert result.verdict is Verdict.FIRED
    assert "-3.7pp" in result.detail


def test_s2_holds_when_both_quarters_rise():
    result = resolve(
        S2,
        [
            obs("2026-09-30", 0.280, prior=0.270),
            obs("2026-12-31", 0.265, prior=0.260),
        ],
    )
    assert result.verdict is Verdict.SURVIVED


def test_margin_change_is_points_not_percent():
    """27.0% -> 23.3% is a fall of 3.7 points. Expressing it as -13.7% would
    invite a threshold in the wrong units."""
    value = measured_value(obs("2026-06-30", 0.233, prior=0.270), "yoy_ratio_change")
    assert round(value * 100, 1) == -3.7


def test_s2_asymmetry_against_s1_on_identical_data():
    """Same two periods, opposite verdicts. The asymmetry is the design."""
    data = [
        obs("2026-09-30", 0.23, prior=0.27),
        obs("2026-12-31", 0.29, prior=0.27),
    ]
    assert resolve(S2, data).verdict is Verdict.FIRED
    all_variant = Criterion(
        measure_id="x",
        comparison="yoy_ratio_change",
        direction=Direction.AT_LEAST,
        threshold=0.0,
        periods_required=2,
        fires_when=FiresWhen.ALL,
    )
    assert resolve(all_variant, data).verdict is Verdict.SURVIVED


# -- implied required performance ----------------------------------------


def test_implied_required_after_three_weak_quarters():
    """The early-death mechanism. Three quarters at roughly 3% growth against
    a 7% full-year target leaves an implausible remainder."""
    baseline = 2_183_040_000  # FY2024 Government Ops
    prior_quarters = [520_000_000, 545_000_000, 560_000_000]
    this_year = [q * 1.03 for q in prior_quarters]
    required, detail = implied_required_growth(baseline, 0.07, this_year, prior_quarters)
    assert required > 0.15
    assert "3 of 4 quarters reported" in detail


def test_implied_required_when_running_ahead():
    baseline = 2_183_040_000
    prior_quarters = [520_000_000, 545_000_000, 560_000_000]
    this_year = [q * 1.12 for q in prior_quarters]
    required, _ = implied_required_growth(baseline, 0.07, this_year, prior_quarters)
    assert required < 0.07


def test_implied_required_needs_matching_periods():
    required, detail = implied_required_growth(100.0, 0.07, [10.0, 20.0], [10.0])
    assert required is None
    assert "do not line up" in detail


def test_implied_required_with_nothing_reported():
    required, detail = implied_required_growth(100.0, 0.07, [], [])
    assert required is None


def test_implausible_returns_false_without_a_benchmark():
    """No historical benchmark means no judgement. An agent declaring a thesis
    dead on a number it invented is the failure this project exists to catch."""
    assert is_implausible(0.45, None) is False
    assert is_implausible(0.45, 0.20) is True
    assert is_implausible(0.10, 0.20) is False


# -- provenance ----------------------------------------------------------


def test_resolution_carries_its_sources():
    result = resolve(B3, [obs("2025-12-31", 2_350_090_000, prior=2_183_040_000,
                              source="0001486957-26-000007")])
    assert result.to_dict()["sources"] == ["0001486957-26-000007"]


def test_unknown_comparison_raises():
    with pytest.raises(ValueError):
        measured_value(obs("2025-12-31", 1.0), "vibes")

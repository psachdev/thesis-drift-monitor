#!/usr/bin/env python3
"""Decide whether a kill criterion has fired. No model involved.

The agent never computes. This module is the arithmetic half of that rule:
given reported figures and a criterion, it returns a verdict that a person can
check by hand. Everything here is a pure function over numbers, so it is
testable without the network.

Three verdicts, not two:

    FIRED     the kill condition is met; the thesis is wrong
    SURVIVED  the measurement period closed and the condition was not met
    OPEN      not yet determinable

OPEN is the common case and the reason the digest is usually silent.

Also computes implied required performance: after each reported period, what
the remainder must deliver for the claim to hold. That is how an annual claim
can die in November instead of February.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class Verdict(str, Enum):
    FIRED = "fired"
    SURVIVED = "survived"
    OPEN = "open"
    UNMEASURABLE = "unmeasurable"


class Direction(str, Enum):
    AT_LEAST = "at_least"
    AT_MOST = "at_most"


class FiresWhen(str, Enum):
    ANY = "any"  # one bad period is enough
    ALL = "all"  # every period in the window must be bad


@dataclass(frozen=True)
class Observation:
    """One reported figure, with enough provenance to re-check it by hand."""

    period_start: str | None
    period_end: str
    value: float
    source: str  # accession number
    prior_value: float | None = None
    prior_period_end: str | None = None

    @property
    def yoy_growth(self) -> float | None:
        if self.prior_value in (None, 0):
            return None
        return (self.value - self.prior_value) / self.prior_value


@dataclass(frozen=True)
class Criterion:
    """The kill condition, in the form the resolver can evaluate.

    ``threshold`` and ``direction`` describe the CLAIM. The kill condition is
    the negation: a claim of "at least 7%" is killed by a figure below 7%.
    Keeping the claim's polarity in the data avoids a double negative in every
    address-file entry.
    """

    measure_id: str
    comparison: str  # yoy_growth | absolute_value | ratio | yoy_ratio_change
    direction: Direction
    threshold: float
    periods_required: int = 1
    fires_when: FiresWhen = FiresWhen.ANY
    resolves_by: str | None = None


@dataclass
class Resolution:
    verdict: Verdict
    criterion_id: str
    detail: str
    observations: list[Observation] = field(default_factory=list)
    actual: float | None = None
    threshold: float | None = None
    implied_required: float | None = None
    implied_detail: str = ""

    def to_dict(self) -> dict:
        return {
            "criterion_id": self.criterion_id,
            "verdict": self.verdict.value,
            "actual": self.actual,
            "threshold": self.threshold,
            "detail": self.detail,
            "implied_required": self.implied_required,
            "implied_detail": self.implied_detail,
            "sources": [o.source for o in self.observations],
        }


# -- the comparison itself ----------------------------------------------


def claim_holds(actual: float, direction: Direction, threshold: float) -> bool:
    """Does this figure satisfy the claim? The kill condition is the negation."""
    if direction is Direction.AT_LEAST:
        return actual >= threshold
    return actual <= threshold


def measured_value(observation: Observation, comparison: str) -> float | None:
    if comparison == "yoy_growth":
        return observation.yoy_growth
    if comparison in ("absolute_value", "ratio"):
        return observation.value
    if comparison == "yoy_ratio_change":
        # value and prior_value are already ratios; the change is the delta,
        # not a growth rate, because a margin moving 27% -> 23.3% is a fall of
        # 3.7 points and expressing that as -13.7% invites the wrong threshold.
        if observation.prior_value is None:
            return None
        return observation.value - observation.prior_value
    raise ValueError(f"Unknown comparison: {comparison}")


def resolve(criterion: Criterion, observations: list[Observation]) -> Resolution:
    """Evaluate a criterion against the periods reported so far."""
    if not observations:
        return Resolution(
            verdict=Verdict.OPEN,
            criterion_id=criterion.measure_id,
            detail="no reported periods yet",
            threshold=criterion.threshold,
        )

    ordered = sorted(observations, key=lambda o: o.period_end)
    window = ordered[-criterion.periods_required :]

    measured = [(o, measured_value(o, criterion.comparison)) for o in window]
    usable = [(o, v) for o, v in measured if v is not None]

    if not usable:
        return Resolution(
            verdict=Verdict.UNMEASURABLE,
            criterion_id=criterion.measure_id,
            detail="reported figures present but no prior-period comparison available",
            observations=window,
            threshold=criterion.threshold,
        )

    breaches = [(o, v) for o, v in usable if not claim_holds(v, criterion.direction, criterion.threshold)]
    latest_obs, latest_value = usable[-1]

    result = Resolution(
        verdict=Verdict.OPEN,
        criterion_id=criterion.measure_id,
        detail="",
        observations=window,
        actual=latest_value,
        threshold=criterion.threshold,
    )

    if criterion.fires_when is FiresWhen.ANY:
        if breaches:
            obs, value = breaches[0]
            result.verdict = Verdict.FIRED
            result.actual = value
            result.detail = (
                f"{_fmt(value, criterion.comparison)} in {obs.period_end} "
                f"against a threshold of {_fmt(criterion.threshold, criterion.comparison)}"
            )
            return result
        if len(usable) >= criterion.periods_required:
            result.verdict = Verdict.SURVIVED
            result.detail = (
                f"{len(usable)} of {criterion.periods_required} periods reported, "
                "none breached"
            )
            return result
        result.detail = (
            f"{len(usable)} of {criterion.periods_required} periods reported, "
            "none breached so far"
        )
        return result

    # fires_when = ALL: every period in the window must breach
    if len(usable) < criterion.periods_required:
        result.detail = (
            f"{len(usable)} of {criterion.periods_required} periods reported; "
            f"{len(breaches)} breached so far"
        )
        return result
    if len(breaches) == len(usable):
        result.verdict = Verdict.FIRED
        result.actual = breaches[-1][1]
        result.detail = (
            f"all {len(usable)} periods breached; latest "
            f"{_fmt(breaches[-1][1], criterion.comparison)} in {breaches[-1][0].period_end}"
        )
        return result
    result.verdict = Verdict.SURVIVED
    result.detail = (
        f"{len(breaches)} of {len(usable)} periods breached; the criterion "
        "requires all of them"
    )
    return result


def _fmt(value: float, comparison: str) -> str:
    if comparison in ("yoy_growth", "ratio"):
        return f"{value * 100:.1f}%"
    if comparison == "yoy_ratio_change":
        return f"{value * 100:+.1f}pp"
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:,.1f}M"
    return f"{value:,.2f}"


# -- implied required performance ---------------------------------------


def implied_required_growth(
    baseline_full_year: float,
    threshold_growth: float,
    reported_this_year: list[float],
    prior_year_same_periods: list[float],
) -> tuple[float | None, str]:
    """What the rest of the year must deliver for an annual growth claim to hold.

    This is how a claim dies early. If three quarters have grown 3% and the
    remainder would need 19% to reach a 7% full-year target, the thesis is
    finished months before the figure is published.

    Returns (required growth rate for the remainder, explanation).
    """
    if not reported_this_year or not prior_year_same_periods:
        return None, "no periods reported yet"
    if len(reported_this_year) != len(prior_year_same_periods):
        return None, "reported periods and prior-year periods do not line up"

    target_full_year = baseline_full_year * (1 + threshold_growth)
    reported_sum = sum(reported_this_year)
    prior_reported_sum = sum(prior_year_same_periods)
    prior_remaining = baseline_full_year - prior_reported_sum

    if prior_remaining <= 0:
        return None, "no prior-year remainder to compare against"

    needed = target_full_year - reported_sum
    required = needed / prior_remaining - 1

    so_far = (
        (reported_sum - prior_reported_sum) / prior_reported_sum
        if prior_reported_sum
        else None
    )
    parts = [
        f"{len(reported_this_year)} of 4 quarters reported",
    ]
    if so_far is not None:
        parts.append(f"growth so far {so_far * 100:+.1f}%")
    parts.append(f"remainder must grow {required * 100:+.1f}%")
    return required, ", ".join(parts)


def is_implausible(required_growth: float, best_historical: float | None) -> bool:
    """Flag a required rate that exceeds anything the business has done.

    Deliberately conservative: without a historical benchmark this returns
    False rather than guessing. An agent declaring a thesis dead on a number
    it invented is the failure this whole project exists to catch.
    """
    if best_historical is None:
        return False
    return required_growth > best_historical


def days_until(deadline: str, today: str | None = None) -> int | None:
    try:
        end = date.fromisoformat(deadline)
    except (TypeError, ValueError):
        return None
    now = date.fromisoformat(today) if today else date.today()
    return (end - now).days

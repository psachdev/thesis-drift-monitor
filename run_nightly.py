#!/usr/bin/env python3
"""The nightly loop. Check new filings against every claim, log what changed.

    export SEC_USER_AGENT="Your Name you@example.com"
    python run_nightly.py                  # normal nightly run
    python run_nightly.py --force          # recompute even if nothing new
    python run_nightly.py --dry-run        # compute and print, write nothing

What counts as evidence
-----------------------
A figure is a test of a claim only if it was FILED AFTER THE CLAIM WAS WRITTEN.
Anything already public when you wrote the claim is baseline -- a claim cannot
"survive" on data its author already had. The start line is each thesis's own
created_at date from theses.consolidated.json, so a claim added next year gets
its own start line with nothing to maintain by hand.

The earlier quarters still matter. An annual claim is about the whole year, so
quarters reported before the claim was written count toward the full-year
arithmetic -- they just cannot settle the claim on their own.

The agent never computes. No model is called here. Figures come from tagged
XBRL; arithmetic happens in resolver.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (HERE.parent / "sec_data_downloader", HERE / "sec_data_downloader"):
    if (candidate / "secedgar").is_dir():
        sys.path.insert(0, str(candidate))
        break

from evidence_log import (  # noqa: E402
    Entry,
    RunSummary,
    append_entries,
    append_run,
    load_seen,
    mark_seen,
    new_documents,
    prune_seen,
    save_seen,
)
from resolver import (  # noqa: E402
    Criterion,
    Direction,
    FiresWhen,
    Observation,
    Verdict,
    implied_required_growth,
    resolve,
)

ADDRESSES = HERE / "theses.addresses.json"
THESES = HERE / "theses.consolidated.json"

TAGGED_FORMS = ["10-Q", "10-Q/A", "10-K", "10-K/A"]
WATCHED_FORMS = TAGGED_FORMS + ["8-K", "8-K/A"]
LOOKBACK_YEARS = 2  # enough history for prior-year comparisons

COMPARISON_ALIASES = {"absolute_ratio": "ratio"}
SKIPPED_COVERAGE = {"manual", "llm_required"}


# -- history: every tagged segment figure, with when it became public -----


@dataclass(frozen=True)
class Point:
    value: float
    start: str | None
    end: str
    bucket: str          # quarter | annual | ytd | instant
    accession: str
    filing_date: str     # when this figure became public


def bucket_for(start: str | None, end: str | None) -> str:
    if not end:
        return "unknown"
    if not start:
        return "instant"
    try:
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return "unknown"
    if days <= 100:
        return "quarter"
    if days >= 300:
        return "annual"
    return "ytd"


def one_year_earlier(day: str) -> str:
    y, m, d = day.split("-")
    return f"{int(y) - 1:04d}-{m}-{d}"


History = dict  # (member, end, bucket) -> Point


def add_point(history: History, member: str, point: Point) -> None:
    """First filing seen wins, and filings arrive newest first, so an
    amendment supersedes its original."""
    key = (member, point.end, point.bucket)
    history.setdefault(key, point)


def derive_fourth_quarters(history: History) -> History:
    """No company files a Q4 10-Q; the 10-K reports the full year. So the
    fourth quarter has no tagged figure anywhere and must be computed.

    The derived figure becomes public on the 10-K's filing date.
    """
    by_member_year: dict[tuple[str, str], dict] = {}
    for (member, end, bucket), point in history.items():
        slot = by_member_year.setdefault((member, end[:4]), {"quarters": {}})
        if bucket == "annual":
            slot["annual"] = point
        elif bucket == "quarter":
            slot["quarters"][end] = point

    derived: History = {}
    for (member, _year), slot in by_member_year.items():
        annual = slot.get("annual")
        if annual is None:
            continue
        quarters = {e: p for e, p in slot["quarters"].items() if e != annual.end}
        if len(quarters) != 3:
            continue
        key = (member, annual.end, "quarter")
        if key in history:
            continue
        derived[key] = Point(
            value=annual.value - sum(p.value for p in quarters.values()),
            start=None,
            end=annual.end,
            bucket="quarter",
            accession=annual.accession,
            filing_date=annual.filing_date,
        )
    return derived


# -- observations --------------------------------------------------------


def growth_observations(
    history: History, member: str, bucket: str, written_on: str
) -> list[Observation]:
    """Periods filed after the claim was written, each with its prior year."""
    out = []
    for (m, end, b), point in history.items():
        if m != member or b != bucket:
            continue
        if point.filing_date <= written_on:
            continue  # already public when the claim was written: baseline
        prior = history.get((member, one_year_earlier(end), bucket))
        out.append(
            Observation(
                period_start=point.start,
                period_end=end,
                value=point.value,
                source=point.accession,
                prior_value=prior.value if prior else None,
                prior_period_end=prior.end if prior else None,
            )
        )
    return sorted(out, key=lambda o: o.period_end)


def ratio_observations(
    numerator: History, denominator: History, member: str, written_on: str
) -> list[Observation]:
    """Segment margin: operating income over revenue, same segment and quarter."""
    def ratio(end: str) -> tuple[float | None, Point | None]:
        num = numerator.get((member, end, "quarter"))
        den = denominator.get((member, end, "quarter"))
        if not num or not den or not den.value:
            return None, None
        return num.value / den.value, num

    out = []
    for (m, end, b), point in numerator.items():
        if m != member or b != "quarter" or point.filing_date <= written_on:
            continue
        current, source = ratio(end)
        prior, _ = ratio(one_year_earlier(end))
        if current is None or source is None:
            continue
        out.append(
            Observation(
                period_start=point.start,
                period_end=end,
                value=current,
                source=source.accession,
                prior_value=prior,
                prior_period_end=one_year_earlier(end) if prior is not None else None,
            )
        )
    return sorted(out, key=lambda o: o.period_end)


def implied_for_annual(
    history: History, member: str, threshold: float, claim_year: str
) -> tuple[float | None, str, Point | None]:
    """What the rest of the claim year must deliver.

    Uses every quarter reported so far in the claim year, including ones that
    were public when the claim was written -- the claim is about the whole year.
    """
    prior_year = str(int(claim_year) - 1)
    baseline = next(
        (p for (m, e, b), p in history.items()
         if m == member and b == "annual" and e.startswith(prior_year)),
        None,
    )
    if baseline is None:
        return None, "no prior full-year figure to compare against", None

    this_year = sorted(
        (p for (m, e, b), p in history.items()
         if m == member and b == "quarter" and e.startswith(claim_year)),
        key=lambda p: p.end,
    )
    pairs = []
    for point in this_year:
        prior = history.get((member, one_year_earlier(point.end), "quarter"))
        if prior:
            pairs.append((point, prior))

    if not pairs:
        return None, "no quarters of the claim year reported yet", None
    if len(pairs) == 4:
        return None, "all four quarters reported; the annual figure settles it", pairs[-1][0]

    required, detail = implied_required_growth(
        baseline_full_year=baseline.value,
        threshold_growth=threshold,
        reported_this_year=[p.value for p, _ in pairs],
        prior_year_same_periods=[q.value for _, q in pairs],
    )
    return required, detail, pairs[-1][0]


# -- the claim, as the resolver sees it ----------------------------------


def criterion_for(measure: dict) -> Criterion:
    return Criterion(
        measure_id=measure["measure_id"],
        comparison=COMPARISON_ALIASES.get(measure["comparison"], measure["comparison"]),
        direction=Direction(measure["direction"]),
        threshold=float(measure["threshold"]),
        periods_required=int(measure.get("periods_required", 1)),
        fires_when=FiresWhen(measure.get("fires_when", "any")),
        resolves_by=measure.get("resolves_by"),
    )


def claim_year(measure: dict) -> str:
    """FY2026 claims resolve on the FY2026 annual figure."""
    for field in ("thesis_id", "measure_id"):
        text = measure.get(field, "")
        for token in text.replace("_", "-").split("-"):
            if token.startswith("fy") and token[2:].isdigit():
                return token[2:]
    return (measure.get("resolves_by") or "")[:4]


# -- fetching ------------------------------------------------------------


def fetch_segment_history(client, ticker: str, concept: str, axis: str, since: str) -> History:
    from secedgar import fetch_instance, iter_filings, segment_totals

    history: History = {}
    for filing in iter_filings(client, ticker, forms=TAGGED_FORMS, since=since):
        instance = fetch_instance(client, filing)
        if instance is None:
            continue
        try:
            facts = segment_totals(instance, concept, axis)
        except Exception:  # noqa: BLE001 -- ambiguous definitions are logged, not fatal
            continue
        for fact in facts:
            member = next(
                (m.rpartition(":")[2] for a, m in fact.context.dimensions
                 if a.rpartition(":")[2] == axis),
                "",
            )
            add_point(
                history,
                member,
                Point(
                    value=fact.numeric,
                    start=fact.period.start,
                    end=fact.period.instant or fact.period.end or "",
                    bucket=bucket_for(fact.period.start, fact.period.instant or fact.period.end),
                    accession=filing.accession,
                    filing_date=filing.filing_date,
                ),
            )
    history.update(derive_fourth_quarters(history))
    return history


def fetch_consolidated_history(client, cik: str, concept: str) -> History:
    from secedgar import company_concept, deduplicate

    history: History = {}
    for value in deduplicate(company_concept(client, cik, concept)):
        add_point(
            history,
            "",
            Point(
                value=value.value,
                start=value.start,
                end=value.end,
                bucket=bucket_for(value.start, value.end),
                accession=value.accession,
                filing_date=value.filed,
            ),
        )
    return history


def subtract_histories(left: History, right: History) -> History:
    """Operating cash flow minus capital expenditure, period by period."""
    out: History = {}
    for key, point in left.items():
        other = right.get(key)
        if other is None:
            continue
        out[key] = Point(
            value=point.value - other.value,
            start=point.start,
            end=point.end,
            bucket=point.bucket,
            accession=point.accession,
            filing_date=max(point.filing_date, other.filing_date),
        )
    return out


# -- the run -------------------------------------------------------------


def load_written_dates() -> dict[str, str]:
    data = json.loads(THESES.read_text())
    return {t["id"]: t["created_at"] for t in data["theses"]}


def evaluate(measure: dict, history: History, written_on: str, extra: History | None = None):
    """Return (Resolution, implied_required, implied_detail, latest Point)."""
    member = measure.get("member") or ""
    comparison = measure["comparison"]
    period_type = measure.get("period_type", "annual")
    bucket = {"annual": "annual", "quarterly": "quarter", "instant": "instant"}[period_type]

    if comparison == "yoy_ratio_change":
        observations = ratio_observations(history, extra or {}, member, written_on)
    elif comparison == "yoy_growth":
        observations = growth_observations(history, member, bucket, written_on)
    else:  # absolute_value
        observations = [
            Observation(period_start=p.start, period_end=e, value=p.value, source=p.accession)
            for (m, e, b), p in sorted(history.items(), key=lambda kv: kv[0][1])
            if m == member and b == bucket and p.filing_date > written_on
        ]

    resolution = resolve(criterion_for(measure), observations)

    implied, implied_detail, latest = None, "", None
    if period_type == "annual" and comparison == "yoy_growth":
        implied, implied_detail, latest = implied_for_annual(
            history, member, float(measure["threshold"]), claim_year(measure)
        )
    return resolution, implied, implied_detail, latest


def run(args) -> RunSummary:
    addresses = json.loads(ADDRESSES.read_text())["measures"]
    written = load_written_dates()
    state = load_seen()
    summary = RunSummary(started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    checkable = [
        m for m in addresses
        if m.get("coverage") not in SKIPPED_COVERAGE and m.get("concept_verified")
    ]
    summary.measures_checked = len(checkable)

    since = (state.get("last_run") or "")[:10] or (
        date.today() - timedelta(days=30)
    ).isoformat()
    if args.since:
        since = args.since

    from secedgar import EdgarClient, iter_filings

    entries: list[Entry] = []
    new_filings_all = []

    with EdgarClient() as client:
        for ticker in sorted({m["ticker"] for m in checkable}):
            recent = list(iter_filings(client, ticker, forms=WATCHED_FORMS, since=since))
            fresh = list(new_documents(state, recent))
            summary.documents_checked += len(recent)
            summary.documents_new += len(fresh)
            new_filings_all.extend(fresh)
            print(f"{ticker}: {len(recent)} filings since {since}, {len(fresh)} new")

            if not fresh and not args.force:
                continue

            ticker_measures = [m for m in checkable if m["ticker"] == ticker]
            lookback = (date.today().replace(year=date.today().year - LOOKBACK_YEARS)).isoformat()

            for measure in ticker_measures:
                written_on = written.get(measure["thesis_id"], "")
                concept = measure["concept_verified"]
                try:
                    if measure.get("axis"):
                        history = fetch_segment_history(
                            client, ticker, concept, measure["axis"], lookback
                        )
                        extra = None
                        if measure["comparison"] == "yoy_ratio_change":
                            denominator = (measure.get("denominator_concept_candidates") or [None])[0]
                            extra = fetch_segment_history(
                                client, ticker, denominator, measure["axis"], lookback
                            )
                    else:
                        left_concept = concept.split(" - ")[0].strip()
                        history = fetch_consolidated_history(client, measure["cik"], left_concept)
                        if " - " in concept:
                            right_concept = concept.split(" - ")[1].strip()
                            history = subtract_histories(
                                history,
                                fetch_consolidated_history(client, measure["cik"], right_concept),
                            )
                        extra = None

                    resolution, implied, implied_detail, latest = evaluate(
                        measure, history, written_on, extra
                    )
                except Exception as exc:  # noqa: BLE001
                    summary.errors.append(f"{measure['measure_id']}: {type(exc).__name__}: {exc}")
                    print(f"  {measure['measure_id']}: ERROR {exc}")
                    continue

                obs = resolution.observations[-1] if resolution.observations else None
                anchor = obs or (
                    Observation(latest.start, latest.end, latest.value, latest.accession)
                    if latest else None
                )
                entry = Entry(
                    logged_at=summary.started_at,
                    measure_id=measure["measure_id"],
                    thesis_id=measure["thesis_id"],
                    ticker=ticker,
                    accession=anchor.source if anchor else "",
                    filing_date="",
                    form="",
                    period_start=anchor.period_start if anchor else None,
                    period_end=anchor.period_end if anchor else None,
                    concept=concept,
                    axis=measure.get("axis"),
                    member=measure.get("member"),
                    value=anchor.value if anchor else None,
                    prior_value=obs.prior_value if obs else None,
                    computed=resolution.actual,
                    comparison=measure["comparison"],
                    threshold=float(measure["threshold"]),
                    verdict=resolution.verdict.value,
                    detail=resolution.detail,
                    implied_required=implied,
                    implied_detail=implied_detail,
                    source="xbrl",
                )
                entries.append(entry)
                summary.note_verdict(entry.verdict)
                print(f"  {measure['measure_id']}: {entry.verdict} -- {entry.detail}")
                if implied_detail:
                    print(f"      {implied_detail}")

    if args.dry_run:
        print(f"\ndry run: {len(entries)} entries computed, nothing written")
        return summary

    written_entries = append_entries(entries)
    summary.entries_written = len(written_entries)

    for filing in new_filings_all:
        mark_seen(state, filing.accession, filing.filing_date, filing.form)
    state["last_run"] = summary.started_at
    prune_seen(state)
    save_seen(state)
    append_run(summary)

    print(
        f"\nChecked {summary.documents_checked} documents "
        f"({summary.documents_new} new) against {summary.measures_checked} measures. "
        f"{summary.entries_written} new log entries."
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", help="Look at filings from this date (YYYY-MM-DD)")
    parser.add_argument("--force", action="store_true", help="Recompute even if nothing is new")
    parser.add_argument("--dry-run", action="store_true", help="Compute and print; write nothing")
    run(parser.parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

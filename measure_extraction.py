#!/usr/bin/env python3
"""Measure the LLM extraction path against XBRL ground truth.

Two paths to the same number. XBRL carries its own segment and period, so
Python can read it deterministically. The earnings release is untagged HTML, so
a model has to read it. This script runs both on the same quarter and scores
the difference.

The scoring has four buckets, not two, because "wrong" is not one thing:

    exact         matches the tagged figure
    near          within tolerance; probably the same number differently rounded
    wrong_level   matches a DIFFERENT real figure in the same filing -- US-only
                  revenue, a product line, a year-to-date total. Correct
                  arithmetic on the wrong row. Invisible without an answer key.
    hallucinated  matches nothing in the filing
    missing       the model declined to answer

wrong_level is the bucket worth publishing. BWXT's FY2025 10-K carries 176
facts for one revenue concept; six are the segment totals. One of the other 170
sits within 1.1% of the right answer.

    export SEC_USER_AGENT="Your Name you@example.com"
    export DEEPSEEK_API_KEY=...
    python measure_extraction.py --since 2024-01-01
    python measure_extraction.py --since 2024-01-01 --dry-run   # no model calls
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (HERE.parent / "sec_data_downloader", HERE / "sec_data_downloader"):
    if (candidate / "secedgar").is_dir():
        sys.path.insert(0, str(candidate))
        break

from secedgar import (  # noqa: E402
    EdgarClient,
    fetch_instance,
    find_earnings_exhibit,
    iter_filings,
    list_documents,
    segment_totals,
)

from llm_extract import (  # noqa: E402
    DEFAULT_MODEL,
    call_model,
    find_segment_table,
    html_to_tables,
)

ADDRESSES = HERE / "theses.addresses.json"
COMPANIES = HERE / "companies.json"
OUTPUT = HERE / "data" / "extraction_comparison.json"

NEAR_TOLERANCE = 0.02  # 2%
WRONG_LEVEL_TOLERANCE = 0.005  # a "different real figure" must match closely

# Earnings releases live in 8-Ks under item 2.02, Results of Operations.
EARNINGS_ITEM = "2.02"


@dataclass
class Comparison:
    ticker: str
    accession: str
    filing_date: str
    period_label: str | None
    measure_read: str | None
    xbrl_period: str
    segment: str
    xbrl_value: float | None
    llm_value: float | None
    bucket: str
    detail: str = ""
    row_label: str = ""
    matched_alternative: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Run:
    started_at: str
    model: str
    comparisons: list[Comparison] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def tally(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for comparison in self.comparisons:
            counts[comparison.bucket] = counts.get(comparison.bucket, 0) + 1
        return dict(sorted(counts.items()))


def load_segment_names(ticker: str) -> list[str]:
    data = json.loads(COMPANIES.read_text())
    return data.get(ticker, {}).get("reported_segments", [])


def load_measures() -> list[dict]:
    data = json.loads(ADDRESSES.read_text())
    return [
        m
        for m in data["measures"]
        if m.get("axis") and m.get("concept_verified") and not m.get("coverage")
    ]


def normalize(name: str) -> str:
    """Collapse a segment name for comparison.

    Reconstructing a display name from an XBRL member is guesswork:
    EInfrastructureSolutionsSegmentMember renders as "EInfrastructure
    Solutions" while the company writes "E-Infrastructure Solutions". Matching
    on letters alone sidesteps the whole question.
    """
    return "".join(c for c in name.lower() if c.isalnum())


def member_to_readable(member: str) -> str:
    """GovernmentOperationsSegmentMember -> 'Government Operations'."""
    name = member.rpartition(":")[2]
    for suffix in ("SegmentMember", "Member"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    out = []
    for index, char in enumerate(name):
        if char.isupper() and index and not name[index - 1].isupper():
            out.append(" ")
        out.append(char)
    return "".join(out).strip()


def all_numeric_facts(instance, concept: str) -> list[tuple[float, str]]:
    """Every figure in the filing for this concept, with its dimensions.

    This is the answer key for the wrong_level bucket: a model answer that
    matches one of these is reading a real row, just not the right one.
    """
    out = []
    for fact in instance.query(concept=concept, numeric_only=True):
        dims = ", ".join(
            f"{a.rpartition(':')[2]}={m.rpartition(':')[2]}"
            for a, m in fact.context.dimensions
        )
        label = f"{dims or 'consolidated'} [{fact.period}]"
        out.append((fact.numeric, label))
    return out


def classify(
    llm_value: float | None,
    xbrl_value: float | None,
    alternatives: list[tuple[float, str]],
) -> tuple[str, str, str]:
    """Return (bucket, detail, matched_alternative)."""
    if llm_value is None:
        return "missing", "model returned no figure", ""
    if xbrl_value is None:
        return "no_ground_truth", "no tagged figure for this segment-period", ""

    if llm_value == xbrl_value:
        return "exact", "", ""

    # A figure off by exactly a power of a thousand, with the same digits, is
    # a units failure, not a misreading. Counting it as a hallucination
    # overstates how often the model reads the wrong row.
    for factor in (1_000, 1_000_000, 0.001, 0.000001):
        if abs(llm_value * factor - xbrl_value) < 1:
            return (
                "scale_error",
                f"correct digits, off by {factor:g}x -- units not resolved",
                "",
            )

    spread = abs(llm_value - xbrl_value) / abs(xbrl_value) if xbrl_value else None

    # Check wrong_level BEFORE near. BWXT's US-only Government Operations
    # revenue sits 1.1% from the segment total -- inside the near tolerance.
    # Scoring it as a near miss would hide the finding: the model read a real
    # row, just not the one the criterion names.
    for value, label in alternatives:
        if value == 0 or value == xbrl_value:
            continue
        if abs(llm_value - value) / abs(value) <= WRONG_LEVEL_TOLERANCE:
            return (
                "wrong_level",
                f"matches another figure in the same filing, {spread * 100:.1f}% "
                "from the one the criterion names",
                label,
            )

    if spread is not None and spread <= NEAR_TOLERANCE:
        return "near", f"{spread * 100:.2f}% from the tagged figure", ""

    return (
        "hallucinated",
        f"matches no figure in the filing; {spread * 100:.1f}% from the tagged value",
        "",
    )


def earnings_filings(client, ticker: str, since: str):
    """8-Ks carrying item 2.02, newest first."""
    for filing in iter_filings(client, ticker, forms=["8-K", "8-K/A"], since=since):
        if EARNINGS_ITEM in filing.items:
            yield filing


def period_days(period) -> int | None:
    from datetime import date

    if period.instant or not (period.start and period.end):
        return None
    try:
        return (date.fromisoformat(period.end) - date.fromisoformat(period.start)).days
    except ValueError:
        return None


def xbrl_truth(
    client, ticker: str, since: str, concept: str, axis: str
) -> dict[tuple[str, str, str], tuple[float, str]]:
    """(segment member, period end, duration bucket) -> (value, accession).

    Keyed by the period the figure covers, never by how close it is to
    anything a model said. Matching ground truth to the answer under test
    fits the key to the guess and turns any error into a near miss.
    """
    truth: dict[tuple[str, str, str], tuple[float, str]] = {}
    forms = ["10-Q", "10-Q/A", "10-K", "10-K/A"]
    for filing in iter_filings(client, ticker, forms=forms, since=since):
        instance = fetch_instance(client, filing)
        if instance is None:
            continue
        try:
            facts = segment_totals(instance, concept, axis)
        except Exception:  # noqa: BLE001
            continue
        for fact in facts:
            member = next(
                (
                    m.rpartition(":")[2]
                    for a, m in fact.context.dimensions
                    if a.rpartition(":")[2] == axis
                ),
                "",
            )
            days = period_days(fact.period)
            if days is None:
                continue
            bucket = "quarter" if days <= 100 else "annual" if days >= 300 else "ytd"
            key = (member, fact.period.end or "", bucket)
            # Amendments supersede originals; newest filing wins.
            truth.setdefault(key, (fact.numeric, filing.accession))

    truth.update(derive_fourth_quarters(truth))
    return truth


def derive_fourth_quarters(truth: dict) -> dict:
    """Add Q4 figures, which no filing tags.

    There is no fourth-quarter 10-Q: the 10-K reports the full year. So the
    quarter an earnings release actually discusses has no tagged counterpart
    and has to be computed -- annual minus the three reported quarters.

    Python does this, not the model. Same rule as everywhere else.
    """
    by_member_year: dict[tuple[str, str], dict[str, float]] = {}
    for (member, period_end, bucket), (value, _source) in truth.items():
        if not period_end:
            continue
        year = period_end[:4]
        slot = by_member_year.setdefault((member, year), {})
        if bucket == "annual":
            slot["annual"] = value
            slot["annual_end"] = period_end
        elif bucket == "quarter":
            slot[period_end] = value

    derived: dict = {}
    for (member, _year), slot in by_member_year.items():
        annual = slot.get("annual")
        annual_end = slot.get("annual_end")
        if annual is None or not annual_end:
            continue
        quarters = [
            value
            for key, value in slot.items()
            if key not in ("annual", "annual_end") and key != annual_end
        ]
        if len(quarters) != 3:
            continue
        key = (member, annual_end, "quarter")
        if key in truth:
            continue
        derived[key] = (annual - sum(quarters), "derived: annual - Q1..Q3")
    return derived


def pick_period(
    truth: dict, member: str, filing_date: str, max_lag_days: int = 120
) -> tuple[float | None, str]:
    """Latest tagged period for this segment that closed before the filing."""
    from datetime import date

    try:
        filed = date.fromisoformat(filing_date)
    except (TypeError, ValueError):
        return None, ""

    # A Q4 release and the full-year figure share a period end. The release
    # reports the quarter -- "Three Months Ended December 31" -- so quarter
    # wins on a tie. Getting this wrong scored six exact Q4 readings as
    # hallucinations, because the model was compared against the full year.
    rank = {"quarter": 0, "ytd": 1, "annual": 2}

    best: tuple[str, int, str, float] | None = None
    for (seg, period_end, bucket), (value, _source) in truth.items():
        if seg != member or not period_end:
            continue
        try:
            end = date.fromisoformat(period_end)
        except ValueError:
            continue
        lag = (filed - end).days
        if lag < 0 or lag > max_lag_days:
            continue
        key = (period_end, -rank.get(bucket, 9))
        if best is None or key > (best[0], -best[1]):
            best = (period_end, rank.get(bucket, 9), bucket, value)

    if best is None:
        return None, ""
    return best[3], f"{best[0]} ({best[2]})"


def run(args) -> Run:
    measures = load_measures()
    by_ticker: dict[str, list[dict]] = {}
    for measure in measures:
        by_ticker.setdefault(measure["ticker"], []).append(measure)

    result = Run(
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        model=args.model,
    )

    with EdgarClient() as client:
        for ticker, ticker_measures in sorted(by_ticker.items()):
            if args.ticker and ticker.upper() != args.ticker.upper():
                continue
            segment_names = load_segment_names(ticker)
            concept = ticker_measures[0]["concept_verified"]
            axis = ticker_measures[0]["axis"]
            wanted_members = {m["member"] for m in ticker_measures}

            print(f"\n=== {ticker} ===")
            truth = xbrl_truth(client, ticker, args.since, concept, axis)
            print(f"  {len(truth)} tagged segment-periods from XBRL")

            for filing in earnings_filings(client, ticker, args.since):
                documents = list_documents(client, filing)
                exhibit = find_earnings_exhibit(documents)
                if exhibit is None:
                    result.skipped.append(
                        {"ticker": ticker, "accession": filing.accession,
                         "reason": "no earnings exhibit"}
                    )
                    continue

                raw = client.get_bytes(exhibit.url)
                all_tables = html_to_tables(raw)
                table = find_segment_table(all_tables, segment_names)
                if table is None and args.debug:
                    print(f"\n--- {filing.accession}: no table selected ---")
                    for candidate in all_tables:
                        print(
                            f"  rows={candidate.row_count:<3} "
                            f"segments={candidate.mentions(segment_names)} "
                            f"revenue={candidate.mentions_revenue} "
                            f"heading={candidate.heading[:70]!r}"
                        )
                    return result
                if table is None:
                    result.skipped.append(
                        {"ticker": ticker, "accession": filing.accession,
                         "reason": "no segment table located"}
                    )
                    print(f"  {filing.accession}  skipped: no segment table")
                    continue

                if args.debug:
                    print(f"\n--- {filing.accession} table ---")
                    print(table.text[:3000])
                    extraction = call_model(
                        table.text,
                        segment_names,
                        document_text=raw.decode("utf-8", "ignore"),
                        model=args.model,
                    )
                    print(f"\n--- model reply ({extraction.model}) ---")
                    print(extraction.raw[:3000] or f"(error: {extraction.error})")
                    print(f"\nparsed segments: {extraction.segments}")
                    print(f"measure_read: {extraction.measure_read}")
                    print(f"units_note: {extraction.units_note}")
                    print(f"looking for: {sorted(member_to_readable(m) for m in wanted_members)}")
                    return result

                if args.dry_run:
                    print(
                        f"  {filing.accession}  table found "
                        f"({table.row_count} rows) -- no model call"
                    )
                    continue

                # Pass the whole release, not just the table. Sterling states
                # its units outside the table, and without them two figures
                # came back exactly 1000x low with identical digits.
                extraction = call_model(
                    table.text,
                    segment_names,
                    document_text=raw.decode("utf-8", "ignore"),
                    model=args.model,
                )
                if extraction.error:
                    result.skipped.append(
                        {"ticker": ticker, "accession": filing.accession,
                         "reason": extraction.error}
                    )
                    print(f"  {filing.accession}  model error: {extraction.error}")
                    continue

                instance = fetch_instance(client, filing)
                alternatives = (
                    all_numeric_facts(instance, concept) if instance else []
                )

                for member in sorted(wanted_members):
                    readable = member_to_readable(member)
                    llm_value = None
                    target = normalize(readable)
                    for name, value in extraction.segments.items():
                        if normalize(name) == target:
                            llm_value = value
                            break

                    # An 8-K's report date is usually the announcement date,
                    # not the period end: BWXT's February release covers
                    # FY2025 but is dated 2026-02-23. So take the latest
                    # tagged period that closed before the filing, within a
                    # quarter of it. Deterministic, and never influenced by
                    # what the model said.
                    xbrl_value, xbrl_period = pick_period(
                        truth, member, filing.filing_date
                    )

                    bucket, detail, matched = classify(
                        llm_value, xbrl_value, alternatives
                    )
                    result.comparisons.append(
                        Comparison(
                            ticker=ticker,
                            accession=filing.accession,
                            filing_date=filing.filing_date,
                            period_label=extraction.period_label,
                            measure_read=extraction.measure_read,
                            xbrl_period=xbrl_period,
                            segment=readable,
                            xbrl_value=xbrl_value,
                            llm_value=llm_value,
                            bucket=bucket,
                            detail=detail,
                            row_label=next(
                                (
                                    str(v)
                                    for k, v in extraction.row_labels.items()
                                    if normalize(k) == target and v
                                ),
                                "",
                            ),
                            matched_alternative=matched,
                        )
                    )
                    mark = {"exact": "OK", "near": "~", "wrong_level": "LEVEL",
                            "hallucinated": "HALLUC", "missing": "-"}.get(bucket, "?")
                    print(
                        f"  {filing.accession}  {readable:<28} {mark:<7} "
                        f"llm={_fmt(llm_value)} xbrl={_fmt(xbrl_value)}"
                    )

    return result


def _fmt(value: float | None) -> str:
    if value is None:
        return "none"
    return f"{value:,.0f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2024-01-01")
    parser.add_argument("--ticker", help="Limit to one ticker")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print the table and the raw model reply for the first filing, then stop",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Find the tables but make no model calls",
    )
    args = parser.parse_args()

    result = run(args)

    print("\n=== tally ===")
    total = len(result.comparisons)
    for bucket, count in result.tally().items():
        share = f"{count / total * 100:.0f}%" if total else "-"
        print(f"  {bucket:<16} {count:>3}  {share}")
    print(f"  {'total':<16} {total:>3}")
    if result.skipped:
        print(f"\n{len(result.skipped)} filings skipped:")
        for item in result.skipped[:10]:
            print(f"  {item['accession']}  {item['reason']}")

    if not args.dry_run and result.comparisons:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(
            json.dumps(
                {
                    "started_at": result.started_at,
                    "model": result.model,
                    "tally": result.tally(),
                    "comparisons": [c.to_dict() for c in result.comparisons],
                    "skipped": result.skipped,
                },
                indent=2,
            )
            + "\n"
        )
        print(f"\nWrote {OUTPUT.relative_to(HERE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Print every figure the Module 2 post will quote, with its source.

    export SEC_USER_AGENT="Your Name you@example.com"
    python verify_post_numbers.py

Each line shows the figure, the filing it came from, and a link. Open the
filings and check them by eye before publishing: every number in this project
came out of a tool that was wrong eleven times during its construction, and a
post about measurement accuracy is the wrong place for an unchecked figure.

This also doubles as something a reader can run themselves.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (HERE.parent / "sec_data_downloader", HERE / "sec_data_downloader"):
    if (candidate / "secedgar").is_dir():
        sys.path.insert(0, str(candidate))
        break

from secedgar import (  # noqa: E402
    EdgarClient,
    company_concept,
    deduplicate,
    fetch_instance,
    iter_filings,
    segment_totals,
)

REVENUE = "RevenueFromContractWithCustomerExcludingAssessedTax"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"

BWXT_10K = "0001486957-26-000007"
STRL_10K = "0000874238-26-000024"


def link(cik: str, accession: str) -> str:
    return ARCHIVE.format(cik=int(cik), acc=accession.replace("-", ""))


def heading(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def money(value: float) -> str:
    return f"{value:>18,.0f}"


def find_filing(client, ticker: str, accession: str):
    for filing in iter_filings(client, ticker, forms=["10-K", "10-K/A"]):
        if filing.accession == accession:
            return filing
    raise SystemExit(f"{accession} not found for {ticker}")


def segment_table(client, ticker: str, accession: str, label: str) -> dict:
    """Segment revenue by member and period, printed with its source."""
    filing = find_filing(client, ticker, accession)
    instance = fetch_instance(client, filing)
    facts = segment_totals(instance, REVENUE, "StatementBusinessSegmentsAxis")

    out: dict[tuple[str, str], float] = {}
    for fact in facts:
        member = next(
            (m.rpartition(":")[2] for a, m in fact.context.dimensions
             if a.rpartition(":")[2] == "StatementBusinessSegmentsAxis"),
            "",
        )
        out[(member, fact.period.end or "")] = fact.numeric

    heading(f"{label} -- segment revenue, from {accession}")
    print(f"source: {link(filing.cik, accession)}\n")
    for (member, end), value in sorted(out.items()):
        print(f"{money(value)}   {end}   {member}")

    # Year-over-year growth, computed here rather than quoted from memory.
    print()
    members = sorted({m for m, _ in out})
    years = sorted({e[:4] for _, e in out})
    for member in members:
        for earlier, later in zip(years, years[1:]):
            before = next((v for (m, e), v in out.items() if m == member and e.startswith(earlier)), None)
            after = next((v for (m, e), v in out.items() if m == member and e.startswith(later)), None)
            if before and after:
                print(f"  {member}: {earlier} to {later} = {(after / before - 1) * 100:+.2f}%")

    # Segment totals should reconcile to the consolidated figure.
    print()
    for year in years:
        total = sum(v for (_, e), v in out.items() if e.startswith(year))
        print(f"  {year} segments sum to {total:,.0f}")
    return out


def consolidated(client, cik: str, concept: str, label: str) -> dict:
    values = deduplicate(company_concept(client, cik, concept))
    annual = [v for v in values if (v.fiscal_period or "").upper() == "FY"][-4:]
    print(f"\n{label}")
    for value in annual:
        print(f"{money(value.value)}   {value.start}..{value.end}   {value.form} {value.accession}")
    return {v.end[:4]: v.value for v in annual}


def count_revenue_facts(client, ticker: str, accession: str) -> None:
    """The claim that one filing holds 176 revenue figures, six of them totals."""
    filing = find_filing(client, ticker, accession)
    instance = fetch_instance(client, filing)
    every = instance.query(concept=REVENUE, numeric_only=True)
    totals = segment_totals(instance, REVENUE, "StatementBusinessSegmentsAxis")

    heading(f"{ticker} -- how many revenue figures are in {accession}")
    print(f"source: {link(filing.cik, accession)}\n")
    print(f"  facts tagged with this one revenue concept: {len(every)}")
    print(f"  of those, segment totals:                   {len(totals)}")
    if every:
        print(f"  share that are segment totals:              {len(totals) / len(every) * 100:.1f}%")

    # The near-miss that matters: the same segment, narrowed to one country.
    print("\n  Government Operations, by how it is tagged:")
    for fact in every:
        dims = {a.rpartition(':')[2]: m.rpartition(':')[2] for a, m in fact.context.dimensions}
        if dims.get("StatementBusinessSegmentsAxis") != "GovernmentOperationsSegmentMember":
            continue
        if fact.period.end != "2025-12-31" or fact.period.start != "2025-01-01":
            continue
        extras = {k: v for k, v in dims.items() if k != "StatementBusinessSegmentsAxis"}
        print(f"{money(fact.numeric)}   {extras or 'segment total'}")


def name_collision(client, accession: str) -> None:
    """A product line and a segment sharing a name, in the same filing."""
    filing = find_filing(client, "BWXT", accession)
    instance = fetch_instance(client, filing)
    heading("BWXT -- two different things called Commercial Operations")
    print(f"source: {link(filing.cik, accession)}\n")
    for fact in instance.query(concept=REVENUE, numeric_only=True):
        dims = {a.rpartition(':')[2]: m.rpartition(':')[2] for a, m in fact.context.dimensions}
        if fact.period.start != "2025-01-01" or fact.period.end != "2025-12-31":
            continue
        if "Commercial" not in str(dims):
            continue
        print(f"{money(fact.numeric)}   {dims}")
    print("\n  One is the Commercial Operations SEGMENT.")
    print("  One is a product line named Commercial Operations, inside the")
    print("  Government Operations segment. Matching on the name gets either.")


def fourth_quarter(bwxt: dict) -> None:
    """No company files a Q4 report, so the quarter has to be computed."""
    heading("BWXT -- the fourth quarter, which nothing tags")
    for member in ("GovernmentOperationsSegmentMember", "CommercialOperationsSegmentMember"):
        annual = bwxt.get((member, "2025-12-31"))
        if annual is None:
            continue
        print(f"\n  {member}")
        print(f"    full year 2025:        {annual:,.0f}")
        print("    minus Q1, Q2 and Q3 (from the quarterly filings)")
        print("    = the fourth quarter. No filing states it directly.")


def main() -> int:
    with EdgarClient() as client:
        bwxt = segment_table(client, "BWXT", BWXT_10K, "BWXT")
        heading("BWXT -- consolidated figures")
        consolidated(client, "0001486957", REVENUE, "revenue")
        ocf = consolidated(client, "0001486957",
                           "NetCashProvidedByUsedInOperatingActivities",
                           "cash from operations")
        capex = consolidated(client, "0001486957",
                             "PaymentsToAcquirePropertyPlantAndEquipment",
                             "capital expenditure")
        print("\nfree cash flow (cash from operations minus capital expenditure)")
        for year in sorted(set(ocf) & set(capex)):
            print(f"{money(ocf[year] - capex[year])}   {year}")
        print("\n  The claim requires FY2026 at or above $345,000,000.")

        count_revenue_facts(client, "BWXT", BWXT_10K)
        name_collision(client, BWXT_10K)
        fourth_quarter(bwxt)

        segment_table(client, "STRL", STRL_10K, "Sterling")
        heading("Sterling -- consolidated revenue and backlog")
        consolidated(client, "0000874238", REVENUE, "revenue")
        backlog = deduplicate(
            company_concept(client, "0000874238", "RevenueRemainingPerformanceObligation")
        )
        print("\nsigned backlog (remaining performance obligation)")
        for value in backlog[-4:]:
            print(f"{money(value.value)}   {value.end}   {value.form} {value.accession}")
        print("\n  The claim requires backlog above $4,000,000,000.")

    print("\n\nCheck each figure against the linked filing before publishing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Resolve every XBRL address in theses.addresses.json against a real filing.

Reports what actually resolved rather than asserting what should. Run this
before building anything on top of the address file: an address that does not
resolve fails silently later, and silence reads as "nothing to report".

    export SEC_USER_AGENT="Your Name you@example.com"
    python verify_addresses.py
    python verify_addresses.py --write   # record verified concepts back

Exit code 0 if every non-manual measure resolved, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
# secedgar lives in the sibling repo
for candidate in (HERE.parent / "sec_data_downloader", HERE / "sec_data_downloader"):
    if (candidate / "secedgar").is_dir():
        sys.path.insert(0, str(candidate))
        break

try:
    from secedgar import (  # noqa: E402
        EdgarClient,
        company_concept,
        deduplicate,
        fetch_instance,
        iter_filings,
        segment_totals,
    )
    from secedgar.xbrl import dimension_shapes  # noqa: E402
except ImportError as exc:  # pragma: no cover
    sys.exit(
        f"Could not import secedgar ({exc}).\n"
        "Expected it at ../sec_data_downloader/secedgar relative to this script."
    )

ADDRESSES = HERE / "theses.addresses.json"

# Amendments supersede originals, so both forms are fetched.
ANNUAL_FORMS = ["10-K", "10-K/A"]
QUARTERLY_FORMS = ["10-Q", "10-Q/A", "10-K", "10-K/A"]


def period_length(fact) -> str:
    """Classify a duration so a quarter is never compared against a half-year.

    A 10-Q instance carries a 3-month and a year-to-date fact for the same
    concept and segment. They look identical apart from the start date.
    """
    period = fact.period
    if period.instant:
        return "instant"
    if not (period.start and period.end):
        return "unknown"
    try:
        start = date.fromisoformat(period.start)
        end = date.fromisoformat(period.end)
    except ValueError:
        return "unknown"
    days = (end - start).days
    if days <= 100:
        return "quarter"
    if days <= 190:
        return "half_year"
    if days <= 285:
        return "nine_months"
    if days <= 380:
        return "annual"
    return f"{days}d"


def latest_filing(client, ticker: str, forms: list[str]):
    for filing in iter_filings(client, ticker, forms=forms):
        return filing
    return None


def check_segment_measure(client, measure: dict) -> dict:
    """Try each candidate concept against the most recent relevant filing."""
    ticker = measure["ticker"]
    quarterly = measure.get("period_type") == "quarterly"
    forms = QUARTERLY_FORMS if quarterly else ANNUAL_FORMS

    result = {"status": "unresolved", "detail": "", "concept": None, "samples": []}

    filing = None
    instance = None
    for candidate_filing in iter_filings(client, ticker, forms=forms):
        got = fetch_instance(client, candidate_filing)
        if got is not None:
            filing, instance = candidate_filing, got
            break

    if instance is None:
        result["detail"] = "no filing with an XBRL instance found"
        return result

    result["filing"] = filing.accession
    result["form"] = filing.form

    for concept in measure["concept_candidates"]:
        facts = segment_totals(instance, concept, measure["axis"])
        matching = [
            f
            for f in facts
            if any(
                m.rpartition(":")[2] == measure["member"]
                for _, m in f.context.dimensions
            )
        ]
        if not matching:
            continue

        wanted = "quarter" if quarterly else "annual"
        sized = [f for f in matching if period_length(f) == wanted]
        if not sized:
            lengths = sorted({period_length(f) for f in matching})
            result["status"] = "wrong_period"
            result["concept"] = concept
            result["detail"] = (
                f"resolved, but no {wanted} duration. found: {', '.join(lengths)}"
            )
            return result

        sized.sort(key=lambda f: f.period.end or "", reverse=True)
        result["status"] = "ok"
        result["concept"] = concept
        result["samples"] = [
            (f.period.start, f.period.end, f.numeric) for f in sized[:3]
        ]
        return result

    shapes = dimension_shapes(instance, measure["concept_candidates"][0])
    result["detail"] = "no candidate concept resolved at that axis+member"
    if shapes:
        top = ", ".join(" + ".join(axes) or "(none)" for axes, _ in shapes[:3])
        result["detail"] += f". shapes present for first candidate: {top}"
    return result


def _resolve_concept(client, cik: str, candidates, annual_only: bool):
    """First candidate that returns values. Returns (concept, sorted values)."""
    for concept in candidates:
        try:
            values = deduplicate(company_concept(client, cik, concept))
        except Exception:  # noqa: BLE001
            continue
        if not values:
            continue
        if annual_only:
            annual = [v for v in values if (v.fiscal_period or "").upper() == "FY"]
            # Only narrow to FY if that leaves something. An instant measure
            # reported every quarter has no FY rows outside the 10-K, and
            # filtering first left this looking two years stale.
            if annual:
                values = annual
        values.sort(key=lambda v: (v.end, v.filed), reverse=True)
        return concept, values
    return None, []


def check_consolidated_measure(client, measure: dict) -> dict:
    """Consolidated figures come from companyconcept, no filing parse needed."""
    result = {"status": "unresolved", "detail": "", "concept": None, "samples": []}
    annual_only = measure.get("period_type") == "annual"

    concept, values = _resolve_concept(
        client, measure["cik"], measure["concept_candidates"], annual_only
    )
    if not concept:
        result["detail"] = "no candidate concept returned values"
        return result

    # A derived measure needs its second leg before it means anything.
    subtract = measure.get("capex_concept_candidates")
    if subtract:
        sub_concept, sub_values = _resolve_concept(
            client, measure["cik"], subtract, annual_only
        )
        if not sub_concept:
            result["status"] = "incomplete"
            result["concept"] = concept
            result["detail"] = (
                f"{concept} resolved but no subtrahend did; "
                f"tried {', '.join(subtract)}"
            )
            return result

        by_period = {(v.start, v.end): v.value for v in sub_values}
        samples = []
        for v in values[:3]:
            leg = by_period.get((v.start, v.end))
            if leg is None:
                continue
            samples.append((v.start, v.end, v.value - leg))
        if not samples:
            result["status"] = "incomplete"
            result["detail"] = f"{concept} and {sub_concept} share no period"
            return result

        result["status"] = "ok"
        result["concept"] = f"{concept} - {sub_concept}"
        result["derived"] = True
        result["samples"] = samples
        return result

    result["status"] = "ok"
    result["concept"] = concept
    result["samples"] = [(v.start, v.end, v.value) for v in values[:3]]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write",
        action="store_true",
        help="Record resolved concepts back into the address file",
    )
    args = parser.parse_args()

    payload = json.loads(ADDRESSES.read_text())
    measures = payload["measures"]

    failures = 0
    with EdgarClient() as client:
        for measure in measures:
            mid = measure["measure_id"]
            coverage = measure.get("coverage")

            if coverage == "manual":
                print(f"[MANUAL ] {mid}: {measure['publisher_detail']}")
                continue
            if coverage == "llm_required":
                print(f"[NO XBRL] {mid}: {measure['quantity']} is non-GAAP")
                continue
            if not measure["concept_candidates"]:
                print(f"[SKIP   ] {mid}: no candidate concepts")
                continue

            if measure.get("axis"):
                outcome = check_segment_measure(client, measure)
            else:
                outcome = check_consolidated_measure(client, measure)

            if outcome["status"] == "ok":
                print(f"[OK     ] {mid}  concept={outcome['concept']}")
                for start, end, value in outcome["samples"]:
                    period = f"{start}..{end}" if start else end
                    print(f"             {value:>18,.0f}  {period}")
                if args.write:
                    measure["concept_verified"] = outcome["concept"]
                    measure["verified_against"] = outcome.get("filing")
            else:
                failures += 1
                print(f"[FAIL   ] {mid}  ({outcome['status']}) {outcome['detail']}")

    if args.write:
        ADDRESSES.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"\nWrote verified concepts to {ADDRESSES.name}")

    print(f"\n{failures} unresolved.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

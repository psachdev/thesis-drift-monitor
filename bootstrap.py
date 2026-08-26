#!/usr/bin/env python3
"""
bootstrap.py — draft a companies.json entry for a new ticker.

audit.py needs to know what a company actually reports before it can tell a
checkable kill criterion from an invented one. Without an entry it guesses, and
guessing is where it fabricates: on one run it asserted that BWXT does not
disclose segment backlog, and killed a good criterion on that basis. BWXT
discloses segment backlog.

So this drafts an entry, and marks it unverified. The model is good at recalling
the shape of a company's reporting and unreliable about the specifics, which
makes it a decent first draft and a poor final answer.

    python bootstrap.py MSFT
    python bootstrap.py MSFT --name "Microsoft Corporation"

Then open the company's latest 10-K, check the segment names against what it
wrote, correct anything wrong, and set "verified": true. Until you do, audit.py
will warn you every run.

Verifying takes about five minutes. Skipping it means the audit is built on
whatever the model half-remembered.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from llm import MODEL_SMART, LLMError, complete_json

REPO_ROOT = Path(__file__).resolve().parent
COMPANIES = REPO_ROOT / "companies.json"

SYSTEM = """You describe what a public company discloses in its regular \
financial reporting.

Return ONLY a JSON object. No prose, no markdown fences.

{
  "name": "Full legal or common company name",
  "reported_segments": ["Exact names of reportable segments as they appear in \
the 10-K"],
  "reports_regularly": ["Figures disclosed on a schedule - segment revenue, \
segment margin, backlog, guidance, and so on. Be specific about the level: say \
'segment revenue' rather than 'revenue' when both exist."],
  "does_not_break_out": ["Things people commonly assume are disclosed but are \
not: subsidiary standalone results, revenue by customer, headcount by skill, \
divisional figures below segment level, individual contract values."],
  "other_publishers_relevant_to_this_company": ["Regulators, agencies, or \
other bodies that publish material affecting this company on a schedule - a \
government procurement plan, an FDA calendar, a regulator's rate decisions."],
  "uncertain_about": ["Anything above you are not confident is correct. Be \
generous here. This list tells the user what to check first."]
}

Rules:
- Segment names must be the ones the company uses in its filings, not \
descriptive labels you invent.
- If you do not know a company's segment structure, say so in uncertain_about \
rather than guessing a plausible-sounding one.
- Prefer omitting an item to inventing it. A short accurate entry is more \
useful than a long speculative one."""


def main() -> None:
    ap = argparse.ArgumentParser(description="Draft a companies.json entry.")
    ap.add_argument("ticker")
    ap.add_argument("--name", default=None, help="Company name, if the ticker is ambiguous")
    ap.add_argument("--model", default=MODEL_SMART)
    ap.add_argument("--force", action="store_true", help="Overwrite an existing entry")
    args = ap.parse_args()

    ticker = args.ticker.upper()

    companies = json.loads(COMPANIES.read_text(encoding="utf-8")) if COMPANIES.exists() else {}

    if ticker in companies and not args.force:
        entry = companies[ticker]
        state = "verified" if entry.get("verified") else "UNVERIFIED"
        sys.exit(f"{ticker} already exists ({state}). Use --force to redraft.")

    who = f"{args.name} ({ticker})" if args.name else ticker
    print(f"drafting entry for {who} with {args.model}...")

    try:
        result = complete_json(
            SYSTEM,
            f"Company: {who}\n\nDescribe its regular financial reporting.",
            args.model,
            temperature=0.1,
        )
    except LLMError as exc:
        sys.exit(str(exc))

    usage = result.pop("_usage", {})
    result["verified"] = False
    companies[ticker] = result

    COMPANIES.write_text(json.dumps(companies, indent=2) + "\n", encoding="utf-8")

    print(f"\ntokens: {usage.get('in')} in / {usage.get('out')} out")
    print(f"\n{result.get('name', ticker)}")
    print("  segments: " + "; ".join(result.get("reported_segments") or ["(none given)"]))
    print("  reports : " + "; ".join((result.get("reports_regularly") or [])[:5]))

    if result.get("uncertain_about"):
        print("\n  the model flagged these as uncertain - check them first:")
        for item in result["uncertain_about"]:
            print(f"    - {item}")

    print(f"\nWritten to companies.json, marked UNVERIFIED.")
    print("Open the latest 10-K, confirm the segment names, then set")
    print(f'  "verified": true')
    print(f"in the {ticker} entry. audit.py warns until you do.")


if __name__ == "__main__":
    main()

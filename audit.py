#!/usr/bin/env python3
"""
check.py — grade the kill criteria on every saved thesis.

Reads the JSON files capture.py wrote, and for each kill criterion asks two
separate questions:

  1. Is it VALID?    Does it survive the five failure modes?
  2. Is it REACHABLE? Can this system actually go and look it up?

These are deliberately kept apart. A criterion can be perfectly well written
and still be beyond what my code can fetch - the Navy's shipbuilding plan is
a real annual public document, but nothing in my news pipeline reads it. That
is a gap in my tooling, not a flaw in the criterion, and the two need
opposite responses: rewrite a bad criterion, but build retrieval for a good
one. Merging them would mean deleting good thinking because the plumbing is
primitive.

Usage:
    python check.py                     # check everything in data/theses.real
    python check.py --profile demo
    python check.py --ticker STRL
    python check.py --limit 3           # try it on a few first

Output goes to data/review.<profile>.json for the review page to read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

from llm import MODEL_SMART, LLMError, complete_json

DEFAULT_MODEL = MODEL_SMART

# Same lesson as capture.py: this budget covers the model's hidden reasoning
# as well as the answer, and reasoning grows with task difficulty.
MAX_TOKENS = 16_000

REPO_ROOT = Path(__file__).resolve().parent

FAILURE_MODES = {
    "wrong_signal": "Measures share price or market behaviour instead of the business",
    "unobservable": "No document from any publisher reports this on a schedule",
    "wrong_level": "Real reported figure, but not the one the claim is about",
    "doesnt_settle": "Whether it happens or not, you still don't know if the claim holds",
    "timeframe_mismatch": "Criterion runs on a shorter clock than the claim",
}

COVERAGE = {
    "news": "Should appear in ticker news or a company press release",
    "filing": "In the company's own quarterly or annual filing",
    "external": "Published by someone else - a regulator, agency, or another company",
    "manual": "Real, but you would have to go look for it yourself",
}


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

# Two deliberate choices here.
#
# First, this call never sees the source document. The model that wrote these
# criteria had the article in front of it and found them justified. Showing it
# the article again invites the same conclusion. Judging the criterion on its
# own text forces the question that actually matters: could anyone check this?
#
# Second, the framing is adversarial. "Is this okay?" gets agreement. "Find
# the flaw" gets scrutiny. Even so, expect this to catch the obvious modes and
# miss the subtle ones - the real fix is a different model doing the grading,
# which is a later module.

# Two framings, identical in every other respect. This is the experiment: does
# telling the model to be suspicious change what it finds, or only how it talks
# about what it finds? Run both on the same theses and compare the counts.

FRAMING = {
    "adversarial": (
        "Your job is to find the flaw in each one. Assume something is wrong "
        "until the criterion proves otherwise. A criterion that survives real "
        "scrutiny is rare."
    ),
    "neutral": (
        "Judge each criterion on its merits. Some criteria are well built and "
        "some are not; report what you find either way. Do not look for "
        "problems that are not there, and do not overlook ones that are."
    ),
}

SYSTEM_TEMPLATE = """You audit kill criteria. A kill criterion is a specific \
observation that would show an investment claim is wrong.

{framing}

Return ONLY a JSON object. No prose, no markdown fences.

For each criterion, judge it two separate ways.

FIRST - validity. Which of these five failures applies, if any:

"wrong_signal" - It measures share price or market behaviour rather than the \
business. "Stock falls 20%" and "underperforms peers" are both this. A stock \
moves for many reasons unrelated to the claim.

"unobservable" - No document from any publisher reports this on a schedule. \
The test: which specific document would you open, who publishes it, and does \
it come out on a calendar? A company knowing something about itself is not \
the same as publishing it. Training progress, hiring status, project counts, \
and customer satisfaction are usually unobservable. A figure in a filing \
table is usually fine. Note that the publisher does NOT have to be the \
company - a government agency, a regulator, or a competitor's filing all \
count as observable.

"wrong_level" - A real reported figure, but not the one the claim is about. A \
claim about one segment tested against consolidated results is this: other \
segments can move the total in either direction and give a false verdict.

"doesnt_settle" - Whether it happens or not, you still don't know whether the \
claim holds. Test it both ways: if this occurs, is the claim dead? If it does \
not occur, is the claim alive? If either answer is "not really", it fails.

"timeframe_mismatch" - The criterion runs on a shorter clock than the claim. A \
claim about a durable multi-year advantage cannot be settled by two quarters \
of data.

Use "ok" if none apply.

IMPORTANT - separate judgment from facts.

Your reliable job is judging STRUCTURE: does this criterion test the claim, is \
it aimed at the right level, does it settle anything, do the clocks match. That \
needs no knowledge of the world.

Your unreliable job is asserting FACTS: whether a company discloses a figure, \
whether a program exists, what a number was last quarter. You will sometimes be \
wrong about these, and a wrong fact produces a confident wrong verdict.

So: whenever your reasoning depends on a fact about the world that is not given \
to you in the reference data above, you MUST list it in factual_assumptions, \
written as a plain checkable statement. If a verdict rests on an assumption you \
had to supply yourself, set confidence to "low" no matter how sure you feel.

Prefer the reference data over your own recollection. If the reference data \
says a figure is reported, it is reported.

SECOND - coverage. Where would the answer actually come from:

"filing" - the company's own quarterly or annual report
"news" - a company press release or ticker news
"external" - published by someone else: a government agency, regulator, or \
another company's filing
"manual" - real, but scattered enough that a person would have to go find it

Coverage is independent of validity. A criterion can be well written and hard \
for an automated system to reach. Say so; do not mark it invalid for that.

Schema:
{
  "criteria": [
    {
      "index": 0,
      "verdict": "ok" or one of the five failure names,
      "reason": "One sentence. Be specific about what fails and why.",
      "document": "The specific document that would answer this, or null if none exists",
      "coverage": "filing" | "news" | "external" | "manual",
      "suggested_fix": "A rewritten criterion that survives, or null if it is already fine",
      "factual_assumptions": [
        "Every fact about the world you relied on that was not given in the \
reference data, written so someone could check it. Empty list if none."
      ],
      "confidence": "high" | "medium" | "low"
    }
  ],
  "claim_note": "Anything wrong with the claim itself - too vague to test, no \
timeframe, more than one claim bundled together - or an empty string."
}

Judge every criterion you are given. Keep the same order and index."""


# --------------------------------------------------------------------------

def load_companies() -> dict:
    path = REPO_ROOT / "companies.json"
    if not path.exists():
        print("  ! companies.json not found - wrong_level checks will be weaker")
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    unverified = [t for t, v in data.items()
                  if isinstance(v, dict) and not v.get("verified", True)]
    if unverified:
        print(f"  ! UNVERIFIED company data: {', '.join(unverified)}")
        print("    These entries were drafted by a model and not checked against")
        print("    a filing. Verdicts resting on them may be wrong.")
    return data


def company_context(ticker: str, companies: dict) -> str:
    """What the checker needs to know to judge scope and observability.

    Without this the model is guessing about whether a figure is reported.
    With it, 'CEC revenue' is knowably not a reported line.
    """
    info = companies.get(ticker.upper())
    if not info:
        return (f"No reference data available for {ticker}. You do not know what "
                "this company discloses, so be explicit in factual_assumptions "
                "about anything you assume it reports.")

    lines = [f"{info.get('name', ticker)} ({ticker.upper()}) reporting practice:"]
    if info.get("reported_segments"):
        lines.append("Reported segments: " + "; ".join(info["reported_segments"]))
    if info.get("reports_regularly"):
        lines.append("Reports regularly: " + "; ".join(info["reports_regularly"]))
    if info.get("does_not_break_out"):
        lines.append("Does NOT break out: " + "; ".join(info["does_not_break_out"]))
    if info.get("verified_facts"):
        lines.append("Verified facts (checked against filings - trust these over "
                     "your own recollection):")
        for fact in info["verified_facts"]:
            lines.append(f"  - {fact}")
    if info.get("other_publishers_relevant_to_this_company"):
        lines.append(
            "Other publishers relevant here: "
            + "; ".join(info["other_publishers_relevant_to_this_company"])
        )
    return "\n".join(lines)


def call_model(thesis: dict, context: str, model: str, framing: str = "adversarial") -> dict:
    criteria = thesis.get("kill_criteria") or []
    numbered = "\n".join(f"[{i}] {c}" for i, c in enumerate(criteria))

    user = (
        f"{context}\n\n"
        f"CLAIM: {thesis.get('claim')}\n"
        f"DIRECTION: {thesis.get('direction')}\n"
        f"STATED HORIZON: {thesis.get('check_horizon')}\n\n"
        f"KILL CRITERIA TO AUDIT:\n{numbered}"
    )

    system = SYSTEM_TEMPLATE.replace("{framing}", FRAMING[framing])
    return complete_json(system, user, model, temperature=0.1)


# --------------------------------------------------------------------------

SYMBOL = {"ok": "OK  ", None: "?   "}


def main() -> None:
    ap = argparse.ArgumentParser(description="Audit kill criteria on saved theses.")
    ap.add_argument("--profile", default="real", choices=["real", "demo"])
    ap.add_argument("--ticker", default=None, help="Only check one ticker")
    ap.add_argument("--limit", type=int, default=None, help="Stop after N theses")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--framing", default="adversarial",
                    choices=["adversarial", "neutral"],
                    help="How to instruct the auditor. Everything else is "
                         "identical, so differences in output are attributable "
                         "to this alone.")
    args = ap.parse_args()

    thesis_dir = REPO_ROOT / "data" / f"theses.{args.profile}"
    if not thesis_dir.exists():
        sys.exit(f"No such directory: {thesis_dir}")

    files = sorted(thesis_dir.glob("*.json"))
    if args.ticker:
        files = [f for f in files if f.name.startswith(args.ticker.lower() + "-")]
    if args.limit:
        files = files[: args.limit]

    if not files:
        sys.exit("No theses found matching that filter.")

    companies = load_companies()
    print(f"checking {len(files)} theses with {args.model}, framing={args.framing}\n")

    reviews = []
    totals = {k: 0 for k in FAILURE_MODES}
    totals["ok"] = 0
    tokens_in = tokens_out = 0
    failures = 0
    assumed = 0

    for path in files:
        thesis = json.loads(path.read_text(encoding="utf-8"))
        ticker = thesis.get("ticker", "")
        criteria = thesis.get("kill_criteria") or []

        print(f"[{ticker}] {thesis.get('claim', '')[:70]}...")

        try:
            result = call_model(thesis, company_context(ticker, companies),
                                args.model, args.framing)
        except LLMError as exc:
            print(f"    FAILED: {exc}\n")
            failures += 1
            continue

        usage = result.pop("_usage", {})
        tokens_in += usage.get("in") or 0
        tokens_out += usage.get("out") or 0

        graded = result.get("criteria") or []
        for item in graded:
            idx = item.get("index", 0)
            verdict = item.get("verdict", "ok")
            totals[verdict] = totals.get(verdict, 0) + 1

            text = criteria[idx] if idx < len(criteria) else "(index out of range)"
            mark = "OK " if verdict == "ok" else "!! "
            print(f"  {mark}[{idx}] {text[:64]}")
            if verdict != "ok":
                print(f"       {verdict}: {item.get('reason', '')}")
                if item.get("suggested_fix"):
                    print(f"       fix: {item['suggested_fix'][:100]}")
            print(f"       coverage: {item.get('coverage', '?')}"
                  f"  doc: {item.get('document') or 'none'}")

            # Facts the model supplied itself. Both of the audit's worst errors
            # were assumptions buried in prose that read as authoritative. You
            # cannot check a hidden assumption; you can check a declared one.
            assumptions = item.get("factual_assumptions") or []
            if isinstance(assumptions, str):
                assumptions = [assumptions]
            for a in assumptions:
                assumed += 1
                print(f"       ASSUMES: {a}")

        if result.get("claim_note"):
            print(f"  note on claim: {result['claim_note']}")
        print()

        reviews.append({
            "thesis_id": thesis.get("id"),
            "file": path.name,
            "ticker": ticker,
            "claim": thesis.get("claim"),
            "direction": thesis.get("direction"),
            "mechanism": thesis.get("mechanism"),
            "check_horizon": thesis.get("check_horizon"),
            "kill_criteria": criteria,
            "audit": graded,
            "claim_note": result.get("claim_note", ""),
            "decision": None,          # you fill this in on the review page
        })

    out_path = REPO_ROOT / "data" / f"review.{args.profile}.{args.framing}.json"
    out_path.write_text(
        json.dumps({
            "generated": date.today().isoformat(),
            "model": args.model,
            "reviews": reviews,
        }, indent=2) + "\n",
        encoding="utf-8",
    )

    total_criteria = sum(totals.values())
    print("=" * 60)
    print(f"{len(reviews)} theses audited, {total_criteria} criteria"
          + (f", {failures} theses failed to process" if failures else ""))
    print(f"  ok                 {totals['ok']}")
    for mode in FAILURE_MODES:
        if totals.get(mode):
            print(f"  {mode:<18} {totals[mode]}")
    print(f"\ntokens: {tokens_in:,} in / {tokens_out:,} out")
    print(f"factual assumptions declared: {assumed}")
    print(f"wrote {out_path.relative_to(REPO_ROOT)}")
    print("\nThese are flags, not verdicts. The model that wrote these criteria")
    print("is close kin to the one grading them - it will catch the obvious")
    print("failures and miss the subtle ones. You still decide.")
    if assumed:
        print(f"\nCheck the {assumed} ASSUMES lines above before trusting any")
        print("verdict that rests on one. Verified facts belong in companies.json.")


if __name__ == "__main__":
    main()

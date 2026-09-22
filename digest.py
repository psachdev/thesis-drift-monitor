#!/usr/bin/env python3
"""The morning note. A view over the evidence log -- never a second record.

    python digest.py

Most mornings this should say almost nothing, and say it in one line. An agent
that writes a paragraph every morning trains you to ignore it within two weeks.
But short is not the same as empty: the note always states what was checked,
because "nothing was filed" and "the job died at 4am" look identical otherwise.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from evidence_log import latest_by_measure, read_entries, read_runs

HERE = Path(__file__).resolve().parent
ADDRESSES = HERE / "theses.addresses.json"
THESES = HERE / "theses.consolidated.json"

STALE_AFTER_HOURS = 36

PLAIN_VERDICT = {
    "fired": "WRONG -- the claim failed its test",
    "survived": "HELD -- the claim passed its test",
    "open": "Still open",
    "unmeasurable": "Cannot be measured from what was filed",
}


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _money(value: float | None) -> str:
    if value is None:
        return "no figure"
    if abs(value) >= 1_000_000_000:
        return f"${value / 1_000_000_000:,.2f} billion"
    return f"${value / 1_000_000:,.1f} million"


def _percent(value: float | None, comparison: str) -> str:
    if value is None:
        return "not yet computed"
    if comparison == "yoy_ratio_change":
        return f"{value * 100:+.1f} percentage points from a year earlier"
    if comparison in ("yoy_growth",):
        return f"{value * 100:+.1f}% compared with a year earlier"
    return _money(value)


def changes(entries) -> list[tuple]:
    """Criteria whose verdict differs from the previous entry for that claim."""
    by_measure: dict[str, list] = {}
    for entry in sorted(entries, key=lambda e: e.logged_at):
        by_measure.setdefault(entry.measure_id, []).append(entry)
    out = []
    for measure_id, history in by_measure.items():
        for earlier, later in zip(history, history[1:]):
            if earlier.verdict != later.verdict:
                out.append((measure_id, earlier.verdict, later.verdict, later))
    return out


def render(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    entries = read_entries()
    runs = read_runs()
    addresses = {m["measure_id"]: m for m in json.loads(ADDRESSES.read_text())["measures"]}
    claims = {t["id"]: t["claim"] for t in json.loads(THESES.read_text())["theses"]}

    lines = [f"Thesis drift monitor -- {now.date().isoformat()}", ""]

    # 1. Did the job actually run? This comes first, always.
    if not runs:
        lines.append("The nightly job has never run. Nothing below is current.")
        return "\n".join(lines)

    last = runs[-1]
    when = _parse(last.started_at)
    hours = (now - when).total_seconds() / 3600 if when else None
    lines.append(
        f"Last checked {last.started_at[:16].replace('T', ' ')} UTC: "
        f"{last.documents_checked} filings looked at, {last.documents_new} of them new, "
        f"against {last.measures_checked} measurable claims."
    )
    if hours is not None and hours > STALE_AFTER_HOURS:
        lines.append(
            f"WARNING: no run in {hours:.0f} hours. The job may have stopped -- "
            "treat everything below as out of date."
        )
    if last.errors:
        lines.append(f"{len(last.errors)} claim(s) could not be checked:")
        lines.extend(f"  - {error}" for error in last.errors)

    # 2. What changed. Usually nothing -- and that is the point.
    changed = changes(entries)
    lines.append("")
    if not changed:
        lines.append("Nothing changed.")
    else:
        lines.append("CHANGED")
        for measure_id, before, after, entry in changed:
            lines.append(f"  {measure_id}: {PLAIN_VERDICT[before]} -> {PLAIN_VERDICT[after]}")
            lines.append(f"    {entry.detail}  (filing {entry.accession})")

    # 3. Where each claim stands.
    lines.append("")
    lines.append("WHERE EACH CLAIM STANDS")
    latest = latest_by_measure(entries)
    for measure_id, measure in addresses.items():
        claim = claims.get(measure["thesis_id"], measure_id)
        coverage = measure.get("coverage")
        lines.append("")
        lines.append(f"  {measure_id}")
        lines.append(f"    {claim}")
        if coverage == "manual":
            lines.append(f"    Checked by hand: {measure.get('publisher_detail', 'see notes')}")
            continue
        if coverage == "llm_required":
            lines.append(
                "    Not in the tagged filing data. This figure is a company-defined "
                "measure that only appears in the earnings press release."
            )
            continue
        entry = latest.get(measure_id)
        if entry is None:
            lines.append("    Not checked yet.")
            continue
        lines.append(f"    {PLAIN_VERDICT[entry.verdict]}. {entry.detail}")
        if entry.computed is not None:
            lines.append(f"    Latest test figure: {_percent(entry.computed, entry.comparison)}")
        if entry.implied_detail:
            lines.append(f"    Rest of the year: {entry.implied_detail}")
        if measure.get("resolves_by"):
            lines.append(f"    Settles by {measure['resolves_by']}.")

    return "\n".join(lines)


if __name__ == "__main__":
    print(render())

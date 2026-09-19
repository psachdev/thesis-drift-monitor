#!/usr/bin/env python3
"""The evidence log: a permanent, append-only record of what was found and when.

Nothing here ever edits a line. If a figure is restated in a later filing,
that is a new entry saying so, not a correction of the old one. The log's
value is that it records what was known on a given date -- and that is the one
thing you cannot reconstruct afterwards.

Two files, deliberately separate:

    data/evidence.jsonl    the permanent record, one JSON object per line
    data/seen.json         which documents have been processed, so a re-run
                           after a crash does not duplicate anything

The log is structured data, not prose. The morning digest is a view rendered
over it. Baking the reader into the record would mean one reader forever.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
EVIDENCE_PATH = DATA / "evidence.jsonl"
SEEN_PATH = DATA / "seen.json"

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Entry:
    """One finding. Every field is something a person could check by hand."""

    logged_at: str          # when this run wrote the line
    measure_id: str         # which criterion, e.g. B3-government-revenue
    thesis_id: str
    ticker: str
    accession: str          # the filing, so the figure can be verified
    filing_date: str
    form: str
    period_start: str | None
    period_end: str | None
    concept: str            # the XBRL concept read
    axis: str | None
    member: str | None
    value: float | None     # the reported figure, as reported
    prior_value: float | None
    computed: float | None  # growth, margin change, whatever the comparison is
    comparison: str
    threshold: float | None
    verdict: str            # fired | survived | open | unmeasurable
    detail: str = ""
    implied_required: float | None = None
    implied_detail: str = ""
    source: str = "xbrl"    # xbrl | llm | derived
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def dedup_key(self) -> tuple:
        """Identity of a finding, independent of when it was written.

        Re-running the same day must not append the same finding twice, but a
        genuinely new figure for the same period -- a restatement -- must.
        """
        return (
            self.measure_id,
            self.accession,
            self.period_end,
            self.value,
            self.verdict,
        )


@dataclass
class RunSummary:
    """What one run did. This is what makes silence trustworthy."""

    started_at: str
    documents_checked: int = 0
    documents_new: int = 0
    measures_checked: int = 0
    entries_written: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def note_verdict(self, verdict: str) -> None:
        self.verdicts[verdict] = self.verdicts.get(verdict, 0) + 1

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -- reading -------------------------------------------------------------


def read_entries(path: Path = EVIDENCE_PATH) -> list[Entry]:
    """Every entry ever written, oldest first."""
    if not path.exists():
        return []
    entries = []
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(Entry(**json.loads(line)))
        except (json.JSONDecodeError, TypeError) as exc:
            # A corrupt line must not silently truncate history. Say which.
            raise ValueError(
                f"{path.name} line {line_number} is not a valid entry: {exc}"
            ) from exc
    return entries


def latest_by_measure(entries: Iterable[Entry]) -> dict[str, Entry]:
    """Most recent entry per criterion. Convenience for the digest."""
    latest: dict[str, Entry] = {}
    for entry in entries:
        current = latest.get(entry.measure_id)
        if current is None or entry.logged_at >= current.logged_at:
            latest[entry.measure_id] = entry
    return latest


def entries_since(entries: Iterable[Entry], when: str) -> list[Entry]:
    return [e for e in entries if e.logged_at >= when]


# -- writing -------------------------------------------------------------


def append_entries(
    new_entries: Iterable[Entry], path: Path = EVIDENCE_PATH
) -> list[Entry]:
    """Append entries that are not already recorded. Returns what was written.

    Append-only: existing lines are never touched. A restatement is a new
    line, and both remain readable.
    """
    new_entries = list(new_entries)
    if not new_entries:
        return []

    existing = {entry.dedup_key for entry in read_entries(path)}
    to_write = []
    for entry in new_entries:
        if entry.dedup_key in existing:
            continue
        existing.add(entry.dedup_key)
        to_write.append(entry)

    if not to_write:
        return []

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for entry in to_write:
            handle.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")
    return to_write


# -- seen documents ------------------------------------------------------


def load_seen(path: Path = SEEN_PATH) -> dict:
    if not path.exists():
        return {"last_run": None, "accessions": {}}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"last_run": None, "accessions": {}}
    data.setdefault("last_run", None)
    data.setdefault("accessions", {})
    return data


def save_seen(state: dict, path: Path = SEEN_PATH) -> None:
    """Write atomically. A half-written seen file would make the next run
    reprocess everything, or skip things it never checked."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8"
    )
    try:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)


def is_seen(state: dict, accession: str) -> bool:
    return accession in state.get("accessions", {})


def mark_seen(state: dict, accession: str, filing_date: str, form: str) -> None:
    state.setdefault("accessions", {})[accession] = {
        "filing_date": filing_date,
        "form": form,
        "first_seen": _now(),
    }


def prune_seen(state: dict, keep: int = 2000) -> int:
    """Keep the most recent accessions. The set grows forever otherwise, and
    EDGAR is only ever asked for filings since the last run."""
    accessions = state.get("accessions", {})
    if len(accessions) <= keep:
        return 0
    ordered = sorted(
        accessions.items(),
        key=lambda item: item[1].get("filing_date", ""),
        reverse=True,
    )
    state["accessions"] = dict(ordered[:keep])
    return len(accessions) - keep


def new_documents(state: dict, filings: Iterable) -> Iterator:
    """Filings not yet processed. An amendment is a separate document and is
    always processed, even when its original has been seen."""
    for filing in filings:
        if not is_seen(state, filing.accession):
            yield filing


# -- run summaries -------------------------------------------------------


def append_run(summary: RunSummary, path: Path | None = None) -> None:
    """Record that a run happened, whether or not it found anything.

    This is what makes an empty digest trustworthy. "Nothing was filed" and
    "the job died at 4am" produce the same silence otherwise.
    """
    path = path or (DATA / "runs.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(summary.to_dict(), sort_keys=True) + "\n")


def read_runs(path: Path | None = None) -> list[RunSummary]:
    path = path or (DATA / "runs.jsonl")
    if not path.exists():
        return []
    runs = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            runs.append(RunSummary(**json.loads(line)))
    return runs

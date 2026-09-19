"""Tests for the evidence log. No network."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evidence_log import (  # noqa: E402
    Entry,
    RunSummary,
    append_entries,
    append_run,
    is_seen,
    latest_by_measure,
    load_seen,
    mark_seen,
    prune_seen,
    read_entries,
    read_runs,
    save_seen,
)


def make_entry(**overrides) -> Entry:
    base = dict(
        logged_at="2026-11-04T12:00:00+00:00",
        measure_id="B3-government-revenue",
        thesis_id="bwxt-government-growth-fy2026",
        ticker="BWXT",
        accession="0001486957-26-000041",
        filing_date="2026-08-03",
        form="10-Q",
        period_start="2026-04-01",
        period_end="2026-06-30",
        concept="RevenueFromContractWithCustomerExcludingAssessedTax",
        axis="StatementBusinessSegmentsAxis",
        member="GovernmentOperationsSegmentMember",
        value=601_291_000.0,
        prior_value=588_959_000.0,
        computed=0.0209,
        comparison="yoy_growth",
        threshold=0.07,
        verdict="open",
        detail="1 of 1 periods reported",
    )
    base.update(overrides)
    return Entry(**base)


# -- append-only behaviour ----------------------------------------------


def test_entries_round_trip(tmp_path):
    path = tmp_path / "evidence.jsonl"
    written = append_entries([make_entry()], path)
    assert len(written) == 1
    read_back = read_entries(path)
    assert len(read_back) == 1
    assert read_back[0].value == 601_291_000.0
    assert read_back[0].accession == "0001486957-26-000041"


def test_rerunning_the_same_day_does_not_duplicate(tmp_path):
    """A crashed run gets re-run. The same finding must not be appended twice."""
    path = tmp_path / "evidence.jsonl"
    append_entries([make_entry()], path)
    again = append_entries([make_entry(logged_at="2026-11-04T18:00:00+00:00")], path)
    assert again == []
    assert len(read_entries(path)) == 1


def test_a_restated_figure_is_a_new_line_not_an_edit(tmp_path):
    """History is never rewritten. If a later filing restates a figure, both
    lines remain readable -- that is the whole point of keeping the log."""
    path = tmp_path / "evidence.jsonl"
    append_entries([make_entry()], path)
    append_entries([make_entry(value=602_000_000.0, accession="0001486957-26-000044")], path)
    entries = read_entries(path)
    assert len(entries) == 2
    assert [e.value for e in entries] == [601_291_000.0, 602_000_000.0]


def test_a_changed_verdict_is_recorded_separately(tmp_path):
    path = tmp_path / "evidence.jsonl"
    append_entries([make_entry(verdict="open")], path)
    append_entries([make_entry(verdict="fired")], path)
    assert [e.verdict for e in read_entries(path)] == ["open", "fired"]


def test_existing_lines_are_never_rewritten(tmp_path):
    path = tmp_path / "evidence.jsonl"
    append_entries([make_entry()], path)
    first_line = path.read_text().splitlines()[0]
    append_entries([make_entry(accession="0001486957-26-000044")], path)
    assert path.read_text().splitlines()[0] == first_line


def test_missing_file_reads_as_empty(tmp_path):
    assert read_entries(tmp_path / "nothing.jsonl") == []


def test_corrupt_line_names_itself(tmp_path):
    """A bad line must not silently truncate history."""
    path = tmp_path / "evidence.jsonl"
    append_entries([make_entry()], path)
    path.write_text(path.read_text() + "{not json}\n")
    with pytest.raises(ValueError) as caught:
        read_entries(path)
    assert "line 2" in str(caught.value)


def test_latest_by_measure_picks_the_most_recent(tmp_path):
    entries = [
        make_entry(logged_at="2026-08-03T00:00:00+00:00", verdict="open"),
        make_entry(logged_at="2026-11-04T00:00:00+00:00", verdict="fired"),
        make_entry(measure_id="B1-commercial-revenue", verdict="survived"),
    ]
    latest = latest_by_measure(entries)
    assert latest["B3-government-revenue"].verdict == "fired"
    assert latest["B1-commercial-revenue"].verdict == "survived"


# -- seen documents ------------------------------------------------------


def test_seen_state_round_trips(tmp_path):
    path = tmp_path / "seen.json"
    state = load_seen(path)
    assert state["accessions"] == {}
    mark_seen(state, "0001486957-26-000041", "2026-08-03", "10-Q")
    save_seen(state, path)
    reloaded = load_seen(path)
    assert is_seen(reloaded, "0001486957-26-000041")
    assert not is_seen(reloaded, "0001486957-26-000099")


def test_corrupt_seen_file_does_not_crash_the_run(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("{ broken")
    state = load_seen(path)
    assert state["accessions"] == {}


def test_prune_keeps_the_newest(tmp_path):
    state = {"accessions": {}}
    for day in range(1, 11):
        mark_seen(state, f"acc-{day:02d}", f"2026-01-{day:02d}", "8-K")
    removed = prune_seen(state, keep=4)
    assert removed == 6
    assert set(state["accessions"]) == {"acc-10", "acc-09", "acc-08", "acc-07"}


def test_prune_is_a_noop_below_the_limit(tmp_path):
    state = {"accessions": {"a": {"filing_date": "2026-01-01"}}}
    assert prune_seen(state, keep=10) == 0


def test_save_seen_is_atomic(tmp_path):
    """A half-written file would make the next run reprocess everything or
    skip filings it never checked."""
    path = tmp_path / "seen.json"
    state = {"last_run": "2026-11-04", "accessions": {"a": {"filing_date": "2026-01-01"}}}
    save_seen(state, path)
    assert json.loads(path.read_text())["last_run"] == "2026-11-04"
    assert not list(tmp_path.glob("tmp*"))


# -- run summaries -------------------------------------------------------


def test_a_run_is_recorded_even_when_nothing_was_found(tmp_path):
    """This is what makes an empty digest trustworthy: 'nothing was filed' and
    'the job died at 4am' produce the same silence otherwise."""
    path = tmp_path / "runs.jsonl"
    summary = RunSummary(started_at="2026-11-04T09:00:00+00:00")
    summary.documents_checked = 9
    summary.measures_checked = 6
    append_run(summary, path)
    runs = read_runs(path)
    assert len(runs) == 1
    assert runs[0].entries_written == 0
    assert runs[0].documents_checked == 9


def test_verdict_tally(tmp_path):
    summary = RunSummary(started_at="2026-11-04T09:00:00+00:00")
    for verdict in ("open", "open", "fired"):
        summary.note_verdict(verdict)
    assert summary.verdicts == {"open": 2, "fired": 1}

"""The digest must never print silence it cannot vouch for."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import digest  # noqa: E402
import evidence_log  # noqa: E402
from evidence_log import Entry, RunSummary, append_entries, append_run  # noqa: E402


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(evidence_log, "EVIDENCE_PATH", data / "evidence.jsonl")
    monkeypatch.setattr(evidence_log, "DATA", data)
    (tmp_path / "theses.addresses.json").write_text(json.dumps({"measures": [
        {"measure_id": "B3-government-revenue", "thesis_id": "t-b3",
         "comparison": "yoy_growth", "resolves_by": "2027-02-28"},
        {"measure_id": "B4-naval-cadence", "thesis_id": "t-b4", "comparison": "manual",
         "coverage": "manual", "publisher_detail": "Navy 30-year shipbuilding plan"},
    ]}))
    (tmp_path / "theses.consolidated.json").write_text(json.dumps({"theses": [
        {"id": "t-b3", "claim": "Government Operations revenue grows at least 7%."},
        {"id": "t-b4", "claim": "Navy cadence holds."},
    ]}))
    monkeypatch.setattr(digest, "ADDRESSES", tmp_path / "theses.addresses.json")
    monkeypatch.setattr(digest, "THESES", tmp_path / "theses.consolidated.json")
    # digest imported the functions by name; point them at the temp files too
    monkeypatch.setattr(digest, "read_entries", lambda: evidence_log.read_entries(data / "evidence.jsonl"))
    monkeypatch.setattr(digest, "read_runs", lambda: evidence_log.read_runs(data / "runs.jsonl"))
    return data


def entry(verdict="open", logged_at="2026-09-20T09:00:00+00:00", **kw):
    base = dict(
        logged_at=logged_at, measure_id="B3-government-revenue", thesis_id="t-b3",
        ticker="BWXT", accession="acc", filing_date="", form="", period_start=None,
        period_end="2026-06-30", concept="Rev", axis="A", member="M",
        value=601_291_000.0, prior_value=None, computed=None, comparison="yoy_growth",
        threshold=0.07, verdict=verdict, detail="no reported periods yet",
        implied_detail="2 of 4 quarters reported, growth so far +3.1%, remainder must grow +10.7%",
    )
    base.update(kw)
    return Entry(**base)


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def test_never_run_says_so(workspace):
    text = digest.render(NOW)
    assert "never run" in text


def test_a_quiet_run_states_what_it_checked(workspace):
    """Silence must be distinguishable from a dead job."""
    run = RunSummary(started_at="2026-09-20T09:00:00+00:00", documents_checked=9,
                     documents_new=0, measures_checked=6)
    append_run(run, workspace / "runs.jsonl")
    append_entries([entry()], workspace / "evidence.jsonl")
    text = digest.render(NOW)
    assert "9 filings looked at" in text
    assert "Nothing changed." in text


def test_a_stale_run_is_flagged(workspace):
    run = RunSummary(started_at="2026-09-17T09:00:00+00:00", documents_checked=9)
    append_run(run, workspace / "runs.jsonl")
    assert "may have stopped" in digest.render(NOW)


def test_a_changed_verdict_is_reported(workspace):
    append_run(RunSummary(started_at="2026-11-04T09:00:00+00:00"), workspace / "runs.jsonl")
    append_entries([
        entry("open", "2026-09-20T09:00:00+00:00"),
        entry("fired", "2026-11-04T09:00:00+00:00", accession="acc-q3",
              detail="+4.0% compared with a year earlier, below 7%"),
    ], workspace / "evidence.jsonl")
    text = digest.render(datetime(2026, 11, 4, 12, tzinfo=timezone.utc))
    assert "CHANGED" in text
    assert "WRONG" in text
    assert "acc-q3" in text


def test_rest_of_year_arithmetic_is_shown(workspace):
    append_run(RunSummary(started_at="2026-09-20T09:00:00+00:00"), workspace / "runs.jsonl")
    append_entries([entry()], workspace / "evidence.jsonl")
    assert "remainder must grow +10.7%" in digest.render(NOW, full=True)


def test_quiet_morning_is_two_lines_not_a_page(workspace):
    """An agent writing a paragraph every morning trains you to ignore it
    within two weeks. When nothing changed, the note is the run line and
    'Nothing changed.' -- full status only on request."""
    append_run(RunSummary(started_at="2026-09-20T09:00:00+00:00", documents_checked=9),
               workspace / "runs.jsonl")
    append_entries([entry()], workspace / "evidence.jsonl")
    text = digest.render(NOW, full=False)
    assert "Nothing changed." in text
    assert "WHERE EACH CLAIM STANDS" not in text
    assert "--brief" in text


def test_a_change_shows_full_status_without_asking(workspace):
    append_run(RunSummary(started_at="2026-11-04T09:00:00+00:00"), workspace / "runs.jsonl")
    append_entries([
        entry("open", "2026-09-20T09:00:00+00:00"),
        entry("fired", "2026-11-04T09:00:00+00:00", accession="q3"),
    ], workspace / "evidence.jsonl")
    text = digest.render(datetime(2026, 11, 4, 12, tzinfo=timezone.utc))
    assert "WHERE EACH CLAIM STANDS" in text


def test_detail_is_written_as_a_sentence(workspace):
    append_run(RunSummary(started_at="2026-09-20T09:00:00+00:00"), workspace / "runs.jsonl")
    append_entries([entry()], workspace / "evidence.jsonl")
    text = digest.render(NOW, full=True)
    assert "Nothing filed since you wrote this claim tests it yet." in text
    assert "open. no reported" not in text


def test_manual_claims_are_listed_not_hidden(workspace):
    append_run(RunSummary(started_at="2026-09-20T09:00:00+00:00"), workspace / "runs.jsonl")
    text = digest.render(NOW, full=True)
    assert "Checked by hand" in text
    assert "Navy 30-year shipbuilding plan" in text

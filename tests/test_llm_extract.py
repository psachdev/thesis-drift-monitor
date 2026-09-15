"""Offline tests for the extraction path. No model call."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm_extract import (  # noqa: E402
    TableCandidate,
    coerce_number,
    find_segment_table,
    html_to_tables,
    parse_model_json,
    scale_factor,
)

RELEASE_HTML = """
<html><body>
<p>BWX Technologies Reports Second Quarter 2026 Results</p>
<p>Condensed Consolidated Statements of Operations</p>
<table>
  <tr><td></td><td>Q2 2026</td><td>Q2 2025</td></tr>
  <tr><td>Revenues</td><td>901,625</td><td>765,000</td></tr>
  <tr><td>Cost of operations</td><td>(700,000)</td><td>(600,000)</td></tr>
  <tr><td>Operating income</td><td>120,000</td><td>100,000</td></tr>
  <tr><td>Interest expense</td><td>(9,000)</td><td>(8,000)</td></tr>
  <tr><td>Net income</td><td>80,000</td><td>70,000</td></tr>
</table>
<p>Segment Revenues (in thousands)</p>
<table>
  <tr><td>Segment</td><td>Q2 2026</td><td>Q2 2025</td></tr>
  <tr><td>Government Operations</td><td>612,400</td><td>571,000</td></tr>
  <tr><td>Commercial Operations</td><td>289,225</td><td>194,000</td></tr>
</table>
</body></html>
"""

NO_HEADING_HTML = """
<html><body>
<table>
  <tr><td>Government Operations</td><td>612,400</td></tr>
  <tr><td>Commercial Operations</td><td>289,225</td></tr>
</table>
</body></html>
"""


def test_tables_are_found_with_headings():
    tables = html_to_tables(RELEASE_HTML)
    assert len(tables) == 2
    assert any("segment" in t.heading.lower() for t in tables)


SEGMENTS = ["Government Operations", "Commercial Operations"]


def test_segment_table_is_chosen_over_the_income_statement():
    """The income statement is bigger and contains 'Revenues'; the segment
    table contains no revenue word at all, only segment names and figures.
    Keyword matching picked the wrong one."""
    table = find_segment_table(html_to_tables(RELEASE_HTML), SEGMENTS)
    assert table is not None
    assert "Government Operations" in table.text
    assert "Interest expense" not in table.text


EBITDA_RECONCILIATION_HTML = """
<html><body>
<p>Three Months Ended June 30, 2026</p>
<table>
  <tr><td></td><td>Operating Income (GAAP)</td><td>Adjusted EBITDA</td></tr>
  <tr><td>Government Operations</td><td>105.7</td><td>126.5</td></tr>
  <tr><td>Commercial Operations</td><td>24.3</td><td>36.1</td></tr>
  <tr><td>Unallocated Corporate</td><td>(15.9)</td><td>(7.1)</td></tr>
</table>
</body></html>
"""


def test_ebitda_reconciliation_is_rejected():
    """BWXT's Q2 2026 release carries an Adjusted EBITDA reconciliation naming
    every segment and containing no revenue column. Selecting on segment names
    alone picked it, and the model correctly returned nulls for a table that
    could not answer the question."""
    tables = html_to_tables(EBITDA_RECONCILIATION_HTML)
    assert tables[0].mentions(SEGMENTS) == 2
    assert not tables[0].mentions_revenue
    assert find_segment_table(tables, SEGMENTS) is None


def test_revenue_table_wins_over_ebitda_table():
    combined = EBITDA_RECONCILIATION_HTML.replace("</body></html>", "") + """
<p>Segment Revenues (in thousands)</p>
<table>
  <tr><td>Segment</td><td>Q2 2026</td></tr>
  <tr><td>Government Operations</td><td>601,291</td></tr>
  <tr><td>Commercial Operations</td><td>302,512</td></tr>
</table>
</body></html>
"""
    table = find_segment_table(html_to_tables(combined), SEGMENTS)
    assert table is not None
    assert "601,291" in table.text
    assert "Adjusted EBITDA" not in table.text


def test_heading_alone_carries_the_revenue_signal():
    """A segment revenue table's rows are often just names and figures; the
    word 'revenue' appears only in the heading."""
    table = find_segment_table(html_to_tables(NO_HEADING_HTML), SEGMENTS)
    assert table is None  # no heading, no revenue word anywhere


def test_returns_none_when_no_table_names_the_segments():
    """No largest-table fallback. A wrong table produces well-formed, wrong
    JSON with no error anywhere -- the worst failure shape available."""
    assert find_segment_table(html_to_tables(RELEASE_HTML), ["Aviation", "Marine"]) is None


def test_single_mention_is_not_enough():
    candidate = TableCandidate(
        heading="Outlook", text="We expect Government Operations to grow.", row_count=1
    )
    assert find_segment_table([candidate], SEGMENTS) is None


def test_scale_factor_from_units_note():
    assert scale_factor("in thousands") == 1_000
    assert scale_factor("$ in millions") == 1_000_000
    assert scale_factor("in billions") == 1_000_000_000
    assert scale_factor(None) == 1
    assert scale_factor("unaudited") == 1


def test_coerce_number_handles_real_formats():
    assert coerce_number("612,400") == 612400
    assert coerce_number("$1,234.5") == 1234.5
    assert coerce_number("(9,000)") == -9000
    assert coerce_number(905001000) == 905001000
    assert coerce_number(None) is None
    assert coerce_number("n/a") is None
    assert coerce_number("") is None


def test_parse_model_json_tolerates_fences():
    assert parse_model_json('```json\n{"segments": {"A": 1}}\n```')["segments"] == {"A": 1}
    assert parse_model_json('{"segments": {}}') == {"segments": {}}


def test_parse_model_json_extracts_from_prose():
    payload = 'Here is the result:\n{"segments": {"A": 2}}\nHope that helps.'
    assert parse_model_json(payload)["segments"] == {"A": 2}


def test_parse_model_json_raises_rather_than_inventing():
    with pytest.raises(Exception):
        parse_model_json("I could not find a segment table.")


def test_scale_is_read_from_the_document_not_the_model():
    """Asked twice for the same Sterling release, the model reported the units
    differently: identical figures, a 1000x difference in the answer. The
    table states its own units, so they are parsed rather than asked for."""
    from llm_extract import scale_from_document

    factor, label = scale_from_document(
        "(Unaudited)(In millions, except per share amounts)\nRevenues\tGov\t601.3"
    )
    assert factor == 1_000_000
    assert "millions" in label

    factor, _ = scale_from_document("Segment Revenues (in thousands)\nGov\t601,291")
    assert factor == 1_000

    factor, _ = scale_from_document("Segment Revenues\nGov\t601,291,000")
    assert factor is None


def test_document_scale_beats_a_conflicting_model_claim():
    from llm_extract import scale_factor, scale_from_document

    document = "(In millions)\nGov\t601.3"
    doc_scale, _ = scale_from_document(document)
    assert doc_scale == 1_000_000
    assert scale_factor("in thousands") == 1_000
    assert doc_scale != scale_factor("in thousands")

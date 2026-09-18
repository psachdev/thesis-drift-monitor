#!/usr/bin/env python3
"""Read segment revenue out of an untagged earnings release, using a model.

This is the other half of the experiment. The XBRL path is deterministic: the
figure carries its own segment and period, and Python does the arithmetic. This
path has none of that. An EX-99.1 earnings release is HTML with no tagging at
all, so the only way to get a number out is to read it -- which is exactly the
job a model is for, and exactly where a model can be confidently wrong.

Design choices that matter:

- No largest-table fallback. If the segment table cannot be located by heading,
  this returns None. Handing the model the biggest table in the document yields
  well-formed JSON containing the wrong numbers, with no error anywhere.
- The model is given the segment names the company actually reports, from
  companies.json. Ungrounded, it invents plausible ones.
- The model never computes growth. It returns figures; comparison happens in
  resolver.py. Same rule as the XBRL path.
- Every returned figure must carry the row label it came from, so a wrong
  answer can be traced to a wrong row rather than guessed at.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from lxml import html as lxml_html

HERE = Path(__file__).resolve().parent
for candidate in (HERE.parent / "sec_data_downloader", HERE / "sec_data_downloader"):
    if (candidate / "secedgar").is_dir():
        sys.path.insert(0, str(candidate))
        break

DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
# Legacy aliases deepseek-chat and deepseek-reasoner were retired 2026-07-24.
DEFAULT_MODEL = os.environ.get("LLM_MODEL_SMART", "deepseek-v4-pro")

SEGMENT_HEADINGS = (
    "segment",
    "reportable segment",
    "business segment",
    "results by segment",
    "segment information",
    "segment results",
)

REVENUE_ROW_HINTS = ("revenue", "revenues", "sales", "net sales")


@dataclass
class TableCandidate:
    """One table from the release, with the heading that introduced it."""

    heading: str
    text: str
    row_count: int

    def mentions(self, segment_names: list[str]) -> int:
        """How many of the company's reported segments appear in this table."""
        lowered = self.text.lower()
        return sum(1 for name in segment_names if name.lower() in lowered)

    @property
    def mentions_revenue(self) -> bool:
        """Heading or body names a revenue measure.

        Checked against heading and body together because a segment revenue
        table often carries the word only in its heading or column header,
        while its rows are segment names and figures.
        """
        import re as _re

        # Heading plus the first few rows only. Scanning the whole table let a
        # footnote decide: Sterling's Adjusted Operating Income table ends with
        # "RHB's revenue is no longer included in consolidated revenue", which
        # counted as a revenue signal and won selection over the real revenue
        # table. A revenue table says so at the top, not in a footnote.
        head_rows = "\n".join(self.text.splitlines()[:4])
        haystack = f"{self.heading} {head_rows}".lower()
        # "% of Revenue" is a denominator in an operating-income table, not a
        # revenue measure. Sterling's Adjusted Operating Income table matched
        # on it and named every segment, so it won selection -- and the model
        # correctly reported that the table had no revenue row.
        haystack = _re.sub(r"%\s*of\s+revenues?", " ", haystack)
        haystack = _re.sub(r"percent(age)?\s+of\s+revenues?", " ", haystack)
        return any(hint in haystack for hint in REVENUE_ROW_HINTS)

    @property
    def heading_suggests_segments(self) -> bool:
        heading = self.heading.lower()
        return any(word in heading for word in SEGMENT_HEADINGS)


@dataclass
class ExtractionResult:
    """What the model returned, plus enough to audit it."""

    segments: dict[str, float] = field(default_factory=dict)
    row_labels: dict[str, str] = field(default_factory=dict)
    period_label: str | None = None
    units_note: str | None = None
    measure_read: str | None = None
    scale: float = 1.0
    scale_source: str = ""
    scale_conflict: bool = False
    model: str = ""
    raw: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.segments)


def html_to_tables(raw_html: str | bytes) -> list[TableCandidate]:
    """Every table in the document, paired with its nearest preceding heading."""
    tree = lxml_html.fromstring(raw_html)
    candidates: list[TableCandidate] = []

    for table in tree.iter("table"):
        rows = []
        for tr in table.iter("tr"):
            cells = [
                " ".join(cell.text_content().split())
                for cell in tr.iter("td", "th")
            ]
            if any(cells):
                rows.append("\t".join(cells))
        if not rows:
            continue

        heading = ""
        node = table
        for _ in range(40):
            node = node.getprevious() if node.getprevious() is not None else node.getparent()
            if node is None:
                break
            text = " ".join(node.text_content().split())[:200]
            if text and not text.isspace():
                heading = text
                break

        candidates.append(
            TableCandidate(heading=heading, text="\n".join(rows), row_count=len(rows))
        )
    return candidates


def find_segment_table(
    candidates: list[TableCandidate], segment_names: list[str] | None = None
) -> TableCandidate | None:
    """The segment REVENUE table, or None.

    Two signals, and both are needed. An earnings release carries several
    tables that name every segment: revenue by segment, operating income by
    segment, and an Adjusted EBITDA reconciliation. Selecting on segment names
    alone picked BWXT's EBITDA reconciliation, which has no revenue column at
    all -- the model correctly returned nulls for a table that could not
    answer the question.

    So a candidate must name at least two of the company's segments AND carry
    a revenue word somewhere in its heading or body.

    Returns None rather than guessing. There is deliberately no
    largest-table fallback: a wrong table produces confident, well-formed,
    wrong JSON with no error anywhere.
    """
    segment_names = segment_names or []
    scored: list[tuple[int, int, TableCandidate]] = []

    for candidate in candidates:
        hits = candidate.mentions(segment_names)
        if segment_names:
            # One mention could be a passing reference in prose.
            if hits < min(2, len(segment_names)):
                continue
        elif not candidate.heading_suggests_segments:
            continue
        if not candidate.mentions_revenue:
            continue
        scored.append((hits, -candidate.row_count, candidate))

    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2]


SYSTEM_PROMPT = """You read segment REVENUE from US earnings releases.

You will be given one table as tab-separated text, and the list of segment
names the company actually reports.

Extract REVENUE for each segment. Nothing else.

A segment table usually carries several measures per segment: revenue,
operating income, EBITDA, margin. Take REVENUE ONLY. Operating income is
typically much smaller than revenue for the same segment -- if the figure you
are about to return is a small fraction of the segment's scale, you are
probably on the wrong row. Re-read the row label before answering.

Return ONLY a JSON object, no prose, no markdown fences:

{
  "period_label": string or null,
  "units_note": string or null,
  "measure_read": string,
  "segments": {"<segment name>": number or null},
  "row_labels": {"<segment name>": "<the exact row label you read>"}
}

Rules:
- Use ONLY the segment names supplied. Do not invent, merge, split or rename.
- If a supplied segment has no revenue row in the table, set it to null. Do not
  substitute a different measure, and do not guess from a similar-sounding row:
  a product line may share a segment's name.
- Report the figure for the MOST RECENT period column only.
- "measure_read" must name the measure you took, e.g. "Revenues" or
  "Revenue". If the table has no revenue row at all, set every segment to null
  and set measure_read to "none found".
- Return figures in the units printed in the table. Put "in millions" or
  "in thousands" in units_note. Do not convert.
- Strip commas. Parentheses mean negative.
- row_labels must quote the row text you actually read, verbatim.
- Do not compute growth, margins, totals or any derived figure."""


def build_user_prompt(table_text: str, segments: list[str]) -> str:
    return (
        "Segment names this company reports:\n"
        + "\n".join(f"- {s}" for s in segments)
        + "\n\nTable:\n\n"
        + table_text
    )


def parse_model_json(content: str) -> dict:
    """Parse the model's reply, tolerating fences but not inventing structure."""
    text = content.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def coerce_number(value) -> float | None:
    """Numbers arrive as strings often enough that this has to be explicit."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "")
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


UNIT_PATTERNS = (
    (r"amounts\s+in\s+thousands?", 1_000.0),
    (r"amounts\s+in\s+millions?", 1_000_000.0),
    (r"\bin\s+billions?\b", 1_000_000_000.0),
    (r"\bin\s+millions?\b", 1_000_000.0),
    (r"\bin\s+thousands?\b", 1_000.0),
    (r"\$\s*(?:in\s+)?billions?\b", 1_000_000_000.0),
    (r"\$\s*(?:in\s+)?millions?\b", 1_000_000.0),
    (r"\$\s*(?:in\s+)?thousands?\b", 1_000.0),
)


def scale_from_document(text: str) -> tuple[float | None, str]:
    """Read the unit scale from the document itself.

    The scale must not come from the model. Asked twice for the same Sterling
    release, it reported the units differently on each run -- identical
    figures, a 1000x difference in the answer. The table states its own units
    ("(In millions, except per share amounts)"), so parse them.
    """
    import re as _re

    # Strip markup so the window is spent on visible text, not <head>.
    if "<" in text[:2000]:
        try:
            text = " ".join(lxml_html.fromstring(text).text_content().split())
        except Exception:  # noqa: BLE001
            pass

    haystack = text[:40000].lower()

    # Earliest occurrence wins, not the first pattern in the list. Returning
    # the first matching pattern meant "in millions" anywhere in a release
    # beat "in thousands" printed above the table, scaling every Sterling
    # figure by a further 1000.
    best = None
    for pattern, factor in UNIT_PATTERNS:
        match = _re.search(pattern, haystack)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), factor, match.group(0).strip())
    if best is None:
        return None, ""
    return best[1], best[2]


def scale_factor(units_note: str | None) -> float:
    if not units_note:
        return 1.0
    lowered = units_note.lower()
    if "million" in lowered:
        return 1_000_000.0
    if "thousand" in lowered:
        return 1_000.0
    if "billion" in lowered:
        return 1_000_000_000.0
    return 1.0


def call_model(
    table_text: str,
    segments: list[str],
    document_text: str | None = None,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.0,
) -> ExtractionResult:
    import requests

    api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_API_KEY")
    if not api_key:
        return ExtractionResult(error="DEEPSEEK_API_KEY not set", model=model)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(table_text, segments)},
        ],
        "temperature": temperature,
        # Reasoning models spend hidden chain-of-thought from the same budget
        # as the visible answer. Too small a budget returns HTTP 200 and an
        # empty string, with no error anywhere.
        "max_tokens": 8000,
    }

    try:
        response = requests.post(
            DEEPSEEK_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=180,
        )
        response.raise_for_status()
        body = response.json()
    except Exception as exc:  # noqa: BLE001
        return ExtractionResult(error=f"{type(exc).__name__}: {exc}", model=model)

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return ExtractionResult(
            error="unexpected response shape", raw=json.dumps(body)[:2000], model=model
        )

    if not content or not content.strip():
        return ExtractionResult(
            error="empty content (budget likely spent on hidden reasoning)",
            raw=json.dumps(body.get("usage", {})),
            model=model,
        )

    try:
        parsed = parse_model_json(content)
    except json.JSONDecodeError as exc:
        return ExtractionResult(error=f"unparseable JSON: {exc}", raw=content, model=model)

    # Document first, model second. If they disagree, the document wins and
    # the disagreement is recorded rather than silently resolved.
    # Units must come from the table the model actually read, not the wider
    # release. Widening the search to the whole document found a units note
    # belonging to a different statement and multiplied every Sterling figure
    # by a further 1000. Fall back to the release only if the table is silent.
    doc_scale, doc_label = scale_from_document(table_text)
    if doc_scale is None and document_text:
        doc_scale, doc_label = scale_from_document(document_text)
        doc_label = f"{doc_label}, from the release"
    model_scale = scale_factor(parsed.get("units_note"))
    scale = doc_scale if doc_scale is not None else model_scale
    scale_source = f"document ({doc_label})" if doc_scale is not None else "model"
    scale_conflict = (
        doc_scale is not None
        and parsed.get("units_note")
        and model_scale != doc_scale
    )
    segments_out: dict[str, float] = {}
    for name, value in (parsed.get("segments") or {}).items():
        number = coerce_number(value)
        if number is not None:
            segments_out[name] = number * scale

    return ExtractionResult(
        segments=segments_out,
        row_labels=parsed.get("row_labels") or {},
        period_label=parsed.get("period_label"),
        units_note=parsed.get("units_note"),
        measure_read=parsed.get("measure_read"),
        scale=scale,
        scale_source=scale_source,
        scale_conflict=bool(scale_conflict),
        model=model,
        raw=content,
    )


def extract_from_filing(client, filing, segments: list[str], model: str = DEFAULT_MODEL):
    """Fetch the earnings exhibit and extract segment revenue from it."""
    from secedgar import find_earnings_exhibit, list_documents

    documents = list_documents(client, filing)
    exhibit = find_earnings_exhibit(documents)
    if exhibit is None:
        return None, ExtractionResult(
            error="no earnings exhibit found in this filing", model=model
        )

    raw = client.get_bytes(exhibit.url)
    table = find_segment_table(html_to_tables(raw), segments)
    if table is None:
        return exhibit, ExtractionResult(
            error="no segment table located by heading", model=model
        )

    return exhibit, call_model(
        table.text, segments, document_text=raw.decode("utf-8", "ignore"), model=model
    )

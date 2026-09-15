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
    """The table a person would read for segment revenue, or None.

    Two signals, because neither alone is reliable. The heading often says
    "Segment Information"; the rows often do not contain the word "revenue" at
    all, being just segment names and figures. So the stronger signal is the
    company's own reported segment names, which we already have grounded in
    companies.json.

    Returns None rather than guessing. There is deliberately no
    largest-table fallback: a wrong table produces confident, well-formed,
    wrong JSON with no error anywhere.
    """
    segment_names = segment_names or []
    scored: list[tuple[int, int, TableCandidate]] = []

    for candidate in candidates:
        hits = candidate.mentions(segment_names)
        if segment_names:
            # Need at least two of the company's segments present. One could
            # be a passing mention in prose or a single-line reference.
            if hits < min(2, len(segment_names)):
                continue
        elif not candidate.heading_suggests_segments:
            continue
        scored.append((hits, -candidate.row_count, candidate))

    if not scored:
        return None
    # Most segments named wins; ties go to the smaller table, because a large
    # match is usually a whole statement that happens to mention segments.
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2]


SYSTEM_PROMPT = """You read segment tables from US earnings releases.

You will be given one table as tab-separated text, and the list of segment
names the company actually reports.

Return ONLY a JSON object, no prose, no markdown fences:

{
  "period_label": string or null,
  "units_note": string or null,
  "segments": {"<segment name>": number or null},
  "row_labels": {"<segment name>": "<the exact row label you read>"}
}

Rules:
- Use ONLY the segment names supplied. Do not invent, merge, split or rename.
- If a supplied segment does not appear in the table, set it to null. Do not
  guess from a similar-sounding row: a product line may share a segment's name.
- Report the figure for the MOST RECENT period column only.
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
        "max_tokens": 4000,
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

    scale = scale_factor(parsed.get("units_note"))
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

    return exhibit, call_model(table.text, segments, model=model)

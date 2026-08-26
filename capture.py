#!/usr/bin/env python3
"""
capture.py — Module 1 of the Thesis Drift Monitor.

Reads a research document (PDF or text file) and extracts the investment
theses it contains, as structured, falsifiable records.

Usage:
    python capture.py sources/Epic_BWXT.pdf --ticker BWXT
    python capture.py sources/Epic_STRL.pdf --ticker STRL --price 603.89
    python capture.py note.txt --ticker BWXT --profile demo

Design notes:
  - One document can contain many theses. A bull/bear piece may have eight.
    The extractor always returns a list.
  - No verbatim source text is ever stored. Claims are paraphrased by the
    model into our own wording. Source PDFs stay outside the repo.
  - A thesis without kill criteria is rejected. That field is the entire
    point of the exercise.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from llm import MODEL_FAST, LLMError, coerce_list, complete_json

DEFAULT_MODEL = MODEL_FAST

# Roughly 40k characters keeps us well inside context while covering a long
# research note. Long documents are truncated with a warning rather than
# silently cut.
MAX_CHARS = 40_000

# Output budget. Must cover the model's hidden reasoning tokens as well as the
# JSON we actually want. See the comment in call_deepseek().
MAX_TOKENS = 16_000

REPO_ROOT = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

# Kept byte-identical across every call so DeepSeek's context cache can hit.
# Do not reformat casually — even whitespace changes break the cache.
SYSTEM_PROMPT = """You extract investment theses from research documents.

A thesis is a claim about a company's future that could turn out to be wrong. \
Your job is to find every distinct thesis in the document and make each one \
testable.

Return ONLY a JSON object. No prose, no markdown fences, no preamble.

Schema:
{
  "theses": [
    {
      "claim": "One sentence. What is asserted about the company's future. \
Write it in your own words - never copy sentences from the document.",
      "direction": "bull" or "bear",
      "mechanism": "One sentence. Why the author thinks this will happen - \
the causal chain.",
      "kill_criteria": [
        "A specific, checkable observation that would show this claim is wrong. \
Must reference something reportable: a metric, a disclosure, a decision, an \
event. Not a feeling or a price move."
      ],
      "check_horizon": "When evidence should be expected. Use an ISO date if \
the document implies one, otherwise a phrase like 'next two earnings calls'.",
      "supporting_facts": [
        "A fact the document cites in support, in your own words. Include \
numbers where given."
      ],
      "confidence_in_extraction": "high" or "medium" or "low"
    }
  ],
  "document_type": "recommendation" or "bull_bear_analysis" or "news" or \
"quote_page" or "other",
  "notes": "Anything that made extraction hard, or an empty string."
}

Rules:
- Paraphrase everything. Never reproduce sentences from the source.
- A bull/bear piece contains multiple theses. Extract each separately.
- Every thesis needs at least one kill criterion. If you cannot write a \
checkable one, set confidence_in_extraction to "low" and explain in notes.
- Do not invent theses. A price quote page usually contains none - return an \
empty list.
- Kill criteria must be falsifiable by observation, not by opinion. \
"The stock falls" is not a kill criterion. "Segment revenue share declines \
for two consecutive quarters" is."""


# --------------------------------------------------------------------------
# Input adapters
# --------------------------------------------------------------------------

@dataclass
class SourceDoc:
    text: str
    filename: str
    kind: str


def read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        sys.exit("pypdf not installed. Run: pip install -r requirements.txt")

    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:  # a single bad page shouldn't kill the run
            print(f"  ! could not read a page: {exc}", file=sys.stderr)
    return "\n\n".join(pages)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def clean(raw: str) -> str:
    """Normalise the mess that comes out of print-to-PDF."""
    # Ligatures and smart quotes -> ascii equivalents. Fidelity and Fool PDFs
    # both produce these, and they show up as 'lifed' for 'lifted' etc.
    text = unicodedata.normalize("NFKD", raw)
    text = text.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    # Collapse runs of blank lines and stray whitespace.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def load_source(path: Path) -> SourceDoc:
    if not path.exists():
        sys.exit(f"No such file: {path}")

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        raw, kind = read_pdf(path), "pdf"
    elif suffix in {".txt", ".md"}:
        raw, kind = read_text(path), "text"
    else:
        sys.exit(
            f"Unsupported file type: {suffix}\n"
            "Supported: .pdf, .txt, .md\n"
            "For a screenshot, print or export it to PDF first."
        )

    text = clean(raw)
    if not text:
        sys.exit("Extracted no text. If this is a scanned image PDF, it needs OCR.")

    if len(text) > MAX_CHARS:
        print(f"  ! document is {len(text):,} chars, truncating to {MAX_CHARS:,}")
        text = text[:MAX_CHARS]

    return SourceDoc(text=text, filename=path.name, kind=kind)


# --------------------------------------------------------------------------
# Model call
# --------------------------------------------------------------------------

def call_deepseek(doc: SourceDoc, ticker: str, model: str) -> dict:
    user_content = (
        f"Ticker under analysis: {ticker}\n"
        f"Source filename: {doc.filename}\n\n"
        f"Document:\n{doc.text}"
    )
    try:
        result = complete_json(SYSTEM_PROMPT, user_content, model)
    except LLMError as exc:
        sys.exit(str(exc))

    usage = result.pop("_usage", {})
    print(f"  tokens: {usage.get('in')} in / {usage.get('out')} out"
          + (f"  (cache hit: {usage['cached']})" if usage.get("cached") else ""))
    if usage.get("hit_ceiling"):
        print("  ! completion hit the max_tokens ceiling - output may be truncated")
    return result


def parse_json(content: str) -> dict:
    """Models sometimes wrap JSON in fences despite being told not to."""
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)

    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        print("\n--- raw model output ---", file=sys.stderr)
        print(content[:2000], file=sys.stderr)
        print("--- end ---\n", file=sys.stderr)
        sys.exit(f"Model did not return valid JSON: {exc}")


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

REQUIRED = ("claim", "mechanism", "kill_criteria")

LIST_FIELDS = ("kill_criteria", "supporting_facts")


def coerce_lists(thesis: dict) -> dict:
    """The model declares these as arrays but sometimes returns a bare string.

    Schema compliance turns out to be per-field: kill_criteria came back as a
    proper list on every thesis in the same response where supporting_facts
    came back as a string. Iterating a string yields characters, so this has
    to be normalised before anything downstream touches it.
    """
    for field in LIST_FIELDS:
        value = thesis.get(field)
        if value is None:
            thesis[field] = []
        elif isinstance(value, str):
            # Split on newlines or sentence-ish boundaries if it looks like
            # several items were flattened; otherwise keep it as one item.
            parts = [p.strip(" -•\t") for p in value.split("\n") if p.strip()]
            thesis[field] = parts if len(parts) > 1 else [value.strip()]
            thesis.setdefault("_coerced", []).append(field)
        elif not isinstance(value, list):
            thesis[field] = [str(value)]
            thesis.setdefault("_coerced", []).append(field)
        else:
            thesis[field] = [str(v) for v in value]
    return thesis


def validate(thesis: dict) -> list[str]:
    problems = []
    for field in REQUIRED:
        if not thesis.get(field):
            problems.append(f"missing {field}")

    kills = thesis.get("kill_criteria") or []
    if isinstance(kills, list) and not kills:
        problems.append("no kill criteria")

    # A kill criterion phrased as a price move is not checkable against
    # filings or news, which is what the nightly job reads.
    for k in kills:
        if isinstance(k, str) and re.search(
            r"\b(stock|share price|shares) (falls|drops|declines|goes)", k.lower()
        ):
            problems.append(f"price-based kill criterion: {k[:60]}")

    return problems


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-")


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def show(thesis: dict, index: int, problems: list[str]) -> None:
    mark = "!" if problems else " "
    print(f"\n{mark} [{index}] {thesis.get('direction', '?').upper()}")
    print(f"    claim      : {thesis.get('claim', '')}")
    print(f"    mechanism  : {thesis.get('mechanism', '')}")
    print(f"    horizon    : {thesis.get('check_horizon', '')}")
    print(f"    confidence : {thesis.get('confidence_in_extraction', '')}")
    print("    kill criteria:")
    for k in thesis.get("kill_criteria") or []:
        print(f"      - {k}")
    facts = thesis.get("supporting_facts") or []
    if facts:
        print("    supporting facts:")
        for f in facts[:4]:
            print(f"      - {f}")
    if thesis.get("_coerced"):
        print(f"    (coerced to list: {', '.join(thesis['_coerced'])})")
    if problems:
        print(f"    PROBLEMS: {', '.join(problems)}")


def save(thesis: dict, ticker: str, source_name: str, price: float | None,
         out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    slug = slugify(thesis.get("claim", "untitled"))
    thesis_id = f"{ticker.lower()}-{slug}-{today}"

    record = {
        "id": thesis_id,
        "ticker": ticker.upper(),
        "created_at": today,
        "source": {
            # Filename only. The document itself stays out of the repo, and
            # no verbatim text from it is ever stored.
            "reference": source_name,
            "type": "research_document",
        },
        "claim": thesis.get("claim"),
        "direction": thesis.get("direction"),
        "mechanism": thesis.get("mechanism"),
        "kill_criteria": thesis.get("kill_criteria"),
        "check_horizon": thesis.get("check_horizon"),
        "supporting_facts": thesis.get("supporting_facts"),
        "price_at_capture": price,
        "position": "watching",
        "status": "active",
        "evidence_log": [],
    }

    path = out_dir / f"{thesis_id}.json"
    record.pop("_coerced", None)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Extract investment theses from a document.")
    ap.add_argument("document", type=Path, help="PDF, TXT or MD file")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--price", type=float, default=None,
                    help="Price at capture. Take it from the quote page.")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--profile", default="real", choices=["real", "demo"])
    ap.add_argument("--dry-run", action="store_true",
                    help="Extract and print, but save nothing.")
    args = ap.parse_args()

    out_dir = REPO_ROOT / "data" / f"theses.{args.profile}"

    print(f"profile : {args.profile}")
    print(f"model   : {args.model}")
    print(f"source  : {args.document}")

    doc = load_source(args.document)
    print(f"  read {len(doc.text):,} chars from {doc.kind}")

    result = call_deepseek(doc, args.ticker, args.model)
    theses = [coerce_lists(t) for t in (result.get("theses") or [])]

    print(f"\ndocument type : {result.get('document_type', '?')}")
    if result.get("notes"):
        print(f"notes         : {result['notes']}")
    print(f"theses found  : {len(theses)}")

    if not theses:
        print("\nNothing to save. Quote pages and headline lists usually "
              "contain no thesis - that is the correct result, not a failure.")
        return

    graded = [(t, validate(t)) for t in theses]
    for i, (thesis, problems) in enumerate(graded, 1):
        show(thesis, i, problems)

    if args.dry_run:
        print("\n(dry run - nothing saved)")
        return

    clean_ones = [t for t, p in graded if not p]
    flagged = len(graded) - len(clean_ones)

    print(f"\n{len(clean_ones)} clean, {flagged} flagged.")
    choice = input("Save which? [a]ll / [c]lean only / [n]one: ").strip().lower()

    if choice.startswith("n") or not choice:
        print("Nothing saved.")
        return
    to_save = [t for t, _ in graded] if choice.startswith("a") else clean_ones

    for thesis in to_save:
        path = save(thesis, args.ticker, doc.filename, args.price, out_dir)
        print(f"  wrote {path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()

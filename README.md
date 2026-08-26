# Thesis Drift Monitor

**Turn investment research into falsifiable claims, then check whether they hold up.**

You read an analyst note, you find it convincing, you buy the stock. Six months
later you still own it — but do you still believe the reason?

Most people cannot answer that, because the reason was never written down. It
lived in your head as a feeling, and feelings drift. This is a tool for writing
the reason down precisely enough that reality can eventually tell you whether
you were right.

This repository is **Module 1 of 7**: capture and audit. It reads a research
document, extracts the claims it makes, and checks whether those claims are
written in a way anyone could ever test.

---

## What it does today, and what it does not

**Today — entirely offline, nothing leaves your machine:**

```
Your PDF  →  capture.py  →  thesis JSON  →  audit.py
                                           "is this checkable?"
```

**Not yet built — Module 2 adds the evidence half:**

```
thesis JSON  →  fetch SEC + news  →  compare  →  morning digest
                                    "does it hold?"
```

`audit.py` judges the *sentence*, not the company. It never fetches a filing,
never reads the news, never tells you whether your thesis is true. It tells you
whether it is the kind of thesis that could ever be tested.

**This does not yet run nightly.** If you came here wanting a monitoring tool,
that is Module 2 and it does not exist yet.

---

## What broke while building it

The interesting part is not the code. It is what an LLM does when you point it
at real documents and then check its work.

**1. HTTP 200 and an empty string.** The first successful API call returned
nothing. Reasoning models produce hidden chain-of-thought that shares the
`max_tokens` budget with the visible answer. The model spent all 4,000 tokens
thinking and stopped before writing a character. `max_tokens` is not the answer
length — it is thinking plus answer, and the thinking scales with task
difficulty.

**2. Schema compliance is per-field and intermittent.** In one response
`kill_criteria` came back as a proper array while `supporting_facts` came back
as a bare string. Same schema, same call. Five of six runs were fine. Anything
you declare as a list needs a type check at the boundary.

**3. The same document, six times, gave 6 to 9 claims.** Temperature 0.2.
Output tokens ranged from 5,431 to 14,964. One run explicitly refused to extract
a thesis that another run extracted and rated medium confidence.

**4. The auditor invented facts.** It claimed the US Navy has no nuclear
battleship program (CBO has scored the BBGN class at roughly $275B for 15 ships)
and that the company does not disclose segment backlog (it does). Both fabrications
were used as the sole basis for killing a criterion — impeccable reasoning from
false premises, wearing the authority of a review step. `companies.json` and
declared `factual_assumptions` exist because of this.

**5. Telling the model to be skeptical changed which flags it produced, but not
by as much as expected.** Adversarial framing produced 41 flags across 45
criteria; neutral framing produced 36. The interesting part is the breakdown:
judgment-based flags (`doesnt_settle`) fell from 21 to 14, while fact-based
flags (`unobservable`, `wrong_level`) barely moved. **An LLM's factual judgments
are stable across prompting; its interpretive judgments are not.** Trust the
first kind. Treat the second as an opinion that could have gone either way.

Total API cost for all of the above, including every failed run: **about $1.50.**

---

## Quickstart

Requires Python 3.11+ and an API key.

```bash
git clone https://github.com/psachdev/thesis-drift-monitor.git
cd thesis-drift-monitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export DEEPSEEK_API_KEY=sk-...
```

**Try it on the demo data — no research subscription needed:**

```bash
python3 consolidate.py --profile demo --source demo/theses.demo.json
python3 audit.py --profile demo --framing neutral
```

Four theses built from public sources. **Two are deliberately broken.** See
whether the audit catches them; the answers are in each file's `notes` field.

**Then on your own research:**

See [Using it on any company](#using-it-on-any-company) below.

---

## Using it on any company

The tool works on any ticker and any research document you legally have. Three
steps the first time, two after that.

### Step 1 — Teach it about the company (once per ticker)

`audit.py` needs to know what a company actually discloses before it can tell a
checkable criterion from an invented one. Without an entry it guesses, and
guessing is where it fabricates.

```bash
python3 bootstrap.py NVDA
python3 bootstrap.py NVDA --name "NVIDIA Corporation"   # if the ticker is ambiguous
```

| Argument | Required | What it does |
|---|---|---|
| `TICKER` | yes | The ticker symbol, e.g. `NVDA` |
| `--name` | no | Full company name, when the ticker alone is ambiguous |
| `--model` | no | Defaults to the smart tier |
| `--force` | no | Redraft an entry that already exists |

This writes an entry to `companies.json` marked **`"verified": false`**.

**Then verify it.** Open the company's latest 10-K, confirm the segment names
are the ones the company actually uses, correct anything wrong, and set
`"verified": true`. `audit.py` warns on every run until you do.

This takes about five minutes and it is the single highest-value thing you can
do for output quality. Skipping it means every audit verdict rests on whatever
the model half-remembered about that company's reporting.

### Step 2 — Capture theses from a document

```bash
python3 capture.py sources/nvda-research.pdf --ticker NVDA --price 187.50
```

| Argument | Required | What it does |
|---|---|---|
| `document` | yes | Path to a `.pdf`, `.txt`, or `.md` file |
| `--ticker` | yes | Ticker the document is about |
| `--price` | no | Share price when you captured it. Take it from a quote page — this is what makes a source scorecard possible later |
| `--profile` | no | `real` (default) or `demo` |
| `--model` | no | Defaults to the fast tier; extraction does not need the smart one |
| `--dry-run` | no | Extract and print, save nothing. Use this first |

**Supported inputs:** PDF, plain text, markdown. For a screenshot, print or
export it to PDF first. There is no OCR, so scanned image PDFs will not work.

The tool never fetches a URL. It reads the file you hand it, which keeps
paywalled research on your machine where it belongs.

**One document can contain many theses.** A bull/bear piece may yield eight; a
single recommendation may yield one; a price quote page yields none. Zero is a
correct answer, not a failure.

You are prompted before anything is saved — `[a]ll`, `[c]lean only`, or
`[n]one`. Read the output before answering. "Clean" only means the required
fields are present and no criterion is phrased as a price move; it is not a
quality judgment.

### Step 3 — Audit the criteria

```bash
python3 audit.py --ticker NVDA --framing neutral
```

| Argument | Required | What it does |
|---|---|---|
| `--ticker` | no | Audit one ticker only; omit to audit everything saved |
| `--framing` | no | `neutral` (recommended) or `adversarial` |
| `--profile` | no | `real` (default) or `demo` |
| `--limit` | no | Stop after N theses. Useful for a cheap first look |
| `--model` | no | Defaults to the smart tier, which is measurably better here |

Results are written to `data/review.<profile>.<framing>.json`.

Read the `ASSUMES:` lines. Every fact the model supplied itself is listed there,
and any verdict resting on one may be wrong. When you verify a fact, put it in
`companies.json` so the guess never recurs.

### Step 4 — Consolidate what survives

Three documents about two companies gave 22 theses, most of them the same
uncertainty stated twice. Merge duplicates, cut the untestable, quantify the
vague, and write the survivors to `theses.consolidated.json`. Then:

```bash
python3 consolidate.py --dry-run
python3 consolidate.py
```

Originals are archived to `data/theses.raw/`, never deleted. The cut list is
saved with reasons — a decision without its reason is not reviewable six months
later.

### Adding more companies

Repeat step 1 per ticker. `companies.json` grows as you go, and every verified
fact makes future audits better for that company permanently. The file is the
accumulating asset here, not the code.

---

## Kill criteria: the idea the whole thing rests on

A **kill criterion** answers one question: *what would have to happen for me to
admit I was wrong?*

If you cannot answer it, you do not have a thesis. You have a hope, and hopes
survive any amount of bad news.

A criterion needs three things: a specific observation, **a publisher who
releases it on a schedule**, and a date by which you would expect to know. The
publisher does not have to be the company — a regulator or a government agency
counts.

### The six ways a kill criterion fails

| Failure | What's broken | Example |
|---|---|---|
| **Wrong signal** | Measures share price instead of the business | "Stock underperforms peers" — a stock moves for a hundred reasons unrelated to your claim |
| **Unobservable** | No document from any publisher reports it on a schedule | "Management reports training is behind schedule" — no company publishes this. Worse than a missing criterion, because it passes validation and then never resolves |
| **Wrong level** | Real reported figure, wrong scope | Claim about one segment, criterion measures consolidated results. Other segments move the total and give a false verdict |
| **Doesn't settle it** | Happens or doesn't, you still don't know | "The company wins Contract X" — the award might not touch revenue for years |
| **Timeframe mismatch** | Criterion runs on a shorter clock than the claim | Two quarters cannot confirm a claim about a durable multi-year advantage |
| **Bundled claim** | Several predictions in one sentence | "Growth slows AND margins compress AND cash flow suffers" — no single criterion kills it |

The five modes are what `audit.py` checks. The sixth belongs to the claim rather
than the criterion, and shows up in the `claim_note` field.

---

## The consolidation result

Three research documents produced **22 theses**. Under neutral audit, 9 of 45
criteria passed — about 20%.

Consolidating those 22 into **6 by hand** — merging duplicates, cutting the
untestable, quantifying the vague — took the pass rate to roughly 67%.

But the audit then caught two logic errors introduced *during* that hand
consolidation: a claim that a *beat* would have falsified, and an each/both
quantifier mismatch made twice in adjacent theses while actively looking for it.

**The human and the model catch different classes of error.** The model cannot
tell that a company discloses segment backlog. You cannot tell that you wrote
"each" and "both" in the same breath.

---

## Files

| File | Purpose |
|---|---|
| `capture.py` | Read a PDF or text file, extract theses with kill criteria |
| `audit.py` | Grade criteria against the five failure modes; tag coverage separately |
| `bootstrap.py` | Draft a `companies.json` entry for a new ticker (then verify it) |
| `consolidate.py` | Archive raw extractions, install a reviewed set |
| `llm.py` | Shared API client; provider-configurable |
| `companies.json` | What each company actually discloses — the grounding that stops fabrication |
| `skill/` | Same methodology as a Claude Skill, no Python or API key needed |

### Using a different provider

The DeepSeek API is OpenAI-compatible, so anything speaking that protocol works
without code changes:

```bash
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_API_KEY=sk-...
export LLM_MODEL_FAST=gpt-4o-mini
export LLM_MODEL_SMART=gpt-4o
```

---

## The build order

Not a roadmap — a sequence of problems, each arriving because the previous step
hit a wall. The later modules are not written yet and the order may change.

1. **Capture** — turn documents into falsifiable claims *(this repo)*
2. **The nightly loop** — because nothing checks the theses once they exist
3. **Retrieval** — because the answers are in 200-page filings, not headlines
4. **Tool use** — because the agent should call your existing analysis scripts
5. **Multiple models** — because one model agrees with whatever you fed it
6. **Evaluation** — because you cannot improve what you have not measured
7. **The scorecard** — which sources actually helped, measured on your own behaviour

---

## Honest limitations

**Testable claims cluster around published guidance.** Nearly every surviving
thesis reduces to "does the company hit its own forecast," because guidance is
the only forward-looking number published on a schedule. The bigger, more
interesting claims — moats, management quality, multi-decade cycles — were cut
precisely because they do not reduce to a checkable number.

The tool tests what is testable, not what matters most. That is a real cost, not
a detail.

**The auditor is close kin to the extractor.** The same model family that wrote
these criteria is grading them. It catches obvious failures and misses subtle
ones. Module 5 addresses this with genuinely different models.

**Source documents never enter this repo.** Paywalled research stays on your
machine. `sources/` and `data/theses.real/` are gitignored. Do not commit them.

---

## Licence

MIT. See [LICENSE](LICENSE).

Nothing here is investment advice. The tickers in the examples are ones I follow;
the point is the tooling, not the picks.

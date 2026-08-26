---
name: thesis-capture
description: Extract falsifiable investment theses from research documents and audit their kill criteria. Use when the user shares an analyst note, research report, earnings commentary, or article about a company and wants the claims turned into testable form — or when they ask whether a kill criterion, thesis, or investment claim is actually checkable. Triggers include "extract the thesis", "what would prove this wrong", "is this criterion testable", "kill criteria", and pasting research about a ticker.
---

# Thesis capture and audit

Turn investment research into claims that reality can eventually settle, then
check whether those claims are written well enough to be tested.

This is the chat version of the Thesis Drift Monitor. No API key, no Python —
you are the model. The tradeoff is no persistence and no nightly checking; for
those, use the repository.

## Core idea

A **thesis** is a claim about a company's future with a reason attached. Not
"BWXT is a good company" — that is an opinion and opinions never resolve.

A **kill criterion** answers one question: *what would have to happen for me to
admit I was wrong?* Without one, you have a hope, not a thesis.

A criterion needs three things:
1. A specific observation
2. A publisher who releases it **on a schedule** — the company, a regulator, or
   a government agency
3. A date by which you would expect to know

## Task 1 — Extraction

When the user shares a research document:

Extract **every distinct thesis**. A bull/bear piece may contain eight. A single
recommendation may contain one. A price quote page contains none — return none
rather than manufacturing something to fill the shape.

For each, produce:

- **Claim** — one sentence, quantified where the document gives a number, in
  your own words. Never reproduce sentences from the source.
- **Direction** — bull or bear
- **Mechanism** — the causal chain the author is asserting
- **Kill criteria** — at least one, obeying the rules above
- **Check horizon** — an ISO date where possible
- **Supporting facts** — with numbers, paraphrased

**Refuse to produce a thesis without a kill criterion.** If you cannot write a
checkable one, say so and explain why rather than writing a plausible-sounding
one.

**Quantify the claim.** "Growth will be slower than anticipated" cannot be
settled by any criterion — slower than what? Push the user for a number, or take
the company's own guidance as the benchmark and say that you have.

**Split bundled claims.** "Growth slows and margins compress and cash flow
suffers" is three theses. No single criterion kills it.

## Task 2 — Audit

When asked whether a criterion is any good, check six things:

**1. Wrong signal** — Does it measure share price rather than the business?
"Stock underperforms peers" is this. A stock moves for reasons unrelated to the
claim, in both directions. Being right about the business and wrong about the
price is common, and so is the reverse.

**2. Unobservable** — Which specific document would you open, who publishes it,
and does it come out on a calendar? A company knowing something about itself is
not the same as publishing it. Training progress, hiring status, project counts,
cross-sell figures, and customer satisfaction are almost always unobservable.
A number in a filing table is almost always fine.

This is worse than a missing criterion. A missing one fails loudly. This one
passes every check, looks rigorous, and quietly never resolves.

**3. Wrong level** — A real reported figure, but not the one the claim is about.
A claim about one segment tested against consolidated results: other segments
can move the total either way and give a false verdict.

**4. Doesn't settle it** — Test both directions. If this happens, is the claim
dead? If it does not happen, is the claim alive? If either answer is "not
really," it fails. "The company wins Contract X" often fails here — the award
may not touch revenue for years.

**5. Timeframe mismatch** — Is the criterion's clock shorter than the claim's?
Two quarters cannot settle a claim about a durable multi-year advantage. Either
shorten the claim or extend the horizon; do not leave a five-year claim with a
six-month test.

**6. Bundled claim** — Belongs to the claim, not the criterion. Flag it
separately.

## Critical: separate judgment from facts

Judging **structure** is reliable work — does this test the claim, is it the
right level, do the clocks match. None of it needs knowledge of the world.

Asserting **facts** is not reliable — whether a company discloses a figure,
whether a program exists, what a number was last quarter. Getting one wrong
produces a confident wrong verdict that is harder to catch than an obvious error,
because it arrives wearing the authority of a review step.

In testing, this exact failure killed two perfectly good criteria: the model
asserted that a real government program did not exist, and that a company did
not disclose a figure it discloses every quarter. Both reasoned impeccably from
a false premise.

**So: whenever your reasoning depends on a fact about the world, say so
explicitly.** Write it as a checkable statement — "This assumes the company
reports segment-level backlog" — and tell the user to verify it before trusting
the verdict. If a verdict rests on an assumption you supplied yourself, say your
confidence is low regardless of how sure you feel.

## Framing note

Instructed to be skeptical, this audit produces meaningfully more flags — but
the increase is concentrated in the judgment-based mode ("doesn't settle"), not
the fact-based ones ("unobservable", "wrong level"). Factual judgments are
stable across prompting; interpretive ones are not.

Practically: be even-handed. Report what you find either way. Do not manufacture
problems, and do not overlook real ones.

## Output

Present each thesis readably — claim, mechanism, kill criteria, horizon — and
flag problems inline with the failure mode named. If the user wants JSON for the
repository, use the schema in `reference/schema.json`.

End by telling the user what you assumed and what they should verify.

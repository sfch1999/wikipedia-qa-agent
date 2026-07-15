# Design

A CLI question-answering agent: Claude plus a single `search_wikipedia(query)`
tool, with an eval suite that measures it across question types and tracks the
effect of each change. This document explains the choices and the iteration
history. (Built in roughly 5 hours, on and off between other work.)

## Approach

**System.** `main.py` takes a question, `agent.py` runs a manual tool-use loop
against the Messages API, and `search_wikipedia` hits the live MediaWiki API and
returns the top results with intro extracts. The loop is written by hand (no
agent framework) for three reasons: it keeps the tool surface to exactly one
tool, it makes the trace of tool calls/queries a first-class return value (the
CLI and the eval both read it), and with hosted search tools ruled out by the
assignment, a hand-written loop is the simplest thing that satisfies the
constraint — a framework earns nothing at this size. The
loop appends the assistant turn verbatim, executes every `tool_use` block, feeds
all `tool_result`s back in one user turn, and stops when `stop_reason` is not
`tool_use`, with a turn cap as a backstop.

**Model choice.** The agent is `claude-sonnet-4-6` — my choice, made because
eval-driven iteration means re-running the full suite after every change, and
Sonnet keeps that loop fast and cheap while remaining a strong tool-use model.
Its cutoffs shaped the eval: reliable knowledge to Aug 2025, training data to
Jan 2026, so the "recent" category targets events after those dates to force a
search. The judge is `claude-opus-4-8` — a stronger, independent grader. Using
the agent to grade itself risks self-bias, and the judge audit (below) showed
the choice of judge materially changes the numbers, so the grader is pinned to a
different, more capable model.

## Eval design

**27 questions, 6 categories**, each stressing one behavior:

- **single_hop** (5) — one lookup, closed-form fact. Baseline retrieval.
- **multi_hop** (5) — needs 2+ searches and a composition/comparison.
- **ambiguous_entity** (5) — terms with several referents; tests disambiguation.
- **unanswerable** (5) — false premises and "the honest answer is none"; the
  correct behavior is to reject the premise, not fabricate.
- **recent** (5) — post-cutoff outcomes; the agent *must* search to answer.
- **no_search** (2) — pure computation; the agent should *not* search. Catches
  over-searching.

**Gold verification.** Stable facts were written directly; volatile and
post-cutoff golds (recent outcomes, populations, award counts) were verified
against live Wikipedia on 2026-07-10 before any run.

**Programmatic vs LLM-judge split.** Deterministic checks run where they are
reliable: closed-form match on `acceptable_answers` (normalized, matched on word
boundaries so `au` does not hit "Australia" and `0` does not hit "2010"),
search behavior from the trace (did it search, how many, and for `no_search`
whether it correctly did not), and a heuristic premise-pushback flag for
false-premise items. The LLM judge handles what regex cannot: correctness as
*equivalence to the gold* (the judge is given the gold and checks equivalence,
it does not answer), groundedness of the answer in the retrieved snippets, and
`honest_decline` (did the agent say it could not find the answer rather than
guess). Multi-hop questions are judge-only because the entity names appear in
the question itself, which would make substring matching meaningless.

**`--no-search` ablation.** The same 27 questions run with the tool withheld
give a memory-only baseline, isolating what search actually contributes.

## What the evals found, and the iteration history

All correctness/groundedness figures below use the `claude-opus-4-8` judge.

1. **Memory-only ablation** (`75d4c83e`): 21/27 correct, but **recent 0/5**. The
   tool's value is concentrated almost entirely in post-cutoff questions; the
   model already knows the stable categories.

2. **Judge audit.** The first judge pass surfaced two grader bugs, not agent
   bugs. (a) Groundedness: with no retrieved snippets the judge still returned
   "grounded", once citing a snippet that did not exist. Fixed by forcing
   `not_applicable` whenever nothing was retrieved. (b) Ambiguous grading was
   grader-sensitive — a lenient rubric let single-referent answers pass. After
   tightening the rubric (require the dominant referent *and* acknowledgement of
   ambiguity) and adding a "grade the core question, not incidental gold detail"
   line, the corrected V0 baseline (`53e803f1`) was **23/27 correct, grounded
   18/21 (86%), 1.93 searches/question, recent 2.67/5 [2–3]** over three runs.

3. **V1 prompt** (disambiguation, search-honesty, efficiency; tool unchanged).
   Efficiency improved cleanly — non-recent searches 1.59 → 1.32/question with no
   correctness loss — and ambiguous moved 3/5 → 4/5 on that run. Recent stayed
   flat (2.33/5), confirming it was retrieval-bound, not a prompt problem. One
   honesty gap remained: on a search error the agent still sometimes fell back to
   memory and asserted an event "has not yet taken place".

4. **Extract tool, first version** (two HTTP calls: search then extracts). Intro
   extracts made the answers present in the results, but the extra call plus the
   agent's bursty searching tripped MediaWiki rate limits — recent stayed noisy
   (2–4/5) and `honest_decline` rose to 3/5 as searches errored out.

5. **Single-request + pacing fix** (final). Folding search+extracts into one
   `generator=search`+`prop=extracts` request, adding a descriptive session
   User-Agent, and sleeping 1s between eval questions removed the errors. Final
   system (`ee8fa7e3` + two recent reruns): **25/27 correct (93%), grounded
   21/23 (91%), 1.44 searches/question**, and **recent 5.00/5 [5–5]** — stable
   across three runs, down from 3.07 to 1.67 searches per recent question.
   `recent_01` (Champions League final), which failed every V0 run, is now
   answered from a single search.

Net over the project: 85% → 93% correct, 86% → 91% grounded, 1.93 → 1.44
searches/question, with the gain concentrated in post-cutoff retrieval and no
regression on categories the model already handled.

## Where it still fails

- **Ambiguous: 3/5.** The two misses (`ambiguous_02` Michael Jordan,
  `ambiguous_05` Ulysses) are overwhelmingly-dominant entities where the prompt's
  disambiguation instruction does not reliably fire. It is noisy at n=5 single
  runs (has reached 4/5 elsewhere).
- **`unanswerable_03`** sometimes answers a false premise from memory with zero
  searches. This is defensible behavior, so it is left as a flagged `search_ok`
  miss rather than prompted around.
- **Recent depends on Wikipedia** having an intro extract that states the fact.
  When it does not, the correct behavior is an honest "couldn't find it", which
  the correctness metric scores as incorrect against a gold that has the answer.

## What I'd do with more time

- A **cutoff-awareness** line in the system prompt (the agent knows its own
  knowledge is stale for recent events, so search rather than trust memory).
- A **citation instruction** so answers name the article they came from, making
  groundedness checkable by the user, not just the judge.
- **N-run averaging across all categories**, not just recent — ambiguous in
  particular is too noisy to trust at a single n=5 run.
- A **larger ambiguous set** to measure disambiguation reliably and iterate the
  prompt against it.

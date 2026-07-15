# Wikipedia QA Agent

A command-line question-answering agent: Claude (`claude-sonnet-4-6`) with a
single `search_wikipedia` tool, plus an eval suite that grades it across six
question categories. See [DESIGN.md](DESIGN.md) for the rationale and results.

## Setup

Requires Python 3.9+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The Anthropic API key is read from the environment and is **never** stored in
code or committed:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

(The SDK also picks up an `ANTHROPIC_API_KEY` already exported in your shell
profile.)

## Quickstart

**Ask a single question.** Prints the trace of Wikipedia searches followed by
the answer:

```bash
python main.py "In what year was the Eiffel Tower completed?"
```

**Demo.** There is no dedicated `--demo` flag; the intended demo is one easy
factual question and one that needs multiple searches:

```bash
python main.py "What is the capital city of Australia?"
python main.py "Which is taller, the Eiffel Tower or the Statue of Liberty, and by roughly how much?"
```

A fast harness smoke test (3 questions, no LLM judge, ~no cost beyond the agent):

```bash
python eval/run_eval.py --limit 3 --no-judge
```

**Run the eval.** Writes one JSON per run to `eval/results/<run_id>.json` and
prints per-question rows plus a per-category summary.

```bash
python eval/run_eval.py                    # full 27, prompt V1, opus judge
python eval/run_eval.py --no-search        # memory-only baseline (tool withheld)
python eval/run_eval.py --prompt v0        # run the original V0 system prompt
python eval/run_eval.py --category recent  # one category only
```

Useful flags: `--limit N`, `--category <name>`, `--prompt {v0,v1}`,
`--no-search`, `--no-judge`, `--judge-model <id>`.

## Repo layout

```
agent.py            Manual tool-use loop + search_wikipedia (single MediaWiki request)
prompts.py          System prompts (V0, V1 default) and the LLM-judge prompt
main.py             CLI: question in, trace + answer out
requirements.txt    anthropic, requests
eval/
  questions.json    27 questions across 6 categories, with verified gold answers
  run_eval.py       Runner + programmatic and LLM-judge grading
  results/          One JSON per run (rows + summary + metadata)
DESIGN.md           Design rationale, findings, iteration history
```

## Models

- **Agent:** `claude-sonnet-4-6` (fixed).
- **Judge:** `claude-opus-4-8` (independent, stronger grader; overridable with
  `--judge-model`).

See [DESIGN.md](DESIGN.md) for why, and for the measured effect of each change.

"""Run the eval set through the QA agent and grade the results.

Grading is layered:
  * programmatic (cheap, deterministic):
      - closed-form match on `acceptable_answers` (normalized, word-boundary,
        NOT raw substring -> "au" doesn't match "Australia", "0" not "2010")
      - search behavior from the trace (did it search? enough searches? and for
        `expected_search: false` questions, did it correctly NOT search?)
      - a heuristic premise-pushback check for false-premise questions
  * LLM-as-judge (JUDGE_SYSTEM / JUDGE_USER_TEMPLATE in prompts.py):
      - correctness vs the gold answer (equivalence, not re-answering)
      - groundedness of the answer in the retrieved snippets

Writes one JSON per run to eval/results/<run_id>.json and prints per-question
rows plus a per-category summary.

Usage:
    ./.venv/bin/python eval/run_eval.py
    ./.venv/bin/python eval/run_eval.py --limit 3 --no-judge      # quick smoke
    ./.venv/bin/python eval/run_eval.py --category recent
    ./.venv/bin/python eval/run_eval.py --judge-model claude-opus-4-8
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
import uuid
from collections import defaultdict

# Allow running from anywhere: import the sibling agent/prompts modules.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, PROJECT_DIR)

from anthropic import Anthropic  # noqa: E402

from agent import MODEL as AGENT_MODEL, QAAgent  # noqa: E402
from prompts import JUDGE_SYSTEM, JUDGE_USER_TEMPLATE, PROMPTS  # noqa: E402

DEFAULT_QUESTIONS = os.path.join(BASE_DIR, "questions.json")
DEFAULT_OUT_DIR = os.path.join(BASE_DIR, "results")
DEFAULT_JUDGE_MODEL = "claude-opus-4-8"  # stronger / less self-biased judge
# than the sonnet-4-6 agent (esp. for groundedness).
JUDGE_MAX_TOKENS = 512

CATEGORY_ORDER = [
    "single_hop",
    "multi_hop",
    "ambiguous_entity",
    "unanswerable",
    "recent",
    "no_search",
]

# Soft cues that a false-premise/unanswerable question was pushed back on rather
# than answered as if true. Heuristic only -- the LLM judge is authoritative.
_REFUSAL_CUES = [
    "no such", "not a", "no element", "has no", "no king", "never won",
    "did not", "does not", "was not", "is not", "isn't", "wasn't", "didn't",
    "doesn't", "false premise", "incorrect", "mistaken", "actually",
    "rather than", "instead", "no record", "no evidence", "none", "zero",
    "cannot answer", "can't answer", "no monarch", "republic",
]


# --------------------------------------------------------------------------- #
# Programmatic grading
# --------------------------------------------------------------------------- #

def normalize(s):
    """Lowercase and collapse everything non-alphanumeric to single spaces."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def closed_form_match(acceptable, answer):
    """Word-boundary match of any acceptable answer within the agent answer.

    Returns (applicable, matched_bool, matched_on). Matching is on normalized
    text with \\b boundaries so short tokens don't hit inside longer words
    ("au" vs "Australia", "0" vs "2010", "40" vs "400").
    """
    if not acceptable:
        return False, None, None
    norm_answer = normalize(answer)
    for a in acceptable:
        pattern = r"\b" + re.escape(normalize(a)) + r"\b"
        if re.search(pattern, norm_answer):
            return True, True, a
    return True, False, None


def grade_search(question, num_searches):
    """Return (did_search, meets_expectation)."""
    did_search = num_searches > 0
    if question["expected_search"]:
        ok = did_search and num_searches >= question.get("min_searches", 1)
    else:
        # For no_search questions, the correct behavior is NOT to search.
        ok = num_searches == 0
    return did_search, ok


def premise_pushback(question, answer):
    """Heuristic: did the answer push back on a false premise?

    Applicable only to refuse_or_correct questions with no closed-form answer
    (the 4 false-premise items). Returns (applicable, cue_present).
    """
    if question["expected_behavior"] != "refuse_or_correct":
        return False, None
    if question.get("acceptable_answers"):
        return False, None  # closed-form handles this one (e.g. Canada -> zero)
    low = answer.lower()
    return True, any(cue in low for cue in _REFUSAL_CUES)


# --------------------------------------------------------------------------- #
# LLM-as-judge
# --------------------------------------------------------------------------- #

def format_snippets(trace):
    if not trace:
        return "(no searches were performed)"
    blocks = []
    for i, call in enumerate(trace, 1):
        blocks.append(f"[Search {i}] query: {call['query']}\n{call['results']}")
    return "\n\n".join(blocks)


def parse_judge_json(text):
    """Best-effort parse of the judge's JSON reply (tolerate fences/prose)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def run_judge(client, judge_model, question, agent_answer, trace):
    user = JUDGE_USER_TEMPLATE.format(
        question=question["question"],
        gold_answer=question["gold_answer"],
        agent_answer=agent_answer,
        snippets=format_snippets(trace),
    )
    resp = client.messages.create(
        model=judge_model,
        max_tokens=JUDGE_MAX_TOKENS,
        system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    verdict = parse_judge_json(text)
    result = {
        "correctness": verdict.get("correctness"),
        "correctness_reason": verdict.get("correctness_reason", ""),
        "groundedness": verdict.get("groundedness"),
        "groundedness_reason": verdict.get("groundedness_reason", ""),
        "honest_decline": verdict.get("honest_decline"),
    }
    # Groundedness is meaningless with no retrieved snippets, and the judge has
    # been observed to mark such answers "grounded" (even citing a nonexistent
    # snippet). Force not_applicable when nothing was retrieved; correctness
    # from the judge still stands.
    if not trace:
        result["groundedness"] = "not_applicable"
        result["groundedness_reason"] = (
            "No searches were performed; groundedness is not applicable."
        )
    return result


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def evaluate(questions, agent, client, judge_model, use_judge, use_tools=True):
    rows = []
    n = len(questions)
    for i, q in enumerate(questions, 1):
        row = {
            "id": q["id"],
            "category": q["category"],
            "question": q["question"],
            "gold_answer": q["gold_answer"],
        }

        # --- run the agent ---
        t0 = time.time()
        try:
            answer, trace = agent.answer(q["question"], use_tools=use_tools)
            row["error"] = None
        except Exception as e:  # keep one failure from killing the run
            answer, trace = "", []
            row["error"] = f"{type(e).__name__}: {e}"
        row["latency_s"] = round(time.time() - t0, 2)
        row["agent_answer"] = answer
        row["queries"] = [c["query"] for c in trace]
        # Persist the full retrieved snippet text (not just queries) so
        # groundedness can be re-audited from the results file after the fact.
        row["retrieved"] = trace
        row["num_searches"] = len(trace)

        # --- programmatic grading ---
        did_search, search_ok = grade_search(q, len(trace))
        row["did_search"] = did_search
        row["search_ok"] = search_ok

        cf_applicable, cf_match, cf_on = closed_form_match(
            q.get("acceptable_answers") or [], answer
        )
        row["closed_form"] = {
            "applicable": cf_applicable,
            "match": cf_match,
            "matched_on": cf_on,
        }

        pb_applicable, pb_cue = premise_pushback(q, answer)
        row["premise_pushback"] = {
            "applicable": pb_applicable,
            "cue_present": pb_cue,
        }

        # --- LLM judge ---
        if use_judge and row["error"] is None:
            try:
                row["judge"] = run_judge(client, judge_model, q, answer, trace)
            except Exception as e:
                row["judge"] = {"error": f"{type(e).__name__}: {e}"}
        else:
            row["judge"] = None

        rows.append(row)
        _print_progress(i, n, row)
        if i < n:
            time.sleep(1.0)  # space out MediaWiki bursts across questions

    return rows


def _fmt_bool(ok):
    if ok is None:
        return "-"
    return "ok" if ok else "X"


def _print_progress(i, n, row):
    judge = row.get("judge")
    if isinstance(judge, dict) and "error" not in judge:
        j = f"{judge.get('correctness')}/{judge.get('groundedness')}"
    elif isinstance(judge, dict):
        j = "judge_err"
    else:
        j = "-"
    cf = row["closed_form"]
    cf_str = "-" if not cf["applicable"] else ("ok" if cf["match"] else "X")
    err = "  ERROR" if row["error"] else ""
    print(
        f"[{i:>2}/{n}] {row['id']:<16} {row['category']:<16} "
        f"searches={row['num_searches']} ({_fmt_bool(row['search_ok'])})  "
        f"closed_form={cf_str}  judge={j}{err}"
    )


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #

def summarize(rows):
    """Aggregate per category and overall."""
    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)

    summary = {}
    cats = [c for c in CATEGORY_ORDER if c in by_cat]
    cats += [c for c in by_cat if c not in CATEGORY_ORDER]
    for cat in cats + ["ALL"]:
        group = rows if cat == "ALL" else by_cat[cat]
        summary[cat] = _summarize_group(group)
    return summary


def _summarize_group(group):
    n = len(group)
    search_ok = sum(1 for r in group if r["search_ok"])

    cf_applicable = [r for r in group if r["closed_form"]["applicable"]]
    cf_match = sum(1 for r in cf_applicable if r["closed_form"]["match"])

    judged = [r for r in group if isinstance(r["judge"], dict)
              and "error" not in r["judge"]]
    correct = sum(1 for r in judged if r["judge"]["correctness"] == "correct")
    partial = sum(1 for r in judged
                  if r["judge"]["correctness"] == "partially_correct")
    incorrect = sum(1 for r in judged
                    if r["judge"]["correctness"] == "incorrect")

    grounded_pool = [r for r in judged
                     if r["judge"]["groundedness"] != "not_applicable"]
    grounded = sum(1 for r in grounded_pool
                   if r["judge"]["groundedness"] == "grounded")

    honest_declines = sum(1 for r in judged
                          if r["judge"].get("honest_decline") is True)

    return {
        "n": n,
        "search_ok": search_ok,
        "closed_form_applicable": len(cf_applicable),
        "closed_form_match": cf_match,
        "judged": len(judged),
        "correct": correct,
        "partially_correct": partial,
        "incorrect": incorrect,
        "grounded_applicable": len(grounded_pool),
        "grounded": grounded,
        "honest_declines": honest_declines,
    }


def _rate(num, den):
    return f"{num}/{den}" + (f" ({num / den:.0%})" if den else "")


def print_summary(summary):
    print("\n" + "=" * 78)
    print("CATEGORY SUMMARY")
    print("=" * 78)
    header = (
        f"{'category':<17}{'n':>3}  {'search_ok':>12}  {'closed_form':>12}  "
        f"{'correct':>12}  {'grounded':>12}"
    )
    print(header)
    print("-" * 78)
    for cat, s in summary.items():
        if cat == "ALL":
            print("-" * 78)
        # correctness rate counts full-correct over judged; partials shown apart
        correct_cell = _rate(s["correct"], s["judged"])
        line = (
            f"{cat:<17}{s['n']:>3}  "
            f"{_rate(s['search_ok'], s['n']):>12}  "
            f"{_rate(s['closed_form_match'], s['closed_form_applicable']):>12}  "
            f"{correct_cell:>12}  "
            f"{_rate(s['grounded'], s['grounded_applicable']):>12}"
        )
        print(line)
        if s["partially_correct"] or s["incorrect"]:
            print(
                f"{'':<20}(partial={s['partially_correct']}, "
                f"incorrect={s['incorrect']})"
            )
        if cat == "recent":
            print(f"{'':<20}(honest_decline={s['honest_declines']}/{s['n']})")
    print("=" * 78)
    print("Notes: search_ok includes no_search items answering WITHOUT a search;")
    print("closed_form only over questions that have acceptable_answers;")
    print("correct/grounded are LLM-judge rates ('correct' excludes partials).")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description="Run and grade the QA agent eval.")
    ap.add_argument("--questions", default=DEFAULT_QUESTIONS)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    ap.add_argument("--no-judge", action="store_true",
                    help="Skip the LLM judge (programmatic grading only).")
    ap.add_argument("--no-search", action="store_true",
                    help="Withhold the search tool (memory-only baseline).")
    ap.add_argument("--prompt", choices=sorted(PROMPTS), default="v1",
                    help="Which system prompt version the agent uses.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only run the first N questions (after --category).")
    ap.add_argument("--category", default=None,
                    help="Only run questions in this category.")
    args = ap.parse_args()

    with open(args.questions) as f:
        data = json.load(f)
    questions = data["questions"]
    if args.category:
        questions = [q for q in questions if q["category"] == args.category]
    if args.limit is not None:
        questions = questions[: args.limit]
    if not questions:
        print("No questions selected.")
        sys.exit(1)

    client = Anthropic()
    agent = QAAgent(client=client, system_prompt=PROMPTS[args.prompt])
    use_judge = not args.no_judge
    use_tools = not args.no_search

    run_id = uuid.uuid4().hex[:8]
    started = datetime.datetime.now().isoformat(timespec="seconds")
    print(f"Run {run_id} | agent={AGENT_MODEL} | prompt={args.prompt} | "
          f"judge={args.judge_model if use_judge else 'OFF'} | "
          f"search={'ON' if use_tools else 'OFF (memory-only)'} | "
          f"{len(questions)} questions\n")

    rows = evaluate(questions, agent, client, args.judge_model, use_judge,
                    use_tools=use_tools)
    summary = summarize(rows)
    print_summary(summary)

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, f"{run_id}.json")
    with open(out_path, "w") as f:
        json.dump(
            {
                "run_id": run_id,
                "started_at": started,
                "agent_model": AGENT_MODEL,
                "prompt_version": args.prompt,
                "search_enabled": use_tools,
                "judge_model": args.judge_model if use_judge else None,
                "questions_file": os.path.abspath(args.questions),
                "num_questions": len(questions),
                "summary": summary,
                "rows": rows,
            },
            f,
            indent=2,
        )
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()

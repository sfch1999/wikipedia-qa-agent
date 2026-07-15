"""CLI entry point for the QA agent.

Usage:
    python main.py "your question here"
    python main.py            # then type a question at the prompt
"""

import sys

from agent import QAAgent


def main():
    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
    else:
        question = input("Question: ").strip()

    if not question:
        print("No question provided.")
        sys.exit(1)

    agent = QAAgent()
    answer, trace = agent.answer(question)

    print("\n=== Tool call trace ===")
    if not trace:
        print("(no searches — answered without using the tool)")
    else:
        for i, call in enumerate(trace, 1):
            print(f"\n[{i}] search_wikipedia(query={call['query']!r})")
            for line in call["results"].splitlines():
                print(f"      {line}")

    print("\n=== Answer ===")
    print(answer)


if __name__ == "__main__":
    main()

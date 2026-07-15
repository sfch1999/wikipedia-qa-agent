"""QA agent: manual Claude tool-use loop over a live Wikipedia search tool."""

import html
import re
import time

import requests
from anthropic import Anthropic

from prompts import PROMPT_V1, SYSTEM_PROMPT  # noqa: F401 (V0 kept for reference)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1024
MAX_TURNS = 10  # safety cap on the tool-use loop

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"

TOOLS = [
    {
        "name": "search_wikipedia",
        "description": (
            "Search English Wikipedia and return the top matching articles "
            "(titles and short snippets). Use this to look up factual "
            "information before answering."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query, e.g. 'capital of Australia'.",
                }
            },
            "required": ["query"],
        },
    }
]

_TAG_RE = re.compile(r"<[^>]+>")


def _clean_snippet(snippet: str) -> str:
    """MediaWiki snippets contain HTML (<span> highlights, entities). Strip them."""
    return html.unescape(_TAG_RE.sub("", snippet)).strip()


# A descriptive User-Agent is requested by MediaWiki API etiquette and helps
# avoid throttling. Reused across calls via a session.
_SESSION = requests.Session()
_SESSION.headers.update(
    {
        "User-Agent": (
            "QA-Agent-Takehome/1.0 (Wikipedia QA eval harness; "
            "contact: sfch199999@gmail.com) python-requests"
        )
    }
)


def _mediawiki_get(params: dict, max_attempts: int = 2):
    """GET the MediaWiki API with one retry + backoff. Returns (json, error)."""
    last_err = None
    for attempt in range(max_attempts):
        try:
            resp = _SESSION.get(WIKIPEDIA_API, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json(), None
        except requests.RequestException as e:
            last_err = e
            if attempt < max_attempts - 1:
                time.sleep(1.0 * (attempt + 1))  # simple linear backoff
    return None, last_err


def search_wikipedia(
    query: str,
    limit: int = 5,
    extract_top: int = 2,
    extract_chars: int = 1000,
    max_attempts: int = 2,
) -> str:
    """Search English Wikipedia and return the top results as text.

    Uses a single MediaWiki request (generator=search + prop=extracts) so the
    top `extract_top` matches carry their intro extract without a second HTTP
    call. Retries once with backoff on transient errors; on final search failure
    returns an explicit 'SEARCH ERROR: ...' string (distinct from
    successful-but-empty).
    """
    data, err = _mediawiki_get(
        {
            "action": "query",
            "format": "json",
            "formatversion": 2,
            "generator": "search",
            "gsrsearch": query,
            "gsrlimit": limit,
            "prop": "extracts",
            "exintro": 1,
            "explaintext": 1,
            "exlimit": extract_top,
            "redirects": 1,
        },
        max_attempts=max_attempts,
    )
    if err or data is None:
        return (
            f"SEARCH ERROR: Wikipedia search for '{query}' failed after "
            f"{max_attempts} attempts ({err})."
        )

    pages = data.get("query", {}).get("pages", [])
    if not pages:
        return f"No Wikipedia results found for '{query}'."

    pages.sort(key=lambda p: p.get("index", 10 ** 9))  # restore search-rank order
    lines = []
    for i, page in enumerate(pages, 1):
        lines.append(f"{i}. {page.get('title', '')}")
        extract = (page.get("extract") or "").strip()
        if extract:
            if len(extract) > extract_chars:
                extract = extract[:extract_chars].rstrip() + "…"
            lines.append(f"   Intro: {extract}")
    return "\n".join(lines)


class QAAgent:
    """Runs the manual tool-use loop and records a trace of tool calls."""

    def __init__(self, client=None, system_prompt=None):
        self.client = client or Anthropic()
        self.system_prompt = system_prompt or PROMPT_V1

    def answer(self, question: str, use_tools: bool = True):
        """Answer a question. Returns (answer_text, trace).

        trace is a list of {"query": str, "results": str} for each search.
        When use_tools is False the search tool is withheld entirely, giving a
        memory-only (parametric) baseline.
        """
        messages = [{"role": "user", "content": question}]
        trace = []

        for _ in range(MAX_TURNS):
            kwargs = dict(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=self.system_prompt,
                messages=messages,
            )
            if use_tools:
                kwargs["tools"] = TOOLS
            response = self.client.messages.create(**kwargs)

            if response.stop_reason != "tool_use":
                # No (more) tool calls — Claude has produced its final answer.
                return self._text(response), trace

            # Record the assistant turn (including tool_use blocks) verbatim.
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if block.name == "search_wikipedia":
                    query = block.input.get("query", "")
                    results = search_wikipedia(query)
                    trace.append({"query": query, "results": results})
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": results,
                        }
                    )
                else:
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Unknown tool: {block.name}",
                            "is_error": True,
                        }
                    )

            messages.append({"role": "user", "content": tool_results})

        return (
            "Stopped after reaching the maximum number of tool-use turns "
            "without a final answer.",
            trace,
        )

    @staticmethod
    def _text(response) -> str:
        return "".join(b.text for b in response.content if b.type == "text").strip()

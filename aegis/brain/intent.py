"""Intent classification.

This runs BEFORE the planner and, critically, sees **only the user's own
request** - never screen text, window titles or page contents. That is what
makes it a trustworthy place to decide capabilities: there is no untrusted
input in its context, so there is nothing to inject.

The intent it returns selects the capability set the planner then decodes
against, so a question about the screen cannot produce an action that touches
the machine.

A rule-based fast path handles common phrasings in well under a millisecond;
anything it is not confident about falls through to a tiny constrained LLM
call. This is a precursor to the full Tier -1 router at M2.
"""

from __future__ import annotations

import json
import logging
import re

import openai
from openai import OpenAI

from aegis.config import settings
from aegis.schema.actions import Intent

log = logging.getLogger(__name__)

# Leading verbs that plainly mean "do something to the machine".
_ACT_PATTERNS = re.compile(
    r"^\s*(?:please\s+|could you\s+|can you\s+|would you\s+)*"
    r"(open|launch|start|run|close|quit|exit|kill|click|press|type|"
    r"play|pause|resume|skip|mute|switch|focus|go to|bring up|fire up|pull up)\b",
    re.IGNORECASE,
)

# Leading forms that plainly mean "tell me something".
_QUERY_PATTERNS = re.compile(
    r"^\s*(?:please\s+|could you\s+|can you\s+|would you\s+)*"
    r"(what|who|when|where|why|which|how|is|are|was|were|do|does|did|"
    r"tell me|read|summarise|summarize|explain|describe)\b",
    re.IGNORECASE,
)

_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "aegis_intent",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": [i.value for i in Intent]}
            },
            "required": ["intent"],
        },
    },
}

_SYSTEM = """\
Classify the user's request into exactly one intent.

- "act": they want something done to the computer (open/close an app, click,
  type, control playback).
- "query": they want information (a question, or a request to read or explain
  something).
- "unknown": the request is too vague or ambiguous to tell.

Reply only with the JSON object.
"""


class IntentClassifier:
    def __init__(self, client: OpenAI | None = None) -> None:
        self._client = client or OpenAI(
            base_url=settings.lms_base_url,
            api_key=settings.lms_api_key,
            timeout=settings.planner_timeout_s,
            max_retries=0,
        )

    def classify(self, request: str) -> tuple[Intent, str]:
        """Return the intent and how it was reached ('rule' or 'llm')."""
        if _ACT_PATTERNS.match(request):
            return Intent.ACT, "rule"
        if _QUERY_PATTERNS.match(request):
            return Intent.QUERY, "rule"

        try:
            completion = self._client.chat.completions.create(
                model=settings.planner_model,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": request},
                ],
                response_format=_SCHEMA,  # type: ignore[arg-type]
                temperature=0.0,
                max_tokens=16,
            )
            raw = json.loads(completion.choices[0].message.content or "{}")
            return Intent(raw["intent"]), "llm"
        except (openai.APIError, json.JSONDecodeError, KeyError, ValueError):
            # Failing closed here costs the user a clarifying question, which is
            # much cheaper than granting capabilities we could not justify.
            log.warning("intent classification failed; defaulting to UNKNOWN", exc_info=True)
            return Intent.UNKNOWN, "fallback"

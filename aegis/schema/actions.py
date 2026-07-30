"""Action schema and per-intent capability sets.

The pydantic models here are the single source of truth for what Aegis can do.
They are also converted to a JSON Schema and handed to LM Studio as a
``response_format``, so the model is constrained *at decode time*.

That constraint is used for security, not just tidiness. The schema is built
per turn from the classified intent, so verbs outside the current capability
set are not in the grammar and are literally undecodable. M0 measured a 3B
model obeying an injected "launch cmd" instruction 3 times out of 4; narrowing
makes that 0 by construction rather than by persuasion.

The schema is kept deliberately flat (a ``verb`` literal plus generic string
fields) rather than a discriminated union of per-verb models. Flat schemas
convert to much simpler grammars, which a 3B model follows far more reliably.
"""

from __future__ import annotations

import copy
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from aegis.config import settings

# Every verb Aegis implements. The executor enforces this list independently of
# the model, so an injected instruction cannot name a capability we lack.
Verb = Literal[
    "launch_app",
    "answer",
    "ask_user",
]

ALL_VERBS: frozenset[str] = frozenset({"launch_app", "answer", "ask_user"})

# Verbs with no effect outside Aegis itself. Everything else is side-effecting.
READ_ONLY_VERBS: frozenset[str] = frozenset({"answer", "ask_user"})

# Verbs whose effects are hard to undo. These always route through the
# human-in-the-loop confirmation regardless of how confident the model is.
DESTRUCTIVE_VERBS: frozenset[str] = frozenset()


class Intent(str, Enum):
    """What the user is asking for, classified from their request alone."""

    ACT = "act"      # do something to the machine
    QUERY = "query"  # answer a question, possibly about the screen
    UNKNOWN = "unknown"


# The capability set granted to each intent. A QUERY turn cannot launch
# anything, because launch_app is absent from the grammar it decodes against.
CAPABILITY_SETS: dict[Intent, frozenset[str]] = {
    Intent.ACT: frozenset({"launch_app", "ask_user"}),
    Intent.QUERY: frozenset({"answer", "ask_user"}),
    Intent.UNKNOWN: frozenset({"ask_user"}),
}


class Action(BaseModel):
    verb: Verb = Field(description="The action to perform.")
    target: str = Field(
        description=(
            "For launch_app: the application name, e.g. 'notepad'. "
            "For answer: the answer text. "
            "For ask_user: the question to put to the user."
        )
    )


class Plan(BaseModel):
    speech: str = Field(
        description="One short sentence spoken aloud, in the Aegis persona. No more than 12 words."
    )
    # min_length becomes "minItems" in the grammar. Without it the model
    # sometimes returns speech with an empty action list - it says "Calculator
    # is open, sir." and does nothing. Every capability set includes ask_user,
    # so there is always a valid action available.
    #
    # max_length mirrors the executor's action ceiling at the decoder, so an
    # overlong plan cannot be generated in the first place.
    actions: list[Action] = Field(
        description="Actions to execute, in order. At least one.",
        min_length=1,
        max_length=settings.max_actions_per_plan,
    )


def plan_json_schema(allowed_verbs: frozenset[str] | None = None) -> dict[str, Any]:
    """Plan schema with the verb enum narrowed to the current capability set."""
    schema = copy.deepcopy(Plan.model_json_schema())
    verbs = sorted(allowed_verbs if allowed_verbs is not None else ALL_VERBS)
    if not verbs:
        raise ValueError("capability set cannot be empty")
    schema["$defs"]["Action"]["properties"]["verb"]["enum"] = verbs
    return schema


def response_format(allowed_verbs: frozenset[str] | None = None) -> dict[str, Any]:
    """The ``response_format`` payload for the LM Studio chat completions API."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "aegis_plan",
            "strict": True,
            "schema": plan_json_schema(allowed_verbs),
        },
    }

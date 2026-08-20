"""Turn execution results into what Aegis actually says.

The planner writes ``plan.speech`` before anything runs, so it is a prediction,
not a report. M0 caught the worst version of this (an empty plan announcing
success) and fixed it in the schema, but the speech-layer variant survived M1:
a plan whose actions failed - or were declined at the confirmation gate - still
got its confident success line read aloud, followed by "That didn't work, sir."

This module decides what may be spoken once the results are known:

* any hard failure -> an honest failure line, never the pre-written success one
* declined by the user -> a brief acknowledgement; they said no, so neither
  claim success nor complain
* a query that produced an answer -> speak the answer itself, which the model
  could not have known when it wrote ``plan.speech``
* everything succeeded -> ``plan.speech``, now earned
"""

from __future__ import annotations

from dataclasses import dataclass

from aegis.execute.registry import ExecutionResult
from aegis.schema.actions import Plan

# Verbs whose payload IS the reply. What they produced during execution is the
# thing worth saying; the plan's pre-written sentence was at best a filler.
# (From M2 this matters more: answers about the screen are produced by
# execution, not predictable at planning time.)
_ANSWER_VERBS = frozenset({"answer"})


@dataclass
class Outcome:
    speech: str          # what to say aloud; "" means say nothing
    status: str | None   # bar status override; None leaves the bar as-is


def speech_for_outcome(plan: Plan, results: list[ExecutionResult]) -> Outcome:
    failed = [r for r in results if not r.ok and not r.declined]
    declined = [r for r in results if not r.ok and r.declined]

    if failed:
        return Outcome("That didn't work, sir.", f"Failed: {failed[0].detail}")
    if declined:
        return Outcome("Very well, sir.", "Cancelled.")

    answers = [
        text
        for r in results
        if r.ok and r.verb in _ANSWER_VERBS
        for text in (r.data.get("answer") or r.data.get("summary"),)
        if text
    ]
    if answers:
        # The bar already shows the answer via the executor's answer callback,
        # so no status override - just say it.
        return Outcome(" ".join(answers), None)

    return Outcome(plan.speech, None)

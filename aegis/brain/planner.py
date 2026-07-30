"""LM Studio planner.

Uses constrained decoding (``response_format`` with a JSON schema derived from
the pydantic action models), so the model cannot emit an invalid plan.
"""

from __future__ import annotations

import json
import logging

import openai
from openai import OpenAI
from pydantic import ValidationError

from aegis.brain.prompt import build_messages
from aegis.config import settings
from aegis.core.timing import Trace
from aegis.schema.actions import ALL_VERBS, Plan, response_format

log = logging.getLogger(__name__)


class PlannerError(RuntimeError):
    """Planning failed in a way the user needs to hear about."""


class Planner:
    def __init__(self) -> None:
        self.client = OpenAI(
            base_url=settings.lms_base_url,
            api_key=settings.lms_api_key,
            timeout=settings.planner_timeout_s,
            max_retries=0,
        )

    def plan(
        self,
        request: str,
        allowed_verbs: frozenset[str] | None = None,
        observations: str | None = None,
        trace: Trace | None = None,
    ) -> Plan:
        """Produce a plan constrained to ``allowed_verbs``.

        The verb enum in the decoder grammar is narrowed to the capability set,
        so verbs outside it cannot be produced at all - not merely discouraged.
        """
        verbs = allowed_verbs if allowed_verbs is not None else ALL_VERBS
        messages = build_messages(request, verbs, observations)

        try:
            if trace is not None:
                with trace.stage("llm"):
                    completion = self._complete(messages, verbs)
            else:
                completion = self._complete(messages, verbs)
        except openai.APIConnectionError as exc:
            raise PlannerError(
                f"Cannot reach LM Studio at {settings.lms_base_url}. "
                "Start the server with `lms server start`."
            ) from exc
        except openai.APIStatusError as exc:
            raise PlannerError(
                f"LM Studio returned {exc.status_code}. Is '{settings.planner_model}' loaded?"
            ) from exc

        content = completion.choices[0].message.content or ""
        try:
            return Plan.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as exc:
            # Constrained decoding should make this unreachable. If it fires,
            # the server ignored the schema - worth knowing loudly.
            log.error("planner returned unusable content: %r", content)
            raise PlannerError("The planner returned a malformed plan.") from exc

    def _complete(self, messages: list[dict[str, str]], allowed_verbs: frozenset[str]):
        return self.client.chat.completions.create(
            model=settings.planner_model,
            messages=messages,  # type: ignore[arg-type]
            response_format=response_format(allowed_verbs),  # type: ignore[arg-type]
            temperature=settings.planner_temperature,
            max_tokens=settings.planner_max_tokens,
        )

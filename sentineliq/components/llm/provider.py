"""LLM provider abstraction.

Business logic must never import a vendor SDK directly. The interface is a
**security control**, not a convenience: it is what makes a fully private
deployment a configuration change rather than a rewrite (Context.md 26b, 35).

Groq's terms were verified against the Services Agreement and "Your Data in
GroqCloud" before selection — see ADR-018.
"""

import logging
import os
from dataclasses import dataclass
from typing import Protocol

from groq import Groq

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    """One completion plus the token counts the caller needs for cost."""

    text: str
    input_tokens: int
    output_tokens: int
    model: str


class LLMProvider(Protocol):
    """What the rest of the system is allowed to depend on."""

    def complete(
        self, system: str, user: str, *, temperature: float, max_tokens: int
    ) -> LLMResponse:
        """Return one completion for a system + user prompt pair."""
        ...


class GroqProvider:
    """Groq-hosted models (ADR-018)."""

    def __init__(self, model: str) -> None:
        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise ValueError("GROQ_API_KEY is not set — see .env.example")
        self.client = Groq(api_key=api_key)
        self.model = model

    def complete(
        self, system: str, user: str, *, temperature: float, max_tokens: int
    ) -> LLMResponse:
        """Send one chat completion request."""
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        usage = response.usage
        return LLMResponse(
            text=response.choices[0].message.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            model=self.model,
        )

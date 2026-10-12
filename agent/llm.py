"""The chat model. Anthropic by default; the model name comes from AGENT_MODEL."""

import os

from langchain_anthropic import ChatAnthropic

from agent.settings import Settings


def make_llm(settings: Settings) -> ChatAnthropic:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set. The agent needs an LLM key to run.")
    # No temperature: current Claude models reject non-default values.
    return ChatAnthropic(model=settings.model, max_tokens=4096)

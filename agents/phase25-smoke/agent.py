"""PydanticAI agent: Phase 25 smoke test agent — echoes task input back as output."""
import os
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from common.memory_tools import search_memory, add_memory
from common.langfuse_tools import get_system_prompt

_SYSTEM_PROMPT_FALLBACK = """You are phase25-smoke, a Hatchet agent. Phase 25 smoke test agent — echoes task input back as output."""


def build_agent() -> Agent:
    base_url = os.environ.get("LITELLM_BASE_URL")
    api_key = os.environ.get("LITELLM_API_KEY")

    if not base_url:
        raise RuntimeError(
            "LITELLM_BASE_URL environment variable is required but was not set"
        )
    if not api_key:
        raise RuntimeError(
            "LITELLM_API_KEY environment variable is required but was not set"
        )

    model = OpenAIChatModel(
        model_name=os.environ.get("LLM_MODEL", "phase25-smoke"),
        provider=OpenAIProvider(
            base_url=base_url,
            api_key=api_key,
        ),
    )
    return Agent(
        model=model,
        system_prompt=get_system_prompt("phase25-smoke-system", fallback=_SYSTEM_PROMPT_FALLBACK),
        tools=[search_memory, add_memory],
    )

"""PydanticAI agent: Phase 25 canary agent — validates the agent factory pipeline end-to-end."""
import os
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from common.memory_tools import search_memory, add_memory
from common.langfuse_tools import get_system_prompt

_SYSTEM_PROMPT_FALLBACK = """You are phase25-canary, a Hatchet agent. Phase 25 canary agent — validates the agent factory pipeline end-to-end."""


def build_agent() -> Agent:
    litellm_base_url = os.environ.get("LITELLM_BASE_URL")
    if not litellm_base_url:
        raise RuntimeError(
            "LITELLM_BASE_URL environment variable is required but was not set"
        )
    litellm_api_key = os.environ.get("LITELLM_API_KEY")
    if not litellm_api_key:
        raise RuntimeError(
            "LITELLM_API_KEY environment variable is required but was not set"
        )

    model = OpenAIChatModel(
        model_name=os.environ.get("LLM_MODEL", "phase25-canary"),
        provider=OpenAIProvider(
            base_url=litellm_base_url,
            api_key=litellm_api_key,
        ),
    )
    return Agent(
        model=model,
        system_prompt=get_system_prompt("phase25-canary-system", fallback=_SYSTEM_PROMPT_FALLBACK),
        tools=[search_memory, add_memory],
    )

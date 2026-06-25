"""PydanticAI agent: A simple echo agent that repeats back the task title as its output"""
import os
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from common.memory_tools import search_memory, add_memory
from common.langfuse_tools import get_system_prompt

_SYSTEM_PROMPT_FALLBACK = """You are echo-agent, a Hatchet agent. A simple echo agent that repeats back the task title as its output"""


def build_agent() -> Agent:
    litellm_base_url = os.environ.get("LITELLM_BASE_URL")
    litellm_api_key = os.environ.get("LITELLM_API_KEY")
    if not litellm_base_url:
        raise RuntimeError(
            "LITELLM_BASE_URL environment variable is required but not set"
        )
    if not litellm_api_key:
        raise RuntimeError(
            "LITELLM_API_KEY environment variable is required but not set"
        )
    model = OpenAIChatModel(
        model_name=os.environ.get("LLM_MODEL", "echo-agent"),
        provider=OpenAIProvider(
            base_url=litellm_base_url,
            api_key=litellm_api_key,
        ),
    )
    return Agent(
        model=model,
        system_prompt=get_system_prompt("echo-agent-system", fallback=_SYSTEM_PROMPT_FALLBACK),
        tools=[search_memory, add_memory],
    )

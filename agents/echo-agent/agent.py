"""PydanticAI agent: A simple echo agent that repeats back the task title as its output"""
import os
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from common.memory_tools import search_memory, add_memory
from common.langfuse_tools import get_system_prompt

_SYSTEM_PROMPT_FALLBACK = """You are echo-agent, a Hatchet agent. A simple echo agent that repeats back the task title as its output"""


def build_agent() -> Agent:
    model = OpenAIChatModel(
        model_name=os.environ.get("LLM_MODEL", "echo-agent"),
        provider=OpenAIProvider(
            base_url=os.environ["LITELLM_BASE_URL"],
            api_key=os.environ["LITELLM_API_KEY"],
        ),
    )
    return Agent(
        model=model,
        system_prompt=get_system_prompt("echo-agent-system", fallback=_SYSTEM_PROMPT_FALLBACK),
        tools=[search_memory, add_memory],
    )

"""PydanticAI research agent with web search via MCP gateway, memory, and Vikunja integration."""
import os
import httpx
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerHTTP
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from common.memory_tools import add_memory, search_memory
from common.langfuse_tools import get_system_prompt

_RESEARCH_SYSTEM_PROMPT_FALLBACK = """You are a research agent. Given a task title and description, you:
1. Search the web for relevant, current information on the topic
2. Synthesize findings into a concise, actionable summary
3. Store key insights in memory under the task namespace
4. Return a markdown-formatted research report

Be thorough but concise. Prefer primary sources. Cite URLs where relevant.
After completing research, call update_vikunja_task to mark the task done with your summary."""


def _build_model() -> OpenAIModel:
    return OpenAIModel(
        model_name=os.environ.get("LLM_MODEL", "qwen3-35b"),
        provider=OpenAIProvider(
            base_url=os.environ["LITELLM_BASE_URL"],
            api_key=os.environ["LITELLM_API_KEY"],
        ),
    )


def _build_mcp_server() -> MCPServerHTTP:
    mcp_url = os.environ.get(
        "LITELLM_MCP_URL",
        os.environ["LITELLM_BASE_URL"].replace("/v1", "/mcp"),
    )
    return MCPServerHTTP(
        url=mcp_url,
        headers={"Authorization": f"Bearer {os.environ['LITELLM_API_KEY']}"},
        read_timeout=60,
    )


async def update_vikunja_task(task_id: int, comment: str, done: bool = True) -> str:
    """Update a Vikunja task: post a comment and optionally mark it done."""
    base = os.environ.get("VIKUNJA_BASE_URL", "https://todo.amer.dev")
    token = os.environ["VIKUNJA_TOKEN"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10) as client:
        comment_resp = await client.put(
            f"{base}/api/v1/tasks/{task_id}/comments",
            json={"comment": comment},
            headers=headers,
        )
        if comment_resp.status_code == 401:
            return "error: Vikunja token expired — research complete but task not updated"
        comment_resp.raise_for_status()
        if done:
            done_resp = await client.post(
                f"{base}/api/v1/tasks/{task_id}",
                json={"done": True},
                headers=headers,
            )
            done_resp.raise_for_status()
    return "task updated"


def build_agent() -> Agent:
    model = _build_model()
    return Agent(
        model=model,
        system_prompt=get_system_prompt("research-system", fallback=_RESEARCH_SYSTEM_PROMPT_FALLBACK),
        mcp_servers=[_build_mcp_server()],
        tools=[add_memory, search_memory, update_vikunja_task],
    )

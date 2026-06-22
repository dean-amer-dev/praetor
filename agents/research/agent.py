"""PydanticAI research agent with web search via direct HTTP, memory, and Vikunja integration."""
import os
import re
import httpx
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from common.memory_tools import add_memory, search_memory
from common.langfuse_tools import get_system_prompt

_RESEARCH_SYSTEM_PROMPT_FALLBACK = """You are a research agent. Given a task title and description, you:
0. FIRST: call search_memory(query=<task title + description>, agent_id="research") to check
   for relevant prior findings. If results cover the topic, skip or minimise web searching.
1. Search the web for relevant, current information on the topic (only what memory doesn't cover)
2. Synthesize findings into a concise, actionable summary
3. If you found new information not already in memory, store key insights:
   add_memory(content=<summary of findings>, agent_id="research")
4. Return a markdown-formatted research report

Be thorough but concise. Prefer primary sources. Cite URLs where relevant.
After completing research, call update_vikunja_task to mark the task done with your summary."""

_SEARXNG_URL = os.environ.get("SEARXNG_URL", "https://searxng.amer.dev")


def _build_model() -> OpenAIChatModel:
    return OpenAIChatModel(
        model_name=os.environ.get("LLM_MODEL", "qwen3-35b"),
        provider=OpenAIProvider(
            base_url=os.environ["LITELLM_BASE_URL"],
            api_key=os.environ["LITELLM_API_KEY"],
        ),
    )


async def web_search(query: str, max_results: int = 5) -> str:
    """Search the web and return scored, deduplicated results as JSON."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{_SEARXNG_URL}/search",
            params={"q": query, "format": "json", "engines": "google,bing,duckduckgo"},
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])[:max_results]
        if not results:
            return "No results found."
        lines = []
        for r in results:
            title = r.get("title", "")
            url = r.get("url", "")
            snippet = r.get("content", "")[:300]
            lines.append(f"**{title}**\n{url}\n{snippet}")
        return "\n\n".join(lines)


async def web_read_url(url: str, max_chars: int = 4000) -> str:
    """Fetch a URL and return its text content (markdown-converted), capped at max_chars."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "praetor-research/1.0"})
        resp.raise_for_status()
        text = resp.text
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]


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
        tools=[web_search, web_read_url, add_memory, search_memory, update_vikunja_task],
    )

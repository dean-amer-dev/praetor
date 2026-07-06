"""Shared Mem0 tools registered on every PydanticAI agent.

Uses direct HTTP calls with x-api-key auth against the self-hosted Mem0 server.
The MemoryClient SDK sends Authorization: Token which the self-hosted server rejects.

search_memory and add_memory are async so that Langfuse @observe() creates proper child
spans when awaited from an async trace context (run_in_executor breaks span propagation).
"""
import asyncio
import logging
import os
import httpx
from common.langfuse_tools import observe

logger = logging.getLogger(__name__)

_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        api_key = os.environ["MEM0_API_KEY"]
        base_url = os.environ["MEM0_BASE_URL"].rstrip("/")
        _client = httpx.Client(
            base_url=base_url,
            headers={"x-api-key": api_key},
            timeout=30,
        )
    return _client


@observe()
async def add_memory(content: str, agent_id: str) -> str:
    def _sync() -> str:
        client = _get_client()
        resp = client.post(
            "/memories",
            json={
                "messages": [{"role": "user", "content": content}],
                "agent_id": agent_id,
                "infer": False,
            },
        )
        resp.raise_for_status()
        return "stored"
    return await asyncio.to_thread(_sync)


@observe()
async def search_memory(query: str, agent_id: str) -> list[str]:
    def _sync() -> list[str]:
        client = _get_client()
        resp = client.post(
            "/search",
            json={"query": query, "agent_id": agent_id},
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return [r["memory"] for r in results]
    return await asyncio.to_thread(_sync)


def get_all_memories(agent_id: str) -> list[str]:
    client = _get_client()
    resp = client.get("/memories", params={"agent_id": agent_id})
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return [r["memory"] for r in results]


@observe()
async def delete_memory(query: str, agent_id: str) -> int:
    """Search for memories matching query and delete them. Returns count deleted."""
    def _sync() -> int:
        client = _get_client()
        resp = client.post(
            "/search",
            json={"query": query, "agent_id": agent_id},
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        deleted = 0
        for r in results:
            memory_id = r.get("id")
            if memory_id:
                del_resp = client.delete(f"/memories/{memory_id}")
                try:
                    del_resp.raise_for_status()
                    deleted += 1
                except httpx.HTTPStatusError as exc:
                    logger.warning("Failed to delete memory %s: %d %s", memory_id, del_resp.status_code, del_resp.text)
                    raise
        return deleted
    return await asyncio.to_thread(_sync)

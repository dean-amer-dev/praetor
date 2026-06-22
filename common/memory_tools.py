"""Shared Mem0 tools registered on every PydanticAI agent.

Uses direct HTTP calls with x-api-key auth against the self-hosted Mem0 server.
The MemoryClient SDK sends Authorization: Token which the self-hosted server rejects.
"""
import os
import httpx
from common.langfuse_tools import observe

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
def add_memory(content: str, agent_id: str) -> str:
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


@observe()
def search_memory(query: str, agent_id: str) -> list[str]:
    client = _get_client()
    resp = client.post(
        "/search",
        json={"query": query, "agent_id": agent_id},
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return [r["memory"] for r in results]


def get_all_memories(agent_id: str) -> list[str]:
    client = _get_client()
    resp = client.get("/memories", params={"agent_id": agent_id})
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return [r["memory"] for r in results]

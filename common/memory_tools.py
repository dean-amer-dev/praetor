"""Shared Mem0 tools registered on every PydanticAI agent."""
import os
from mem0 import MemoryClient

_client: MemoryClient | None = None


def _get_client() -> MemoryClient:
    global _client
    if _client is None:
        _client = MemoryClient(
            host=os.environ["MEM0_BASE_URL"],
            api_key=os.environ["MEM0_API_KEY"],
        )
    return _client


def add_memory(content: str, agent_id: str) -> str:
    _get_client().add(content, agent_id=agent_id)
    return "stored"


def search_memory(query: str, agent_id: str) -> list[str]:
    results = _get_client().search(query, agent_id=agent_id)
    return [r["memory"] for r in results]


def get_all_memories(agent_id: str) -> list[str]:
    results = _get_client().get_all(agent_id=agent_id)
    return [r["memory"] for r in results]

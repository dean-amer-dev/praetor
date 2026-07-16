"""Unit tests for common/memory_tools.py — direct HTTP Mem0 wrapper."""
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_client():
    """Reset singleton between tests to avoid state bleed."""
    import common.memory_tools as mt
    mt._client = None
    yield
    mt._client = None


@pytest.fixture
def mock_http(env_mem0):
    """Patch httpx.Client and return the mock instance."""
    mock_instance = MagicMock()
    with patch("common.memory_tools.httpx.Client", return_value=mock_instance) as MockClass:
        yield mock_instance, MockClass


class TestAddMemory:
    async def test_calls_post_memories(self, mock_http):
        instance, _ = mock_http
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        instance.post.return_value = mock_resp

        from common.memory_tools import add_memory
        await add_memory("a fact", "agent-1")

        instance.post.assert_called_once()
        call_args = instance.post.call_args
        assert call_args[0][0] == "/memories"
        body = call_args[1]["json"]
        assert body["messages"] == [{"role": "user", "content": "a fact"}]
        assert body["agent_id"] == "agent-1"
        assert body["infer"] is False

    async def test_returns_stored(self, mock_http):
        instance, _ = mock_http
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        instance.post.return_value = mock_resp

        from common.memory_tools import add_memory
        result = await add_memory("x", "a")
        assert result == "stored"

    async def test_returns_skipped_on_http_error(self, mock_http):
        instance, _ = mock_http
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("500 Internal Server Error")
        instance.post.return_value = mock_resp

        from common.memory_tools import add_memory
        result = await add_memory("x", "a")
        assert result == "skipped"


class TestSearchMemory:
    async def test_calls_post_search(self, mock_http):
        instance, _ = mock_http
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"results": []}
        instance.post.return_value = mock_resp

        from common.memory_tools import search_memory
        await search_memory("query", "agent-1")

        instance.post.assert_called_once()
        call_args = instance.post.call_args
        assert call_args[0][0] == "/search"
        body = call_args[1]["json"]
        assert body["query"] == "query"
        assert body["agent_id"] == "agent-1"

    async def test_extracts_memory_field(self, mock_http):
        instance, _ = mock_http
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"results": [{"memory": "fact1"}, {"memory": "fact2"}]}
        instance.post.return_value = mock_resp

        from common.memory_tools import search_memory
        result = await search_memory("q", "a")
        assert result == ["fact1", "fact2"]

    async def test_empty_results(self, mock_http):
        instance, _ = mock_http
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"results": []}
        instance.post.return_value = mock_resp

        from common.memory_tools import search_memory
        assert await search_memory("q", "a") == []


class TestDeleteMemory:
    async def test_searches_then_deletes_each_match(self, mock_http):
        instance, _ = mock_http
        search_resp = MagicMock()
        search_resp.raise_for_status = MagicMock()
        search_resp.json.return_value = {
            "results": [{"id": "abc", "memory": "fact1"}, {"id": "def", "memory": "fact2"}]
        }
        delete_resp = MagicMock()
        delete_resp.status_code = 200
        instance.post.return_value = search_resp
        instance.delete.return_value = delete_resp

        from common.memory_tools import delete_memory
        count = await delete_memory("shopping", "planner-global")

        assert count == 2
        instance.post.assert_called_once_with(
            "/search", json={"query": "shopping", "agent_id": "planner-global"}
        )
        assert instance.delete.call_count == 2
        instance.delete.assert_any_call("/memories/abc")
        instance.delete.assert_any_call("/memories/def")

    async def test_returns_zero_when_no_results(self, mock_http):
        instance, _ = mock_http
        search_resp = MagicMock()
        search_resp.raise_for_status = MagicMock()
        search_resp.json.return_value = {"results": []}
        instance.post.return_value = search_resp

        from common.memory_tools import delete_memory
        count = await delete_memory("nonexistent", "a")
        assert count == 0
        instance.delete.assert_not_called()

    async def test_skips_entries_without_id(self, mock_http):
        instance, _ = mock_http
        search_resp = MagicMock()
        search_resp.raise_for_status = MagicMock()
        search_resp.json.return_value = {"results": [{"memory": "no id here"}]}
        instance.post.return_value = search_resp

        from common.memory_tools import delete_memory
        count = await delete_memory("q", "a")
        assert count == 0
        instance.delete.assert_not_called()


class TestGetAllMemories:
    def test_calls_get_memories(self, mock_http):
        instance, _ = mock_http
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"results": []}
        instance.get.return_value = mock_resp

        from common.memory_tools import get_all_memories
        get_all_memories("agent-x")

        instance.get.assert_called_once()
        call_args = instance.get.call_args
        assert call_args[0][0] == "/memories"
        assert call_args[1]["params"]["agent_id"] == "agent-x"

    def test_extracts_memory_field(self, mock_http):
        instance, _ = mock_http
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"results": [{"memory": "m1"}, {"memory": "m2"}]}
        instance.get.return_value = mock_resp

        from common.memory_tools import get_all_memories
        result = get_all_memories("a")
        assert result == ["m1", "m2"]


class TestClientSingleton:
    async def test_client_created_once(self, mock_http):
        instance, MockClass = mock_http
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        instance.post.return_value = mock_resp

        from common.memory_tools import add_memory
        await add_memory("a", "x")
        await add_memory("b", "x")
        assert MockClass.call_count == 1

    async def test_client_uses_x_api_key_header(self, env_mem0, monkeypatch):
        monkeypatch.setenv("MEM0_API_KEY", "my-api-key")
        monkeypatch.setenv("MEM0_BASE_URL", "https://mem0.test")

        with patch("common.memory_tools.httpx.Client") as MockClass:
            mock_instance = MagicMock()
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_instance.post.return_value = mock_resp
            MockClass.return_value = mock_instance

            from common.memory_tools import add_memory
            await add_memory("x", "y")

        call_kwargs = MockClass.call_args[1]
        assert call_kwargs["headers"]["x-api-key"] == "my-api-key"
        assert "Authorization" not in call_kwargs["headers"]

    async def test_client_uses_base_url(self, monkeypatch):
        monkeypatch.setenv("MEM0_API_KEY", "key")
        monkeypatch.setenv("MEM0_BASE_URL", "https://custom-mem0.test")

        with patch("common.memory_tools.httpx.Client") as MockClass:
            mock_instance = MagicMock()
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_instance.post.return_value = mock_resp
            MockClass.return_value = mock_instance

            from common.memory_tools import add_memory
            await add_memory("x", "y")

        call_kwargs = MockClass.call_args[1]
        assert call_kwargs["base_url"] == "https://custom-mem0.test"

    async def test_missing_env_returns_skipped(self, monkeypatch):
        monkeypatch.delenv("MEM0_BASE_URL", raising=False)
        monkeypatch.delenv("MEM0_API_KEY", raising=False)
        from common.memory_tools import add_memory
        result = await add_memory("x", "y")
        assert result == "skipped"

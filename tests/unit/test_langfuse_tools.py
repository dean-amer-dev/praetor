"""Unit tests for common/langfuse_tools.py — prompt management and no-op stubs."""
import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest


def _reload_langfuse_tools():
    """Force a clean reimport of langfuse_tools to pick up new env vars."""
    import common.langfuse_tools as lt
    importlib.reload(lt)
    return lt


class TestDisabledPath:
    """When LANGFUSE_* env vars are absent the module runs in no-op mode."""

    def test_get_system_prompt_returns_fallback(self, monkeypatch):
        for key in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            monkeypatch.delenv(key, raising=False)
        lt = _reload_langfuse_tools()
        result = lt.get_system_prompt("any-prompt", fallback="my fallback")
        assert result == "my fallback"

    def test_get_system_prompt_returns_empty_without_fallback(self, monkeypatch):
        for key in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            monkeypatch.delenv(key, raising=False)
        lt = _reload_langfuse_tools()
        result = lt.get_system_prompt("any-prompt")
        assert result == ""

    def test_observe_noop_passes_through(self, monkeypatch):
        for key in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            monkeypatch.delenv(key, raising=False)
        lt = _reload_langfuse_tools()

        call_log = []

        @lt.observe()
        def my_fn(x):
            call_log.append(x)
            return x * 2

        result = my_fn(5)
        assert result == 10
        assert call_log == [5]

    def test_langfuse_context_noop_does_not_raise(self, monkeypatch):
        for key in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            monkeypatch.delenv(key, raising=False)
        lt = _reload_langfuse_tools()
        lt.langfuse_context.update_current_trace(name="x", input={}, tags=["a"])


class TestCreatePrompt:
    def test_create_prompt_returns_false_when_disabled(self, monkeypatch):
        for key in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            monkeypatch.delenv(key, raising=False)
        lt = _reload_langfuse_tools()
        assert lt.create_prompt("test-prompt", "You are a test agent.") is False

    def test_create_prompt_returns_false_on_exception(self, monkeypatch):
        for key in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            monkeypatch.delenv(key, raising=False)
        lt = _reload_langfuse_tools()
        mock_client = MagicMock()
        mock_client.create_prompt.side_effect = RuntimeError("API error")
        lt._client = mock_client
        assert lt.create_prompt("test-prompt", "text") is False

    def test_create_prompt_calls_client_with_production_label(self, monkeypatch):
        for key in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            monkeypatch.delenv(key, raising=False)
        lt = _reload_langfuse_tools()
        mock_client = MagicMock()
        lt._client = mock_client
        result = lt.create_prompt("my-prompt", "prompt text")
        assert result is True
        mock_client.create_prompt.assert_called_once_with(
            name="my-prompt", prompt="prompt text", labels=["production"]
        )


class TestFallbackOnException:
    def test_get_system_prompt_fallback_when_client_raises(self, monkeypatch):
        for key in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            monkeypatch.delenv(key, raising=False)
        lt = _reload_langfuse_tools()
        # Even with _client = None, get_system_prompt should return fallback cleanly
        result = lt.get_system_prompt("broken", fallback="safe fallback")
        assert result == "safe fallback"

    def test_get_system_prompt_fallback_when_mock_client_raises(self, monkeypatch):
        for key in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            monkeypatch.delenv(key, raising=False)
        lt = _reload_langfuse_tools()
        # Inject a mock client that raises
        mock_client = MagicMock()
        mock_client.get_prompt.side_effect = RuntimeError("connection failed")
        lt._client = mock_client
        result = lt.get_system_prompt("prompt-name", fallback="the fallback")
        assert result == "the fallback"

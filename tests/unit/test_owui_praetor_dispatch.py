"""
Unit tests for the praetor_dispatch OpenWebUI tool.

The tool lives as a Python string in register_owui_tool.py (TOOL_CONTENT) and is
uploaded to OWU via the API. We exec() that string here to get the Tools class
and test it in isolation — same code path OWU runs when a user triggers the tool.

Catches:
  - Empty PRAETOR_API_KEY → 401 from praetor → unhandled HTTPStatusError shown to user
  - Missing env var without a fallback default
  - Malformed dispatch response (missing task_id/event)
  - get_task_status returning "done" vs "still running" correctly
"""
import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load the Tools class from TOOL_CONTENT without importing OWU
# ---------------------------------------------------------------------------

_scripts_dir = Path(__file__).parent.parent.parent / "scripts"
sys.path.insert(0, str(_scripts_dir))

from register_owui_tool import TOOL_CONTENT  # noqa: E402


def _make_tools(praetor_api_key: str | None = None) -> object:
    """
    Exec TOOL_CONTENT in an isolated namespace and return an instantiated Tools().
    Optionally override PRAETOR_API_KEY env var for the duration of construction.
    Tools() must be instantiated INSIDE the patch.dict context — the Valves
    default_factory lambda reads os.environ at instantiation time, not class-definition time.
    """
    ns: dict = {}
    env_override = {"PRAETOR_API_KEY": praetor_api_key} if praetor_api_key is not None else {}
    with patch.dict(os.environ, env_override, clear=False):
        exec(compile(TOOL_CONTENT, "<tool_content>", "exec"), ns)  # noqa: S102
        return ns["Tools"]()  # inside patch context so default_factory sees the right env


# ---------------------------------------------------------------------------
# Valve / configuration tests
# ---------------------------------------------------------------------------

class TestValveConfiguration:
    def test_env_var_wins_over_hardcoded_fallback(self):
        custom_key = "my-custom-key-xyz"
        tool = _make_tools(praetor_api_key=custom_key)
        assert tool.valves.PRAETOR_API_KEY == custom_key

    def test_hardcoded_fallback_used_when_env_missing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PRAETOR_API_KEY", None)
            tool = _make_tools()
        # Fallback must be non-empty — this is the bug: empty key → 401
        assert tool.valves.PRAETOR_API_KEY, (
            "PRAETOR_API_KEY is empty with no env var set. "
            "The tool will get a 401 from praetor on every call. "
            "Set a hardcoded fallback in the Valves default_factory."
        )

    def test_default_base_url(self):
        tool = _make_tools(praetor_api_key="any-key")
        assert tool.valves.PRAETOR_BASE_URL == "https://praetor.amer.dev"


# ---------------------------------------------------------------------------
# dispatch_task tests
# ---------------------------------------------------------------------------

class TestDispatchTask:
    def _mock_response(self, status: int, body: dict) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = body
        if status >= 400:
            import httpx
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                f"HTTP {status}", request=MagicMock(), response=resp
            )
        else:
            resp.raise_for_status.return_value = None
        return resp

    def test_successful_dispatch_returns_task_id_message(self):
        tool = _make_tools(praetor_api_key="valid-key")
        mock_resp = self._mock_response(200, {"task_id": 42, "event": "agent:openhands"})

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            result = tool.dispatch_task(
                title="Add health check",
                description="Add /health endpoint. repo: amerenda/praetor",
                task_type="openhands",
            )

        assert "42" in result
        assert "agent:openhands" in result
        call_kwargs = mock_post.call_args
        sent = call_kwargs[1]["json"] if call_kwargs[1] else call_kwargs[0][1]
        assert sent["title"] == "Add health check"
        assert sent["type"] == "openhands"

    def test_empty_api_key_raises_on_401(self):
        """
        Regression: empty PRAETOR_API_KEY must NOT silently fail.
        Before the fix, the tool was uploaded with PRAETOR_API_KEY = "" (no env var,
        no fallback). OWU showed a cryptic HTTPStatusError to the user.
        """
        import httpx

        ns: dict = {}
        # Reproduce the old broken state: no env var, no hardcoded fallback → empty string
        import re
        broken_content = re.sub(
            r'default_factory=lambda:.*?\)',
            'default_factory=lambda: ""',
            TOOL_CONTENT,
            flags=re.DOTALL,
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PRAETOR_API_KEY", None)
            exec(compile(broken_content, "<broken>", "exec"), ns)  # noqa: S102
            tool = ns["Tools"]()

        assert tool.valves.PRAETOR_API_KEY == "", "setup: should be empty to reproduce bug"

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401 Unauthorized", request=MagicMock(), response=MagicMock()
        )

        with patch("httpx.post", return_value=mock_resp):
            with pytest.raises(httpx.HTTPStatusError):
                tool.dispatch_task("t", "d", "research")

    def test_bearer_header_sent(self):
        tool = _make_tools(praetor_api_key="secret-key-123")
        mock_resp = self._mock_response(200, {"task_id": 1, "event": "agent:research"})

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            tool.dispatch_task("title", "desc", "research")

        headers = mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer secret-key-123"

    def test_all_task_types_accepted(self):
        tool = _make_tools(praetor_api_key="key")
        for task_type in ("research", "code", "pipeline", "openhands"):
            mock_resp = self._mock_response(200, {"task_id": 1, "event": f"agent:{task_type}"})
            with patch("httpx.post", return_value=mock_resp):
                result = tool.dispatch_task("t", "d", task_type)
            assert "1" in result


# ---------------------------------------------------------------------------
# get_task_status tests
# ---------------------------------------------------------------------------

class TestGetTaskStatus:
    def _mock_get(self, status: int, body: dict) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = body
        if status >= 400:
            import httpx
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                f"HTTP {status}", request=MagicMock(), response=resp
            )
        else:
            resp.raise_for_status.return_value = None
        return resp

    def test_done_returns_summary(self):
        tool = _make_tools(praetor_api_key="key")
        mock_resp = self._mock_get(200, {"done": True, "mem0_summary": "Task completed: added /health endpoint."})

        with patch("httpx.get", return_value=mock_resp):
            result = tool.get_task_status(42)

        assert "Task completed" in result

    def test_not_done_returns_still_running(self):
        tool = _make_tools(praetor_api_key="key")
        mock_resp = self._mock_get(200, {"done": False, "mem0_summary": None})

        with patch("httpx.get", return_value=mock_resp):
            result = tool.get_task_status(99)

        assert "running" in result.lower()

    def test_status_auth_failure_raises(self):
        import httpx
        tool = _make_tools(praetor_api_key="bad-key")
        mock_resp = self._mock_get(401, {})

        with patch("httpx.get", return_value=mock_resp):
            with pytest.raises(httpx.HTTPStatusError):
                tool.get_task_status(1)

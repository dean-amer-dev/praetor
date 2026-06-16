"""Shared fixtures for praetor test suite."""
import hashlib
import hmac
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def vikunja_secret(monkeypatch):
    monkeypatch.setenv("VIKUNJA_WEBHOOK_SECRET", "test-secret")
    return "test-secret"


@pytest.fixture
def github_secret(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-github-secret")
    return "test-github-secret"


@pytest.fixture
def env_mem0(monkeypatch):
    monkeypatch.setenv("MEM0_BASE_URL", "http://mem0.local")
    monkeypatch.setenv("MEM0_API_KEY", "test-key")


@pytest.fixture
def env_litellm(monkeypatch):
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.local")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")


@pytest.fixture
def scratch_dir(tmp_path, monkeypatch):
    d = tmp_path / "scratch"
    d.mkdir()
    monkeypatch.setenv("SCRATCH_DIR", str(d))
    # Also patch the module-level constant in coder/agent
    import agents.coder.agent as coder_agent
    monkeypatch.setattr(coder_agent, "SCRATCH_DIR", str(d))
    return d


# ---------------------------------------------------------------------------
# Mock Hatchet client
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_hatchet():
    """Patch shared dispatch singleton and github router so no real Hatchet SDK is needed."""
    mock = MagicMock()
    mock.event.push = MagicMock()
    with (
        patch("common.dispatch._get_hatchet", return_value=mock),
        patch("webhooks.github._get_hatchet", return_value=mock),
    ):
        yield mock


# ---------------------------------------------------------------------------
# HMAC helpers used by multiple test modules
# ---------------------------------------------------------------------------

def make_vikunja_sig(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def make_github_sig(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

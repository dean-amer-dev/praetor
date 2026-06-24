"""Unit tests for Phase 23: context management + feature decomposition pipeline."""
from __future__ import annotations

import textwrap
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pipelines.feature_pipeline import (
    _extract_spec,
    _feature_prompt,
    _finalize_prompt,
    _parse_features,
    _run_feature_pipeline,
    _setup_branch_prompt,
    FeaturePipelineInput,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

_MULTI_FEATURE_SPEC = textwrap.dedent("""\
    [task]
    type        = "new_app"
    title       = "Blog homepage"
    description = "Stateless React blog"

    [repos]
    primary = "amerenda/blog"
    gitops  = "amerenda/k3s-dean-gitops"

    [infra]
    type          = "stateless"
    k3s_namespace = "apps"
    hostname      = "blog.amer.dev"
    port          = 3000

    [app]
    runtime   = "node"
    framework = "react-vite"
    features  = [
        "Post listing page",
        "Individual post page",
        "Dark mode toggle",
    ]

    [dispatch]
    agents           = ["app_factory", "coder"]
    reviewer_enabled = true
    request_limit    = 80
""")

_SINGLE_FEATURE_SPEC = textwrap.dedent("""\
    [task]
    type  = "modify_app"
    title = "Add dark mode"

    [repos]
    primary = "amerenda/blog"

    [app]
    changes = ["Add dark mode toggle"]

    [dispatch]
    request_limit = 40
""")

_MULTI_FEATURE_MODIFY_SPEC = textwrap.dedent("""\
    [task]
    type  = "modify_app"
    title = "Add new features"

    [repos]
    primary = "amerenda/blog"

    [app]
    changes = ["Add dark mode toggle", "Add search bar", "Add pagination"]

    [dispatch]
    request_limit = 60
""")


# ---------------------------------------------------------------------------
# _parse_features
# ---------------------------------------------------------------------------

def test_parse_features_extracts_list():
    spec = {"app": {"features": ["feat A", "feat B", "feat C"]}}
    assert _parse_features(spec) == ["feat A", "feat B", "feat C"]


def test_parse_features_empty_when_no_app():
    assert _parse_features({}) == []


def test_parse_features_empty_when_no_features_key():
    assert _parse_features({"app": {}}) == []


# ---------------------------------------------------------------------------
# _feature_prompt
# ---------------------------------------------------------------------------

def test_feature_prompt_includes_do_not_open_pr():
    import tomllib
    spec = tomllib.loads(_MULTI_FEATURE_SPEC)
    prompt = _feature_prompt(spec, "Post listing page", 0, 3, 42)
    assert "do NOT open a PR" in prompt.lower() or "Do NOT open a PR" in prompt


def test_feature_prompt_includes_branch():
    import tomllib
    spec = tomllib.loads(_MULTI_FEATURE_SPEC)
    prompt = _feature_prompt(spec, "Post listing page", 0, 3, 42)
    assert "praetor-coder/task-42" in prompt


def test_feature_prompt_shows_index():
    import tomllib
    spec = tomllib.loads(_MULTI_FEATURE_SPEC)
    prompt = _feature_prompt(spec, "Individual post page", 1, 3, 42)
    assert "2/3" in prompt


def test_feature_prompt_includes_feature_name():
    import tomllib
    spec = tomllib.loads(_MULTI_FEATURE_SPEC)
    prompt = _feature_prompt(spec, "Dark mode toggle", 2, 3, 42)
    assert "Dark mode toggle" in prompt


# ---------------------------------------------------------------------------
# _finalize_prompt
# ---------------------------------------------------------------------------

def test_finalize_prompt_lists_all_features():
    import tomllib
    spec = tomllib.loads(_MULTI_FEATURE_SPEC)
    features = _parse_features(spec)
    prompt = _finalize_prompt(spec, features, 42)
    for feature in features:
        assert feature in prompt


def test_finalize_prompt_mentions_draft_pr():
    import tomllib
    spec = tomllib.loads(_MULTI_FEATURE_SPEC)
    features = _parse_features(spec)
    prompt = _finalize_prompt(spec, features, 42)
    assert "draft PR" in prompt or "draft pr" in prompt.lower()


# ---------------------------------------------------------------------------
# _extract_spec
# ---------------------------------------------------------------------------

def test_extract_spec_parses_toml_block():
    description = f"```toml\n{_MULTI_FEATURE_SPEC}```"
    spec = _extract_spec(description)
    assert spec is not None
    assert spec["task"]["type"] == "new_app"
    assert spec["repos"]["primary"] == "amerenda/blog"


def test_extract_spec_returns_none_when_no_block():
    assert _extract_spec("just plain text") is None


def test_extract_spec_returns_none_on_invalid_toml():
    assert _extract_spec("```toml\nnot valid [[[\n```") is None


# ---------------------------------------------------------------------------
# crash recovery: skip done features
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crash_recovery_skips_done_features():
    """When Mem0 has 'feature done: Post listing page', that feature is skipped."""
    import tomllib
    spec = tomllib.loads(_MULTI_FEATURE_SPEC)
    description = f"```toml\n{_MULTI_FEATURE_SPEC}```"
    task_input = FeaturePipelineInput(task_id=99, task_title="Blog", task_description=description)

    agent_run_calls = []

    async def mock_search_memory(query: str, agent_id: str) -> list[str]:
        if "branch-setup" in query:
            return ["branch-setup done"]
        if "Post listing page" in query:
            return ["feature done: Post listing page - committed"]
        return []

    async def mock_agent_run(*args, **kwargs):
        agent_run_calls.append(args[0] if args else kwargs.get("prompt"))
        result = MagicMock()
        result.output = "done"
        return result

    mock_agent = MagicMock()
    mock_agent.run = mock_agent_run

    with (
        patch("pipelines.feature_pipeline.search_memory", side_effect=mock_search_memory),
        patch("pipelines.feature_pipeline.add_memory", AsyncMock()),
        patch("pipelines.feature_pipeline.build_coder_agent", return_value=mock_agent),
        patch("pipelines.feature_pipeline.task_active"),
        patch("pipelines.feature_pipeline.task_invocations"),
        patch("pipelines.feature_pipeline.task_duration"),
    ):
        ctx = MagicMock()
        await _run_feature_pipeline(task_input, ctx)

    # Post listing page should be skipped; Individual post page and Dark mode toggle should run.
    # The finalize PR prompt also mentions all features — exclude it by checking "open draft PR".
    non_finalize = [c for c in agent_run_calls if "open draft PR" not in str(c)]
    feature_prompts = [c for c in non_finalize if "Individual post page" in str(c)
                       or "Dark mode toggle" in str(c)]
    skipped_prompts = [c for c in non_finalize if "Post listing page" in str(c)
                       and "feature" in str(c).lower()]
    assert len(feature_prompts) == 2
    assert len(skipped_prompts) == 0


# ---------------------------------------------------------------------------
# spec routing in webhooks/spec.py
# ---------------------------------------------------------------------------

def test_single_feature_routes_to_code_not_pipeline():
    """modify_app with 1 change dispatches agent:code, not pipeline:feature_decompose."""
    import os
    os.environ.setdefault("PRAETOR_API_KEY", "test-key")
    os.environ.setdefault("MEM0_BASE_URL", "http://mem0.test")
    os.environ.setdefault("MEM0_API_KEY", "mem0-key")
    from fastapi.testclient import TestClient
    from webhooks.app import app

    client = TestClient(app)
    dispatch_calls = []

    def mock_dispatch(task_id, title, description, agent_type, **kwargs):
        dispatch_calls.append(agent_type)
        return ["agent:code"]

    with (
        patch("webhooks.spec.dispatch_agent", side_effect=mock_dispatch),
        patch.dict("os.environ", {"PRAETOR_API_KEY": "test-key"}),
    ):
        resp = client.post(
            "/api/v1/spec/execute",
            json={"spec_toml": _SINGLE_FEATURE_SPEC},
            headers={"Authorization": "Bearer test-key"},
        )

    assert resp.status_code == 200
    assert dispatch_calls == ["code"]


def test_multi_feature_modify_routes_to_feature_pipeline():
    """modify_app with >1 changes dispatches pipeline:feature_decompose."""
    import os
    os.environ.setdefault("PRAETOR_API_KEY", "test-key")
    os.environ.setdefault("MEM0_BASE_URL", "http://mem0.test")
    os.environ.setdefault("MEM0_API_KEY", "mem0-key")
    from fastapi.testclient import TestClient
    from webhooks.app import app

    client = TestClient(app)
    dispatch_calls = []

    def mock_dispatch(task_id, title, description, agent_type, **kwargs):
        dispatch_calls.append(agent_type)
        return ["pipeline:feature_decompose"]

    with (
        patch("webhooks.spec.dispatch_agent", side_effect=mock_dispatch),
        patch.dict("os.environ", {"PRAETOR_API_KEY": "test-key"}),
    ):
        resp = client.post(
            "/api/v1/spec/execute",
            json={"spec_toml": _MULTI_FEATURE_MODIFY_SPEC},
            headers={"Authorization": "Bearer test-key"},
        )

    assert resp.status_code == 200
    assert dispatch_calls == ["feature_pipeline"]


# ---------------------------------------------------------------------------
# agent.py tool truncation
# ---------------------------------------------------------------------------

def test_read_file_truncates_head():
    """read_file returns first 8000 chars (head), not tail."""
    from agents.coder.agent import _FILE_HEAD_LIMIT
    assert _FILE_HEAD_LIMIT == 8000


def test_shell_tail_limit():
    """run_shell truncation limit is 3000 chars."""
    from agents.coder.agent import _SHELL_TAIL_LIMIT
    assert _SHELL_TAIL_LIMIT == 3000


# ---------------------------------------------------------------------------
# save_progress tool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_progress_writes_to_task_namespace():
    """save_progress calls add_memory with task-{id} namespace."""
    from agents.coder.agent import save_progress

    added: list[tuple[str, str]] = []

    async def mock_add_memory(content: str, agent_id: str) -> str:
        added.append((content, agent_id))
        return "saved"

    with patch("agents.coder.agent.add_memory", side_effect=mock_add_memory):
        result = await save_progress(
            task_id=42,
            done=["PostList"],
            remaining=["PostPage", "DarkMode"],
            notes="PostList done, working on PostPage next",
        )

    assert result == "checkpoint saved — 2 items remaining"
    assert len(added) == 1
    content, ns = added[0]
    assert ns == "task-42"
    assert "CHECKPOINT task-42" in content
    assert "PostList" in content
    assert "PostPage" in content

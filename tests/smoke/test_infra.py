"""Smoke tests: live service health checks. Run with SMOKE_TESTS=1."""
import os
import subprocess

import httpx
import pytest

pytestmark = pytest.mark.smoke

if not os.environ.get("SMOKE_TESTS"):
    pytest.skip("Set SMOKE_TESTS=1 to run smoke tests", allow_module_level=True)


# ---------------------------------------------------------------------------
# Secret helpers — pull from k8s secrets at test collection time.
# Falls back to env vars so tests can also run with LITELLM_API_KEY=xxx etc.
# ---------------------------------------------------------------------------

def _k8s_secret(namespace: str, secret: str, key: str) -> str:
    try:
        out = subprocess.check_output(
            ["kubectl", "get", "secret", "-n", namespace, secret, "-o", f"jsonpath={{.data.{key}}}"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        import base64
        return base64.b64decode(out).decode().strip()
    except Exception:
        return ""


LITELLM_API_KEY = (
    os.environ.get("LITELLM_API_KEY")
    or _k8s_secret("litellm", "litellm-secrets", "master-key")
)
QDRANT_API_KEY = (
    os.environ.get("QDRANT_API_KEY")
    or _k8s_secret("default", "qdrant-api-key", "api-key")  # fallback if not in env
)
MEM0_API_KEY = (
    os.environ.get("MEM0_API_KEY")
    or _k8s_secret("mem0", "mem0-auth-secrets", "admin-api-key")
)
VIKUNJA_TOKEN = (
    os.environ.get("VIKUNJA_TOKEN_PRAETOR")
    or _k8s_secret("praetor", "praetor-webhook-secrets", "vikunja-token")
    or os.environ.get("VIKUNJA_TOKEN", "")
)

# Qdrant API key is stored in docker env on mac-mini — pull via SSH if not already found
if not QDRANT_API_KEY:
    try:
        out = subprocess.check_output(
            ["ssh", "mini", "docker exec qdrant env 2>/dev/null"],
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        for line in out.decode().splitlines():
            if "QDRANT__SERVICE__API_KEY" in line:
                QDRANT_API_KEY = line.split("=", 1)[1].strip()
                break
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Phase 1: LiteLLM
# ---------------------------------------------------------------------------

class TestPhase1LiteLLM:
    def test_litellm_health(self):
        if not LITELLM_API_KEY:
            pytest.skip("LITELLM_API_KEY not available")
        resp = httpx.get(
            "https://litellm.amer.dev/health",
            headers={"Authorization": f"Bearer {LITELLM_API_KEY}"},
            timeout=60,  # health probes all backends live, slow if any are unhealthy
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "healthy_endpoints" in data
        assert data["healthy_count"] > 0, f"No healthy endpoints: {data}"

    def test_litellm_lists_models(self):
        if not LITELLM_API_KEY:
            pytest.skip("LITELLM_API_KEY not available")
        resp = httpx.get(
            "https://litellm.amer.dev/v1/models",
            headers={"Authorization": f"Bearer {LITELLM_API_KEY}"},
            timeout=10,
        )
        assert resp.status_code == 200
        data = resp.json()
        model_ids = [m["id"] for m in data.get("data", [])]
        assert "qwen3-35b" in model_ids, f"qwen3-35b not in model list: {model_ids}"

    def test_litellm_chat_completion(self):
        if not LITELLM_API_KEY:
            pytest.skip("LITELLM_API_KEY not available")
        resp = httpx.post(
            "https://litellm.amer.dev/v1/chat/completions",
            json={
                "model": "qwen3-35b",
                "messages": [{"role": "user", "content": "Reply with just the word: ok"}],
                "max_tokens": 10,
            },
            headers={"Authorization": f"Bearer {LITELLM_API_KEY}"},
            timeout=120,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "choices" in data
        assert data["choices"][0]["message"]["content"]

    def test_litellm_response_is_openai_format(self):
        if not LITELLM_API_KEY:
            pytest.skip("LITELLM_API_KEY not available")
        resp = httpx.post(
            "https://litellm.amer.dev/v1/chat/completions",
            json={"model": "qwen3-35b", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
            headers={"Authorization": f"Bearer {LITELLM_API_KEY}"},
            timeout=120,
        )
        data = resp.json()
        for field in ("id", "object", "choices", "model"):
            assert field in data, f"missing field: {field}"


# ---------------------------------------------------------------------------
# Phase 2: Qdrant
# ---------------------------------------------------------------------------

class TestPhase2Qdrant:
    def test_qdrant_health(self):
        resp = httpx.get("http://10.100.20.18:6333/healthz", timeout=10)
        assert resp.status_code == 200
        # Qdrant /healthz returns plain text, not JSON
        assert "healthz check passed" in resp.text

    def test_qdrant_collections_endpoint(self):
        if not QDRANT_API_KEY:
            pytest.skip("QDRANT_API_KEY not available")
        resp = httpx.get(
            "http://10.100.20.18:6333/collections",
            headers={"api-key": QDRANT_API_KEY},
            timeout=10,
        )
        assert resp.status_code == 200
        assert "result" in resp.json()

    def test_qdrant_vector_crud(self):
        if not QDRANT_API_KEY:
            pytest.skip("QDRANT_API_KEY not available")
        import time
        collection = f"smoke-test-{int(time.time())}"
        base = "http://10.100.20.18:6333"
        headers = {"api-key": QDRANT_API_KEY, "Content-Type": "application/json"}

        # Create (optimizer workers can take 30s+ to spin up on first collection)
        resp = httpx.put(
            f"{base}/collections/{collection}",
            json={"vectors": {"size": 4, "distance": "Cosine"}},
            headers=headers,
            timeout=60,
        )
        assert resp.status_code in (200, 201), f"create failed: {resp.text}"

        # Insert
        resp = httpx.put(
            f"{base}/collections/{collection}/points",
            json={"points": [{"id": 1, "vector": [0.1, 0.2, 0.3, 0.4]}]},
            headers=headers,
            timeout=30,
        )
        assert resp.status_code == 200

        # Search
        resp = httpx.post(
            f"{base}/collections/{collection}/points/search",
            json={"vector": [0.1, 0.2, 0.3, 0.4], "limit": 1},
            headers=headers,
            timeout=30,
        )
        assert resp.status_code == 200
        assert len(resp.json()["result"]) == 1

        # Delete collection
        resp = httpx.delete(f"{base}/collections/{collection}", headers=headers, timeout=30)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Phase 3: Hatchet
# ---------------------------------------------------------------------------

class TestPhase3Hatchet:
    def test_hatchet_ui_accessible(self):
        resp = httpx.get("https://hatchet.amer.dev", timeout=15, follow_redirects=True)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Phase 4: Mem0
# ---------------------------------------------------------------------------

class TestPhase4Mem0:
    """
    Self-hosted Mem0 uses x-api-key header (not Authorization: Bearer).
    API paths: /memories, /search (no /v1/ prefix).
    Response format: {"results": [...]} not bare list.
    Use infer=false to bypass LLM extraction and store content directly.
    """

    def test_mem0_empty_namespace(self):
        if not MEM0_API_KEY:
            pytest.skip("MEM0_API_KEY not available")
        import time
        agent_id = f"smoke-empty-{int(time.time())}"
        resp = httpx.get(
            "https://mem0.amer.dev/memories",
            params={"agent_id": agent_id},
            headers={"x-api-key": MEM0_API_KEY},
            timeout=15,
        )
        assert resp.status_code == 200
        assert resp.json().get("results", []) == []

    def test_mem0_add_and_retrieve(self):
        if not MEM0_API_KEY:
            pytest.skip("MEM0_API_KEY not available")
        import time
        agent_id = f"smoke-write-{int(time.time())}"
        headers = {"x-api-key": MEM0_API_KEY, "Content-Type": "application/json"}
        base = "https://mem0.amer.dev"

        # Add with infer=false (bypasses LLM extraction — content stored verbatim)
        resp = httpx.post(
            f"{base}/memories",
            json={
                "messages": [{"role": "user", "content": "smoke test fact stored directly"}],
                "agent_id": agent_id,
                "infer": False,
            },
            headers=headers,
            timeout=30,
        )
        assert resp.status_code in (200, 201), f"add failed: {resp.text[:200]}"
        assert len(resp.json().get("results", [])) > 0

        # Search
        resp = httpx.post(
            f"{base}/search",
            json={"query": "smoke test fact", "agent_id": agent_id},
            headers=headers,
            timeout=30,
        )
        assert resp.status_code == 200
        results = resp.json().get("results", [])
        assert any("smoke" in r.get("memory", "").lower() for r in results), \
            f"written memory not found in search results: {results}"


# ---------------------------------------------------------------------------
# Phase 5: Praetor webhook adapter
# ---------------------------------------------------------------------------

class TestPhase5PraetorAdapter:
    def test_praetor_healthz(self):
        resp = httpx.get("https://praetor.amer.dev/healthz", timeout=10)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_vikunja_webhook_registered(self):
        if not VIKUNJA_TOKEN:
            pytest.skip("VIKUNJA_TOKEN not available")
        resp = httpx.get(
            "https://todo.amer.dev/api/v1/projects/21/webhooks",
            headers={"Authorization": f"Bearer {VIKUNJA_TOKEN}"},
            timeout=10,
        )
        assert resp.status_code == 200, f"Vikunja API returned {resp.status_code}"
        webhooks = resp.json() or []
        target_urls = [wh.get("target_url", "") for wh in webhooks]
        assert any("praetor.amer.dev/webhooks/vikunja" in url for url in target_urls), \
            f"Vikunja webhook not registered. Found: {target_urls}"


# ---------------------------------------------------------------------------
# Phase 9: Langfuse (may not be deployed yet)
# ---------------------------------------------------------------------------

LANGFUSE_PUBLIC_KEY = (
    os.environ.get("LANGFUSE_PUBLIC_KEY")
    or _k8s_secret("praetor", "praetor-research-secrets", "langfuse-public-key")
)
LANGFUSE_SECRET_KEY = (
    os.environ.get("LANGFUSE_SECRET_KEY")
    or _k8s_secret("praetor", "praetor-research-secrets", "langfuse-secret-key")
)


class TestPhase9Langfuse:
    def test_langfuse_ui_accessible(self):
        try:
            resp = httpx.get("https://langfuse.amer.dev", timeout=10, follow_redirects=True)
            assert resp.status_code == 200
        except httpx.ConnectError:
            pytest.skip("langfuse.amer.dev DNS not resolving — Langfuse not yet deployed")

    def test_langfuse_api_reachable(self):
        if not LANGFUSE_PUBLIC_KEY or not LANGFUSE_SECRET_KEY:
            pytest.skip("Langfuse keys not in praetor-research-secrets")
        resp = httpx.get(
            "https://langfuse.amer.dev/api/public/projects",
            auth=(LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY),
            timeout=10,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data.get("data", [])) > 0, "no Langfuse projects found — first-login setup not completed"

    def test_coder_system_prompt_exists(self):
        if not LANGFUSE_PUBLIC_KEY or not LANGFUSE_SECRET_KEY:
            pytest.skip("Langfuse keys not in praetor-research-secrets")
        # GET /prompts?name=X returns a single prompt object (not a paginated list)
        resp = httpx.get(
            "https://langfuse.amer.dev/api/public/prompts",
            params={"name": "coder-system"},
            auth=(LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY),
            timeout=10,
        )
        assert resp.status_code == 200, f"coder-system prompt not found: {resp.text[:200]}"
        data = resp.json()
        assert "production" in data.get("labels", []), \
            f"coder-system prompt exists but is not labeled 'production': labels={data.get('labels')}"

    def test_research_system_prompt_exists(self):
        if not LANGFUSE_PUBLIC_KEY or not LANGFUSE_SECRET_KEY:
            pytest.skip("Langfuse keys not in praetor-research-secrets")
        # GET /prompts?name=X returns a single prompt object (not a paginated list)
        resp = httpx.get(
            "https://langfuse.amer.dev/api/public/prompts",
            params={"name": "research-system"},
            auth=(LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY),
            timeout=10,
        )
        assert resp.status_code == 200, f"research-system prompt not found: {resp.text[:200]}"
        data = resp.json()
        assert "production" in data.get("labels", []), \
            f"research-system prompt exists but is not labeled 'production': labels={data.get('labels')}"


# ---------------------------------------------------------------------------
# Phase 10: LiteLLM MCP Gateway (not started yet — tests skip until deployed)
# ---------------------------------------------------------------------------

LITELLM_API_KEY = (
    os.environ.get("LITELLM_API_KEY")
    or _k8s_secret("litellm", "litellm-secrets", "master-key")
)


def _mcp_tools_list(api_key: str) -> list[dict]:
    """Fetch the MCP tools list via JSON-RPC over Streamable HTTP.

    The /mcp/ endpoint requires Accept: text/event-stream and returns
    a single SSE 'data:' line with the JSON-RPC response.
    """
    import json as _json
    resp = httpx.post(
        "https://litellm.amer.dev/mcp/",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        timeout=15,
    )
    assert resp.status_code == 200, f"MCP tools/list returned {resp.status_code}: {resp.text[:200]}"
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            payload = _json.loads(line[len("data:"):].strip())
            return payload["result"]["tools"]
    raise AssertionError(f"No SSE data line in MCP response: {resp.text[:500]}")


class TestPhase10MCPGateway:
    def test_mcp_tools_endpoint_exists(self):
        if not LITELLM_API_KEY:
            pytest.skip("LITELLM_API_KEY not available")
        try:
            tools = _mcp_tools_list(LITELLM_API_KEY)
        except httpx.ConnectError:
            pytest.skip("MCP gateway endpoint not reachable — Phase 10 not yet deployed")
        assert isinstance(tools, list) and len(tools) > 0, f"expected non-empty tool list, got: {tools}"

    def test_mcp_tools_include_web_search(self):
        if not LITELLM_API_KEY:
            pytest.skip("LITELLM_API_KEY not available")
        try:
            tools = _mcp_tools_list(LITELLM_API_KEY)
        except httpx.ConnectError:
            pytest.skip("MCP gateway not reachable — Phase 10 not yet deployed")
        tool_names = [t.get("name", "") for t in tools]
        assert any("search" in n.lower() for n in tool_names), \
            f"searxng web_search tool not in gateway tool list: {tool_names}"

"""FastAPI router: intelligent MCP agent — research, decide, register or scaffold."""
from __future__ import annotations

import json
import logging
import os
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, field_validator

from common.dispatch import dispatch_agent
from webhooks.mcp_factory import _load_registry, McpRegistration

logger = logging.getLogger(__name__)
router = APIRouter()
_bearer = HTTPBearer(auto_error=False)

_LITELLM_BASE = os.environ.get("LITELLM_BASE_URL", "http://litellm.praetor.svc.cluster.local/v1")
_LITELLM_KEY = os.environ.get("LITELLM_API_KEY", "")
_PRAETOR_BASE = os.environ.get("PRAETOR_BASE_URL", "http://localhost:8000")
_PRAETOR_API_KEY_ENV = "PRAETOR_API_KEY"

CONFIDENCE_THRESHOLD = 0.85


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _check_auth(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    expected = os.environ.get(_PRAETOR_API_KEY_ENV, "")
    if not expected:
        raise HTTPException(status_code=500, detail="PRAETOR_API_KEY not configured on server")
    token = creds.credentials if creds else ""
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class McpRequest(BaseModel):
    capability: str
    preferred_name: str | None = None

    @field_validator("capability")
    @classmethod
    def _validate_capability(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 10:
            raise ValueError("capability must be at least 10 characters")
        return v


class McpResearchResult(BaseModel):
    found: bool
    confidence: float
    image: str | None = None
    name: str | None = None
    notes: str


class McpRequestResponse(BaseModel):
    decision: str             # "already_registered" | "use_existing" | "scaffold_new"
    task_id: int | None = None
    pr_url: str | None = None
    image: str | None = None
    research_summary: str
    message: str


# ---------------------------------------------------------------------------
# Registry duplicate check
# ---------------------------------------------------------------------------

def _find_in_registry(registry: dict, capability: str) -> str | None:
    """Return the MCP name if capability keywords match an existing registry entry.

    Matching strategy (in order):
    1. Exact substring: registry name appears verbatim in capability.
    2. Word-level: any significant word from the name (minus "mcp-" prefix, ≥3 chars)
       appears in the capability string.
    """
    cap_lower = capability.lower()
    for name in registry:
        name_lower = name.lower()
        if name_lower in cap_lower:
            return name
        parts = [p for p in name_lower.replace("mcp-", "").split("-") if len(p) >= 3]
        if any(part in cap_lower for part in parts):
            return name
    return None


# ---------------------------------------------------------------------------
# Inline LLM research
# ---------------------------------------------------------------------------

_RESEARCH_SYSTEM = """\
You are an MCP server discovery agent. Given a capability description, determine whether \
a well-known, production-ready MCP server already exists that provides it.

Search your knowledge of:
1. Smithery (smithery.ai) — largest MCP index
2. mcp.so — community registry
3. ModelContextProtocol GitHub org — reference implementations
4. Popular MCP servers on GitHub (mcp-server-* repos)

Return ONLY a JSON object with these fields:
{
  "found": <true|false>,
  "confidence": <0.0 to 1.0>,
  "image": "<docker-image:tag or null>",
  "name": "<canonical-mcp-name or null>",
  "notes": "<2-3 sentence summary of what you found or why nothing matched>"
}

Confidence guidance:
- 0.9+: well-known server, Docker image on public registry, actively maintained
- 0.7-0.9: server exists but image uncertain or maintenance unclear
- 0.5-0.7: partial match or multiple candidates
- <0.5: no strong match found
"""


async def _research_mcp(capability: str) -> McpResearchResult:
    """Ask the LLM to discover existing MCP servers for the given capability."""
    payload = {
        "model": os.environ.get("LLM_MODEL", "qwen3-35b"),
        "messages": [
            {"role": "system", "content": _RESEARCH_SYSTEM},
            {"role": "user", "content": f"Find an MCP server for: {capability}"},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 512,
    }
    headers = {
        "Authorization": f"Bearer {_LITELLM_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{_LITELLM_BASE}/chat/completions", headers=headers, json=payload)
    if resp.status_code != 200:
        logger.error("LLM research call failed (%s): %s", resp.status_code, resp.text[:300])
        return McpResearchResult(
            found=False,
            confidence=0.0,
            notes=f"Research failed: LiteLLM returned {resp.status_code}",
        )
    try:
        content = resp.json()["choices"][0]["message"]["content"] or ""
        # Strip markdown code fences (llama.cpp doesn't strictly enforce json_object mode)
        content = content.strip()
        if content.startswith("```"):
            # ```json ... ``` or ``` ... ```
            inner = content.split("```", 2)
            content = inner[1].lstrip("json").strip() if len(inner) > 1 else content
        data = json.loads(content)
        return McpResearchResult(
            found=bool(data.get("found", False)),
            confidence=float(data.get("confidence", 0.0)),
            image=data.get("image") or None,
            name=data.get("name") or None,
            notes=str(data.get("notes", "")),
        )
    except Exception as exc:
        logger.error("failed to parse LLM research response: %s", exc)
        return McpResearchResult(found=False, confidence=0.0, notes=f"Parse error: {exc}")


# ---------------------------------------------------------------------------
# Path A: register existing image
# ---------------------------------------------------------------------------

async def _register_existing(research: McpResearchResult, req: McpRequest, api_key: str) -> str:
    """Call POST /api/v1/mcp/register and return the PR URL."""
    name = req.preferred_name or research.name or ""
    if not name:
        raise HTTPException(status_code=422, detail="could not determine MCP name from research results")

    registration = McpRegistration(name=name, image=research.image, port=8000)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_PRAETOR_BASE}/api/v1/mcp/register",
            json=registration.model_dump(),
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail=resp.json().get("detail", "already registered"))
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"mcp/register failed ({resp.status_code}): {resp.text[:200]}",
        )
    return resp.json()["pr_url"]


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.post(
    "/api/v1/mcp/request",
    response_model=McpRequestResponse,
    dependencies=[Depends(_check_auth)],
)
async def request_mcp(req: McpRequest) -> McpRequestResponse:
    """
    Intelligent MCP agent: find or scaffold an MCP server for a requested capability.

    Steps:
    1. Check the praetor registry — return immediately if already registered.
    2. Ask the LLM to search for existing production MCP servers.
    3a. confidence >= 0.85 + image found → call mcp/register (opens GitOps PR).
    3b. Otherwise → dispatch scaffold worker to build a new MCP from scratch.
    """
    api_key = os.environ.get(_PRAETOR_API_KEY_ENV, "")

    # Step 1 — duplicate check
    registry = await _load_registry()
    existing_name = _find_in_registry(registry, req.capability)
    if existing_name:
        entry = registry[existing_name]
        return McpRequestResponse(
            decision="already_registered",
            pr_url=entry.get("pr_url"),
            image=entry.get("image"),
            research_summary=f"'{existing_name}' is already registered in the praetor MCP registry.",
            message=f"MCP '{existing_name}' already covers this capability — no new registration needed.",
        )

    # Step 2 — LLM research
    research = await _research_mcp(req.capability)
    logger.info(
        "mcp-request: capability=%r found=%s confidence=%.2f image=%s",
        req.capability, research.found, research.confidence, research.image,
    )

    # Step 3a — use existing image
    if research.found and research.confidence >= CONFIDENCE_THRESHOLD and research.image:
        try:
            pr_url = await _register_existing(research, req, api_key)
            return McpRequestResponse(
                decision="use_existing",
                pr_url=pr_url,
                image=research.image,
                research_summary=research.notes,
                message=(
                    f"Found existing MCP server '{research.name}' with confidence {research.confidence:.0%}. "
                    f"Registration PR opened: {pr_url}"
                ),
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("mcp-request: register_existing failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"registration failed: {exc}") from exc

    # Step 3b — scaffold new MCP
    name = req.preferred_name or (research.name if research.name else None)
    if not name:
        # derive from capability: take first two words, kebab-case, prefix mcp-
        words = req.capability.lower().split()[:2]
        name = "mcp-" + "-".join(w.strip(".,;:!?") for w in words if w.strip(".,;:!?"))

    task_id = int(time.time())
    description = (
        f"Scaffold a new MCP server named '{name}'.\n"
        f"Capability: {req.capability}\n"
        f"Research notes: {research.notes}\n"
        f"\n"
        f"Create dean-mcp/{name}/server.py using FastMCP with the appropriate tools, "
        f"a Dockerfile, and a /health endpoint. Open a PR on amerenda/dean-mcp."
    )
    try:
        dispatch_agent(task_id, f"Scaffold MCP: {name}", description, "scaffold")
        logger.info("mcp-request: scaffold dispatched task_id=%s name=%s", task_id, name)
    except Exception as exc:
        logger.error("mcp-request: scaffold dispatch failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"scaffold dispatch failed: {exc}") from exc

    return McpRequestResponse(
        decision="scaffold_new",
        task_id=task_id,
        research_summary=research.notes,
        message=(
            f"No existing MCP found (confidence {research.confidence:.0%}). "
            f"Scaffold worker dispatched for '{name}' (task_id={task_id}). "
            f"Track at https://hatchet.amer.dev — scaffold worker will open a PR on amerenda/dean-mcp."
        ),
    )

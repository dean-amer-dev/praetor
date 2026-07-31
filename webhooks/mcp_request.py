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
_SEARXNG_URL = os.environ.get("SEARXNG_URL", "https://searxng.amer.dev")

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
    port: int | None = None                  # container port (if known)
    requires_k8s_sa: bool = False            # needs in-cluster Kubernetes service account
    env_vars: dict[str, str] = {}            # non-secret env vars required by the server
    env_secrets: dict[str, str] = {}         # {env_var_name: bws_secret_key} pairs
    args: list[str] = []                     # container entrypoint args (e.g. ["fastmcp","run","config.json"])


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

KNOWN PRODUCTION-READY MCP SERVERS — use these exact values:
- Kubernetes cluster management (get pods/deployments/logs/events, scale, apply):
  image="flux159/mcp-server-kubernetes:latest", port=3000, requires_k8s_sa=true,
  env_vars={"HOST":"0.0.0.0","ENABLE_UNSAFE_STREAMABLE_HTTP_TRANSPORT":"true","ALLOW_ONLY_READONLY_TOOLS":"true"}
- GitHub repository operations:
  image="ghcr.io/github/github-mcp-server:latest", port=8080
- Home Assistant (entity states, events, automations, error logs):
  image="ghcr.io/homeassistant-ai/ha-mcp:4.12.0", port=8086,
  args=["fastmcp","run","fastmcp-http.json"],
  env_vars={"HOMEASSISTANT_URL": "<ha_url>"},
  env_secrets={"HOMEASSISTANT_TOKEN": "<bws_key_for_ha_token>"}
- Web search (SearXNG): internal deployment only, do not suggest a public image

Return ONLY a JSON object with these exact fields:
{
  "found": <true|false>,
  "confidence": <0.0 to 1.0>,
  "image": "<docker-image:tag or null>",
  "name": "<canonical-mcp-name or null>",
  "notes": "<2-3 sentence summary of what you found or why nothing matched>",
  "port": <container port as integer, or null if unknown>,
  "requires_k8s_sa": <true if MCP needs in-cluster Kubernetes API access, otherwise false>,
  "env_vars": <object of non-secret env var name→value pairs, e.g. {"HOST":"0.0.0.0"}, or {}>,
  "env_secrets": <object of {env_var_name: bws_secret_key} for secrets the server needs, e.g. {"API_TOKEN": "my-service-token"}, or {}>,
  "args": <list of container entrypoint args if required, e.g. ["fastmcp","run","fastmcp-http.json"], or []>
}

For "env_secrets": if the capability description mentions a token, password, or credential needed by the server,
extract it as {ENV_VAR_NAME: bws-secret-key}. Parse bws key from "from BWS <key>" or "BWS key: <key>" patterns.
Example: if capability says "HOMEASSISTANT_TOKEN from BWS home-assistant-access-token", output:
  "env_secrets": {"HOMEASSISTANT_TOKEN": "home-assistant-access-token"}

IMPORTANT: Only return images you are certain exist on public container registries. \
Do NOT guess or hallucinate image names — return null if you are not confident. \
If unsure about the image, set confidence below 0.85.

Confidence guidance:
- 0.9+: well-known server, Docker image on public registry, actively maintained
- 0.7-0.9: server exists but image uncertain or maintenance unclear
- 0.5-0.7: partial match or multiple candidates
- <0.5: no strong match found
"""


async def _searxng_search(query: str, max_results: int = 5) -> str:
    """Search the web via the self-hosted SearXNG instance. Returns formatted text, or a
    'No results' / error string on failure — never raises, so research can proceed without it."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_SEARXNG_URL}/search",
                params={"q": query, "format": "json", "engines": "google,bing,duckduckgo,github"},
            )
            resp.raise_for_status()
            data = resp.json()
        results = data.get("results", [])[:max_results]
        if not results:
            return "No search results found."
        lines = []
        for r in results:
            title = r.get("title", "")
            url = r.get("url", "")
            snippet = r.get("content", "")[:300]
            lines.append(f"- {title}\n  {url}\n  {snippet}")
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("mcp-request: searxng search failed (%s) — proceeding without it", exc)
        return "Search unavailable."


async def _research_mcp(capability: str) -> McpResearchResult:
    """Search the web for existing MCP servers, then ask the LLM to evaluate the results."""
    search_results = await _searxng_search(f"MCP server (Model Context Protocol) {capability}")

    payload = {
        "model": os.environ.get("LLM_MODEL", "coder"),
        "messages": [
            {"role": "system", "content": _RESEARCH_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Find an MCP server for: {capability}\n\n"
                    f"Web search results:\n{search_results}\n\n"
                    "Base your answer on these search results where they name a real, specific "
                    "server/image/repo. If the results are generic, unrelated, or say "
                    "'Search unavailable' / 'No search results found', treat this the same as "
                    "finding nothing — do not fall back to guessing from memory, and keep "
                    "confidence below 0.85 unless a search result directly confirms a public "
                    "Docker image or GitHub repo for an MCP server."
                ),
            },
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
        raw_env = data.get("env_vars") or {}
        env_vars = {str(k): str(v) for k, v in raw_env.items()} if isinstance(raw_env, dict) else {}
        raw_secrets = data.get("env_secrets") or {}
        env_secrets = {str(k): str(v) for k, v in raw_secrets.items()} if isinstance(raw_secrets, dict) else {}
        raw_args = data.get("args") or []
        args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
        return McpResearchResult(
            found=bool(data.get("found", False)),
            confidence=float(data.get("confidence", 0.0)),
            image=data.get("image") or None,
            name=data.get("name") or None,
            notes=str(data.get("notes", "")),
            port=int(data["port"]) if data.get("port") else None,
            requires_k8s_sa=bool(data.get("requires_k8s_sa", False)),
            env_vars=env_vars,
            env_secrets=env_secrets,
            args=args,
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

    sa_name = f"{name}-sa" if research.requires_k8s_sa else None
    cluster_role = "view" if research.requires_k8s_sa else None

    registration = McpRegistration(
        name=name,
        image=research.image,
        port=research.port or 8000,
        env_vars=research.env_vars,
        env_secrets=research.env_secrets,
        args=research.args,
        service_account_name=sa_name,
        cluster_role=cluster_role,
    )
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
        f"repo: amerenda/dean-mcp\n"
        f"scaffold: true\n"
        f"Scaffold a new MCP server named '{name}'.\n"
        f"Capability: {req.capability}\n"
        f"Research notes: {research.notes}\n"
        f"\n"
        f"Create {name}/server.py in this repo using FastMCP with the appropriate tools, "
        f"a Dockerfile, and a /health endpoint. Follow the existing sibling MCP servers in "
        f"this repo (e.g. mcp-searxng, secure-search-mcp) for project layout and conventions.\n"
        f"\n"
        f"Also create {name}/mcp.json — this is the single source of truth the MCP factory "
        f"deployment manifest gets generated from later, so it must match the server code "
        f"exactly (container port, health path, and every env var/secret the server actually "
        f"reads). Shape:\n"
        f'{{\n'
        f'  "port": 8000,\n'
        f'  "health_path": null,\n'
        f'  "env_vars": {{}},\n'
        f'  "env_secrets": {{}},\n'
        f'  "service_account_name": null,\n'
        f'  "cluster_role": null\n'
        f'}}\n'
        f'"env_vars" is {{ENV_VAR_NAME: literal_value}} for non-secret config; "env_secrets" is '
        f'{{ENV_VAR_NAME: bws_secret_key}} for anything that must come from Bitwarden — never '
        f'hardcode a credential in server.py or mcp.json itself. Leave health_path null and '
        f'service_account_name/cluster_role null unless the server actually implements a '
        f'/health endpoint or needs in-cluster Kubernetes API access.\n'
        f"\n"
        f"Also add .github/workflows/build-{name}.yaml: copy the structure of an existing "
        f"sibling workflow (e.g. build-mcp-searxng.yaml) for the test/build/push jobs "
        f"(building and pushing amerenda/{name}:latest and amerenda/{name}:sha-<short-sha> on "
        f"push to main), but do NOT include a 'deploy' job that opens a k3s-dean-gitops PR — "
        f"unlike the older hand-written sibling servers, this one is deployed by a separate "
        f"MCP factory registration call, not by CI.\n"
        f"\n"
        f"At the end of the build job (after the image push step), add one more step that "
        f"posts {name}/mcp.json to the praetor MCP factory's CI callback so the first "
        f"successful build auto-opens the registration PR — no manual /mcp/register call "
        f"needed:\n"
        f'      - name: Notify MCP factory\n'
        f'        env:\n'
        f'          PRAETOR_API_KEY: ${{{{ secrets.PRAETOR_API_KEY }}}}\n'
        f'        run: |\n'
        f'          curl -sf -X POST https://praetor.amer.dev/api/v1/mcp/register-from-ci \\\n'
        f'            -H "Authorization: Bearer ${{PRAETOR_API_KEY}}" \\\n'
        f'            -H "Content-Type: application/json" \\\n'
        f'            -d "$(python3 -c \'import json; d=json.load(open("{name}/mcp.json")); '
        f'd["name"]="{name}"; d["image"]="${{{{ env.IMAGE }}}}:latest"; print(json.dumps(d))\')"\n'
        f"PRAETOR_API_KEY is already available as a dean-mcp repo Actions secret — reference it "
        f"exactly as above, do not create or modify it. Open a PR."
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

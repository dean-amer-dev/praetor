"""FastAPI router: model switch endpoint — hot-swap the active llama.cpp model on murderbot.

Calls model-switcher's async /switch, then polls /status until terminal state.
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()
_bearer = HTTPBearer(auto_error=False)

_PRAETOR_API_KEY = os.environ.get("PRAETOR_API_KEY", "")
_LLM_SWITCH_URL = os.environ.get("LLM_SWITCH_URL", "http://10.100.20.19:8091")
_LLM_SWITCH_PSK = os.environ.get("LLM_SWITCH_PSK", "")

# Friendly name → GGUF path on the host (inside the MODEL_VOLUME mount)
_KNOWN_MODELS: dict[str, str] = {
    "qwen3-35b": "/mnt/models/llms/qwen36/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
    "qwen36-unrestricted": "/mnt/models/llms/qwen36/Qwen3.6-35B-A3B-uncensored-Q4_K_M.gguf",
}

_POLL_INTERVAL = 5      # seconds between status polls
_POLL_TIMEOUT = 420     # max seconds to wait for terminal state (7 min)


def _check_auth(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    if not _PRAETOR_API_KEY:
        raise HTTPException(status_code=500, detail="PRAETOR_API_KEY not configured on server")
    if creds is None or creds.credentials != _PRAETOR_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


class ModelSwitchRequest(BaseModel):
    model: str


class ModelSwitchResponse(BaseModel):
    status: str
    model: str
    path: str
    message: str
    error: str | None = None
    error_detail: str | None = None


@router.post("/api/v1/model/switch", response_model=ModelSwitchResponse)
async def switch_model(
    req: ModelSwitchRequest,
    _: None = Depends(_check_auth),
) -> ModelSwitchResponse:
    """
    Switch the active llama.cpp model on murderbot.

    Initiates an async model swap and waits for the terminal state.
    Valid model names: qwen3-35b, qwen36-unrestricted
    """
    if not _LLM_SWITCH_PSK:
        raise HTTPException(status_code=500, detail="LLM_SWITCH_PSK not configured")

    model_path = _KNOWN_MODELS.get(req.model)
    if not model_path:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown model {req.model!r}. Valid: {list(_KNOWN_MODELS)}",
        )

    headers = {"x-psk": _LLM_SWITCH_PSK}

    async with httpx.AsyncClient(timeout=30) as client:
        # Initiate the switch — model-switcher returns immediately
        try:
            resp = await client.post(
                f"{_LLM_SWITCH_URL}/switch",
                json={"path": model_path},
                headers=headers,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"model-switcher unreachable: {exc}")

        if resp.status_code == 409:
            raise HTTPException(status_code=409, detail=resp.json().get("detail", "Switch already in progress"))
        if not resp.is_success:
            raise HTTPException(
                status_code=502,
                detail=f"model-switcher returned {resp.status_code}: {resp.text[:300]}",
            )

        # Poll /status until terminal
        elapsed = 0
        data: dict = {}
        while elapsed < _POLL_TIMEOUT:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL
            try:
                sr = await client.get(f"{_LLM_SWITCH_URL}/status", headers=headers)
                data = sr.json()
            except Exception as exc:
                logger.warning("Status poll failed (%ds elapsed): %s", elapsed, exc)
                continue

            state = data.get("state", "")
            logger.info("model-switcher state=%s (%ds elapsed)", state, elapsed)

            if state == "ready":
                return ModelSwitchResponse(
                    status="ready",
                    model=req.model,
                    path=model_path,
                    message=f"Model {req.model!r} is loaded and ready.",
                )
            if state.startswith("error_"):
                return ModelSwitchResponse(
                    status=state,
                    model=req.model,
                    path=model_path,
                    message=f"Switch failed: {state}",
                    error=data.get("error"),
                    error_detail=data.get("error_detail"),
                )

        # Timed out polling
        return ModelSwitchResponse(
            status="error_poll_timeout",
            model=req.model,
            path=model_path,
            message=f"Praetor gave up polling after {_POLL_TIMEOUT}s. "
                    f"Last state: {data.get('state', 'unknown')}. "
                    f"Check GET /status on model-switcher directly.",
            error="error_poll_timeout",
            error_detail=data.get("error_detail"),
        )

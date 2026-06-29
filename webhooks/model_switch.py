"""FastAPI router: model switch endpoint — hot-swap the active llama.cpp model on murderbot."""
from __future__ import annotations

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

_KNOWN_MODELS = ("qwen3-35b", "qwen36-unrestricted")


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
    message: str


@router.post("/api/v1/model/switch", response_model=ModelSwitchResponse)
async def switch_model(
    req: ModelSwitchRequest,
    _: None = Depends(_check_auth),
) -> ModelSwitchResponse:
    """
    Switch the active llama.cpp model on murderbot.

    Stops the running llama-server container and restarts it with the requested model.
    The server takes 2–5 minutes to load — callers should poll /health on :8088.

    Valid model names: qwen3-35b, qwen36-unrestricted
    """
    if not _LLM_SWITCH_PSK:
        raise HTTPException(status_code=500, detail="LLM_SWITCH_PSK not configured")

    if req.model not in _KNOWN_MODELS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown model {req.model!r}. Valid: {list(_KNOWN_MODELS)}",
        )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_LLM_SWITCH_URL}/switch",
                json={"model": req.model},
                headers={"x-psk": _LLM_SWITCH_PSK},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error("model switch failed: %s — %s", exc.response.status_code, exc.response.text)
        raise HTTPException(
            status_code=502,
            detail=f"model-switcher returned {exc.response.status_code}: {exc.response.text[:200]}",
        )
    except Exception as exc:
        logger.error("model switch error: %s", exc)
        raise HTTPException(status_code=502, detail=f"model-switcher unreachable: {exc}")

    return ModelSwitchResponse(
        status=data.get("status", "switching"),
        model=req.model,
        message=data.get("note", "Model switch initiated — server loading in background."),
    )

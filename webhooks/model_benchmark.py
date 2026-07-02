"""FastAPI router: model benchmark endpoint — research + capability eval for a model on a runner.

POST /api/v1/model/benchmark
  Dispatches pipeline:model_evaluate (research → benchmark) via Hatchet.
  Returns immediately with a task_id for status polling.
"""
from __future__ import annotations

import logging
import os
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from agents.model_benchmark.bench import RUNNERS

logger = logging.getLogger(__name__)

router = APIRouter()
_bearer = HTTPBearer(auto_error=False)

_PRAETOR_API_KEY = os.environ.get("PRAETOR_API_KEY", "")


def _check_auth(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    if not _PRAETOR_API_KEY:
        raise HTTPException(status_code=500, detail="PRAETOR_API_KEY not configured")
    if creds is None or creds.credentials != _PRAETOR_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


class ModelBenchmarkRequest(BaseModel):
    model: str
    runner: str = "archlinux"
    quant: str = ""


class ModelBenchmarkResponse(BaseModel):
    task_id: int
    model: str
    runner: str
    event: str
    message: str


@router.post("/api/v1/model/benchmark", response_model=ModelBenchmarkResponse)
async def benchmark_model(
    req: ModelBenchmarkRequest,
    _: None = Depends(_check_auth),
) -> ModelBenchmarkResponse:
    """
    Research a model and run the Phase 29 capability benchmark suite.

    Dispatches pipeline:model_evaluate — research phase runs first (HF card,
    quantization options, context length, tool calling notes), then the
    4-category benchmark suite runs against the specified runner.

    Valid runners: archlinux, mac-mini, murderbot
    """
    if req.runner not in RUNNERS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown runner {req.runner!r}. Valid: {list(RUNNERS)}",
        )

    task_id = int(time.time())
    event = "pipeline:model_evaluate"

    try:
        from hatchet_sdk import Hatchet
        hatchet = Hatchet()
        hatchet.event.push(event, {
            "task_id": task_id,
            "model":   req.model,
            "runner":  req.runner,
            "quant":   req.quant,
        })
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Hatchet dispatch failed: {exc}")

    logger.info("Dispatched model_evaluate for %s @ %s (task_id=%d)", req.model, req.runner, task_id)
    return ModelBenchmarkResponse(
        task_id=task_id,
        model=req.model,
        runner=req.runner,
        event=event,
        message=(
            f"Benchmark pipeline dispatched. Research phase runs first, then the 4-category "
            f"suite against {req.runner}. Results will appear in Langfuse and Mem0 "
            f"under agent_id='model-factory'."
        ),
    )

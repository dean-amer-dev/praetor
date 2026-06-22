"""FastAPI application entrypoint for the praetor webhook adapter."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .vikunja import register_webhook_on_startup, router as vikunja_router
from .github import router as github_router
from .dispatch_api import router as dispatch_router
from .mcp_factory import router as mcp_factory_router
from .app_factory import router as app_factory_router
from .mcp_request import router as mcp_request_router
from .agent_factory import router as agent_factory_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await register_webhook_on_startup()
    yield


app = FastAPI(title="praetor-webhook-adapter", lifespan=lifespan)
app.include_router(vikunja_router)
app.include_router(github_router)
app.include_router(dispatch_router)
app.include_router(mcp_factory_router)
app.include_router(app_factory_router)
app.include_router(mcp_request_router)
app.include_router(agent_factory_router)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}

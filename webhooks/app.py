"""FastAPI application entrypoint for the praetor webhook adapter."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .vikunja import register_webhook_on_startup, router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await register_webhook_on_startup()
    yield


app = FastAPI(title="praetor-webhook-adapter", lifespan=lifespan)
app.include_router(router)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}

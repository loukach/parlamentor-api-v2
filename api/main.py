"""FastAPI application entry point."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.config import settings
from api.db import app_engine, parla_engine

logger = logging.getLogger(__name__)

langfuse_client = None


def _init_langfuse():
    """Initialize Langfuse client, propagating settings to os.environ."""
    global langfuse_client
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        logger.warning("Langfuse keys not configured -- skipping init")
        return
    try:
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
        os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
        os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)
        from langfuse import Langfuse

        langfuse_client = Langfuse()
        logger.info("Langfuse initialized")
    except Exception:
        logger.exception("Langfuse init failed -- continuing without observability")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_langfuse()
    yield
    await app_engine.dispose()
    await parla_engine.dispose()
    if langfuse_client:
        langfuse_client.shutdown()


app = FastAPI(title="Parlamentor", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}

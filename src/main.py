import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from src.api import router
from src.core.config import settings
from src.core.db import engine
from src.core.logging import configure_logging
from src.core.middleware import RequestIDMiddleware, unhandled_exception_handler
from src.core.redis import get_redis_pool
from src.services import meili as meili_svc
from src.services import nats as nats_svc

configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


async def _startup_probes() -> None:
    """Fail fast if critical dependencies are unreachable at startup."""
    import redis.asyncio as aioredis

    # Postgres
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("startup probe: postgres OK")

    # Redis
    client = aioredis.Redis(connection_pool=get_redis_pool())
    await client.ping()
    await client.aclose()
    logger.info("startup probe: redis OK")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    await _startup_probes()
    await nats_svc.connect(settings.nats_url)
    await meili_svc.init(settings.meilisearch_url, settings.meilisearch_api_key or None)

    js = nats_svc.get_js()
    if js is not None:
        from src.subscribers import conversation, indexing
        from src.subscribers import transcription as transcription_sub

        await indexing.start(js)
        await conversation.start(js)
        await transcription_sub.start(js)

    yield

    # --- shutdown ---
    await nats_svc.close()
    await meili_svc.close()


app = FastAPI(title="OpenClaw REST API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)
app.add_middleware(RequestIDMiddleware)

app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(router)

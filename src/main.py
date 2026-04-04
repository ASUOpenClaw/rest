from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.auth import router as auth_router
from src.api.conversations import router as conversations_router
from src.api.file_permissions import router as file_permissions_router
from src.api.files import router as files_router
from src.api.folders import router as folders_router
from src.api.rag import router as rag_router
from src.api.transcribe import router as transcribe_router
from src.api.workspaces import router as workspaces_router
from src.core.config import settings
from src.services import nats as nats_svc


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    await nats_svc.connect(settings.nats_url)

    js = nats_svc.get_js()
    if js is not None:
        from src.subscribers import conversation, indexing

        await indexing.start(js)
        await conversation.start(js)

    yield

    # --- shutdown ---
    await nats_svc.close()


app = FastAPI(title="OpenClaw REST API", version="0.1.0", lifespan=lifespan)

app.include_router(auth_router, prefix="/v1")
app.include_router(workspaces_router, prefix="/v1")
app.include_router(folders_router, prefix="/v1")
app.include_router(files_router, prefix="/v1")
app.include_router(file_permissions_router, prefix="/v1")
app.include_router(rag_router, prefix="/v1")
app.include_router(transcribe_router, prefix="/v1")
app.include_router(conversations_router, prefix="/v1")

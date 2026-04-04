from fastapi import APIRouter

from src.api.auth import router as auth_router
from src.api.conversations import router as conversations_router
from src.api.file_permissions import router as file_permissions_router
from src.api.files import router as files_router
from src.api.folders import router as folders_router
from src.api.rag import router as rag_router
from src.api.transcribe import router as transcribe_router
from src.api.workspaces import router as workspaces_router

router = APIRouter(prefix="/v1")

router.include_router(auth_router)
router.include_router(workspaces_router)
router.include_router(folders_router)
router.include_router(files_router)
router.include_router(file_permissions_router)
router.include_router(rag_router)
router.include_router(transcribe_router)
router.include_router(conversations_router)

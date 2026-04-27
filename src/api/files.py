import uuid
from typing import Annotated, Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.deps import CurrentAnyAuth, CurrentAuth
from src.core.redis import get_redis
from src.schemas.file import (
    DownloadUrlOut,
    FileListOut,
    FileOut,
    FilePatchRequest,
    PublishWorkspaceFileRequest,
    ReindexOut,
)
from src.services import file as file_svc

router = APIRouter(prefix="/workspaces/{workspace_id}/files", tags=["files"])

_SENTINEL = object()  # used to detect whether folder_id was explicitly passed


@router.post("", response_model=FileOut, status_code=status.HTTP_201_CREATED)
async def upload_file(
    workspace_id: uuid.UUID,
    auth: CurrentAnyAuth,
    file: UploadFile = File(...),
    folder_id: uuid.UUID | None = Form(default=None),
    description: str | None = Form(default=None),
    auto_index: bool = Form(default=True),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    return await file_svc.upload_file(
        workspace_id=workspace_id,
        user=auth.user,
        upload=file,
        folder_id=folder_id,
        description=description,
        auto_index=auto_index,
        db=db,
        redis=redis,
    )


@router.get("", response_model=FileListOut)
async def list_files(
    workspace_id: uuid.UUID,
    auth: CurrentAnyAuth,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    folder_id: uuid.UUID | None = Query(default=None),
    root_only: bool = Query(
        default=False, description="List only files in workspace root (folder_id=null)"
    ),
    recursive: bool = Query(default=False),
    mime_type: str | None = Query(default=None),
    search: str | None = Query(default=None),
    indexing_status: str | None = Query(default=None),
    sort_by: Literal["created_at", "name", "size_bytes"] = Query(default="created_at"),
    sort_order: Literal["asc", "desc"] = Query(default="desc"),
    db: AsyncSession = Depends(get_db),
):
    # Determine if folder_id filter is active at all
    folder_id_set = folder_id is not None or root_only
    effective_folder_id = None if root_only else folder_id

    items, total = await file_svc.list_files(
        workspace_id=workspace_id,
        user=auth.user,
        page=page,
        per_page=per_page,
        folder_id=effective_folder_id,
        folder_id_set=folder_id_set,
        recursive=recursive,
        mime_type=mime_type,
        search=search,
        indexing_status=indexing_status,
        sort_by=sort_by,
        sort_order=sort_order,
        db=db,
    )
    return FileListOut(items=items, total=total, page=page, per_page=per_page)


@router.post(
    "/publish-workspace-file",
    response_model=FileOut,
    status_code=status.HTTP_201_CREATED,
)
async def publish_workspace_file(
    workspace_id: uuid.UUID,
    body: PublishWorkspaceFileRequest,
    auth: CurrentAnyAuth,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    return await file_svc.publish_workspace_file(
        workspace_id=workspace_id,
        user=auth.user,
        goclaw_path=body.goclaw_path,
        dest_filename=body.dest_filename,
        folder_id=body.folder_id,
        description=body.description,
        auto_delete=body.auto_delete,
        db=db,
        redis=redis,
    )


@router.get("/{file_id}", response_model=FileOut)
async def get_file(
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    auth: CurrentAnyAuth,
    db: AsyncSession = Depends(get_db),
):
    return await file_svc.get_file(
        workspace_id=workspace_id, file_id=file_id, user=auth.user, db=db
    )


@router.patch("/{file_id}", response_model=FileOut)
async def update_file(
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    body: FilePatchRequest,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    return await file_svc.update_file(
        workspace_id=workspace_id,
        file_id=file_id,
        user=auth.user,
        description=body.description,
        folder_id=body.folder_id,
        move_to_root=body.move_to_root,
        body_security_mode=body.security_mode,
        db=db,
    )


@router.get("/{file_id}/download", response_model=DownloadUrlOut)
async def get_download_url(
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    auth: CurrentAnyAuth,
    expires_in: int = Query(default=3600, ge=60, le=86400),
    db: AsyncSession = Depends(get_db),
):
    data = await file_svc.get_download_url(
        workspace_id=workspace_id,
        file_id=file_id,
        user=auth.user,
        db=db,
        expires_in=expires_in,
    )
    return DownloadUrlOut(**data)


@router.post(
    "/{file_id}/reindex",
    response_model=ReindexOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reindex_file(
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    data = await file_svc.reindex_file(
        workspace_id=workspace_id, file_id=file_id, user=auth.user, db=db
    )
    return ReindexOut(**data)


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    await file_svc.delete_file(
        workspace_id=workspace_id, file_id=file_id, user=auth.user, db=db, redis=redis
    )

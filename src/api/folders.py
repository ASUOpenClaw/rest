import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.deps import CurrentAnyAuth, CurrentAuth
from src.schemas.folder import (
    FolderCreateRequest,
    FolderDetailOut,
    FolderListOut,
    FolderOut,
    FolderPatchRequest,
    FolderTreeOut,
)
from src.services import folder as folder_svc

router = APIRouter(prefix="/workspaces/{workspace_id}/folders", tags=["folders"])


@router.post("", response_model=FolderOut, status_code=status.HTTP_201_CREATED)
async def create_folder(
    workspace_id: uuid.UUID,
    body: FolderCreateRequest,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    return await folder_svc.create_folder(
        workspace_id=workspace_id,
        user=auth.user,
        name=body.name,
        parent_id=body.parent_id,
        db=db,
    )


@router.get("/tree", response_model=FolderTreeOut)
async def get_folder_tree(
    workspace_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    data = await folder_svc.get_folder_tree(
        workspace_id=workspace_id, user=auth.user, db=db
    )
    return FolderTreeOut(**data)


@router.get("", response_model=FolderListOut)
async def list_folders(
    workspace_id: uuid.UUID,
    auth: CurrentAnyAuth,
    parent_id: uuid.UUID | None = Query(default=None),
    recursive: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
):
    data = await folder_svc.list_folders(
        workspace_id=workspace_id,
        user=auth.user,
        parent_id=parent_id,
        recursive=recursive,
        db=db,
    )
    return FolderListOut(**data)


@router.get("/{folder_id}", response_model=FolderDetailOut)
async def get_folder(
    workspace_id: uuid.UUID,
    folder_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    return await folder_svc.get_folder(
        workspace_id=workspace_id, folder_id=folder_id, user=auth.user, db=db
    )


@router.patch("/{folder_id}", response_model=FolderOut)
async def update_folder(
    workspace_id: uuid.UUID,
    folder_id: uuid.UUID,
    body: FolderPatchRequest,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    return await folder_svc.update_folder(
        workspace_id=workspace_id,
        folder_id=folder_id,
        user=auth.user,
        name=body.name,
        new_parent_id=body.parent_id,
        move_to_root=body.move_to_root,
        db=db,
    )


@router.delete("/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_folder(
    workspace_id: uuid.UUID,
    folder_id: uuid.UUID,
    auth: CurrentAuth,
    mode: Literal["fail", "move_to_parent", "cascade"] = Query(default="fail"),
    db: AsyncSession = Depends(get_db),
):
    await folder_svc.delete_folder(
        workspace_id=workspace_id,
        folder_id=folder_id,
        user=auth.user,
        mode=mode,
        db=db,
    )

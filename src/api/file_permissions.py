import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.deps import CurrentAuth
from src.schemas.file_permission import (
    FilePermissionListOut,
    FilePermissionOut,
    FilePermissionUpsertRequest,
)
from src.services import file_permission as fp_svc

router = APIRouter(
    prefix="/workspaces/{workspace_id}/files/{file_id}/permissions",
    tags=["file-permissions"],
)


@router.get("", response_model=FilePermissionListOut)
async def list_permissions(
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    items = await fp_svc.list_permissions(
        workspace_id=workspace_id, file_id=file_id, caller=auth.user, db=db
    )
    return FilePermissionListOut(items=items, total=len(items))


@router.put("/{user_id}", response_model=FilePermissionOut)
async def upsert_permission(
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    user_id: uuid.UUID,
    body: FilePermissionUpsertRequest,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    return await fp_svc.upsert_permission(
        workspace_id=workspace_id,
        file_id=file_id,
        target_user_id=user_id,
        permission=body.permission,
        caller=auth.user,
        db=db,
    )


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_permission(
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    user_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    await fp_svc.revoke_permission(
        workspace_id=workspace_id,
        file_id=file_id,
        target_user_id=user_id,
        caller=auth.user,
        db=db,
    )

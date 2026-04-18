import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.deps import CurrentAnyAuth
from src.models import WorkspaceMember
from src.schemas.rag import RagIssuesOut, RagSearchOut, RagSearchRequest, RagStatusOut
from src.services import rag_client

router = APIRouter(prefix="/workspaces/{workspace_id}/rag", tags=["rag"])


async def _require_member(
    workspace_id: uuid.UUID, user_id: uuid.UUID, db: AsyncSession
) -> None:
    member = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not a workspace member"
        )


@router.post("/search", response_model=RagSearchOut)
async def rag_search(
    workspace_id: uuid.UUID,
    body: RagSearchRequest,
    auth: CurrentAnyAuth,
    db: AsyncSession = Depends(get_db),
):
    return await rag_client.search(
        workspace_id=workspace_id,
        user_id=auth.user.id,
        query=body.query,
        top_k=body.top_k,
        min_score=body.min_score,
        filters=body.filters,
        db=db,
    )


@router.get("/status", response_model=RagStatusOut)
async def rag_status(
    workspace_id: uuid.UUID,
    auth: CurrentAnyAuth,
    db: AsyncSession = Depends(get_db),
):
    await _require_member(workspace_id, auth.user.id, db)
    return await rag_client.get_status(workspace_id=workspace_id, db=db)


@router.get("/issues", response_model=RagIssuesOut)
async def rag_issues(
    workspace_id: uuid.UUID,
    auth: CurrentAnyAuth,
    db: AsyncSession = Depends(get_db),
):
    await _require_member(workspace_id, auth.user.id, db)
    return await rag_client.get_issues(workspace_id=workspace_id, db=db)

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.deps import CurrentAnyAuth, CurrentAuth
from src.schemas.conversation import (
    ConversationListOut,
    ConversationOut,
    ConversationPatchRequest,
    MessageListOut,
    MessageOut,
)
from src.services import conversation as conv_svc

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["conversations"])


@router.get("/conversations", response_model=ConversationListOut)
async def list_conversations(
    workspace_id: uuid.UUID,
    auth: CurrentAnyAuth,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    items, total = await conv_svc.list_conversations(
        workspace_id=workspace_id,
        user_id=auth.user.id,
        page=page,
        per_page=per_page,
        db=db,
    )
    return ConversationListOut(
        items=[ConversationOut.model_validate(c) for c in items],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get("/admin/conversations", response_model=ConversationListOut)
async def list_all_conversations(
    workspace_id: uuid.UUID,
    auth: CurrentAuth,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    items, total = await conv_svc.list_all_conversations(
        workspace_id=workspace_id,
        caller_id=auth.user.id,
        page=page,
        per_page=per_page,
        db=db,
    )
    return ConversationListOut(
        items=[ConversationOut.model_validate(c) for c in items],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    conv = await conv_svc.get_conversation(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        user_id=auth.user.id,
        db=db,
    )
    return ConversationOut.model_validate(conv)


@router.get("/conversations/{conversation_id}/messages", response_model=MessageListOut)
async def list_messages(
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    auth: CurrentAnyAuth,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    items, total = await conv_svc.list_messages(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        user_id=auth.user.id,
        page=page,
        per_page=per_page,
        db=db,
    )
    return MessageListOut(
        items=[MessageOut.model_validate(m) for m in items],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.patch("/conversations/{conversation_id}", response_model=ConversationOut)
async def update_conversation(
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    body: ConversationPatchRequest,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    conv = await conv_svc.update_title(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        user_id=auth.user.id,
        title=body.title,
        db=db,
    )
    return ConversationOut.model_validate(conv)


@router.delete(
    "/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_conversation(
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    await conv_svc.delete_conversation(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        user_id=auth.user.id,
        db=db,
    )

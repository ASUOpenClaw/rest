from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Conversation, ConversationMessage, WorkspaceMember, WorkspaceRole

_ROLE_ORDER = [
    WorkspaceRole.guest,
    WorkspaceRole.member,
    WorkspaceRole.admin,
    WorkspaceRole.owner,
]


def _role_gte(role: WorkspaceRole, min_role: WorkspaceRole) -> bool:
    return _ROLE_ORDER.index(role) >= _ROLE_ORDER.index(min_role)


async def _get_member(
    workspace_id: uuid.UUID, user_id: uuid.UUID, db: AsyncSession
) -> WorkspaceMember:
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
    return member


# ---------------------------------------------------------------------------
# List conversations — own only
# ---------------------------------------------------------------------------


async def list_conversations(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    page: int,
    per_page: int,
    db: AsyncSession,
) -> tuple[list[Conversation], int]:
    await _get_member(workspace_id, user_id, db)

    q = select(Conversation).where(
        Conversation.workspace_id == workspace_id,
        Conversation.user_id == user_id,
    )
    total = await db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = (
        q.order_by(Conversation.last_message_at.desc().nullslast())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    result = await db.execute(q)
    return list(result.scalars().all()), total


# ---------------------------------------------------------------------------
# Admin: list all conversations in workspace
# ---------------------------------------------------------------------------


async def list_all_conversations(
    workspace_id: uuid.UUID,
    caller_id: uuid.UUID,
    page: int,
    per_page: int,
    db: AsyncSession,
) -> tuple[list[Conversation], int]:
    member = await _get_member(workspace_id, caller_id, db)
    if not _role_gte(member.role, WorkspaceRole.admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Requires admin role"
        )

    q = select(Conversation).where(Conversation.workspace_id == workspace_id)
    total = await db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = (
        q.order_by(Conversation.last_message_at.desc().nullslast())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    result = await db.execute(q)
    return list(result.scalars().all()), total


# ---------------------------------------------------------------------------
# Get single conversation
# ---------------------------------------------------------------------------


async def get_conversation(
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> Conversation:
    member = await _get_member(workspace_id, user_id, db)

    conv = await db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.workspace_id == workspace_id,
        )
    )
    if conv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )

    # Non-admin users can only see their own conversations
    if not _role_gte(member.role, WorkspaceRole.admin) and conv.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )

    return conv


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


async def list_messages(
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    page: int,
    per_page: int,
    db: AsyncSession,
) -> tuple[list[ConversationMessage], int]:
    # Reuse get_conversation for access check
    await get_conversation(workspace_id, conversation_id, user_id, db)

    q = select(ConversationMessage).where(
        ConversationMessage.conversation_id == conversation_id
    )
    total = await db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = (
        q.order_by(ConversationMessage.created_at.asc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    result = await db.execute(q)
    return list(result.scalars().all()), total


# ---------------------------------------------------------------------------
# Patch title
# ---------------------------------------------------------------------------


async def update_title(
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    title: str,
    db: AsyncSession,
) -> Conversation:
    conv = await get_conversation(workspace_id, conversation_id, user_id, db)

    member = await _get_member(workspace_id, user_id, db)
    if not _role_gte(member.role, WorkspaceRole.admin) and conv.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot edit another user's conversation",
        )

    conv.title = title
    await db.commit()
    await db.refresh(conv)
    return conv


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def delete_conversation(
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> None:
    conv = await get_conversation(workspace_id, conversation_id, user_id, db)

    member = await _get_member(workspace_id, user_id, db)
    if not _role_gte(member.role, WorkspaceRole.admin) and conv.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot delete another user's conversation",
        )

    await db.delete(conv)
    await db.commit()

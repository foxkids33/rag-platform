from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatMessage, ChatSession, Workspace
from app.db.session import get_db
from app.services.conversation_context import derive_conversation_title

router = APIRouter(prefix="/workspaces/{workspace_id}/conversations", tags=["conversations"])


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=500)


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=500)


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    title: str | None
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str
    metadata: dict
    created_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[ChatMessageOut]


def _conversation_out(session: ChatSession, message_count: int = 0) -> ConversationOut:
    return ConversationOut(
        id=session.id,
        workspace_id=session.workspace_id,
        title=session.title,
        created_at=session.created_at,
        updated_at=session.updated_at,
        message_count=message_count,
    )


def _message_out(message: ChatMessage) -> ChatMessageOut:
    return ChatMessageOut(
        id=message.id,
        session_id=message.session_id,
        role=message.role,
        content=message.content,
        metadata=message.metadata_ or {},
        created_at=message.created_at,
    )


async def _workspace_or_404(db: AsyncSession, workspace_id: uuid.UUID) -> Workspace:
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


async def _conversation_or_404(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> ChatSession:
    conversation = await db.get(ChatSession, conversation_id)
    if conversation is None or conversation.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    workspace_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> list[ConversationOut]:
    await _workspace_or_404(db, workspace_id)
    count_subquery = (
        select(func.count(ChatMessage.id))
        .where(ChatMessage.session_id == ChatSession.id)
        .correlate(ChatSession)
        .scalar_subquery()
    )
    result = await db.execute(
        select(ChatSession, count_subquery.label("message_count"))
        .where(ChatSession.workspace_id == workspace_id)
        .order_by(ChatSession.updated_at.desc(), ChatSession.created_at.desc())
    )
    return [_conversation_out(session, int(count or 0)) for session, count in result.all()]


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    workspace_id: uuid.UUID,
    payload: ConversationCreate,
    db: AsyncSession = Depends(get_db),
) -> ConversationOut:
    await _workspace_or_404(db, workspace_id)
    title = derive_conversation_title(payload.title or "")
    conversation = ChatSession(workspace_id=workspace_id, title=title)
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return _conversation_out(conversation)


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ConversationDetail:
    conversation = await _conversation_or_404(db, workspace_id, conversation_id)
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == conversation.id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
    )
    messages = list(result.scalars())
    return ConversationDetail(
        **_conversation_out(conversation, len(messages)).model_dump(),
        messages=[_message_out(message) for message in messages],
    )


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def update_conversation(
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    payload: ConversationUpdate,
    db: AsyncSession = Depends(get_db),
) -> ConversationOut:
    conversation = await _conversation_or_404(db, workspace_id, conversation_id)
    conversation.title = derive_conversation_title(payload.title, max_chars=500)
    await db.commit()
    await db.refresh(conversation)
    count = await db.scalar(
        select(func.count(ChatMessage.id)).where(ChatMessage.session_id == conversation.id)
    )
    return _conversation_out(conversation, int(count or 0))


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Response:
    conversation = await _conversation_or_404(db, workspace_id, conversation_id)
    await db.delete(conversation)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

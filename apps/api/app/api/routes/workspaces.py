from __future__ import annotations

import uuid
from datetime import UTC, datetime

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import knowledge_base_for_principal, workspace_for_principal
from app.core.config import settings
from app.core.security import Principal, get_current_principal
from app.db.models import ChatSession, Document, KnowledgeBase, KnowledgeBaseVersion, Workspace
from app.db.session import get_db
from app.services.storage import StorageError, storage
from app.services.workspace_sources import (
    WorkspaceSourceMode,
    default_source_mode,
    includes_knowledge_base,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    base_knowledge_base_id: uuid.UUID | None = None
    source_mode: WorkspaceSourceMode | None = None


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    base_knowledge_base_id: uuid.UUID | None = None
    source_mode: WorkspaceSourceMode | None = None


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    tenant_id: str
    name: str
    base_knowledge_base_id: uuid.UUID | None
    base_knowledge_base_name: str | None
    active_knowledge_base_version_id: uuid.UUID | None
    active_knowledge_base_version: int | None
    source_mode: WorkspaceSourceMode
    user_id: str
    document_count: int
    searchable_document_count: int
    conversation_count: int
    created_at: datetime
    updated_at: datetime


async def _knowledge_base(
    db: AsyncSession,
    knowledge_base_id: uuid.UUID | None,
    principal: Principal,
) -> KnowledgeBase | None:
    if knowledge_base_id is None:
        return None
    return await knowledge_base_for_principal(db, knowledge_base_id, principal)


async def _workspace_out(db: AsyncSession, workspace: Workspace) -> WorkspaceOut:
    document_count = await db.scalar(
        select(func.count(Document.id)).where(Document.workspace_id == workspace.id)
    )
    searchable_document_count = await db.scalar(
        select(func.count(Document.id)).where(
            Document.workspace_id == workspace.id,
            Document.status == "READY",
            Document.search_enabled.is_(True),
        )
    )
    conversation_count = await db.scalar(
        select(func.count(ChatSession.id)).where(ChatSession.workspace_id == workspace.id)
    )

    knowledge_base: KnowledgeBase | None = None
    active_version: KnowledgeBaseVersion | None = None
    if workspace.base_knowledge_base_id is not None:
        knowledge_base = await db.get(KnowledgeBase, workspace.base_knowledge_base_id)
        if knowledge_base is not None and knowledge_base.tenant_id != workspace.tenant_id:
            knowledge_base = None
        if knowledge_base is not None and knowledge_base.active_version_id is not None:
            active_version = await db.get(KnowledgeBaseVersion, knowledge_base.active_version_id)

    return WorkspaceOut(
        id=workspace.id,
        tenant_id=workspace.tenant_id,
        name=workspace.name,
        base_knowledge_base_id=workspace.base_knowledge_base_id,
        base_knowledge_base_name=knowledge_base.name if knowledge_base else None,
        active_knowledge_base_version_id=active_version.id if active_version else None,
        active_knowledge_base_version=active_version.version if active_version else None,
        source_mode=WorkspaceSourceMode(workspace.source_mode),
        user_id=workspace.user_id,
        document_count=int(document_count or 0),
        searchable_document_count=int(searchable_document_count or 0),
        conversation_count=int(conversation_count or 0),
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
    )


def _validate_source_configuration(
    source_mode: WorkspaceSourceMode,
    knowledge_base_id: uuid.UUID | None,
) -> None:
    if includes_knowledge_base(source_mode) and knowledge_base_id is None:
        raise HTTPException(
            status_code=422,
            detail="A knowledge base must be selected for this source mode",
        )


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> list[WorkspaceOut]:
    query = select(Workspace).where(Workspace.tenant_id == principal.tenant_id)
    if not principal.has_role(settings.auth_admin_role):
        query = query.where(Workspace.user_id == principal.subject)
    result = await db.execute(query.order_by(Workspace.updated_at.desc()))
    return [await _workspace_out(db, workspace) for workspace in result.scalars()]


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceCreate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> WorkspaceOut:
    knowledge_base = await _knowledge_base(db, payload.base_knowledge_base_id, principal)
    source_mode = payload.source_mode or default_source_mode(knowledge_base is not None)
    _validate_source_configuration(source_mode, payload.base_knowledge_base_id)

    workspace = Workspace(
        tenant_id=principal.tenant_id,
        name=payload.name.strip(),
        base_knowledge_base_id=payload.base_knowledge_base_id,
        source_mode=source_mode.value,
        user_id=principal.subject,
    )
    db.add(workspace)
    await db.commit()
    await db.refresh(workspace)
    return await _workspace_out(db, workspace)


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
async def update_workspace(
    workspace_id: uuid.UUID,
    payload: WorkspaceUpdate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> WorkspaceOut:
    workspace = await workspace_for_principal(db, workspace_id, principal)

    updates = payload.model_dump(exclude_unset=True)
    next_knowledge_base_id = updates.get(
        "base_knowledge_base_id",
        workspace.base_knowledge_base_id,
    )
    await _knowledge_base(db, next_knowledge_base_id, principal)

    if "source_mode" in updates and updates["source_mode"] is not None:
        next_source_mode = WorkspaceSourceMode(updates["source_mode"])
    elif "base_knowledge_base_id" in updates:
        next_source_mode = default_source_mode(next_knowledge_base_id is not None)
    else:
        next_source_mode = WorkspaceSourceMode(workspace.source_mode)

    _validate_source_configuration(next_source_mode, next_knowledge_base_id)

    if "name" in updates and updates["name"] is not None:
        workspace.name = updates["name"].strip()
    if "base_knowledge_base_id" in updates:
        workspace.base_knowledge_base_id = next_knowledge_base_id
    workspace.source_mode = next_source_mode.value
    workspace.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(workspace)
    return await _workspace_out(db, workspace)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> Response:
    workspace = await workspace_for_principal(db, workspace_id, principal)

    result = await db.execute(select(Document).where(Document.workspace_id == workspace_id))
    documents = list(result.scalars())
    if any(document.status in {"QUEUED", "PROCESSING"} for document in documents):
        raise HTTPException(
            status_code=409,
            detail="Workspace cannot be deleted while document ingestion is running",
        )

    for document in documents:
        try:
            await to_thread.run_sync(storage.delete, document.object_key)
        except StorageError as exc:
            raise HTTPException(
                status_code=503,
                detail="Object storage is unavailable; workspace was not deleted",
            ) from exc

    await db.delete(workspace)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

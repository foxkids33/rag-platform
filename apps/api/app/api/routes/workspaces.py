import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import KnowledgeBase, Workspace
from app.db.session import get_db

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    base_knowledge_base_id: uuid.UUID | None = None
    user_id: str | None = None


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    base_knowledge_base_id: uuid.UUID | None = None


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    base_knowledge_base_id: uuid.UUID | None
    user_id: str | None


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Workspace).order_by(Workspace.created_at.desc()))
    return list(result.scalars())


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(payload: WorkspaceCreate, db: AsyncSession = Depends(get_db)):
    if payload.base_knowledge_base_id is not None:
        knowledge_base = await db.get(KnowledgeBase, payload.base_knowledge_base_id)
        if knowledge_base is None:
            raise HTTPException(status_code=404, detail="Knowledge base not found")

    workspace = Workspace(**payload.model_dump())
    db.add(workspace)
    await db.commit()
    await db.refresh(workspace)
    return workspace


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
async def update_workspace(
    workspace_id: uuid.UUID,
    payload: WorkspaceUpdate,
    db: AsyncSession = Depends(get_db),
):
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    updates = payload.model_dump(exclude_unset=True)
    if "base_knowledge_base_id" in updates and updates["base_knowledge_base_id"] is not None:
        knowledge_base = await db.get(KnowledgeBase, updates["base_knowledge_base_id"])
        if knowledge_base is None:
            raise HTTPException(status_code=404, detail="Knowledge base not found")

    for field, value in updates.items():
        setattr(workspace, field, value)

    await db.commit()
    await db.refresh(workspace)
    return workspace

"""Tenant-aware resource lookups that avoid cross-tenant existence leaks."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import Principal
from app.db.models import KnowledgeBase, Workspace


def principal_can_access_workspace(principal: Principal, workspace: Workspace) -> bool:
    return workspace.tenant_id == principal.tenant_id and (
        workspace.user_id == principal.subject or principal.has_role(settings.auth_admin_role)
    )


async def workspace_for_principal(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    principal: Principal,
) -> Workspace:
    query = select(Workspace).where(
        Workspace.id == workspace_id,
        Workspace.tenant_id == principal.tenant_id,
    )
    if not principal.has_role(settings.auth_admin_role):
        query = query.where(Workspace.user_id == principal.subject)
    workspace = await db.scalar(query)
    if workspace is None or not principal_can_access_workspace(principal, workspace):
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


async def knowledge_base_for_principal(
    db: AsyncSession,
    knowledge_base_id: uuid.UUID,
    principal: Principal,
) -> KnowledgeBase:
    knowledge_base = await db.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.id == knowledge_base_id,
            KnowledgeBase.tenant_id == principal.tenant_id,
        )
    )
    if knowledge_base is None or knowledge_base.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return knowledge_base

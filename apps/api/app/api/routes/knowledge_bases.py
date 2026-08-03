from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, KnowledgeBase, KnowledgeBaseVersion
from app.db.session import get_db

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-bases"])


class KnowledgeBaseVersionOut(BaseModel):
    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    version: int
    status: str
    embedding_model: str
    embedding_dimension: int
    chunker_version: str
    document_count: int
    created_at: datetime
    activated_at: datetime | None


class KnowledgeBaseOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    active_version_id: uuid.UUID | None
    active_version: int | None
    active_version_status: str | None
    active_document_count: int
    graph_enabled: bool


async def _version_out(
    db: AsyncSession,
    version: KnowledgeBaseVersion,
) -> KnowledgeBaseVersionOut:
    document_count = await db.scalar(
        select(func.count(Document.id)).where(
            Document.knowledge_base_version_id == version.id,
            Document.status == "READY",
            Document.search_enabled.is_(True),
        )
    )
    return KnowledgeBaseVersionOut(
        id=version.id,
        knowledge_base_id=version.knowledge_base_id,
        version=version.version,
        status=version.status,
        embedding_model=version.embedding_model,
        embedding_dimension=version.embedding_dimension,
        chunker_version=version.chunker_version,
        document_count=int(document_count or 0),
        created_at=version.created_at,
        activated_at=version.activated_at,
    )


async def _knowledge_base_out(
    db: AsyncSession,
    knowledge_base: KnowledgeBase,
) -> KnowledgeBaseOut:
    active_version: KnowledgeBaseVersion | None = None
    active_document_count = 0
    if knowledge_base.active_version_id is not None:
        active_version = await db.get(KnowledgeBaseVersion, knowledge_base.active_version_id)
        if active_version is not None:
            active_document_count = int(
                await db.scalar(
                    select(func.count(Document.id)).where(
                        Document.knowledge_base_version_id == active_version.id,
                        Document.status == "READY",
                        Document.search_enabled.is_(True),
                    )
                )
                or 0
            )

    return KnowledgeBaseOut(
        id=knowledge_base.id,
        slug=knowledge_base.slug,
        name=knowledge_base.name,
        description=knowledge_base.description,
        active_version_id=knowledge_base.active_version_id,
        active_version=active_version.version if active_version else None,
        active_version_status=active_version.status if active_version else None,
        active_document_count=active_document_count,
        graph_enabled=knowledge_base.graph_enabled,
    )


@router.get("", response_model=list[KnowledgeBaseOut])
async def list_knowledge_bases(
    db: AsyncSession = Depends(get_db),
) -> list[KnowledgeBaseOut]:
    result = await db.execute(select(KnowledgeBase).order_by(KnowledgeBase.name))
    return [await _knowledge_base_out(db, knowledge_base) for knowledge_base in result.scalars()]


@router.get("/{knowledge_base_id}/versions", response_model=list[KnowledgeBaseVersionOut])
async def list_knowledge_base_versions(
    knowledge_base_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> list[KnowledgeBaseVersionOut]:
    knowledge_base = await db.get(KnowledgeBase, knowledge_base_id)
    if knowledge_base is None:
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    result = await db.execute(
        select(KnowledgeBaseVersion)
        .where(KnowledgeBaseVersion.knowledge_base_id == knowledge_base_id)
        .order_by(KnowledgeBaseVersion.version.desc())
    )
    return [await _version_out(db, version) for version in result.scalars()]

"""Conservative quotas for self-registered accounts, not the legacy corpus."""

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import Principal
from app.db.models import Document, KnowledgeBase, KnowledgeBaseVersion, Workspace
from app.services.upload_validation import IMAGE_EXTENSIONS


def is_private_account(principal: Principal) -> bool:
    return (settings.auth_enabled and settings.auth_mode == "password"
            and not principal.has_role(settings.auth_admin_role))


def upload_limit_bytes(principal: Principal, extension: str) -> int:
    limit_mb = settings.upload_max_mb
    if is_private_account(principal):
        limit_mb = min(limit_mb, getattr(settings, "user_image_max_mb", 10)
                       if extension in IMAGE_EXTENSIONS else getattr(settings, "user_upload_max_mb", 25))
    return limit_mb * 1024 * 1024


async def check_workspace_quota(db: AsyncSession, principal: Principal) -> None:
    if not is_private_account(principal):
        return
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                     {"key": f"workspace-quota:{principal.tenant_id}:{principal.subject}"})
    count = await db.scalar(select(func.count(Workspace.id)).where(
        Workspace.tenant_id == principal.tenant_id, Workspace.user_id == principal.subject))
    if int(count or 0) >= getattr(settings, "user_max_workspaces", 10):
        raise HTTPException(status_code=413, detail="Workspace quota exceeded")


async def check_knowledge_base_quota(db: AsyncSession, principal: Principal, knowledge_base_id=None) -> None:
    if not is_private_account(principal):
        return
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                     {"key": f"kb-quota:{principal.tenant_id}:{principal.subject}"})
    query = (select(func.count(KnowledgeBase.id)).where(
        KnowledgeBase.tenant_id == principal.tenant_id, KnowledgeBase.owner_subject == principal.subject)
        if knowledge_base_id is None else select(func.count(KnowledgeBaseVersion.id)).where(
            KnowledgeBaseVersion.knowledge_base_id == knowledge_base_id))
    if int(await db.scalar(query) or 0) >= 10:
        raise HTTPException(status_code=413, detail="Knowledge base or version quota exceeded")


async def check_upload_quota(db: AsyncSession, principal: Principal, workspace_id, size: int,
                            *, version_id=None, replacing: Document | None = None) -> None:
    if not is_private_account(principal):
        return
    # Serialize accounting across new accounts. The check and document INSERT
    # share one transaction, so concurrent uploads cannot bypass the quotas.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                     {"key": f"password-upload-quota:{principal.tenant_id}"})
    scope = (Document.workspace_id == workspace_id if workspace_id is not None
             else Document.knowledge_base_version_id == version_id)
    count = await db.scalar(select(func.count(Document.id)).where(scope))
    if replacing is None and int(count or 0) >= getattr(settings, "user_max_documents_per_workspace", 100):
        raise HTTPException(status_code=413, detail="Document quota exceeded")
    if replacing is not None:
        size -= replacing.size_bytes or 0
        if size <= 0:
            return
    accounting = (select(func.coalesce(func.sum(Document.size_bytes), 0))
        .select_from(Document).outerjoin(Workspace, Document.workspace_id == Workspace.id)
        .outerjoin(KnowledgeBaseVersion, Document.knowledge_base_version_id == KnowledgeBaseVersion.id)
        .outerjoin(KnowledgeBase, KnowledgeBaseVersion.knowledge_base_id == KnowledgeBase.id))
    own_bytes = await db.scalar(accounting.where(or_(
        and_(Workspace.tenant_id == principal.tenant_id, Workspace.user_id == principal.subject),
        and_(KnowledgeBase.tenant_id == principal.tenant_id, KnowledgeBase.owner_subject == principal.subject))))
    if int(own_bytes or 0) + size > getattr(settings, "user_storage_max_mb", 512) * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Personal storage quota exceeded")
    total_bytes = await db.scalar(accounting.where(or_(
        and_(Workspace.tenant_id == principal.tenant_id, Workspace.user_id != settings.auth_local_subject),
        and_(KnowledgeBase.tenant_id == principal.tenant_id,
             KnowledgeBase.owner_subject.is_not(None), KnowledgeBase.owner_subject != settings.auth_local_subject))))
    if int(total_bytes or 0) + size > getattr(settings, "password_uploads_max_mb", 2048) * 1024 * 1024:
        raise HTTPException(status_code=507, detail="Registered-user storage capacity exceeded")

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from anyio import to_thread
from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import knowledge_base_for_principal
from app.core.config import settings
from app.core.security import Principal, get_current_principal, require_tenant_admin
from app.db.models import Document, IngestionJob, KnowledgeBase, KnowledgeBaseVersion, Workspace
from app.db.session import get_db
from app.services.knowledge_base_lifecycle import (
    BUSY_DOCUMENT_STATUSES,
    derive_version_status,
    version_can_publish,
    version_is_editable,
)
from app.services.queue import QueueError, ingestion_queue
from app.services.storage import StorageError, storage

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-bases"])

ALLOWED_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".html", ".htm", ".xml", ".pdf", ".docx"}
READ_CHUNK_SIZE = 1024 * 1024
DEFAULT_CHUNKER_VERSION = "docling-hybrid-parent-child-v1"


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(default=None, max_length=120)
    description: str | None = None


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    graph_enabled: bool | None = None


class KnowledgeBaseVersionCreate(BaseModel):
    embedding_model: str | None = None
    embedding_dimension: int | None = None
    chunker_version: str | None = None


class KnowledgeBaseDocumentUpdate(BaseModel):
    search_enabled: bool


class KnowledgeBaseDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    knowledge_base_version_id: uuid.UUID | None
    filename: str
    object_key: str
    mime_type: str | None
    sha256: str
    status: str
    search_enabled: bool
    created_at: datetime


class KnowledgeBaseVersionOut(BaseModel):
    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    version: int
    status: str
    embedding_model: str
    embedding_dimension: int
    chunker_version: str
    document_count: int
    ready_document_count: int
    searchable_document_count: int
    failed_document_count: int
    processing_document_count: int
    created_at: datetime
    activated_at: datetime | None


class KnowledgeBaseOut(BaseModel):
    id: uuid.UUID
    tenant_id: str
    slug: str
    name: str
    description: str | None
    active_version_id: uuid.UUID | None
    active_version: int | None
    active_version_status: str | None
    active_document_count: int
    graph_enabled: bool


def _safe_filename(filename: str) -> str:
    name = Path(filename).name.strip()
    name = re.sub(r"[^\w.()\- ]+", "_", name, flags=re.UNICODE)
    return name[:500] or "document"


def _normalise_slug(value: str | None, name: str) -> str:
    source = (value or name).strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", source).strip("-")
    return slug[:110] or f"kb-{uuid.uuid4().hex[:10]}"


async def _hash_and_measure(file: UploadFile) -> tuple[str, int]:
    digest = hashlib.sha256()
    total_size = 0
    while chunk := await file.read(READ_CHUNK_SIZE):
        digest.update(chunk)
        total_size += len(chunk)
        if total_size > settings.upload_max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File exceeds the {settings.upload_max_mb} MB upload limit",
            )
    await file.seek(0)
    return digest.hexdigest(), total_size


async def _get_knowledge_base(
    db: AsyncSession,
    knowledge_base_id: uuid.UUID,
    principal: Principal,
) -> KnowledgeBase:
    return await knowledge_base_for_principal(db, knowledge_base_id, principal)


async def _get_version(
    db: AsyncSession,
    knowledge_base_id: uuid.UUID,
    version_id: uuid.UUID,
    principal: Principal,
) -> KnowledgeBaseVersion:
    await _get_knowledge_base(db, knowledge_base_id, principal)
    version = await db.get(KnowledgeBaseVersion, version_id)
    if version is None or version.knowledge_base_id != knowledge_base_id:
        raise HTTPException(status_code=404, detail="Knowledge base version not found")
    return version


async def _version_documents(
    db: AsyncSession,
    version_id: uuid.UUID,
) -> list[Document]:
    result = await db.execute(
        select(Document)
        .where(Document.knowledge_base_version_id == version_id)
        .order_by(Document.created_at.desc())
    )
    return list(result.scalars())


async def _synchronize_version_status(
    db: AsyncSession,
    version: KnowledgeBaseVersion,
) -> bool:
    result = await db.execute(
        select(Document.status).where(Document.knowledge_base_version_id == version.id)
    )
    next_status = derive_version_status(version.status, result.scalars().all())
    if next_status == version.status:
        return False
    version.status = next_status
    return True


async def _version_out(
    db: AsyncSession,
    version: KnowledgeBaseVersion,
) -> KnowledgeBaseVersionOut:
    documents = await _version_documents(db, version.id)
    return KnowledgeBaseVersionOut(
        id=version.id,
        knowledge_base_id=version.knowledge_base_id,
        version=version.version,
        status=version.status,
        embedding_model=version.embedding_model,
        embedding_dimension=version.embedding_dimension,
        chunker_version=version.chunker_version,
        document_count=len(documents),
        ready_document_count=sum(document.status == "READY" for document in documents),
        searchable_document_count=sum(
            document.status == "READY" and document.search_enabled for document in documents
        ),
        failed_document_count=sum(document.status == "FAILED" for document in documents),
        processing_document_count=sum(
            document.status in BUSY_DOCUMENT_STATUSES for document in documents
        ),
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
        tenant_id=knowledge_base.tenant_id,
        slug=knowledge_base.slug,
        name=knowledge_base.name,
        description=knowledge_base.description,
        active_version_id=knowledge_base.active_version_id,
        active_version=active_version.version if active_version else None,
        active_version_status=active_version.status if active_version else None,
        active_document_count=active_document_count,
        graph_enabled=knowledge_base.graph_enabled,
    )


async def _ensure_editable_version(version: KnowledgeBaseVersion) -> None:
    if not version_is_editable(version.status):
        raise HTTPException(
            status_code=409,
            detail="Published knowledge base versions are immutable; create a new version",
        )


async def _delete_objects(documents: list[Document]) -> None:
    for document in documents:
        try:
            await to_thread.run_sync(storage.delete, document.object_key)
        except StorageError as exc:
            raise HTTPException(
                status_code=503,
                detail="Object storage is unavailable; database records were not deleted",
            ) from exc


@router.get("", response_model=list[KnowledgeBaseOut])
async def list_knowledge_bases(
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> list[KnowledgeBaseOut]:
    result = await db.execute(
        select(KnowledgeBase)
        .where(KnowledgeBase.tenant_id == principal.tenant_id)
        .order_by(KnowledgeBase.name)
    )
    return [await _knowledge_base_out(db, knowledge_base) for knowledge_base in result.scalars()]


@router.post("", response_model=KnowledgeBaseOut, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> KnowledgeBaseOut:
    require_tenant_admin(principal)
    name = payload.name.strip()
    slug = _normalise_slug(payload.slug, name)
    existing = await db.scalar(
        select(KnowledgeBase.id).where(
            KnowledgeBase.tenant_id == principal.tenant_id,
            KnowledgeBase.slug == slug,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="Knowledge base slug already exists")

    knowledge_base = KnowledgeBase(
        id=uuid.uuid4(),
        tenant_id=principal.tenant_id,
        slug=slug,
        name=name,
        description=payload.description,
        active_version_id=None,
        graph_enabled=False,
    )
    db.add(knowledge_base)
    await db.commit()
    await db.refresh(knowledge_base)
    return await _knowledge_base_out(db, knowledge_base)


@router.patch("/{knowledge_base_id}", response_model=KnowledgeBaseOut)
async def update_knowledge_base(
    knowledge_base_id: uuid.UUID,
    payload: KnowledgeBaseUpdate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> KnowledgeBaseOut:
    require_tenant_admin(principal)
    knowledge_base = await _get_knowledge_base(db, knowledge_base_id, principal)
    updates = payload.model_dump(exclude_unset=True)
    if "name" in updates and updates["name"] is not None:
        knowledge_base.name = updates["name"].strip()
    if "description" in updates:
        knowledge_base.description = updates["description"]
    if "graph_enabled" in updates and updates["graph_enabled"] is not None:
        knowledge_base.graph_enabled = updates["graph_enabled"]
    await db.commit()
    await db.refresh(knowledge_base)
    return await _knowledge_base_out(db, knowledge_base)


@router.delete("/{knowledge_base_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    knowledge_base_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> Response:
    require_tenant_admin(principal)
    knowledge_base = await _get_knowledge_base(db, knowledge_base_id, principal)
    workspace_count = int(
        await db.scalar(
            select(func.count(Workspace.id)).where(
                Workspace.base_knowledge_base_id == knowledge_base_id
            )
        )
        or 0
    )
    if workspace_count:
        raise HTTPException(
            status_code=409,
            detail="Disconnect the knowledge base from all workspaces before deleting it",
        )

    result = await db.execute(
        select(Document)
        .join(
            KnowledgeBaseVersion,
            KnowledgeBaseVersion.id == Document.knowledge_base_version_id,
        )
        .where(KnowledgeBaseVersion.knowledge_base_id == knowledge_base_id)
    )
    documents = list(result.scalars())
    if any(document.status in BUSY_DOCUMENT_STATUSES for document in documents):
        raise HTTPException(
            status_code=409,
            detail="Knowledge base cannot be deleted while indexing is running",
        )

    await _delete_objects(documents)
    await db.delete(knowledge_base)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{knowledge_base_id}/versions", response_model=list[KnowledgeBaseVersionOut])
async def list_knowledge_base_versions(
    knowledge_base_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> list[KnowledgeBaseVersionOut]:
    await _get_knowledge_base(db, knowledge_base_id, principal)
    result = await db.execute(
        select(KnowledgeBaseVersion)
        .where(KnowledgeBaseVersion.knowledge_base_id == knowledge_base_id)
        .order_by(KnowledgeBaseVersion.version.desc())
    )
    versions = list(result.scalars())
    changed = False
    for version in versions:
        changed = await _synchronize_version_status(db, version) or changed
    if changed:
        await db.commit()
    return [await _version_out(db, version) for version in versions]


@router.post(
    "/{knowledge_base_id}/versions",
    response_model=KnowledgeBaseVersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_knowledge_base_version(
    knowledge_base_id: uuid.UUID,
    payload: KnowledgeBaseVersionCreate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> KnowledgeBaseVersionOut:
    require_tenant_admin(principal)
    await _get_knowledge_base(db, knowledge_base_id, principal)
    embedding_model = payload.embedding_model or settings.embedding_model
    embedding_dimension = payload.embedding_dimension or settings.embedding_dim
    chunker_version = payload.chunker_version or DEFAULT_CHUNKER_VERSION

    if embedding_model != settings.embedding_model or embedding_dimension != settings.embedding_dim:
        raise HTTPException(
            status_code=409,
            detail="Version embedding configuration must match the active retrieval service",
        )

    latest = await db.scalar(
        select(func.max(KnowledgeBaseVersion.version)).where(
            KnowledgeBaseVersion.knowledge_base_id == knowledge_base_id
        )
    )
    version = KnowledgeBaseVersion(
        id=uuid.uuid4(),
        knowledge_base_id=knowledge_base_id,
        version=int(latest or 0) + 1,
        status="DRAFT",
        embedding_model=embedding_model,
        embedding_dimension=embedding_dimension,
        chunker_version=chunker_version,
    )
    db.add(version)
    await db.commit()
    await db.refresh(version)
    return await _version_out(db, version)


@router.delete(
    "/{knowledge_base_id}/versions/{version_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_knowledge_base_version(
    knowledge_base_id: uuid.UUID,
    version_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> Response:
    require_tenant_admin(principal)
    knowledge_base = await _get_knowledge_base(db, knowledge_base_id, principal)
    version = await _get_version(db, knowledge_base_id, version_id, principal)
    if knowledge_base.active_version_id == version.id or version.status == "ACTIVE":
        raise HTTPException(status_code=409, detail="The active version cannot be deleted")

    documents = await _version_documents(db, version.id)
    if any(document.status in BUSY_DOCUMENT_STATUSES for document in documents):
        raise HTTPException(
            status_code=409,
            detail="Version cannot be deleted while indexing is running",
        )
    await _delete_objects(documents)
    await db.delete(version)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{knowledge_base_id}/versions/{version_id}/publish",
    response_model=KnowledgeBaseOut,
)
async def publish_knowledge_base_version(
    knowledge_base_id: uuid.UUID,
    version_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> KnowledgeBaseOut:
    require_tenant_admin(principal)
    knowledge_base = await _get_knowledge_base(db, knowledge_base_id, principal)
    version = await _get_version(db, knowledge_base_id, version_id, principal)
    await _synchronize_version_status(db, version)

    documents = await _version_documents(db, version.id)
    if any(document.status in BUSY_DOCUMENT_STATUSES for document in documents):
        raise HTTPException(status_code=409, detail="Wait for version indexing to finish")
    if any(document.status == "FAILED" for document in documents):
        raise HTTPException(status_code=409, detail="Remove or reindex failed documents first")
    if not version_can_publish(version.status):
        raise HTTPException(
            status_code=409,
            detail="Only READY or ARCHIVED versions can be published",
        )
    if not any(document.status == "READY" and document.search_enabled for document in documents):
        raise HTTPException(status_code=409, detail="Version has no searchable READY documents")

    now = datetime.now(UTC)
    if knowledge_base.active_version_id and knowledge_base.active_version_id != version.id:
        previous = await db.get(KnowledgeBaseVersion, knowledge_base.active_version_id)
        if previous is not None:
            previous.status = "ARCHIVED"

    knowledge_base.active_version_id = version.id
    version.status = "ACTIVE"
    version.activated_at = now
    await db.commit()
    await db.refresh(knowledge_base)
    return await _knowledge_base_out(db, knowledge_base)


@router.get(
    "/{knowledge_base_id}/versions/{version_id}/documents",
    response_model=list[KnowledgeBaseDocumentOut],
)
async def list_knowledge_base_documents(
    knowledge_base_id: uuid.UUID,
    version_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> list[KnowledgeBaseDocumentOut]:
    version = await _get_version(db, knowledge_base_id, version_id, principal)
    if await _synchronize_version_status(db, version):
        await db.commit()
    return await _version_documents(db, version.id)


@router.post(
    "/{knowledge_base_id}/versions/{version_id}/documents",
    response_model=KnowledgeBaseDocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_knowledge_base_document(
    knowledge_base_id: uuid.UUID,
    version_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> KnowledgeBaseDocumentOut:
    require_tenant_admin(principal)
    version = await _get_version(db, knowledge_base_id, version_id, principal)
    await _ensure_editable_version(version)

    if not file.filename:
        raise HTTPException(status_code=400, detail="Uploaded file has no filename")
    safe_filename = _safe_filename(file.filename)
    extension = Path(safe_filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type. Allowed extensions: {allowed}",
        )

    sha256, size = await _hash_and_measure(file)
    if size == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    duplicate = await db.scalar(
        select(Document.id).where(
            Document.knowledge_base_version_id == version.id,
            Document.sha256 == sha256,
        )
    )
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="This file is already present in the version")

    document_id = uuid.uuid4()
    job_id = uuid.uuid4()
    object_key = (
        f"knowledge-bases/{knowledge_base_id}/versions/{version_id}/"
        f"documents/{document_id}/{safe_filename}"
    )
    content_type = file.content_type or "application/octet-stream"
    uploaded = False
    committed = False

    try:
        try:
            await to_thread.run_sync(
                lambda: storage.upload(
                    object_key=object_key,
                    stream=file.file,
                    size=size,
                    content_type=content_type,
                )
            )
            uploaded = True
        except StorageError as exc:
            raise HTTPException(status_code=503, detail="Object storage is unavailable") from exc

        document = Document(
            id=document_id,
            knowledge_base_version_id=version.id,
            workspace_id=None,
            filename=safe_filename,
            object_key=object_key,
            mime_type=content_type,
            sha256=sha256,
            status="QUEUED",
            search_enabled=True,
        )
        job = IngestionJob(
            id=job_id,
            document_id=document_id,
            status="QUEUED",
            progress=0,
        )
        version.status = "INDEXING"
        db.add_all([document, job])
        try:
            await db.commit()
            committed = True
            await db.refresh(document)
        except Exception:
            await db.rollback()
            if uploaded:
                try:
                    await to_thread.run_sync(storage.delete, object_key)
                except StorageError:
                    pass
            raise

        try:
            await ingestion_queue.enqueue(job_id)
        except QueueError as exc:
            await db.delete(document)
            await db.flush()
            await _synchronize_version_status(db, version)
            await db.commit()
            committed = False
            try:
                await to_thread.run_sync(storage.delete, object_key)
            except StorageError:
                pass
            raise HTTPException(status_code=503, detail="Ingestion queue is unavailable") from exc

        return document
    except Exception:
        if uploaded and not committed:
            try:
                await to_thread.run_sync(storage.delete, object_key)
            except StorageError:
                pass
        raise
    finally:
        await file.close()


@router.post(
    "/{knowledge_base_id}/versions/{version_id}/documents/{document_id}/reindex",
    response_model=KnowledgeBaseDocumentOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reindex_knowledge_base_document(
    knowledge_base_id: uuid.UUID,
    version_id: uuid.UUID,
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> KnowledgeBaseDocumentOut:
    require_tenant_admin(principal)
    version = await _get_version(db, knowledge_base_id, version_id, principal)
    await _ensure_editable_version(version)
    document = await db.get(Document, document_id)
    if document is None or document.knowledge_base_version_id != version.id:
        raise HTTPException(status_code=404, detail="Document not found")
    if document.status in BUSY_DOCUMENT_STATUSES:
        raise HTTPException(status_code=409, detail="Document is already being processed")

    previous_status = document.status
    job = IngestionJob(
        id=uuid.uuid4(),
        document_id=document.id,
        status="QUEUED",
        progress=0,
    )
    document.status = "QUEUED"
    version.status = "INDEXING"
    db.add(job)
    await db.commit()
    try:
        await ingestion_queue.enqueue(job.id)
    except QueueError as exc:
        job.status = "FAILED"
        job.error = "Ingestion queue is unavailable"
        document.status = previous_status
        await _synchronize_version_status(db, version)
        await db.commit()
        raise HTTPException(status_code=503, detail="Ingestion queue is unavailable") from exc

    await db.refresh(document)
    return document


@router.patch(
    "/{knowledge_base_id}/versions/{version_id}/documents/{document_id}",
    response_model=KnowledgeBaseDocumentOut,
)
async def update_knowledge_base_document(
    knowledge_base_id: uuid.UUID,
    version_id: uuid.UUID,
    document_id: uuid.UUID,
    payload: KnowledgeBaseDocumentUpdate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> KnowledgeBaseDocumentOut:
    require_tenant_admin(principal)
    version = await _get_version(db, knowledge_base_id, version_id, principal)
    await _ensure_editable_version(version)
    document = await db.get(Document, document_id)
    if document is None or document.knowledge_base_version_id != version.id:
        raise HTTPException(status_code=404, detail="Document not found")
    if payload.search_enabled and document.status != "READY":
        raise HTTPException(status_code=409, detail="Only READY documents can be enabled")
    document.search_enabled = payload.search_enabled
    await db.commit()
    await db.refresh(document)
    return document


@router.delete(
    "/{knowledge_base_id}/versions/{version_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_knowledge_base_document(
    knowledge_base_id: uuid.UUID,
    version_id: uuid.UUID,
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> Response:
    require_tenant_admin(principal)
    version = await _get_version(db, knowledge_base_id, version_id, principal)
    await _ensure_editable_version(version)
    document = await db.get(Document, document_id)
    if document is None or document.knowledge_base_version_id != version.id:
        raise HTTPException(status_code=404, detail="Document not found")
    if document.status in BUSY_DOCUMENT_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Document cannot be deleted while ingestion is running",
        )

    try:
        await to_thread.run_sync(storage.delete, document.object_key)
    except StorageError as exc:
        raise HTTPException(
            status_code=503,
            detail="Object storage is unavailable; document was not deleted",
        ) from exc

    await db.delete(document)
    await db.flush()
    await _synchronize_version_status(db, version)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

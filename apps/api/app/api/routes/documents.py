from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime
from pathlib import Path

from anyio import to_thread
from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Document, IngestionJob, Workspace
from app.db.session import get_db
from app.services.queue import QueueError, ingestion_queue
from app.services.storage import StorageError, storage

router = APIRouter(prefix="/workspaces/{workspace_id}/documents", tags=["documents"])

ALLOWED_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".html", ".htm", ".xml", ".pdf", ".docx"}
READ_CHUNK_SIZE = 1024 * 1024


class DocumentUpdate(BaseModel):
    search_enabled: bool


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID | None
    filename: str
    object_key: str
    mime_type: str | None
    sha256: str
    status: str
    search_enabled: bool
    created_at: datetime


def _safe_filename(filename: str) -> str:
    name = Path(filename).name.strip()
    name = re.sub(r"[^\w.()\- ]+", "_", name, flags=re.UNICODE)
    return name[:500] or "document"


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


@router.get("", response_model=list[DocumentOut])
async def list_workspace_documents(
    workspace_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    result = await db.execute(
        select(Document)
        .where(Document.workspace_id == workspace_id)
        .order_by(Document.created_at.desc())
    )
    return list(result.scalars())


@router.post("", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def upload_workspace_document(
    workspace_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

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

    document_id = uuid.uuid4()
    job_id = uuid.uuid4()
    object_key = f"workspaces/{workspace_id}/documents/{document_id}/{safe_filename}"
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
            workspace_id=workspace_id,
            knowledge_base_version_id=None,
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
            # Keep PostgreSQL and MinIO consistent when Redis is unavailable.
            await db.delete(document)
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
            # The object may already have been removed; MinIO deletion is idempotent.
            try:
                await to_thread.run_sync(storage.delete, object_key)
            except StorageError:
                pass
        raise
    finally:
        await file.close()


@router.post("/{document_id}/reindex", response_model=DocumentOut, status_code=status.HTTP_202_ACCEPTED)
async def reindex_workspace_document(
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    document = await db.get(Document, document_id)
    if document is None or document.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Document not found")
    if document.status in {"QUEUED", "PROCESSING"}:
        raise HTTPException(status_code=409, detail="Document is already being processed")

    previous_status = document.status
    job = IngestionJob(
        id=uuid.uuid4(),
        document_id=document.id,
        status="QUEUED",
        progress=0,
    )
    document.status = "QUEUED"
    db.add(job)
    await db.commit()

    try:
        await ingestion_queue.enqueue(job.id)
    except QueueError as exc:
        job.status = "FAILED"
        job.error = "Ingestion queue is unavailable"
        document.status = previous_status
        await db.commit()
        raise HTTPException(status_code=503, detail="Ingestion queue is unavailable") from exc

    await db.refresh(document)
    return document

@router.patch("/{document_id}", response_model=DocumentOut)
async def update_workspace_document(
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    payload: DocumentUpdate,
    db: AsyncSession = Depends(get_db),
):
    document = await db.get(Document, document_id)
    if document is None or document.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Document not found")

    if payload.search_enabled and document.status != "READY":
        raise HTTPException(
            status_code=409,
            detail="Only READY documents can be enabled for search",
        )

    document.search_enabled = payload.search_enabled
    await db.commit()
    await db.refresh(document)
    return document


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace_document(
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Response:
    document = await db.get(Document, document_id)
    if document is None or document.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Document not found")

    if document.status in {"QUEUED", "PROCESSING"}:
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
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

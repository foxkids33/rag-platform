from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Literal

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import workspace_for_principal
from app.core.config import settings
from app.core.integration_access import confluence_principal as get_current_principal
from app.core.security import Principal
from app.db.models import Document, IngestionJob
from app.db.session import get_db
from app.services.confluence import (
    ConfluenceClient,
    ConfluenceError,
    ConfluencePage,
    confluence,
)
from app.services.object_cleanup import (
    schedule_object_deletions,
    try_delete_scheduled_objects,
)
from app.services.queue import ingestion_queue
from app.services.storage import StorageError, storage
from app.services.upload_quota import check_upload_quota, upload_limit_bytes

router = APIRouter(tags=["confluence"])
logger = logging.getLogger(__name__)
PAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SPACE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")


class ConfluenceStatus(BaseModel):
    enabled: bool
    can_import: bool
    deployment: Literal["cloud", "data_center"]
    auth_mode: Literal["basic", "bearer"]
    host: str | None
    credentials_required: bool = True
    max_pages_per_import: int
    parser_format: str = "confluence_storage_v1"


class ConfluenceCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str | None = Field(default=None, max_length=320)
    token: SecretStr

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: str | None) -> str | None:
        cleaned = value.strip() if value else None
        return cleaned or None

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: SecretStr) -> SecretStr:
        cleaned = value.get_secret_value().strip()
        if not cleaned:
            raise ValueError("Confluence token is required")
        if len(cleaned) > 4096:
            raise ValueError("Confluence token is too long")
        return SecretStr(cleaned)

    @model_validator(mode="after")
    def validate_auth_mode(self) -> ConfluenceCredentials:
        if settings.confluence_auth_mode == "basic" and not self.email:
            raise ValueError("Email is required for Confluence Cloud basic authentication")
        return self


class ConfluenceImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_ids: list[str] = Field(default_factory=list, max_length=500)
    space_key: str | None = None
    force: bool = False
    expected_versions: dict[str, str] = Field(default_factory=dict, max_length=500)
    credentials: ConfluenceCredentials

    @field_validator("page_ids")
    @classmethod
    def validate_page_ids(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values))
        if any(not PAGE_ID_RE.fullmatch(value) for value in cleaned):
            raise ValueError("Confluence page ids may contain only letters, digits, _ and -")
        return cleaned

    @field_validator("space_key")
    @classmethod
    def validate_space_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not SPACE_KEY_RE.fullmatch(cleaned):
            raise ValueError("Confluence space key may contain only letters, digits, _ and -")
        return cleaned

    @model_validator(mode="after")
    def validate_source(self) -> ConfluenceImportRequest:
        if bool(self.page_ids) == bool(self.space_key):
            raise ValueError("Provide either page_ids or space_key")
        if len(self.page_ids) > settings.confluence_max_pages_per_import:
            raise ValueError(
                "page_ids exceeds CONFLUENCE_MAX_PAGES_PER_IMPORT"
            )
        if any(key not in self.page_ids for key in self.expected_versions):
            raise ValueError("expected_versions must refer to explicitly requested page_ids")
        return self


class ConfluenceImportItem(BaseModel):
    page_id: str
    page_version: str
    title: str
    status: Literal["created", "updated", "reindexed", "skipped", "busy", "failed"]
    document_id: uuid.UUID | None = None
    document_status: str | None = None
    detail: str | None = None


class ConfluenceImportResponse(BaseModel):
    requested: int
    queued: int
    skipped: int
    failed: int
    results: list[ConfluenceImportItem]


class ConfluenceConnectionCheck(BaseModel):
    connected: bool
    host: str | None


def _client_for_credentials(credentials: ConfluenceCredentials) -> ConfluenceClient:
    return ConfluenceClient(
        email=credentials.email,
        token=credentials.token.get_secret_value(),
    )


def _safe_filename(title: str, page_id: str) -> str:
    name = re.sub(r"[^\w.()\- ]+", "_", Path(title).name.strip(), flags=re.UNICODE)
    name = (name[:420].rstrip() or "confluence-page").removesuffix(".html")
    return f"{name}-{page_id}.confluence.json"


async def _resolve_page_ids(
    payload: ConfluenceImportRequest,
    client: ConfluenceClient,
) -> list[str]:
    page_ids = payload.page_ids
    if payload.space_key is not None:
        page_ids = await client.list_space_page_ids(payload.space_key)
    return list(dict.fromkeys(page_ids))[: settings.confluence_max_pages_per_import]


async def _import_page(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    page: ConfluencePage,
    *,
    force: bool,
    principal: Principal | None = None,
) -> ConfluenceImportItem:
    # Versioned envelope preserves provenance and storage CDATA. Empty landing
    # pages remain representable by their title instead of failing ingestion.
    content = json.dumps({
        "format": "confluence_storage_v1",
        "id": page.id,
        "title": page.title,
        "version": page.version,
        "source_url": page.source_url,
        "space_key": page.space_key,
        "ancestors": page.ancestors,
        "labels": page.labels,
        "storage_html": page.storage_html,
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")
    limit = upload_limit_bytes(principal, ".json") if principal else settings.upload_max_bytes
    if len(content) > limit:
        return ConfluenceImportItem(
            page_id=page.id,
            page_version=page.version,
            title=page.title,
            status="failed",
            detail=f"Page exceeds the {limit // (1024 * 1024)} MB import limit",
        )

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"confluence:{workspace_id}:{page.id}"},
    )
    existing = await db.scalar(
        select(Document)
        .where(
            Document.workspace_id == workspace_id,
            Document.source_type == "CONFLUENCE",
            Document.external_id == page.id,
        )
        .with_for_update()
    )
    sha256 = hashlib.sha256(content).hexdigest()
    now = datetime.now(UTC)

    if existing is not None and existing.status in {"QUEUED", "PROCESSING"}:
        # Keep metadata aligned with the object that is actually being indexed.
        await db.commit()
        return ConfluenceImportItem(
            page_id=page.id,
            page_version=page.version,
            title=page.title,
            status="busy",
            document_id=existing.id,
            document_status=existing.status,
            detail="The previous import is still being indexed",
        )

    unchanged = existing is not None and existing.sha256 == sha256
    if unchanged and existing is not None and existing.status == "READY" and not force:
        existing.title = page.title
        existing.source_url = page.source_url
        existing.external_version = page.version
        existing.synced_at = now
        await db.commit()
        return ConfluenceImportItem(
            page_id=page.id,
            page_version=page.version,
            title=page.title,
            status="skipped",
            document_id=existing.id,
            document_status=existing.status,
            detail="Content hash is unchanged",
        )

    document_id = existing.id if existing is not None else uuid.uuid4()
    filename = _safe_filename(page.title, page.id)
    previous_object_key = existing.object_key if existing is not None else None
    object_key = previous_object_key
    uploaded = False
    if not unchanged:
        if principal is not None:
            await check_upload_quota(db, principal, workspace_id, len(content), replacing=existing)
        object_key = (
            f"workspaces/{workspace_id}/confluence/{page.id}/"
            f"{page.version}-{uuid.uuid4().hex[:12]}/{filename}"
        )
        await to_thread.run_sync(
            lambda: storage.upload(
                object_key=object_key,
                stream=BytesIO(content),
                size=len(content),
                content_type="application/json; charset=utf-8",
            )
        )
        uploaded = True
    if object_key is None:
        raise RuntimeError("Confluence import did not produce an object key")

    job = IngestionJob(
        id=uuid.uuid4(),
        document_id=document_id,
        status="QUEUED",
        progress=0,
        attempts=0,
    )
    if existing is None:
        document = Document(
            id=document_id,
            workspace_id=workspace_id,
            knowledge_base_version_id=None,
            filename=filename,
            object_key=object_key,
            mime_type="application/json; charset=utf-8",
            sha256=sha256,
            status="QUEUED",
            search_enabled=True,
            title=page.title,
            source_url=page.source_url,
            source_type="CONFLUENCE",
            external_id=page.id,
            external_version=page.version,
            synced_at=now,
        )
        result_status = "created"
    else:
        document = existing
        document.filename = filename
        document.object_key = object_key
        document.mime_type = "application/json; charset=utf-8"
        document.sha256 = sha256
        document.status = "QUEUED"
        document.title = page.title
        document.source_url = page.source_url
        document.external_version = page.version
        document.synced_at = now
        result_status = "reindexed" if unchanged else "updated"

    document.size_bytes = len(content)
    retired_keys: list[str] = []
    try:
        if existing is None:
            db.add(document)
            await db.flush()
        db.add(job)
        if uploaded and previous_object_key and previous_object_key != object_key:
            retired_keys = await schedule_object_deletions(db, [previous_object_key])
        await db.commit()
    except Exception:
        await db.rollback()
        if uploaded and object_key:
            try:
                await to_thread.run_sync(storage.delete, object_key)
            except StorageError:
                logger.error("Failed to clean up uncommitted Confluence object %s", object_key)
        raise

    await ingestion_queue.notify(job.id)
    await try_delete_scheduled_objects(db, retired_keys)
    return ConfluenceImportItem(
        page_id=page.id,
        page_version=page.version,
        title=page.title,
        status=result_status,
        document_id=document.id,
        document_status=document.status,
    )


@router.get("/integrations/confluence/status", response_model=ConfluenceStatus)
async def confluence_status(
    _principal: Principal = Depends(get_current_principal),
) -> ConfluenceStatus:
    return ConfluenceStatus(
        enabled=settings.confluence_enabled,
        can_import=True,
        deployment=settings.confluence_deployment,
        auth_mode=settings.confluence_auth_mode,
        host=confluence.host,
        max_pages_per_import=settings.confluence_max_pages_per_import,
    )


@router.post(
    "/integrations/confluence/check",
    response_model=ConfluenceConnectionCheck,
)
async def check_confluence_connection(
    credentials: ConfluenceCredentials,
    _principal: Principal = Depends(get_current_principal),
) -> ConfluenceConnectionCheck:
    if not settings.confluence_enabled:
        raise HTTPException(status_code=503, detail="Confluence integration is disabled")
    client = _client_for_credentials(credentials)
    try:
        await client.check_connection()
    except ConfluenceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.close()
    return ConfluenceConnectionCheck(connected=True, host=client.host)


@router.post(
    "/workspaces/{workspace_id}/imports/confluence",
    response_model=ConfluenceImportResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def import_confluence(
    workspace_id: uuid.UUID,
    payload: ConfluenceImportRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> ConfluenceImportResponse:
    await workspace_for_principal(db, workspace_id, principal)
    if not settings.confluence_enabled:
        raise HTTPException(status_code=503, detail="Confluence integration is disabled")
    credentials = payload.credentials
    client = _client_for_credentials(credentials)
    try:
        try:
            page_ids = await _resolve_page_ids(payload, client)
        except ConfluenceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        results: list[ConfluenceImportItem] = []
        for page_id in page_ids:
            try:
                page = await client.get_page(page_id)
                expected = payload.expected_versions.get(page_id)
                if expected is not None and page.version != expected:
                    raise ConfluenceError(
                        "Page version changed since snapshot; repeat synchronization"
                    )
            except ConfluenceError as exc:
                results.append(
                    ConfluenceImportItem(
                        page_id=page_id,
                        page_version="unknown",
                        title=f"Confluence page {page_id}",
                        status="failed",
                        detail=str(exc),
                    )
                )
                continue
            try:
                item = await _import_page(db, workspace_id, page, force=payload.force)
            except StorageError:
                await db.rollback()
                item = ConfluenceImportItem(
                    page_id=page.id,
                    page_version=page.version,
                    title=page.title,
                    status="failed",
                    detail="Object storage is unavailable",
                )
            results.append(item)
    finally:
        await client.close()

    queued_statuses = {"created", "updated", "reindexed"}
    return ConfluenceImportResponse(
        requested=len(page_ids),
        queued=sum(item.status in queued_statuses for item in results),
        skipped=sum(item.status in {"skipped", "busy"} for item in results),
        failed=sum(item.status == "failed" for item in results),
        results=results,
    )

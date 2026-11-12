from __future__ import annotations

import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.confluence import ConfluenceImportItem, ConfluenceImportResponse, _import_page
from app.core.access import workspace_for_principal
from app.core.config import settings
from app.core.integration_access import confluence_principal as get_current_principal
from app.core.security import Principal, require_tenant_admin
from app.db.models import Connector, ConnectorJob
from app.db.session import get_db
from app.services.confluence import ConfluenceError, ConfluencePage
from app.services.confluence_direct import DirectConfluenceClient, selected_confluence_mode
from app.services.connector_queue import (
    connector_queue,
    issue_bound_token,
    token_connector_id,
    token_matches,
)
from app.services.storage import StorageError

router = APIRouter(tags=["connectors"])
logger = logging.getLogger(__name__)
machine_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="ConnectorBearer",
    description="Machine credential issued during connector enrollment; not an OIDC user token.",
)
# Personal Confluence spaces use a leading '~'. Treat it as an identity,
# not as a path; traversal, wildcard and URL characters remain forbidden.
SPACE_KEY_RE = re.compile(
    r"^(?:[A-Za-z0-9_-]{1,255}|~[A-Za-z0-9_][A-Za-z0-9_.@-]{0,253})$"
)
PAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SUPPORTED_CAPABILITIES = frozenset({"LIST_SPACES", "LIST_CHILDREN", "LIST_SPACE_PAGES", "IMPORT_PAGES"})


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _clean_spaces(values: list[str]) -> list[str]:
    cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
    if not cleaned or any(not SPACE_KEY_RE.fullmatch(value) for value in cleaned):
        raise ValueError("At least one valid Confluence space key is required")
    return cleaned


def _space_allowed(connector: Connector, space_key: str) -> bool:
    requested = space_key.casefold()
    return any(str(item).casefold() == requested for item in connector.allowed_spaces)


def _connector_online(connector: Connector, now: datetime | None = None) -> bool:
    last_seen = _aware(connector.last_seen_at)
    return bool(
        connector.enabled
        and connector.credential_hash
        and last_seen
        and (now or _now()) - last_seen
        <= timedelta(seconds=settings.confluence_connector_offline_seconds)
    )


class EnrollmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="Confluence Connector", min_length=1, max_length=255)
    allowed_spaces: list[str] = Field(default_factory=lambda: ["KBR"], max_length=1000)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("allowed_spaces")
    @classmethod
    def validate_spaces(cls, value: list[str]) -> list[str]:
        return _clean_spaces(value)


class EnrollmentCreated(BaseModel):
    connector_id: uuid.UUID
    enrollment_token: str
    expires_at: datetime
    allowed_spaces: list[str]


class ConnectorEnrollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enrollment_token: str = Field(min_length=20, max_length=512)
    version: str = Field(default="unknown", min_length=1, max_length=100)
    capabilities: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip().upper() for value in values))
        if not cleaned or any(value not in SUPPORTED_CAPABILITIES for value in cleaned):
            raise ValueError("Connector capabilities are missing or unsupported")
        return cleaned


class ConnectorEnrolled(BaseModel):
    connector_id: uuid.UUID
    credential: str
    allowed_spaces: list[str]
    long_poll_seconds: int
    lease_seconds: int


class ConnectorSummary(BaseModel):
    id: uuid.UUID
    name: str
    enabled: bool
    enrolled: bool
    online: bool
    enrollment_pending: bool
    enrollment_expires_at: datetime | None
    allowed_spaces: list[str]
    capabilities: list[str]
    version: str | None
    last_seen_at: datetime | None


class ConnectorIntegrationStatus(BaseModel):
    enabled: bool
    configured: bool
    online: bool
    can_manage: bool
    connectors: list[ConnectorSummary]
    max_pages_per_job: int
    max_job_seconds: int


class ConfluenceConnectionStatus(BaseModel):
    mode: Literal["direct", "connector"]
    enabled: bool
    configured: bool
    ready: bool
    readiness_reason: str | None = None
    missing_configuration: list[str] = Field(default_factory=list)
    host: str | None = None
    allowed_spaces: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    max_pages_per_job: int
    max_job_seconds: int
    connector: ConnectorIntegrationStatus | None = None


class SpaceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=500)
    homepage_id: str | None = Field(default=None, max_length=128)
    web_url: str | None = Field(default=None, max_length=4000)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        cleaned = value.strip()
        if not SPACE_KEY_RE.fullmatch(cleaned):
            raise ValueError("Invalid Confluence space key")
        return cleaned


class PageNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=1000)
    space_key: str = Field(min_length=1, max_length=255)
    version: str = Field(default="unknown", max_length=255)
    parent_id: str | None = Field(default=None, max_length=128)
    has_children: bool = False
    web_url: str | None = Field(default=None, max_length=4000)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not PAGE_ID_RE.fullmatch(cleaned):
            raise ValueError("Invalid Confluence page id")
        return cleaned

    @field_validator("space_key")
    @classmethod
    def validate_space_key(cls, value: str) -> str:
        cleaned = value.strip()
        if not SPACE_KEY_RE.fullmatch(cleaned):
            raise ValueError("Invalid Confluence space key")
        return cleaned


class PagePayload(PageNode):
    storage_html: str = Field(max_length=100_000_000)
    ancestors: list[str] = Field(default_factory=list, max_length=100)
    labels: list[str] = Field(default_factory=list, max_length=500)


class PageFetchError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1000)


class SpacesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spaces: list[SpaceItem] = Field(max_length=1000)


class ChildrenResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pages: list[PageNode] = Field(max_length=1000)


class ImportPagesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pages: list[PagePayload] = Field(default_factory=list, max_length=100)
    errors: list[PageFetchError] = Field(default_factory=list, max_length=100)


class SpacePagesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pages: list[PageNode] = Field(max_length=100)
    next_start: int | None = Field(default=None, ge=0)


class DirectSpacePagesResult(SpacePagesResult):
    next_cursor: str | None = None


class ConnectorResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_id: uuid.UUID
    result: dict[str, Any]


class ConnectorErrorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_id: uuid.UUID
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool = True


class ConnectorHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str | None = Field(default=None, max_length=100)
    capabilities: list[str] | None = Field(default=None, max_length=20)
    job_id: uuid.UUID | None = None
    lease_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_lease(self) -> ConnectorHeartbeat:
        if self.capabilities is not None:
            self.capabilities = ConnectorEnrollRequest.validate_capabilities(self.capabilities)
        if bool(self.job_id) != bool(self.lease_id):
            raise ValueError("job_id and lease_id must be supplied together")
        return self


class ConnectorJobLease(BaseModel):
    job_id: uuid.UUID
    lease_id: uuid.UUID
    type: Literal["LIST_SPACES", "LIST_CHILDREN", "LIST_SPACE_PAGES", "IMPORT_PAGES"]
    payload: dict[str, Any]
    leased_until: datetime


class JobAck(BaseModel):
    job_id: uuid.UUID
    status: Literal["QUEUED", "SUCCEEDED", "FAILED"]


class ChildrenOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_key: str
    parent_id: str | None = None

    @field_validator("space_key")
    @classmethod
    def validate_space(cls, value: str) -> str:
        cleaned = value.strip()
        if not SPACE_KEY_RE.fullmatch(cleaned):
            raise ValueError("Invalid Confluence space key")
        return cleaned

    @field_validator("parent_id")
    @classmethod
    def validate_parent(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not PAGE_ID_RE.fullmatch(cleaned):
            raise ValueError("Invalid Confluence parent page id")
        return cleaned


class SpacePagesOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_key: str
    start: int = Field(default=0, ge=0)

    @field_validator("space_key")
    @classmethod
    def validate_space(cls, value: str) -> str:
        return ChildrenOperationRequest.validate_space(value)


class ImportOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_ids: list[str] = Field(min_length=1, max_length=100)
    page_spaces: dict[str, str] = Field(default_factory=dict, max_length=100)
    expected_versions: dict[str, str] = Field(default_factory=dict, max_length=100)
    force: bool = False

    @field_validator("page_ids")
    @classmethod
    def validate_page_ids(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values))
        if not cleaned or any(not PAGE_ID_RE.fullmatch(value) for value in cleaned):
            raise ValueError("Invalid Confluence page id")
        if len(cleaned) > settings.confluence_connector_max_pages_per_job:
            raise ValueError("Too many pages in one connector job")
        return cleaned

    @model_validator(mode="after")
    def validate_versions(self) -> ImportOperationRequest:
        if any(page_id not in self.page_ids for page_id in self.expected_versions):
            raise ValueError("expected_versions must refer to requested page_ids")
        if set(self.page_spaces) != set(self.page_ids):
            raise ValueError("page_spaces must contain every requested page id")
        if any(not SPACE_KEY_RE.fullmatch(value.strip()) for value in self.page_spaces.values()):
            raise ValueError("page_spaces contains an invalid space key")
        self.page_spaces = {page_id: self.page_spaces[page_id].strip() for page_id in self.page_ids}
        return self


class DirectImportRequest(ImportOperationRequest):
    @field_validator("page_ids")
    @classmethod
    def validate_page_ids(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values))
        if not cleaned or any(not PAGE_ID_RE.fullmatch(value) for value in cleaned):
            raise ValueError("Invalid Confluence page id")
        if len(cleaned) > settings.confluence_max_pages_per_import:
            raise ValueError("Too many pages in one direct import")
        return cleaned


class DirectSpacePagesRequest(SpacePagesOperationRequest):
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)


class OperationCreated(BaseModel):
    operation_id: uuid.UUID
    status: str
    type: str


class OperationStatus(BaseModel):
    operation_id: uuid.UUID
    status: str
    type: str
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime


async def _machine_connector(
    connector_id: uuid.UUID,
    credentials: HTTPAuthorizationCredentials | None = Depends(machine_bearer),
    db: AsyncSession = Depends(get_db),
) -> Connector:
    if (
        credentials is None
        or credentials.scheme.casefold() != "bearer"
        or len(credentials.credentials) > 512
    ):
        raise HTTPException(status_code=401, detail="Invalid connector credential")
    connector = await db.get(Connector, connector_id)
    if (
        connector is None
        or not connector.enabled
        or not token_matches(credentials.credentials, connector.id, connector.credential_hash)
    ):
        raise HTTPException(status_code=401, detail="Invalid connector credential")
    connector.last_seen_at = _now()
    connector.updated_at = _now()
    return connector


def _summary(connector: Connector) -> ConnectorSummary:
    expires = _aware(connector.enrollment_expires_at)
    return ConnectorSummary(
        id=connector.id,
        name=connector.name,
        enabled=connector.enabled,
        enrolled=bool(connector.credential_hash),
        online=_connector_online(connector),
        enrollment_pending=bool(connector.enrollment_hash and expires and expires > _now()),
        enrollment_expires_at=connector.enrollment_expires_at,
        allowed_spaces=[str(item) for item in connector.allowed_spaces],
        capabilities=[str(item) for item in connector.capabilities],
        version=connector.version,
        last_seen_at=connector.last_seen_at,
    )


@router.get(
    "/integrations/confluence/connector",
    response_model=ConnectorIntegrationStatus,
)
async def connector_status(
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> ConnectorIntegrationStatus:
    connectors = list(
        await db.scalars(
            select(Connector)
            .where(Connector.tenant_id == principal.tenant_id, Connector.enabled.is_(True))
            .order_by(Connector.created_at.desc())
        )
    )
    summaries = [_summary(connector) for connector in connectors]
    return ConnectorIntegrationStatus(
        enabled=settings.confluence_connector_enabled,
        configured=any(item.enrolled or item.enrollment_pending for item in summaries),
        online=any(item.online for item in summaries),
        can_manage=principal.has_role(settings.auth_admin_role),
        connectors=summaries,
        max_pages_per_job=settings.confluence_connector_max_pages_per_job,
        max_job_seconds=settings.confluence_connector_max_job_seconds,
    )


@router.get("/integrations/confluence/connection", response_model=ConfluenceConnectionStatus)
async def confluence_connection_status(
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> ConfluenceConnectionStatus:
    mode = selected_confluence_mode()
    if mode == "connector":
        connector = await connector_status(db, principal)
        return ConfluenceConnectionStatus(
            mode=mode, enabled=connector.enabled, configured=connector.configured,
            ready=connector.enabled and connector.online,
            readiness_reason=None if connector.enabled and connector.online else "connector_offline" if connector.enabled else "disabled",
            missing_configuration=[] if connector.enabled else ["CONFLUENCE_CONNECTOR_ENABLED"],
            max_pages_per_job=connector.max_pages_per_job,
            max_job_seconds=connector.max_job_seconds, connector=connector,
        )
    client = DirectConfluenceClient()
    reason = client.readiness_reason
    return ConfluenceConnectionStatus(
        mode=mode, enabled=client.enabled, configured=reason is None, ready=reason is None,
        readiness_reason=reason, host=client.host,
        missing_configuration=client.missing_configuration,
        allowed_spaces=list(client.allowed_spaces.values()),
        capabilities=sorted(SUPPORTED_CAPABILITIES),
        max_pages_per_job=min(100, settings.confluence_max_pages_per_import),
        max_job_seconds=settings.confluence_connector_max_job_seconds,
    )


@asynccontextmanager
async def _direct_workspace_client(
    workspace_id: uuid.UUID, db: AsyncSession, principal: Principal,
):
    # Authorise the target workspace before any upstream request or data import.
    await workspace_for_principal(db, workspace_id, principal)
    if selected_confluence_mode() != "direct":
        raise HTTPException(status_code=409, detail="This deployment uses a Confluence connector")
    client = DirectConfluenceClient()
    if client.readiness_reason:
        raise HTTPException(status_code=503, detail="Direct Confluence access is not configured")
    try:
        yield client
    except ConfluenceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.close()


def _require_direct_space(client: DirectConfluenceClient, space_key: str) -> None:
    if space_key.casefold() not in client.allowed_spaces:
        raise HTTPException(status_code=403, detail="Space is outside the configured allowlist")


@router.post(
    "/workspaces/{workspace_id}/integrations/confluence/direct/spaces",
    response_model=SpacesResult,
)
async def list_direct_spaces(
    workspace_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> SpacesResult:
    async with _direct_workspace_client(workspace_id, db, principal) as client:
        return SpacesResult(spaces=await client.list_spaces())


@router.post(
    "/workspaces/{workspace_id}/integrations/confluence/direct/children",
    response_model=ChildrenResult,
)
async def list_direct_children(
    workspace_id: uuid.UUID,
    payload: ChildrenOperationRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> ChildrenResult:
    async with _direct_workspace_client(workspace_id, db, principal) as client:
        _require_direct_space(client, payload.space_key)
        return ChildrenResult(pages=await client.list_children(payload.space_key, payload.parent_id))


@router.post(
    "/workspaces/{workspace_id}/integrations/confluence/direct/space-pages",
    response_model=DirectSpacePagesResult,
)
async def list_direct_space_pages(
    workspace_id: uuid.UUID,
    payload: DirectSpacePagesRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> DirectSpacePagesResult:
    async with _direct_workspace_client(workspace_id, db, principal) as client:
        _require_direct_space(client, payload.space_key)
        return DirectSpacePagesResult(**await client.list_space_pages(payload.space_key, payload.start, payload.cursor))


@router.post(
    "/workspaces/{workspace_id}/integrations/confluence/direct/imports",
    response_model=ConfluenceImportResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def import_direct_pages(
    workspace_id: uuid.UUID,
    payload: DirectImportRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> ConfluenceImportResponse:
    results = []
    async with _direct_workspace_client(workspace_id, db, principal) as client:
        for space in payload.page_spaces.values():
            _require_direct_space(client, space)
        for page_id in payload.page_ids:
            try:
                page = await client.get_page(page_id)
                # Never trust the space claimed by the browser for a page ID.
                client.require_allowed(page.space_key)
                if (page.space_key or "").casefold() != payload.page_spaces[page_id].casefold():
                    raise ConfluenceError("Confluence returned a page from another space")
                expected = payload.expected_versions.get(page_id)
                if expected is not None and page.version != expected:
                    raise ConfluenceError("Page version changed; refresh the page selection")
                item = await _import_page(db, workspace_id, page, force=payload.force)
            except ConfluenceError as exc:
                item = ConfluenceImportItem(
                    page_id=page_id, page_version="unknown", title=f"Confluence page {page_id}",
                    status="failed", detail=str(exc),
                )
            except StorageError:
                await db.rollback()
                item = ConfluenceImportItem(
                    page_id=page_id, page_version="unknown", title=f"Confluence page {page_id}",
                    status="failed", detail="Object storage is unavailable",
                )
            results.append(item)
    return ConfluenceImportResponse(
        requested=len(payload.page_ids),
        queued=sum(item.status in {"created", "updated", "reindexed"} for item in results),
        skipped=sum(item.status in {"skipped", "busy"} for item in results),
        failed=sum(item.status == "failed" for item in results), results=results,
    )


@router.post(
    "/integrations/confluence/connectors/enrollments",
    response_model=EnrollmentCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_connector_enrollment(
    payload: EnrollmentCreate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> EnrollmentCreated:
    require_tenant_admin(principal)
    if not settings.confluence_connector_enabled:
        raise HTTPException(status_code=503, detail="Confluence connector is disabled")
    connector_id = uuid.uuid4()
    token, digest = issue_bound_token(connector_id)
    expires_at = _now() + timedelta(minutes=settings.confluence_connector_enrollment_minutes)
    connector = Connector(
        id=connector_id,
        tenant_id=principal.tenant_id,
        name=payload.name,
        enabled=True,
        enrollment_hash=digest,
        enrollment_expires_at=expires_at,
        allowed_spaces=payload.allowed_spaces,
        capabilities=[],
    )
    db.add(connector)
    await db.commit()
    return EnrollmentCreated(
        connector_id=connector.id,
        enrollment_token=token,
        expires_at=expires_at,
        allowed_spaces=payload.allowed_spaces,
    )


@router.delete(
    "/integrations/confluence/connectors/{connector_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_connector(
    connector_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> Response:
    require_tenant_admin(principal)
    connector = await db.scalar(
        select(Connector).where(
            Connector.id == connector_id,
            Connector.tenant_id == principal.tenant_id,
        )
    )
    if connector is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    connector.enabled = False
    connector.credential_hash = None
    connector.enrollment_hash = None
    connector.updated_at = _now()
    jobs = list(
        await db.scalars(
            select(ConnectorJob).where(
                ConnectorJob.connector_id == connector.id,
                ConnectorJob.status.in_(["QUEUED", "LEASED"]),
            )
        )
    )
    for job in jobs:
        job.status = "CANCELLED"
        job.error = "Connector was revoked"
        job.completed_at = _now()
        job.updated_at = _now()
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/connectors/enroll", response_model=ConnectorEnrolled)
async def enroll_connector(
    payload: ConnectorEnrollRequest,
    db: AsyncSession = Depends(get_db),
) -> ConnectorEnrolled:
    connector_id = token_connector_id(payload.enrollment_token)
    connector = (
        await db.scalar(select(Connector).where(Connector.id == connector_id).with_for_update())
        if connector_id
        else None
    )
    expires = _aware(connector.enrollment_expires_at) if connector else None
    if (
        connector is None
        or not connector.enabled
        or expires is None
        or expires <= _now()
        or not token_matches(
            payload.enrollment_token,
            connector.id,
            connector.enrollment_hash,
        )
    ):
        raise HTTPException(status_code=401, detail="Invalid or expired enrollment token")

    credential, digest = issue_bound_token(connector.id)
    connector.credential_hash = digest
    connector.enrollment_hash = None
    connector.enrollment_expires_at = None
    connector.capabilities = payload.capabilities
    connector.version = payload.version
    connector.last_seen_at = _now()
    connector.updated_at = _now()
    await db.commit()
    return ConnectorEnrolled(
        connector_id=connector.id,
        credential=credential,
        allowed_spaces=[str(item) for item in connector.allowed_spaces],
        long_poll_seconds=settings.confluence_connector_long_poll_seconds,
        lease_seconds=settings.confluence_connector_lease_seconds,
    )


async def _choose_connector(
    db: AsyncSession,
    tenant_id: str,
    capability: str,
    space_keys: set[str] | None = None,
) -> Connector:
    connectors = list(
        await db.scalars(
            select(Connector)
            .where(
                Connector.tenant_id == tenant_id,
                Connector.enabled.is_(True),
                Connector.credential_hash.is_not(None),
            )
            .order_by(Connector.last_seen_at.desc().nullslast())
        )
    )
    for connector in connectors:
        if not _connector_online(connector):
            continue
        if capability not in connector.capabilities:
            continue
        if space_keys and any(not _space_allowed(connector, space_key) for space_key in space_keys):
            continue
        return connector
    raise HTTPException(
        status_code=503,
        detail="No online Confluence connector can handle this request",
    )


async def _create_job(
    db: AsyncSession,
    *,
    connector: Connector,
    workspace_id: uuid.UUID,
    requested_by: str,
    job_type: str,
    payload: dict[str, Any],
) -> OperationCreated:
    job = ConnectorJob(
        id=uuid.uuid4(),
        connector_id=connector.id,
        tenant_id=connector.tenant_id,
        workspace_id=workspace_id,
        requested_by=requested_by,
        type=job_type,
        payload=payload,
        status="QUEUED",
        attempts=0,
        deadline_at=_now() + timedelta(seconds=settings.confluence_connector_job_timeout_seconds),
    )
    db.add(job)
    await db.commit()
    await connector_queue.notify(connector.id, job.id)
    return OperationCreated(operation_id=job.id, status=job.status, type=job.type)


@router.post(
    "/workspaces/{workspace_id}/integrations/confluence/spaces",
    response_model=OperationCreated,
    status_code=status.HTTP_202_ACCEPTED,
)
async def list_spaces_operation(
    workspace_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> OperationCreated:
    await workspace_for_principal(db, workspace_id, principal)
    connector = await _choose_connector(db, principal.tenant_id, "LIST_SPACES")
    return await _create_job(
        db,
        connector=connector,
        workspace_id=workspace_id,
        requested_by=principal.subject,
        job_type="LIST_SPACES",
        payload={},
    )


@router.post(
    "/workspaces/{workspace_id}/integrations/confluence/children",
    response_model=OperationCreated,
    status_code=status.HTTP_202_ACCEPTED,
)
async def list_children_operation(
    workspace_id: uuid.UUID,
    payload: ChildrenOperationRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> OperationCreated:
    await workspace_for_principal(db, workspace_id, principal)
    connector = await _choose_connector(
        db,
        principal.tenant_id,
        "LIST_CHILDREN",
        {payload.space_key},
    )
    return await _create_job(
        db,
        connector=connector,
        workspace_id=workspace_id,
        requested_by=principal.subject,
        job_type="LIST_CHILDREN",
        payload=payload.model_dump(mode="json"),
    )


@router.post(
    "/workspaces/{workspace_id}/integrations/confluence/space-pages",
    response_model=OperationCreated,
    status_code=status.HTTP_202_ACCEPTED,
)
async def list_space_pages_operation(
    workspace_id: uuid.UUID,
    payload: SpacePagesOperationRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> OperationCreated:
    await workspace_for_principal(db, workspace_id, principal)
    connector = await _choose_connector(
        db, principal.tenant_id, "LIST_SPACE_PAGES", {payload.space_key},
    )
    return await _create_job(
        db, connector=connector, workspace_id=workspace_id,
        requested_by=principal.subject, job_type="LIST_SPACE_PAGES",
        payload=payload.model_dump(mode="json"),
    )


@router.post(
    "/workspaces/{workspace_id}/integrations/confluence/imports",
    response_model=OperationCreated,
    status_code=status.HTTP_202_ACCEPTED,
)
async def import_pages_operation(
    workspace_id: uuid.UUID,
    payload: ImportOperationRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> OperationCreated:
    await workspace_for_principal(db, workspace_id, principal)
    connector = await _choose_connector(
        db,
        principal.tenant_id,
        "IMPORT_PAGES",
        set(payload.page_spaces.values()),
    )
    return await _create_job(
        db,
        connector=connector,
        workspace_id=workspace_id,
        requested_by=principal.subject,
        job_type="IMPORT_PAGES",
        payload=payload.model_dump(mode="json"),
    )


@router.get(
    "/workspaces/{workspace_id}/integrations/confluence/operations/{operation_id}",
    response_model=OperationStatus,
)
async def get_operation(
    workspace_id: uuid.UUID,
    operation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> OperationStatus:
    await workspace_for_principal(db, workspace_id, principal)
    job = await db.scalar(
        select(ConnectorJob)
        .where(
            ConnectorJob.id == operation_id,
            ConnectorJob.workspace_id == workspace_id,
            ConnectorJob.tenant_id == principal.tenant_id,
        )
        .with_for_update()
    )
    if job is None or (
        not principal.has_role(settings.auth_admin_role)
        and job.requested_by != principal.subject
    ):
        raise HTTPException(status_code=404, detail="Confluence operation not found")
    deadline = _aware(job.deadline_at)
    if job.status in {"QUEUED", "LEASED"} and deadline and deadline <= _now():
        job.status = "FAILED"
        job.error = "Connector operation timed out"
        job.completed_at = _now()
        job.updated_at = _now()
        await db.commit()
    return OperationStatus(
        operation_id=job.id,
        status=job.status,
        type=job.type,
        result=job.result,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


async def _recover_connector_jobs(db: AsyncSession, connector_id: uuid.UUID) -> None:
    now = _now()
    jobs = list(
        await db.scalars(
            select(ConnectorJob)
            .where(
                ConnectorJob.connector_id == connector_id,
                ConnectorJob.status.in_(["QUEUED", "LEASED"]),
            )
            .with_for_update(skip_locked=True)
        )
    )
    for job in jobs:
        deadline = _aware(job.deadline_at)
        lease_until = _aware(job.leased_until)
        if deadline and deadline <= now:
            job.status = "FAILED"
            job.error = "Connector operation timed out"
            job.completed_at = now
        elif job.status == "LEASED" and lease_until and lease_until <= now:
            if job.attempts >= settings.confluence_connector_max_attempts:
                job.status = "FAILED"
                job.error = "Connector job lease expired after the final attempt"
                job.completed_at = now
            else:
                job.status = "QUEUED"
                job.error = "Connector job lease expired; retrying"
            job.lease_id = None
            job.leased_until = None
        job.updated_at = now
    await db.commit()


async def _claim_connector_job(
    db: AsyncSession,
    connector: Connector,
) -> ConnectorJobLease | None:
    await _recover_connector_jobs(db, connector.id)
    job = await db.scalar(
        select(ConnectorJob)
        .where(
            ConnectorJob.connector_id == connector.id,
            ConnectorJob.status == "QUEUED",
            ConnectorJob.attempts < settings.confluence_connector_max_attempts,
            ConnectorJob.deadline_at > _now(),
        )
        .order_by(ConnectorJob.created_at, ConnectorJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    connector.last_seen_at = _now()
    connector.updated_at = _now()
    if job is None:
        await db.commit()
        return None
    lease_id = uuid.uuid4()
    leased_until = _now() + timedelta(seconds=settings.confluence_connector_lease_seconds)
    job.status = "LEASED"
    job.attempts += 1
    job.lease_id = lease_id
    job.leased_until = leased_until
    job.error = None
    job.updated_at = _now()
    await db.commit()
    return ConnectorJobLease(
        job_id=job.id,
        lease_id=lease_id,
        type=job.type,
        payload=job.payload,
        leased_until=leased_until,
    )


def _progress_deadline(job: ConnectorJob, now: datetime) -> datetime:
    created = _aware(job.created_at) or now
    return min(
        now + timedelta(seconds=settings.confluence_connector_job_timeout_seconds),
        created + timedelta(seconds=settings.confluence_connector_max_job_seconds),
    )


@router.get(
    "/connectors/{connector_id}/jobs/next",
    response_model=None,
    responses={
        200: {"model": ConnectorJobLease, "description": "A connector job lease"},
        204: {"description": "No job is available after long polling"},
    },
    description=(
        "Long-poll for the next connector job. Returns a JSON lease when a job "
        "is available, or HTTP 204 with an empty body otherwise. Authenticate "
        "with this connector's machine credential, not an OIDC user token."
    ),
)
async def next_connector_job(
    connector_id: uuid.UUID,
    wait: int = Query(default=25, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
    connector: Connector = Depends(_machine_connector),
) -> ConnectorJobLease | Response:
    wait = min(wait, settings.confluence_connector_long_poll_seconds)
    job = await _claim_connector_job(db, connector)
    if job is None:
        await connector_queue.wait(connector.id, wait)
        job = await _claim_connector_job(db, connector)
    if job is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return job


@router.post(
    "/connectors/{connector_id}/heartbeat",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def connector_heartbeat(
    connector_id: uuid.UUID,
    payload: ConnectorHeartbeat,
    db: AsyncSession = Depends(get_db),
    connector: Connector = Depends(_machine_connector),
) -> Response:
    if payload.version:
        connector.version = payload.version
    if payload.capabilities is not None:
        connector.capabilities = payload.capabilities
    if payload.job_id and payload.lease_id:
        job = await db.scalar(
            select(ConnectorJob)
            .where(
                ConnectorJob.id == payload.job_id,
                ConnectorJob.connector_id == connector.id,
                ConnectorJob.status == "LEASED",
                ConnectorJob.lease_id == payload.lease_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is not None:
            _validate_lease(job, payload.lease_id)
            job.leased_until = _now() + timedelta(
                seconds=settings.confluence_connector_lease_seconds
            )
            job.deadline_at = _progress_deadline(job, _now())
            job.updated_at = _now()
        else:
            raise HTTPException(status_code=409, detail="Connector job lease is no longer valid")
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _validate_lease(job: ConnectorJob, lease_id: uuid.UUID) -> None:
    if job.status == "SUCCEEDED":
        return
    now = _now()
    deadline = _aware(job.deadline_at)
    leased_until = _aware(job.leased_until)
    if (
        job.status != "LEASED"
        or job.lease_id != lease_id
        or deadline is None
        or deadline <= now
        or leased_until is None
        or leased_until <= now
    ):
        raise HTTPException(status_code=409, detail="Connector job lease is no longer valid")


async def _leased_job(
    db: AsyncSession,
    connector: Connector,
    job_id: uuid.UUID,
    lease_id: uuid.UUID,
) -> ConnectorJob:
    job = await db.scalar(
        select(ConnectorJob)
        .where(
            ConnectorJob.id == job_id,
            ConnectorJob.connector_id == connector.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Connector job not found")
    _validate_lease(job, lease_id)
    return job


def _safe_source_url(value: str | None) -> str | None:
    if not value:
        return None
    actual = urlsplit(value)
    if actual.scheme != "https" or not actual.netloc or actual.username or actual.password:
        return None
    configured = urlsplit(settings.confluence_base_url.strip())
    if configured.netloc and actual.netloc.casefold() != configured.netloc.casefold():
        return None
    return value


async def _complete_import(
    db: AsyncSession,
    connector: Connector,
    job: ConnectorJob,
    raw_result: dict[str, Any],
    lease_id: uuid.UUID,
) -> dict[str, Any]:
    result = ImportPagesResult.model_validate(raw_result)
    requested = [str(item) for item in job.payload.get("page_ids", [])]
    expected_versions = {
        str(key): str(value) for key, value in (job.payload.get("expected_versions") or {}).items()
    }
    expected_spaces = {
        str(key): str(value) for key, value in (job.payload.get("page_spaces") or {}).items()
    }
    force = bool(job.payload.get("force"))
    requested_set = set(requested)
    returned_ids = [page.id for page in result.pages]
    error_ids = [item.page_id for item in result.errors]
    if len(set(returned_ids + error_ids)) != len(returned_ids + error_ids):
        raise ValueError("Connector returned duplicate page results")
    if any(page_id not in requested_set for page_id in returned_ids + error_ids):
        raise ValueError("Connector returned a page that was not requested")

    items: list[ConfluenceImportItem] = []
    by_id = {page.id: page for page in result.pages}
    errors = {item.page_id: item.message for item in result.errors}
    for page_id in requested:
        # _import_page commits individual documents; reacquire and validate the
        # lease before each next page so cancellation/expiry cannot be ignored.
        job = await _leased_job(db, connector, job.id, lease_id)
        if job.status == "SUCCEEDED":
            return job.result or {}
        page = by_id.get(page_id)
        if page is None:
            items.append(
                ConfluenceImportItem(
                    page_id=page_id,
                    page_version="unknown",
                    title=f"Confluence page {page_id}",
                    status="failed",
                    detail=errors.get(page_id, "Connector did not return this page"),
                )
            )
            continue
        if not _space_allowed(connector, page.space_key):
            items.append(
                ConfluenceImportItem(
                    page_id=page.id,
                    page_version=page.version,
                    title=page.title,
                    status="failed",
                    detail="Page belongs to a space outside the connector allowlist",
                )
            )
            continue
        expected_space = expected_spaces.get(page.id)
        if expected_space is None or expected_space.casefold() != page.space_key.casefold():
            items.append(
                ConfluenceImportItem(
                    page_id=page.id,
                    page_version=page.version,
                    title=page.title,
                    status="failed",
                    detail="Page belongs to a different space than the selected tree node",
                )
            )
            continue
        expected = expected_versions.get(page.id)
        if expected is not None and expected != page.version:
            items.append(
                ConfluenceImportItem(
                    page_id=page.id,
                    page_version=page.version,
                    title=page.title,
                    status="failed",
                    detail="Page version changed; refresh the Confluence tree",
                )
            )
            continue
        try:
            imported = await _import_page(
                db,
                job.workspace_id,
                ConfluencePage(
                    id=page.id,
                    title=page.title,
                    storage_html=page.storage_html,
                    version=page.version,
                    source_url=_safe_source_url(page.web_url),
                    space_key=page.space_key,
                    ancestors=page.ancestors,
                    labels=page.labels,
                ),
                force=force,
            )
        except StorageError:
            await db.rollback()
            # rollback expires ORM attributes even with expire_on_commit=False.
            # Refresh before the next lease check or final acknowledgement.
            await db.refresh(connector)
            await db.refresh(job)
            imported = ConfluenceImportItem(
                page_id=page.id,
                page_version=page.version,
                title=page.title,
                status="failed",
                detail="Object storage is unavailable",
            )
        items.append(imported)

    queued_statuses = {"created", "updated", "reindexed"}
    response = ConfluenceImportResponse(
        requested=len(requested),
        queued=sum(item.status in queued_statuses for item in items),
        skipped=sum(item.status in {"skipped", "busy"} for item in items),
        failed=sum(item.status == "failed" for item in items),
        results=items,
    )
    return response.model_dump(mode="json")


@router.post(
    "/connectors/{connector_id}/jobs/{job_id}/result",
    response_model=JobAck,
)
async def connector_job_result(
    connector_id: uuid.UUID,
    job_id: uuid.UUID,
    payload: ConnectorResultRequest,
    db: AsyncSession = Depends(get_db),
    connector: Connector = Depends(_machine_connector),
) -> JobAck:
    job = await _leased_job(db, connector, job_id, payload.lease_id)
    if job.status == "SUCCEEDED":
        await db.commit()
        return JobAck(job_id=job.id, status="SUCCEEDED")
    try:
        if job.type == "LIST_SPACES":
            parsed = SpacesResult.model_validate(payload.result)
            parsed.spaces = [item for item in parsed.spaces if _space_allowed(connector, item.key)]
            for item in parsed.spaces:
                item.web_url = _safe_source_url(item.web_url)
            result = parsed.model_dump(mode="json")
        elif job.type in {"LIST_CHILDREN", "LIST_SPACE_PAGES"}:
            parsed = (SpacePagesResult if job.type == "LIST_SPACE_PAGES" else ChildrenResult).model_validate(payload.result)
            if (
                isinstance(parsed, SpacePagesResult)
                and parsed.next_start is not None
                and (not parsed.pages or parsed.next_start <= int(job.payload.get("start", 0)))
            ):
                raise ValueError("Space pagination did not advance")
            requested_space = str(job.payload.get("space_key") or "")
            if any(
                page.space_key.casefold() != requested_space.casefold()
                or not _space_allowed(connector, page.space_key)
                for page in parsed.pages
            ):
                raise ValueError("Connector returned pages outside the requested space")
            for page in parsed.pages:
                page.web_url = _safe_source_url(page.web_url)
            result = parsed.model_dump(mode="json")
        elif job.type == "IMPORT_PAGES":
            result = await _complete_import(db, connector, job, payload.result, payload.lease_id)
            job = await _leased_job(db, connector, job_id, payload.lease_id)
            if job.status == "SUCCEEDED":
                await db.commit()
                return JobAck(job_id=job.id, status="SUCCEEDED")
        else:
            raise ValueError("Unsupported connector job type")
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail="Connector result has an invalid shape"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job.status = "SUCCEEDED"
    job.result = result
    job.error = None
    job.lease_id = None
    job.leased_until = None
    job.completed_at = _now()
    job.updated_at = _now()
    connector.last_seen_at = _now()
    connector.updated_at = _now()
    await db.commit()
    return JobAck(job_id=job.id, status="SUCCEEDED")


@router.post(
    "/connectors/{connector_id}/jobs/{job_id}/error",
    response_model=JobAck,
)
async def connector_job_error(
    connector_id: uuid.UUID,
    job_id: uuid.UUID,
    payload: ConnectorErrorRequest,
    db: AsyncSession = Depends(get_db),
    connector: Connector = Depends(_machine_connector),
) -> JobAck:
    job = await _leased_job(db, connector, job_id, payload.lease_id)
    if job.status == "SUCCEEDED":
        await db.commit()
        return JobAck(job_id=job.id, status="SUCCEEDED")
    deadline = _aware(job.deadline_at)
    retry = bool(
        payload.retryable
        and job.attempts < settings.confluence_connector_max_attempts
        and deadline
        and deadline > _now()
    )
    job.status = "QUEUED" if retry else "FAILED"
    job.error = payload.message
    job.lease_id = None
    job.leased_until = None
    job.updated_at = _now()
    if not retry:
        job.completed_at = _now()
    await db.commit()
    if retry:
        await connector_queue.notify(connector.id, job.id)
    return JobAck(job_id=job.id, status="QUEUED" if retry else "FAILED")

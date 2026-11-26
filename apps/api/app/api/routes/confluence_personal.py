"""Personal Confluence operations in a signed-in user's own RAG session."""

import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.confluence import ConfluenceImportResponse
from app.api.routes.connectors import (
    SUPPORTED_CAPABILITIES,
    ChildrenOperationRequest,
    ChildrenResult,
    ConfluenceConnectionStatus,
    DirectImportRequest,
    DirectSpacePagesRequest,
    DirectSpacePagesResult,
    SpaceItem,
    import_selection,
)
from app.core.access import workspace_for_principal
from app.core.config import settings
from app.core.security import Principal, get_current_principal
from app.db.session import get_db
from app.services import confluence_personal as personal
from app.services.confluence import ConfluenceError, ConfluencePermissionError

router = APIRouter(tags=["personal-confluence"])
CONNECTION = "/integrations/confluence/personal/connection"
WORKSPACE = "/workspaces/{workspace_id}/integrations/confluence/personal"


class PersonalToken(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr


class PersonalSpaces(BaseModel):
    spaces: list[SpaceItem]


@asynccontextmanager
async def upstream(
    token: str, spaces: Iterable[str] = ()
) -> AsyncIterator[personal.PersonalConfluenceClient]:
    try:
        client = personal.PersonalConfluenceClient(token, spaces)
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "Не удалось открыть доверенное соединение Confluence.") from exc
    try:
        yield client
    except ConfluencePermissionError as exc:
        raise HTTPException(
            403, "Confluence отклонил токен или доступ к выбранной странице."
        ) from exc
    except ConfluenceError as exc:
        raise HTTPException(502, "Не удалось прочитать Confluence через серверный VPN.") from exc
    finally:
        await client.close()


@asynccontextmanager
async def workspace_client(
    request: Request,
    workspace_id: uuid.UUID,
    db: AsyncSession,
    principal: Principal,
    spaces: Iterable[str] = (),
):
    workspace = await workspace_for_principal(db, workspace_id, principal)
    if workspace.user_id != principal.subject:
        raise HTTPException(404, "Workspace not found")
    token = await personal.get_token(personal.session_key(request, principal))
    if token is None:
        raise HTTPException(409, "Подключите личный токен Confluence для этого сеанса RAG.")
    async with upstream(token, spaces) as client:
        yield client


def connection_status(connected: bool) -> ConfluenceConnectionStatus:
    enabled = personal.enabled()
    base_url = settings.confluence_base_url.rstrip("/")
    return ConfluenceConnectionStatus(
        mode="personal",
        enabled=enabled,
        configured=enabled,
        ready=connected,
        host=base_url.split("://")[-1].split("/")[0],
        personal_token_url=(f"{base_url}/plugins/personalaccesstokens/usertokens.action" if enabled else None),
        capabilities=sorted(SUPPORTED_CAPABILITIES),
        max_pages_per_job=min(100, settings.confluence_max_pages_per_import),
        max_job_seconds=settings.confluence_connector_max_job_seconds,
    )


@router.get(CONNECTION, response_model=ConfluenceConnectionStatus)
async def status(request: Request, principal: Principal = Depends(get_current_principal)):
    if not personal.enabled():
        return connection_status(False)
    return connection_status(
        bool(await personal.get_token(personal.session_key(request, principal)))
    )


@router.post(CONNECTION, response_model=ConfluenceConnectionStatus)
async def connect(
    payload: PersonalToken, request: Request, principal: Principal = Depends(get_current_principal)
):
    key = personal.session_key(request, principal)
    token = payload.token.get_secret_value().strip()
    if (
        not token
        or len(token) > 4096
        or not token.isascii()
        or any(ord(c) < 33 or ord(c) > 126 for c in token)
    ):
        raise HTTPException(422, "Проверьте формат личного токена Confluence.")
    async with upstream(token) as client:
        account, _ = await client._get(client.rest_url("user/current"))
        if (
            not (account.get("userKey") or account.get("username"))
            or account.get("type") == "anonymous"
        ):
            raise HTTPException(403, "Confluence не подтвердил личную учётную запись.")
    await personal.save_token(key, token, request.state.auth_session_expires_at)
    return connection_status(True)


@router.delete(CONNECTION, status_code=204)
async def disconnect(request: Request, principal: Principal = Depends(get_current_principal)):
    await personal.forget_token(personal.session_key(request, principal))
    return Response(status_code=204)


@router.post(WORKSPACE + "/spaces", response_model=PersonalSpaces)
async def spaces(
    workspace_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    async with workspace_client(request, workspace_id, db, principal) as client:
        return PersonalSpaces(spaces=await client.list_spaces())


@router.post(WORKSPACE + "/children", response_model=ChildrenResult)
async def children(
    workspace_id: uuid.UUID,
    payload: ChildrenOperationRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    async with workspace_client(
        request, workspace_id, db, principal, [payload.space_key]
    ) as client:
        return ChildrenResult(
            pages=await client.list_children(payload.space_key, payload.parent_id)
        )


@router.post(WORKSPACE + "/space-pages", response_model=DirectSpacePagesResult)
async def space_pages(
    workspace_id: uuid.UUID,
    payload: DirectSpacePagesRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    async with workspace_client(
        request, workspace_id, db, principal, [payload.space_key]
    ) as client:
        return DirectSpacePagesResult(
            **await client.list_space_pages(payload.space_key, payload.start, payload.cursor)
        )


@router.post(WORKSPACE + "/imports", response_model=ConfluenceImportResponse, status_code=202)
async def import_pages(
    workspace_id: uuid.UUID,
    payload: DirectImportRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    async with workspace_client(
        request, workspace_id, db, principal, payload.page_spaces.values()
    ) as client:
        return await import_selection(db, workspace_id, payload, client, principal)

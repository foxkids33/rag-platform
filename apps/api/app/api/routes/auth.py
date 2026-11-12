from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import Principal, get_current_principal, require_tenant_admin
from app.db.models import AuthUser
from app.db.session import get_db
from app.services.password_auth import (
    issue_session,
    limit_auth_request,
    no_store,
    password_hash_async,
    password_matches_async,
    principal_for_user,
    revoke_session,
    validate_auth_json,
)

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.get("/admin-access", status_code=status.HTTP_204_NO_CONTENT)
async def administrator_access(principal: Principal = Depends(get_current_principal)) -> Response:
    """Nginx auth_request guard for legacy presentation files."""
    require_tenant_admin(principal)
    return Response(status_code=204)


class PrincipalOut(BaseModel):
    subject: str
    tenant_id: str
    roles: list[str]
    auth_enabled: bool
    username: str | None = None
    display_name: str | None = None
    csrf_token: str | None = None


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=128)
    remember_me: bool = False

    @field_validator("username")
    @classmethod
    def username_format(cls, value: str) -> str:
        value = value.lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", value):
            raise ValueError("Username must use 3–64 ASCII letters, digits, dots, underscores or hyphens")
        return value


class RegisterRequest(LoginRequest):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    password: SecretStr = Field(min_length=12, max_length=128)


def _password_mode() -> None:
    if not settings.auth_enabled or settings.auth_mode != "password":
        raise HTTPException(status_code=404, detail="Local authentication is not enabled")


def _principal_out(principal: Principal, csrf_token: str | None = None) -> PrincipalOut:
    return PrincipalOut(
        subject=principal.subject, tenant_id=principal.tenant_id,
        roles=sorted(principal.roles), auth_enabled=settings.auth_enabled,
        username=principal.claims.get("username"), display_name=principal.claims.get("display_name"),
        csrf_token=csrf_token,
    )


@router.get("/me", response_model=PrincipalOut, response_model_exclude_none=True)
async def current_user(
    request: Request,
    response: Response,
    principal: Principal = Depends(get_current_principal),
) -> PrincipalOut:
    no_store(response)
    return _principal_out(principal, getattr(request.state, "auth_csrf_token", None))


@router.post("/register", response_model=PrincipalOut, response_model_exclude_none=True,
             status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest, request: Request, response: Response,
    db: AsyncSession = Depends(get_db),
) -> PrincipalOut:
    _password_mode()
    validate_auth_json(request)
    if not settings.auth_registration_enabled:
        raise HTTPException(status_code=403, detail="Registration is disabled")
    await limit_auth_request(db, request, payload.username, register=True)
    if payload.username in {"admin", "administrator", "root"}:
        raise HTTPException(status_code=409, detail="This username is unavailable")
    existing = await db.scalar(select(AuthUser.id).where(AuthUser.username == payload.username))
    if existing is not None:
        raise HTTPException(status_code=409, detail="This username is unavailable")
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('rag-registration', 0))"))
    registered = await db.scalar(select(func.count(AuthUser.id)).where(AuthUser.is_admin.is_(False)))
    if registered is None or int(registered) >= settings.auth_max_registered_users:
        raise HTTPException(status_code=503, detail="Registration capacity has been reached")
    identity = uuid.uuid4()
    user = AuthUser(
        id=identity, username=payload.username, subject=f"user:{identity.hex}",
        tenant_id=settings.auth_local_tenant, display_name=payload.display_name or payload.username,
        password_hash=await password_hash_async(payload.password.get_secret_value()),
        is_admin=False, is_active=True,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="This username is unavailable") from None
    csrf_token = await issue_session(db, user, request, response, remember=payload.remember_me)
    return _principal_out(principal_for_user(user), csrf_token)


@router.post("/login", response_model=PrincipalOut, response_model_exclude_none=True)
async def login(
    payload: LoginRequest, request: Request, response: Response,
    db: AsyncSession = Depends(get_db),
) -> PrincipalOut:
    _password_mode()
    validate_auth_json(request)
    await limit_auth_request(db, request, payload.username, register=False)
    user = await db.scalar(select(AuthUser).where(AuthUser.username == payload.username))
    matches = await password_matches_async(
        payload.password.get_secret_value(), user.password_hash if user else None
    )
    if user is None or not user.is_active or not matches:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    csrf_token = await issue_session(db, user, request, response, remember=payload.remember_me)
    return _principal_out(principal_for_user(user), csrf_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request, response: Response,
    _principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    _password_mode()
    await revoke_session(db, request, response)
